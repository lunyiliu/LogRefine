"""Command-line interface for log instruction data construction.

Configure LOGREFINE_API_BASE, LOGREFINE_API_KEY, and LOGREFINE_MODEL
to use an OpenAI-compatible chat endpoint. The offline-fixture option
loads scripted role responses for local tests."""
import argparse
import json
import sys
from pathlib import Path

from .client import ChatClient, gate_stats
from .pipeline import FourRolePipeline, run_batch
from .roles import dispatch as role_dispatch
from .seed_pool import load_seed_pool

LIVE_ROLE_MAX_OUTPUT_TOKENS = {'planner': 8192, 'generalizer': 8192,
                               'inspector': 8192, 'optimizer': 8192}


def _paper_task_policies(pipe, args):
    """Configure task-specific expansion constraints on the pipeline."""
    policy, mode, extra = {}, {}, {}
    if args.parsing_task_constraint:
        policy['Parsing'] = ['question_constraint_expansion',
                             'evidence_grounded_depth']
        extra.setdefault('Parsing', []).extend([
            'do NOT add question demands that conflict with the ESTABLISHED '
            'template annotation convention of the real parent sources',
            'the parent gold template stays IMMOVABLE: never rewrite, '
            're-tokenize or "fix" the trusted gold - a genuine gold fault '
            'would be isolated by id, never relabeled'])
    if args.ad_task_constraint:
        policy['AD'] = ['question_constraint_expansion',
                        'evidence_grounded_depth']
    if args.rca_task_constraint:
        policy['RCA'] = ['question_constraint_expansion',
                         'evidence_grounded_depth']
        mode['RCA'] = 'background_grounded'
        extra.setdefault('RCA', []).extend(
            ['every diagnosed cause MUST be supportable by the real parent '
             'sources; separate OBSERVED evidence from HYPOTHESIS',
             'with weak evidence do NOT demand THE unique primary root '
             'cause: pick an answerable real parent seed/question instead '
             'of guessing environments',
             'background added to the user-visible input must already exist '
             'as NON-ANSWER context in the original sources; NEVER copy the '
             'gold root cause/answer into the input as fake background'])
    if args.sr_task_constraint:
        policy['SR'] = ['question_constraint_expansion',
                        'evidence_grounded_depth',
                        'supported_scenario_expansion']
        mode['SR'] = 'background_grounded'
    if policy:
        pipe.task_operator_policy = policy
    if mode:
        pipe.task_constraint_mode = mode
    if extra:
        pipe.task_extra_invariants = extra


def _dispatcher(client, max_tokens=None):
    caps = dict(LIVE_ROLE_MAX_OUTPUT_TOKENS)
    if max_tokens:
        caps = {k: int(max_tokens) for k in caps}

    def dispatch_role(role, packet):
        return role_dispatch(role, packet, client, purpose='lr.run.' + role,
                             max_tokens=caps[role])
    return dispatch_role


def _fixture_script(path):
    """Load scripted role responses from a JSON test fixture."""
    script = json.loads(Path(path).read_text(encoding='utf-8'))

    def make():
        state = {role: {'i': 0} for role in script}

        def dispatch_role(role, packet):
            seq = script[role]
            idx = min(state[role]['i'], len(seq))
            if len(seq):
                state[role]['i'] += 1
            reply = seq[idx] if len(seq) else None
            return json.loads(json.dumps(reply)), {
                'request_id': 'fixture', 'attempts': 0, 'usage': None,
                'consumed_hashes': {}}
        return dispatch_role
    return make()


def student_export_row(rec):
    """Export instruction, input, output, optional reasoning, and record identity."""
    row = {'instruction': rec['instruction'], 'input': rec['input'],
           'output': rec['output']}
    if rec.get('reasoning'):
        row['reasoning'] = rec['reasoning']
    row.update(sample_id=rec['sample_id'], task=rec['task'],
               parent_seed_id=rec['parent_seed_id'])
    return row


