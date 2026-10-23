#!/usr/bin/env python3
"""Tests for role contracts, source checks, and the two feedback loops."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lr import roles as R                     # noqa: E402
from lr.asset_audit import input_hash         # noqa: E402
from lr.client import APIError, ChatClient    # noqa: E402
from lr.pipeline import (MAX_REPAIR_ROUNDS, FourRolePipeline,     # noqa: E402
                         run_batch)
from lr.seed_pool import TestAccessError, load_seed_pool          # noqa: E402

PINNED_RLOG_REV = '51d58fa60b39f38bcd8d497a6ddaa899c151127c'
CHECKS = ('task_validity', 'correctness', 'grounding', 'completeness',
          'usability', 'novelty')


def _clean_checks():
    return dict.fromkeys(CHECKS, 'pass')


def candidate_from(packet, suffix=' Answer using the log only.'):
    seed = list(packet['seeds'].values())[0]
    return {'sample_id': 'cand-' + seed['record_id'],
            'parent_seed_id': seed['record_id'],
            'source_record_ids': [seed['record_id']],
            'source_id': seed['record_id'],
            'source_revision': seed.get('source_revision', PINNED_RLOG_REV),
            'source_label_version': 'train-view-v1',
            'task': seed['task'], 'difficulty': 'basic',
            'generation_operator': 'question_constraint_expansion',
            'novelty_kind': 'paraphrase', 'input_mode': 'log_only',
            'reasoning': None, 'evidence_refs': [], 'scenario_tags': [],
            'preserved_invariants': ['trusted label value kept'],
            'instruction': seed['instruction'] + suffix,
            'input': seed['input'], 'output': seed['label'],
            'label_derivation': None}


class Scenario:
    """Scripted role responses for pipeline tests."""

    def __init__(self, name):
        self.name = name
        self.inspections = 0
        self.optimizations = 0
        self.generalizations = 0
        self.planner_packets = []

    def reply(self, role, packet):
        fn = getattr(self, '_' + role)
        if role == 'inspector':
            self.inspections += 1
        if role == 'optimizer':
            self.optimizations += 1
        if role == 'generalizer':
            self.generalizations += 1
        return fn(packet)

    def _planner(self, packet):
        self.planner_packets.append(json.loads(json.dumps(packet)))
        menu = packet.get('seed_task_menu') or {}
        items = []
        for task, cell in list(menu.items())[:1]:
            ids = cell.get('record_ids') or []
            if not ids:
                continue
            rid = ids[0]
            items.append({'plan_id': 'p-%s' % task,
                          'seed_ids': [rid], 'target_task': task,
                          'target_cell': {'source': cell['source'],
                                          'scenario': 'scenario-a',
                                          'difficulty': 'basic'},
                          'operator': 'question_constraint_expansion',
                          'evidence_ids': [rid],
                          'invariants': ['trusted label value kept'],
                          'requested_count': 1, 'attempt_budget': 2})
        return {'version': 'logrefine-role-v1', 'role': 'planner',
                'plan_items': items, 'unfilled_cells': []}

    def _generalizer(self, packet):
        if self.name == 'needs_evidence':
            return {'version': 'logrefine-role-v1', 'role': 'generalizer',
                    'status': 'needs_evidence',
                    'missing_requirements':
                    ['no train evidence supports a NEW event'],
                    'candidate': None}
        if self.name == 'malformed':
            return 'THIS IS NOT JSON {{{'
        suffix = (' See audit_raw.' if self.name == 'bad_accept'
                  else ' Answer using the log only.')
        return {'version': 'logrefine-role-v1', 'role': 'generalizer',
                'status': 'candidate', 'novelty_kind': 'paraphrase',
                'preserved_invariants': ['trusted label value kept'],
                'candidate': candidate_from(packet, suffix)}

    def _accept(self):
        return {'version': 'logrefine-role-v1', 'role': 'inspector',
                'checks': _clean_checks(), 'defects': [], 'route': 'accept',
                'route_reason': 'all checks pass'}

    def _repairing(self, code, field, scope):
        ch = _clean_checks()
        ch['correctness'] = 'fail'
        return {'version': 'logrefine-role-v1', 'role': 'inspector',
                'checks': ch,
                'defects': [{'defect_id': 'd1', 'code': code,
                             'severity': 'minor', 'location': field,
                             'evidence_ids': [], 'description':
                             'bounded mechanical defect on %s' % field,
                             'repairability': 'repairable',
                             'edit_scope': [scope],
                             'preserve': ['task', 'input']}],
                'route': 'repair', 'route_reason': 'single bounded field fix'}

    def _inspector(self, packet):
        if self.name == 'repair_first':
            if self.inspections == 1:
                return self._repairing('instruction_clarifier_missing',
                                       'instruction tail', 'instruction')
            return self._accept()
        if self.name == 'always_repair':
            return self._repairing('output_overlong', 'output', 'output')
        if self.name == 'always_repair_no_opt':
            if self.inspections > MAX_REPAIR_ROUNDS + 5:
                return self._accept()
            return self._repairing('reasoning_verbosity', 'reasoning',
                                   'reasoning')
        if self.name == 'regen_once':
            if self.inspections == 1:
                return {'version': 'logrefine-role-v1', 'role': 'inspector',
                        'checks': dict.fromkeys(CHECKS, 'fail'),
                        'defects': [{'defect_id': 'd1', 'code': 'task_wrong',
                                     'severity': 'critical',
                                     'location': 'task',
                                     'evidence_ids': [],
                                     'description': 'wrong task for parent',
                                     'repairability': 'unrepairable',
                                     'edit_scope': [], 'preserve': []}],
                        'route': 'regeneralize',
                        'route_reason': 'parent carries no such task'}
            return self._accept()
        return self._accept()

    def _optimizer(self, packet):
        cand = dict(packet['candidate'])
        rep = packet['inspection_report']
        d0 = rep['defects'][0]
        field = d0['edit_scope'][0]
        if self.name == 'always_repair_no_opt':
            return {'version': 'logrefine-role-v1', 'role': 'optimizer',
                    'status': 'unrepairable', 'revised_candidate': None,
                    'defect_to_edit': [], 'unresolved_defects': ['d1'],
                    'requested_route': 'quarantine'}
        if field == 'instruction':
            cand['instruction'] += ' What is the final judgement?'
        elif field == 'output':
            cand['output'] = cand['output'][:60]
        elif field == 'reasoning':
            cand['reasoning'] = (cand.get('reasoning') or '') + ' brief'
        return {'version': 'logrefine-role-v1', 'role': 'optimizer',
                'status': 'revised', 'revised_candidate': cand,
                'defect_to_edit': [{'defect_id': d0['defect_id'],
                                    'fields_changed': [field],
                                    'evidence_ids': [], 'before': 'old',
                                    'after': 'fixed', 'why_resolved':
                                    'defect removed, identity kept'}],
                'unresolved_defects': [], 'requested_route': 'reinspect'}


class FakeRoleClient:
    """Chat-client test double that returns scripted role responses."""

    def __init__(self, scenario, fail_at=None):
        self.scenario = scenario
        self.fail_at = fail_at
        self.failed = False

    def chat(self, msgs, purpose, max_tokens=None, json_mode=False):
        role = msgs[0]['content'].split('ROLE: ', 1)[1].split(
            ' ')[0].strip().lower()
        packet = json.loads(msgs[1]['content'])
        if self.fail_at == role and not self.failed:
            self.failed = True
            raise APIError('timeout', 'gateway read timeout after send')
        reply = self.scenario.reply(role, packet)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return {'content': content, 'request_id': 'fake-1', 'attempts': 1,
                'usage': {'prompt_tokens': 7, 'completion_tokens': 9}}


TARGETS1 = {'AD': 1}
TARGETS5 = {'Parsing': 1, 'AD': 1, 'Interpretation': 1, 'RCA': 1, 'SR': 1}


def build_pipe(tmp, scenario, fail_at=None):
    seeds, view = load_seed_pool(
        str(Path(__file__).resolve().parents[1] / 'examples' /
            'demo_seeds.jsonl'))
    fc = FakeRoleClient(scenario, fail_at=fail_at)

    def dispatcher(role, packet):
        return R.dispatch(role, packet, fc, purpose='test.' + role,
                          max_tokens=1024)
    pipe = FourRolePipeline(dispatcher, seeds=seeds, run_root=str(tmp / 'run'))
    return pipe, fc


def _run(tmp, scenario, targets=None, **kw):
    pipe, fc = build_pipe(tmp, scenario)
    res = run_batch(pipe, targets=targets or TARGETS1, batch_limit=4,
                    max_rounds=6, wall_sec=10, **kw)
    return pipe, fc, res


class OfflineCore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_five_task_happy_path(self):
        pipe, fc, res = _run(self.tmp, Scenario('happy'), targets=TARGETS5)
        stats = res['stats']
        self.assertEqual(stats['accepted_by_task'], {t: 1 for t in TARGETS5})
        self.assertEqual(stats['stop_reason'], 'quota_reached')
        self.assertEqual(sorted(r['kind'] for r in res['rows'] if r),
                         ['accepted'] * 5)
        self.assertEqual(fc.scenario.generalizations, 5)
        self.assertEqual(fc.scenario.inspections, 5)
        self.assertEqual(fc.scenario.optimizations, 0)

        for role, name in (('planner', 'shared.md'), ('planner', 'planner.md'),
                           ('generalizer', 'generalizer.md'),
                           ('inspector', 'inspector.md'),
                           ('inspector', 'shared.md')):
            digest = R.consumed_hashes(role)[name]
            self.assertIn((role, name, digest), pipe.consumed_hashes, name)

        pinned = R.manifest_hashes()
        import hashlib
        for f in ('shared.md', 'planner.md', 'generalizer.md', 'inspector.md',
                  'optimizer.md'):
            raw = (R.PROMPTS / f).read_bytes()
            self.assertEqual(pinned[f], hashlib.sha256(raw).hexdigest())

        for rec in pipe.state.data['records']:
            self.assertEqual(rec['sample_id'][:4], 'syn-')
        aud = pipe.state.data['audit'][0]
        self.assertEqual(aud['inspector_route'], 'accept')

    def test_repair_then_reinspection_then_accept(self):
        pipe, fc, res = _run(self.tmp, Scenario('repair_first'))
        sc = fc.scenario
        self.assertEqual([r['kind'] for r in res['rows'] if r], ['accepted'])
        self.assertEqual(sc.optimizations, 1)
        self.assertEqual(sc.inspections, 2)
        aud = pipe.state.data['audit'][0]
        self.assertEqual(aud['optimizer_edits'][0]['defect_id'], 'd1')
        self.assertEqual(aud['review']['inspector']['route'], 'accept')

        rec = pipe.state.data['records'][0]
        self.assertTrue(rec['instruction'].endswith(
            'What is the final judgement?'))

    def test_repair_rounds_are_hard_bounded(self):
        sc = Scenario('always_repair')
        pipe, fc, res = _run(self.tmp, sc, targets={'Parsing': 3})
        row = [r for r in res['rows'] if r][0]
        self.assertEqual(row['kind'], 'quarantine')
        self.assertEqual(row['code'], 'max_repair_rounds_stopped')
        self.assertLessEqual(sc.optimizations, MAX_REPAIR_ROUNDS)
        self.assertNotIn('AD', pipe.state.data['accepted_by_task'])
        self._tmp_ok = True

    def test_failed_repair_diagnosis_quarantines(self):
        sc = Scenario('always_repair_no_opt')
        pipe, fc, res = _run(self.tmp, sc, targets={'Parsing': 3})
        row = [r for r in res['rows'] if r][0]
        self.assertEqual(row['code'], 'optimizer_unrepairable')

    def test_regeneralize_retries_bounded_then_accepts(self):
        pipe, fc, res = _run(self.tmp, Scenario('regen_once'),
                             targets={'Parsing': 3})
        row = [r for r in res['rows'] if r][0]
        self.assertEqual(row['kind'], 'accepted')
        self.assertEqual(fc.scenario.generalizations, 2)

    def test_malformed_reply_is_quarantined_never_accepted(self):
        pipe, fc, res = _run(self.tmp, Scenario('malformed'))
        row = [r for r in res['rows'] if r][0]
        self.assertEqual(row['kind'], 'quarantine')
        self.assertEqual(row['code'], 'schema_invalid')
        self.assertEqual(pipe.state.data['accepted_ids'], [])

    def test_api_error_isolated_never_score_zero(self):
        seeds, view = load_seed_pool(
            str(Path(__file__).resolve().parents[1] / 'examples' /
                'demo_seeds.jsonl'))
        sc = Scenario('happy')
        fc = FakeRoleClient(sc, fail_at='inspector')

        def dispatcher(role, packet):
            return R.dispatch(role, packet, fc, purpose='t.' + role)
        pipe = FourRolePipeline(dispatcher, seeds=seeds,
                                run_root=str(self.tmp / 'r'))
        res = run_batch(pipe, targets=TARGETS1, batch_limit=2, max_rounds=1,
                        wall_sec=5)
        kinds = [r['kind'] for r in res['rows'] if r]
        self.assertTrue(kinds)
        self.assertNotIn('accepted', kinds)
        codes = json.dumps(res['rows'])
        self.assertIn('timeout', codes)

    def test_needs_evidence_generalizer_never_becomes_candidate(self):
        pipe, fc, res = _run(self.tmp, Scenario('needs_evidence'))
        row = [r for r in res['rows'] if r][0]
        self.assertEqual(row['kind'], 'regeneralize_deferred')
        self.assertEqual(pipe.state.data['accepted_ids'], [])

    def test_bad_accept_overridden_by_deterministic_gate(self):
        pipe, fc, res = _run(self.tmp, Scenario('bad_accept'))
        blob = json.dumps([r for r in res['rows'] if r])
        self.assertIn('gate:', blob)
        self.assertEqual(pipe.state.data['accepted_ids'], [])

    def test_planner_packet_carries_aggregate_feedback(self):
        sc = Scenario('happy')
        pipe, fc = build_pipe(self.tmp, sc)
        res = run_batch(pipe, targets=TARGETS5, batch_limit=4,
                        max_rounds=3, wall_sec=10)
        self.assertGreaterEqual(len(sc.planner_packets), 2)
        p0, p1 = sc.planner_packets[0], sc.planner_packets[1]
        self.assertEqual(sum(p0['accepted_counts'].values()), 0)
        self.assertEqual(p0['deficits'], dict(TARGETS5))

        self.assertEqual(sum(p1['accepted_counts'].values()), 1)
        first_task = list(p0['seed_task_menu'])[0]
        self.assertEqual(p1['deficits'][first_task], 0)
        self.assertNotIn(first_task, p1['seed_task_menu'])

        self.assertEqual(p1['coverage_targets'], dict(TARGETS5))
        self.assertIn('failure_summary', p1)

    def test_failure_summary_feeds_planner_after_quarantine(self):
        sc = Scenario('malformed')
        pipe, fc = build_pipe(self.tmp, sc)
        run_batch(pipe, targets=TARGETS1, batch_limit=2, max_rounds=2,
                  wall_sec=5)
        self.assertLessEqual(len(sc.planner_packets), 2)
        packets = sc.planner_packets
        if len(packets) >= 2:
            fb = packets[1]['failure_summary']['counts']
            self.assertTrue(any('quarantine' in k for k in fb), fb)

    def test_no_auto_recovery_state_reloads(self):
        seeds, view = load_seed_pool(
            str(Path(__file__).resolve().parents[1] / 'examples' /
                'demo_seeds.jsonl'))

        def make_d(sc):
            fc = FakeRoleClient(sc)

            def d(role, packet):
                return R.dispatch(role, packet, fc, purpose='x.' + role)
            return d
        p = self.tmp / 'r'
        pipe1 = FourRolePipeline(make_d(Scenario('happy')), seeds=seeds,
                                 run_root=str(p))
        run_batch(pipe1, targets=TARGETS1, batch_limit=2, max_rounds=2,
                  wall_sec=5)
        sid = pipe1.state.data['accepted_ids'][0]
        pipe2 = FourRolePipeline(make_d(Scenario('happy')), seeds=seeds,
                                 run_root=str(p))
        self.assertEqual(pipe2.state.data['accepted_ids'], [sid])
        self.assertTrue(pipe2.state.already_accepted(sid))

    def test_role_scope_and_pin_drift(self):
        with self.assertRaises(ValueError):
            R.messages('data_quality_judge', {})
        with self.assertRaises(R.RoleContractError):
            R.dispatch('inspector', {}, FakeRoleClient(Scenario('happy')),
                       't.inspector', pin={'shared.md': 'x' * 64})

    def test_no_implicit_run_dir_on_import_construct(self):
        seeds, view = load_seed_pool(
            str(Path(__file__).resolve().parents[1] / 'examples' /
                'demo_seeds.jsonl'))
        with self.assertRaises(ValueError):
            FourRolePipeline(lambda r, p: (None, {}), seeds=seeds)


class ClientConfig(unittest.TestCase):
    def test_missing_env_refuses_with_zero_http(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in
                 ('LOGREFINE_API_BASE', 'LOGREFINE_API_KEY',
                  'LOGREFINE_MODEL')}
        try:
            c = ChatClient()
            with self.assertRaises(APIError) as ctx:
                c.chat([{'role': 'user', 'content': 'hi'}], 'p', max_tokens=8)
            self.assertEqual(ctx.exception.code, 'config')
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_no_embedded_private_domains(self):
        import inspect as _i
        src = _i.getsource(sys.modules['lr.client'])
        for bad in ('localhost', '127.0.0.1', '192.168.', '10.0.'):
            self.assertNotIn(bad, src.lower())


class ClientContract(unittest.TestCase):
    def test_env_config_and_redaction(self):
        import os
        import lr.client as lc
        c = ChatClient(base_url='https://example.invalid/v1', model='m',
                       api_key='sk-abcdefghijklmnopqrstuvwxyz')
        red = lc._redact('failed with sk-abcdefghijklmnopqrstuvwxyz inside')
        self.assertIn('[REDACTED]', red)
        self.assertNotIn('abcdefghijklmnop', red)
        with self.assertRaises(APIError) as ctx:
            ChatClient(base_url='ftp://bad', model='m', api_key='k')
        self.assertEqual(ctx.exception.code, 'config')
        with self.assertRaises(APIError) as ctx2:
            ChatClient(base_url='https://example.invalid/v1', model='m',
                       api_key='').chat([{'role': 'user', 'content': 'q'}],
                                        'p')
        self.assertEqual(ctx2.exception.code, 'config')

    def test_missing_model_refuses_before_socket(self):
        import os
        saved = os.environ.pop('LOGREFINE_MODEL', None)
        try:
            c = ChatClient(base_url='https://example.invalid/v1',
                           model=None, api_key='sk-fixtureonlykey')
            with self.assertRaises(APIError) as ctx:
                c.chat([{'role': 'user', 'content': 'q'}], 'p')
            self.assertEqual(ctx.exception.code, 'config')
        finally:
            if saved is not None:
                os.environ['LOGREFINE_MODEL'] = saved

    def test_bounded_retries_and_status_handling(self):
        import threading
        seen = {'n': 0, 'peak': 0, 'live': 0}
        lock = threading.Lock()

        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {'model': 'm', 'usage': {'total_tokens': 5},
                        'choices': [{'message': {'content': '{"ok": 1}'},
                                     'finish_reason': 'stop'}]}

        class FakeSession:
            def __init__(self, status):
                self.status = status

            def post(self, *a, **k):
                with lock:
                    seen['n'] += 1
                    seen['live'] += 1
                    seen['peak'] = max(seen['peak'], seen['live'])
                try:
                    if isinstance(self.status, list):
                        code = self.status[min(seen['n'] - 1,
                                               len(self.status) - 1)]
                    else:
                        code = self.status
                    if code >= 400:
                        r = Resp()
                        r.status_code = code
                        r.json = lambda: {'error': 'x'}
                        import types
                        r.text = 'upstream busy'
                        return r
                    return Resp()
                finally:
                    with lock:
                        seen['live'] -= 1

        c = ChatClient(base_url='https://example.invalid/v1', model='m',
                       api_key='k')

        c.session = FakeSession([503])
        c.max_retries = 2
        with self.assertRaises(APIError) as ctx:
            c.chat([{'role': 'user', 'content': 'q'}], 'p')
        self.assertEqual(ctx.exception.code, 'api')
        self.assertEqual(seen['n'], 3)

        seen2 = {'n': 0}

        class Seq(FakeSession):
            def post(self, *a, **k):
                seen2['n'] += 1
                code = 429 if seen2['n'] == 1 else 200
                rr = Resp()
                rr.status_code = code
                if code == 429:
                    rr.text = 'rate'
                return rr
        c.session = Seq(200)
        out = c.chat([{'role': 'user', 'content': 'q'}], 'p')
        self.assertEqual(out['content'], '{"ok": 1}')
        self.assertEqual(out['attempts'], 2)

    def test_shared_pool_bounds_concurrency_at_cap(self):
        import threading
        import time as _t
        import lr.client as lc
        live = {'n': 0, 'peak': 0}
        lk = threading.Lock()

        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {'model': 'm', 'usage': {'total_tokens': 1},
                        'choices': [{'message': {'content': '{}'},
                                     'finish_reason': 'stop'}]}

        class S:
            def post(self, *a, **k):
                with lk:
                    live['n'] += 1
                    live['peak'] = max(live['peak'], live['n'])
                _t.sleep(0.02)
                with lk:
                    live['n'] -= 1
                return Resp()
        c = ChatClient(base_url='https://example.invalid/v1', model='m',
                       api_key='k')
        c.max_retries = 0
        c.session = S()
        ts = [threading.Thread(target=c.chat,
                               args=([{'role': 'user', 'content': 'q'}], 'p'))
              for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertGreater(live['peak'], 1)
        self.assertLessEqual(live['peak'], lc.MAX_CONCURRENCY_CAP)
        st = lc.gate_stats()
        self.assertLessEqual(st['peak'], lc.MAX_CONCURRENCY_CAP)


class SeedPoolGuards(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_alias_dedup_and_canonical_carriage(self):
        p = self.tmp / 's.jsonl'
        base = {'record_id': 'demo-parsing-0001', 'task': 'Parsing',
                'source': 'self-authored-demo',
                'instruction': 'mask the volatile tokens',
                'input': 'Alpha 3 down', 'label': '<S> <*> down'}
        twin = dict(base, record_id='demo-parsing-0002',
                    label='ignored twin')
        other = {'record_id': 'demo-ad-0003', 'task': 'AD',
                 'source': 'self-authored-demo', 'instruction': 'judge',
                 'input': 'Disk full', 'label': 'abnormal'}
        p.write_text('\n'.join(json.dumps(r) for r in (base, twin, other)),
                     'utf-8')
        seeds, view = load_seed_pool(str(p))
        self.assertEqual([s['record_id'] for s in seeds],
                         ['demo-parsing-0001', 'demo-ad-0003'])
        self.assertEqual(seeds[0]['pair_alias_record_ids'],
                         ['demo-parsing-0002'])
        self.assertEqual(view['record_to_group']['demo-parsing-0002'],
                         'demo-parsing-0001')

    def test_pair_dedup_first_seen_row_is_canonical(self):
        p = self.tmp / 's2.jsonl'
        r1 = {'record_id': 'zzz-0002', 'task': 'SR',
              'source': 'self-authored-demo', 'instruction': 'fix',
              'input': 'same', 'label': 'restart'}
        r2 = {'record_id': 'aaa-0001', 'task': 'SR',
              'source': 'self-authored-demo', 'instruction': 'fix',
              'input': 'same', 'label': 'ignored'}
        p.write_text('\n'.join(json.dumps(r) for r in (r1, r2)), 'utf-8')
        seeds, view = load_seed_pool(str(p))


        self.assertEqual([s['record_id'] for s in seeds], ['zzz-0002'])
        self.assertEqual(seeds[0]['pair_alias_record_ids'], ['aaa-0001'])
        self.assertEqual(view['record_to_group']['aaa-0001'], 'aaa-0001')
        self.assertEqual(view['record_to_group']['zzz-0002'], 'aaa-0001')

    def test_forbidden_hash_guard_fail_closed(self):
        from lr.asset_audit import input_hash
        p = self.tmp / 's3.jsonl'
        p.write_text(json.dumps({'record_id': 'demo-ad-0001', 'task': 'AD',
                                 'source': 'self-authored-demo',
                                 'instruction': 'judge', 'input': 'X',
                                 'label': 'abnormal'}), 'utf-8')
        fb = self.tmp / 'fb.json'
        fb.write_text(json.dumps(
            {'forbidden_input_sha256': [input_hash('X')]}), 'utf-8')
        with self.assertRaises(TestAccessError):
            load_seed_pool(str(p), forbidden_hashes=str(fb))

    def test_schema_fail_closed_and_exclusion(self):
        p = self.tmp / 's4.jsonl'
        p.write_text(json.dumps({'record_id': 'demo-x-0001',
                                 'task': 'nonsense_task',
                                 'source': 'self-authored-demo',
                                 'instruction': 'a', 'input': 'b',
                                 'label': 'c'}), 'utf-8')
        with self.assertRaises(TestAccessError):
            load_seed_pool(str(p))
        p.write_text(json.dumps({'record_id': 'demo-ad-0002', 'task': 'AD',
                                 'source': 'self-authored-demo',
                                 'instruction': 'a', 'input': 'b'}), 'utf-8')
        with self.assertRaises(TestAccessError):
            load_seed_pool(str(p))
        p.write_text('\n'.join([
            json.dumps({'record_id': 'demo-ad-0003', 'task': 'AD',
                        'source': 'self-authored-demo',
                        'instruction': 'judge', 'input': 'ok',
                        'label': 'normal'}),
            json.dumps({'record_id': 'demo-ad-0004', 'task': 'AD',
                        'source': 'self-authored-demo',
                        'instruction': 'judge', 'input': 'quarantined row',
                        'label': 'x',
                        'pair_quarantine_reason': 'conflicting gold'})]),
            'utf-8')
        seeds, view = load_seed_pool(str(p))
        self.assertEqual([s['record_id'] for s in seeds], ['demo-ad-0003'])

    def test_demo_pool_covers_all_five_tasks(self):
        seeds, view = load_seed_pool(
            str(Path(__file__).resolve().parents[1] / 'examples' /
                'demo_seeds.jsonl'))
        self.assertEqual({s['task'] for s in seeds},
                         set(R.TASKS))
        for s in seeds:
            self.assertTrue(s['group_id'])


class CliRealCommandRegression(unittest.TestCase):
    """Test command-line execution and exported files through the repair loop."""

    def test_subprocess_cli_fixture_run_writes_outputs(self):
        import subprocess
        import tempfile
        tmp = tempfile.mkdtemp()
        root = Path(__file__).resolve().parents[1]
        r = subprocess.run(
            [sys.executable, '-m', 'lr.cli', 'run',
             '--seeds', 'examples/demo_seeds.jsonl',
             '--out-dir', tmp, '--targets', json.dumps({'AD': 1}),
             '--batch-limit', '2', '--max-rounds', '2', '--wall-sec', '10',
             '--offline-fixture', 'tests/fixtures/offline_full_loop.json'],
            cwd=str(root), capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-300:] + r.stderr[-300:])
        summary = json.loads((Path(tmp) / 'summary.json').read_text())
        self.assertEqual(summary['accepted_total'], 1)
        train = (Path(tmp) / 'train.jsonl').read_text().strip()
        rec = json.loads(train)
        self.assertEqual(set(rec), {'instruction', 'input', 'output',
                                    'sample_id', 'task', 'parent_seed_id'})
        self.assertTrue(rec['sample_id'].startswith('syn-'))

        self.assertTrue(rec['instruction'].endswith(
            'What is the final state?'))
        audit = [json.loads(x) for x in
                 (Path(tmp) / 'audit.jsonl').read_text().splitlines()]
        self.assertEqual(audit[0]['inspector_route'], 'accept')
        self.assertEqual(audit[0]['optimizer_edits'][0]['defect_id'], 'd1')


class CliOfflineEndToEnd(unittest.TestCase):
    """Test CLI dispatch from planning through repair and reinspection."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        from lr.cli import _fixture_script
        self.disp = _fixture_script(
            str(Path(__file__).resolve().parent / 'fixtures' /
                'offline_full_loop.json'))

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_exercises_repair_loop_end_to_end(self):
        seeds, view = load_seed_pool(
            str(Path(__file__).resolve().parents[1] / 'examples' /
                'demo_seeds.jsonl'))
        pipe = FourRolePipeline(self.disp, seeds=seeds,
                                run_root=str(self.tmp / 'r'))
        res = run_batch(pipe, targets={'AD': 1}, batch_limit=2,
                        max_rounds=3, wall_sec=5)
        self.assertEqual([r['kind'] for r in res['rows'] if r],
                         ['accepted'])
        self.assertEqual(res['stats']['stop_reason'], 'quota_reached')
        aud = pipe.state.data['audit'][0]
        self.assertEqual(aud['inspector_route'], 'accept')
        self.assertEqual(aud['optimizer_edits'][0]['defect_id'], 'd1')
        rec = pipe.state.data['records'][0]
        self.assertTrue(rec['instruction'].endswith(
            'What is the final state?'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