def cmd_run(args):
    seeds, group_view = load_seed_pool(
        args.seeds, forbidden_hashes=args.forbidden_hashes)
    if args.out_dir is None:
        args.out_dir = 'run'
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if args.offline_fixture:
        print('OFFLINE FIXTURE MODE: scripted replies from %s. This is a '
              'DEVELOPMENT/TEST path, NOT the released synthesis runtime.'
              % args.offline_fixture, file=sys.stderr)
        dispatcher = _fixture_script(args.offline_fixture)
    else:
        client = ChatClient()
        if not client.base_url or not client.api_key or not client.model:
            raise SystemExit('set LOGREFINE_API_BASE, LOGREFINE_API_KEY and '
                             'LOGREFINE_MODEL (real OpenAI-compatible HTTP); '
                             'no fallback provider is compiled in')
        dispatcher = _dispatcher(client, args.max_tokens)
    targets = json.loads(args.targets) if args.targets else None
    pipe = FourRolePipeline(dispatcher, seeds=seeds, run_root=out)
    _paper_task_policies(pipe, args)
    result = run_batch(
        pipe, targets=targets,
        max_candidates=args.candidates,
        batch_limit=args.batch_limit, max_rounds=args.max_rounds,
        wall_sec=args.wall_sec,
        first_g_accept_gate=args.first_g_accept_gate,
        generation_caps=json.loads(args.generation_caps)
        if args.generation_caps else None,
        task_no_accept_gate=args.task_no_accept_gate)
    accepted = sorted(set(r['sample_id'] for r in
                         pipe.state.data.get('records', [])))
    records = {r['sample_id']: r
               for r in pipe.state.data.get('records', [])}


    train_rows = [student_export_row(records[sid]) for sid in accepted]
    train = out / 'train.jsonl'
    audit = out / 'audit.jsonl'
    with open(train, 'w', encoding='utf-8') as ft, \
            open(audit, 'w', encoding='utf-8') as fa:
        for r in train_rows:
            ft.write(json.dumps(r, ensure_ascii=False) + '\n')
        for a in pipe.state.data.get('audit', []):
            fa.write(json.dumps(a, ensure_ascii=False) + '\n')
    summary = {'stats': result['stats'],
               'gate': gate_stats(),
               'accepted_total': len(train_rows),
               'train_view': str(train), 'audit': str(audit),
               'consumed_prompt_hashes': sorted(pipe.consumed_hashes)}
    (out / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if result['stats']['stop_reason'] != 'error' else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog='logrefine', description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run', help='one bounded four-role synthesis batch')
    run.add_argument('--seeds', required=True,
                     help='train-side seed JSONL (schema: data/README.md)')
    run.add_argument('--forbidden-hashes', default=None)
    run.add_argument('--out-dir', default=None)
    run.add_argument('--targets', default=None,
                     help='accepted quota per task JSON, e.g. '
                          '{"AD":2,"Interpretation":2,"Parsing":2,'
                          '"RCA":2,"SR":2}')
    run.add_argument('--generation-caps', default=None)
    run.add_argument('--candidates', type=int, default=None)
    run.add_argument('--batch-limit', type=int, default=10)
    run.add_argument('--max-rounds', type=int, default=6)
    run.add_argument('--wall-sec', type=float, default=600)
    run.add_argument('--first-g-accept-gate', type=int, default=None)
    run.add_argument('--task-no-accept-gate', type=int, default=None)
    run.add_argument('--max-tokens', type=int, default=None)
    run.add_argument('--parsing-task-constraint', action='store_true')
    run.add_argument('--ad-task-constraint', action='store_true')
    run.add_argument('--rca-task-constraint', action='store_true')
    run.add_argument('--sr-task-constraint', action='store_true')
    run.add_argument('--offline-fixture', default=None, help=argparse.SUPPRESS)
    run.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
