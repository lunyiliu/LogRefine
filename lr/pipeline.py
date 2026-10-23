"""Four-agent log instruction data construction.

The Planner selects expansion work, the Generalizer creates candidates,
the Inspector checks them, and the Optimizer repairs diagnosed defects.
Optimizer revisions return to the Inspector for up to two repair rounds.
Inspection findings and coverage counts return to the Planner each round.
The run stops at its task targets or configured generation and time limits."""
import hashlib
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from .client import APIError
from .roles import (QUALITY_CHECKS, RoleContractError, dispatch,
                    validate_role_v1)
from .seed_pool import load_seed_pool
from .source_groups import UnknownParentError, load_group_view
from .tools import (DIFFICULTY_LEVELS, OPERATORS, QUOTA_TOTAL,
                    TASK_TARGETS, actual_cross_task_projection,
                    accepted_pool_confirms_duplicate, alias_to_record,
                    clean_seed_view, coverage_stats, failure_summary,
                    inventory, near_duplicate_search, packet_for,
                    seed_source_name, source_label_check,
                    task_contract_check,
                    usable_accepted_counts)

MAX_REPAIR_ROUNDS = 2
ATTEMPT_FACTOR = 3
PER_PLAN_ATTEMPT_CAP = 4


def _sha(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


class _Outcome:
    """Per-candidate outcome and diagnostic fields."""

    def __init__(self, kind, **fields):
        self.kind = kind
        self.fields = fields

    def as_row(self):
        return {'kind': self.kind, **self.fields}


class PipelineState:
    """Persist accepted examples, failures, and progress for resumed runs."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {'version': 'logrefine-pipeline-state-v1',
                     'accepted_ids': [], 'accepted_by_task': {},
                     'processed_plans': [], 'failures': [], 'groups': {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if loaded.get('version') != self.data['version']:
                raise ValueError('pipeline state version mismatch; refusing '
                                 'to clobber %s' % self.path)
            self.data = loaded
        self._seen = set(self.data['accepted_ids'])

    def save(self):
        import os
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(self.data, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def already_accepted(self, sample_id):
        return sample_id in self._seen

    def accept(self, record, audit):
        sid = record['sample_id']
        if sid in self._seen:
            return False
        self._seen.add(sid)
        self.data.setdefault('sample_to_parent', {})[sid] = \
            audit['parent_seed_id']
        self.data['accepted_ids'].append(sid)
        self.data['accepted_by_task'].setdefault(record['task'], []).append(sid)
        self.data.setdefault('audit', []).append(audit)
        self.data.setdefault('records', []).append(record)
        return True


def _isolate(exc, code_hint=''):
    if isinstance(exc, UnknownParentError):
        return _Outcome('quarantine', code='unknown_parent',
                        reason=str(exc)[:200])
    if isinstance(exc, RoleContractError):
        return _Outcome('quarantine', code='schema_invalid',
                        reason=str(exc)[:200])
    if isinstance(exc, APIError):
        return _Outcome('api_error', code=exc.code,
                        reason='isolated; never scored 0 or accepted')
    return _Outcome('error', code=code_hint or type(exc).__name__,
                    reason=str(exc)[:200])


class FourRolePipeline:
    """Coordinate the four roles through a configurable dispatcher."""

    PLANNER_MENU_WINDOW = 16

    def __init__(self, dispatcher, seeds=None, run_root=None,
                 state=None, rng_seed=0):
        self.dispatcher = dispatcher
        if seeds is not None:
            self.seeds = list(seeds)
        else:
            raise ValueError('pass seeds loaded via lr.seed_pool.load_seed_pool')
        self.seeds_by_id = {s['record_id']: s for s in self.seeds}
        self.groups = load_group_view()
        self.run_root = Path(run_root) if run_root else None
        if self.run_root is not None:
            self.run_root.mkdir(parents=True, exist_ok=True)
        if state is not None:
            self.state = state
        elif self.run_root is not None:
            self.state = PipelineState(self.run_root / 'pipeline_state.json')
        else:
            raise ValueError('FourRolePipeline needs run_root or state; no '
                             'implicit run directory is created by import')
        self.rng_seed = rng_seed
        self.consumed_hashes = set()
        self.accept_insp = None
        self.recent_edits = []
        self.repair_rounds = 0

    def canon_refs(self, ids):
        """Resolve canonical and alias seed identifiers to their source records."""
        out = []
        for pid in ids or []:
            canon = alias_to_record(self.groups, pid)
            if canon is None:
                raise UnknownParentError('unknown parent reference %s' % pid)
            if canon not in self.seeds_by_id:
                canon = next((sid for sid, sd in self.seeds_by_id.items()
                              if (sd.get('pair_alias_record_ids')
                                  and pid in sd['pair_alias_record_ids'])),
                             None)
                if canon is None:
                    raise UnknownParentError(
                        'parent %s resolves outside available train seeds'
                        % pid)
            if canon not in out:
                out.append(canon)
        return out

    def seed_task_universe(self):
        """Group available seed identifiers by task and source."""
        menu = {}
        for s in self.seeds:
            cell = menu.setdefault(s['task'], {'source': set(),
                                               'record_ids': []})
            cell['record_ids'].append(s['record_id'])
            cell['source'].add(seed_source_name(s))
        for cell in menu.values():
            srcs = sorted(cell['source'])
            cell['source'] = srcs[0] if len(srcs) == 1 else srcs
        return menu

    def planner_menu(self, generation_remaining, rnd):
        """Build bounded, rotating seed menus for tasks with generation capacity."""
        universe = self.seed_task_universe()
        window = self.PLANNER_MENU_WINDOW
        isolated = set((self.state.data.get('seed_isolation') or {}).keys())
        closed = getattr(self, '_closed_tasks_view', None) or set()
        menu = {}
        for task in TASK_TARGETS:
            cell = universe.get(task)
            if cell is None or generation_remaining.get(task, 0) <= 0:
                continue
            if task in closed:
                continue
            ids = [i for i in cell['record_ids'] if i not in isolated]
            if not ids:
                continue
            n = len(ids)
            start = (int(rnd) * window) % n
            picked = ids[start:start + window] + ids[:max(
                0, start + window - n)]
            cell_srcs = {seed_source_name(self.seeds_by_id[i])
                         for i in picked}
            menu[task] = {
                'source': sorted(cell_srcs)[0] if len(cell_srcs) == 1
                else sorted(cell_srcs),
                'record_ids': picked,
                'full_canonical_count': n,
                'menu_window': [start, start + len(picked)],
            }
        return menu

    def planner_packet(self, targets, batch_limit, rnd=0):
        """Combine coverage, remaining capacity, and inspection findings for planning."""
        cov = coverage_stats(self.state.data['accepted_by_task'], targets)
        tmap = self.state.data.get('plan_task_index', {})
        raw_caps = getattr(self, '_generation_caps', None)
        caps = dict(raw_caps) if raw_caps is not None else None
        gen_left = {}
        for t in targets:
            used = sum(n for pid, n in
                       self.state.data.get('candidate_counts', {}).items()
                       if tmap.get(pid) == t)
            if caps is not None:

                cap = int(caps.get(t, 0))
            else:
                cap = targets[t] or QUOTA_TOTAL
            gen_left[t] = max(0, cap - used)
        menu = self.planner_menu(gen_left, rnd)
        policy = dict(getattr(self, 'task_operator_policy', None) or {})
        modes = dict(getattr(self, 'task_constraint_mode', None) or {})
        extras = dict(getattr(self, 'task_extra_invariants', None) or {})
        constraints = {t: {
            'allowed_operators': list(ops),
            'extra_invariants': list(extras.get(t, ())),
            'invariants': (
                ['suggestion/answer may be reworded ONLY into what is '
                 'EVIDENCED by the real parent sources',
                 'user-VISIBLE input must contain the needed real '
                 'background: every parent log line preserved verbatim',
                 'no invented events, entities, parameters or context '
                 'beyond the resolved parent lineage']
                if modes.get(t) == 'background_grounded' else
                ['original parent log input preserved verbatim',
                 'trusted template gold preserved verbatim',
                 'only the explicit task question/constraints may '
                 'change', 'no new events, no new template '
                 'parameters, no parameter transform without '
                 'executable evidence'])}
            for t, ops in policy.items()}
        execution_requirements = {
            'batch_limit_means': 'independent plan ITEMS, not samples/item',
            'max_items': int(batch_limit),
            'requested_count_per_item': 1,
            'fill_rule': ('when the seed menu and remaining generation room '
                          'support it, return the full %d DISTINCT items '
                          '(unique plan_id, distinct real parent seed '
                          'material per item, operators inside each task '
                          'policy). NEVER pad the count by repeating the '
                          'same plan; NEVER raise requested_count above 1 '
                          'to substitute for more items.' % int(batch_limit)),
        }
        return packet_for(
            role='planner',
            inventory_summary=inventory(self.seeds),
            supported_cells=[{'task': t,
                              'sources': [menu[t]['source']]
                              if isinstance(menu[t]['source'], str)
                              else menu[t]['source'],
                              'difficulty_levels': list(DIFFICULTY_LEVELS)}
                             for t in TASK_TARGETS if t in menu],
            coverage_targets=targets,
            accepted_counts=cov['accepted_counts'],
            deficits=cov['deficits'],
            generation_remaining=gen_left,
            failure_summary=failure_summary(self.state.data['failures']),
            latest_failures=self.state.data['failures'][-20:],
            operator_catalog=list(OPERATORS),
            remaining_budget=None,
            batch_limit=batch_limit,
            execution_requirements=execution_requirements,
            task_constraints=(constraints or None),
            seed_task_menu=menu)

    def _plan_compat_precheck(self, item, canon_ids):
        """Check seed, task, and operator compatibility before generation.

        Cross-task projection requires a seed of the target task in the same
        source group."""
        tt = item['target_task']
        op = item.get('operator')
        policy = dict(getattr(self, 'task_operator_policy', None) or {})
        if tt in policy and op not in set(policy[tt]):
            raise RoleContractError(
                'plan %s operator %r outside the explicit %s task policy %s'
                % (item['plan_id'], op, tt, sorted(policy[tt])))
        tasks = [self.seeds_by_id[sid]['task'] for sid in canon_ids]
        if tt not in tasks:
            raise RoleContractError(
                'plan %s incompatible before dispatch: target_task %r not '
                'carried by its real parent seeds %s' % (item['plan_id'], tt,
                                                         sorted(set(tasks))))
        if op != 'cross_task_projection' and set(tasks) != {tt}:
            raise RoleContractError(
                'plan %s parent_task_mismatch before dispatch: operator %r '
                'has no registered cross-task route for parents %s'
                % (item['plan_id'], op, [
                    (sid, self.seeds_by_id[sid]['task'])
                    for sid, t in zip(canon_ids, tasks) if t != tt]))
        if op == 'cross_task_projection':
            for sid in canon_ids:
                if self.seeds_by_id[sid]['task'] == tt:
                    continue
                proj = actual_cross_task_projection(
                    {'parent_seed_id': sid, 'task': tt,
                     'generation_operator': op}, self.seeds_by_id, self.groups)
                if not proj['ok']:
                    raise RoleContractError(
                        'plan %s parent %s has no real same-group %s seed '
                        '(%s)' % (item['plan_id'], sid, tt, proj['reason']))
        want_src = (item.get('target_cell') or {}).get('source')
        if want_src is not None and not isinstance(want_src, str):
            raise RoleContractError('plan %s target_cell.source must be a '
                                    'string' % item['plan_id'])
        if want_src:
            real_srcs = {seed_source_name(self.seeds_by_id[sid])
                         for sid, t in zip(canon_ids, tasks) if t == tt}
            if want_src not in real_srcs:
                raise RoleContractError(
                    'plan %s: target source %r not backed by its real '
                    'target-task seeds (have %s)' % (item['plan_id'], want_src,
                                                     sorted(real_srcs)))

    def generalizer_packet(self, item):


        canon_ids = self.canon_refs(item['seed_ids'])
        for sid in canon_ids:
            if sid not in self.seeds_by_id:
                raise UnknownParentError(
                    'plan %s selects unavailable seed %s' % (item['plan_id'],
                                                             sid))
        self._plan_compat_precheck(item, canon_ids)
        from .tools import OPERATOR_DEFINITIONS
        catalog = {'selected_operator': item.get('operator'),
                   'definitions': dict(OPERATOR_DEFINITIONS)}
        item = dict(item, seed_ids=canon_ids,
                    operator_catalog=catalog)
        seeds_view = {sid: clean_seed_view(self.seeds_by_id[sid])
                      for sid in canon_ids}


        policy = dict(getattr(self, 'task_operator_policy', None) or {})
        cons = policy.get(item['target_task'])
        mode = dict(getattr(self, 'task_constraint_mode', None) or {}).get(
            item['target_task'], 'gold_verbatim')
        extras = dict(getattr(self, 'task_extra_invariants', None) or {})
        gcons = None
        if cons:
            gcons = {'allowed_operators': list(cons),
                     'extra_invariants': list(extras.get(
                         item['target_task'], ()))}
            if mode == 'background_grounded':
                gcons['invariants'] = [
                    'keep the parent log input byte-identical (the '
                    'user-visible input MUST contain the needed real '
                    'background)',
                    'ground every suggested fix/recommendation ONLY in the '
                    'provided parent sources/evidence: no invented '
                    'context, events or tool assumptions']
            else:
                gcons['invariants'] = [
                    'keep the parent log input byte-identical',
                    'keep the trusted template gold byte-identical',
                    'change ONLY the explicit question/constraints']


        from .tools import OPERATOR_INVARIANTS, INPUT_CHANGING_OPS
        sel_op = item.get('operator')
        if sel_op in INPUT_CHANGING_OPS:
            gcons = gcons or {}
            gcons['invariants'] = list(OPERATOR_INVARIANTS[sel_op])
            gcons['selected_operator'] = sel_op
        return packet_for(
            role='generalizer', plan_item=item,
            seeds=seeds_view, seeds_by_id=seeds_view,
            evidence_contents=self._evidence_content(
                item.get('evidence_ids', [])),
            task_contract={'tasks': list(TASK_TARGETS),
                           'operators': list(OPERATORS)},
            task_constraints=gcons,
            neighbor_summaries=self._neighbors(item),
            source_evidence=item.get('evidence_ids', []))

    def _evidence_content(self, ids, max_chars=4000):
        """Read seed fields and recorded evidence for the role context."""
        out = {}
        for rid in ids or []:
            seed = self.seeds_by_id.get(rid)
            if seed is None:
                continue
            ev = clean_seed_view(seed)
            blob = json.dumps(ev, ensure_ascii=False)
            out[rid] = blob[:max_chars]
        return out

    def _neighbors(self, item):
        out = []
        for sid in item['seed_ids']:
            seed = self.seeds_by_id[sid]
            out.append({'record_id': sid,
                        'group_id': seed['group_id'],
                        'input_sha256': _sha(seed['input'])})
        return out[:8]

    def inspector_packet(self, candidate, dup=None):
        tcc = task_contract_check(candidate)
        slc = source_label_check(candidate, self.seeds_by_id, self.groups)
        dup_view = None if dup is None else {
            k: v for k, v in dup.items() if k != 'hits'}
        if dup_view is not None:
            dup_view['confirmed_prohibited'] = bool(dup.get(
                'confirmed_prohibited'))
        if dup is not None:
            dup_view['hits'] = [
                dict(h, exact_pair=(h['kind'] == 'exact_pair'),
                     confirmed_prohibited=bool(dup.get(
                         'confirmed_prohibited')))
                for h in dup['hits'][:5]]


        reference = None
        parent = self.seeds_by_id.get(candidate.get('parent_seed_id'))
        if parent is not None:
            from .tools import _norm
            same_task = (candidate.get('task') == parent.get('task')
                         == 'Parsing')
            same_input = (str(candidate.get('input') or '')
                          == str(parent.get('input') or ''))
            if same_task and same_input:
                gold = parent.get('label')
                if gold:
                    eid = 'ev-' + hashlib.sha256(
                        parent['record_id'].encode()
                        ).hexdigest()[:12]
                    reference = {'target': gold,
                                 'provenance': {
                            'canonical_parent': eid},
                            'convention': 'train-view-v1 gold'}
        return packet_for(
            role='inspector', candidate=candidate,
            task_contract={'tasks': list(TASK_TARGETS)},
            deterministic_checks={'task_contract_check': tcc,
                                  'source_label_check': slc,
                                  'near_duplicate_search': dup_view},
            evidence_contents=self._evidence_content(
                candidate.get('evidence_refs', [])
                + list(candidate.get('source_record_ids', []))),
            source_label_version=candidate.get('source_label_version'),
            reference=reference,
            neighbor_summaries=[{'record_id': candidate['parent_seed_id']}])

    def optimizer_packet(self, candidate, report, round_no, prev_edits):
        repairable = [d for d in report['defects']
                      if d['repairability'] == 'repairable']
        scope = sorted({f for d in repairable for f in d['edit_scope']})
        want_ids = sorted({e for d in repairable for e in d['evidence_ids']}
                          | set(candidate.get('source_record_ids', [])))
        contents = self._evidence_content(want_ids)
        tsk = candidate.get('task')
        policy = dict(getattr(self, 'task_operator_policy', None) or {})
        extras = dict(getattr(self, 'task_extra_invariants', None) or {})
        repair_constraints = None
        if tsk in policy:
            mode = dict(getattr(self, 'task_constraint_mode', None) or {}).get(
                tsk, 'gold_verbatim')
            immutable = (
                'the trusted parent gold/log content is IMMUTABLE: never '
                're-tokenize, re-space or "correct" it to satisfy a defect '
                'report (a genuine gold fault is isolated, not edited); '
                'never add question demands that conflict with the '
                'established template annotation convention'
                if mode == 'gold_verbatim' else
                'never inject gold answer/root-cause text into the '
                'user-visible input as fake background; background may '
                'only be non-answer context already present in the real '
                'parent sources')
            repair_constraints = {
                'allowed_operators': list(policy[tsk]),
                'invariants': [immutable] + list(extras.get(tsk, ())),
                'forbidden_edit_targets': ['output'] if mode == 'gold_verbatim' else []}
        return packet_for(
            role='optimizer', candidate=candidate,
            inspection_report=report, allowed_edit_scope=scope,
            defect_report=repairable, evidence_ids=want_ids,
            evidence_contents=contents,
            missing_evidence=[e for e in want_ids if e not in contents],
            preserve=sorted({p for d in repairable for p in d['preserve']}),
            previous_edits=prev_edits, repair_round=round_no,
            task_repair_constraints=repair_constraints)


    def _packet_hash(self, packet):
        blob = json.dumps(packet, sort_keys=True, default=str)
        return 'sha256:' + hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _dispatch_tag(self, role, packet):
        """Create a stable request identifier from the role and context packet."""
        core = json.dumps({'role': role,
                           'packet_sha256': self._packet_hash(packet)},
                          sort_keys=True, default=str)
        digest = hashlib.sha256(core.encode()).hexdigest()[:12]
        return 'logrefine.%s:%s' % (role, digest)

    def _call(self, role, packet):
        phash = self._packet_hash(packet)
        packet['_packet_sha256'] = phash
        packet['_dispatch_tag'] = self._dispatch_tag(role, packet)
        try:
            result, receipt = self.dispatcher(role, packet)
        except (UnknownParentError, RoleContractError):
            raise
        except AssertionError as exc:
            raise RoleContractError('dispatch refused: %s'
                                    % str(exc)[:120]) from None
        except APIError:
            raise

        except Exception as exc:
            raise RoleContractError('dispatch failed for %s: %s'
                                    % (role, type(exc).__name__)) from None

        validate_role_v1(role, result, packet)
        for name, digest in receipt.get('consumed_hashes', {}).items():
            self.consumed_hashes.add((role, name, digest))
        return result

    def _route_gates(self, insp, candidate):
        """Check acceptance and repair conditions; return a route override if needed."""
        five_pass = all(insp['checks'][k] == 'pass' for k in QUALITY_CHECKS)
        confirmed_dup = any(d['code'].startswith('duplicate_')
                            and d['severity'] in ('major', 'critical')
                            for d in insp['defects'])
        if insp['route'] == 'accept':
            if not five_pass or confirmed_dup:
                return ('quarantine', 'gate: accept without clean checks/no-dup')
            tcc = task_contract_check(candidate)
            if not tcc['valid']:
                return ('quarantine', 'gate: format_error %s' % tcc['errors'])
            try:
                slc = source_label_check(candidate, self.seeds_by_id, self.groups)
            except UnknownParentError as exc:
                return ('quarantine', 'gate: unknown_parent %s' % str(exc)[:120])
            if not slc['ok']:
                needs = [e for e in slc['errors']
                         if e.startswith('changed_event_conditions')
                         or e.startswith('label_derivation_needs_evidence')]
                if needs:
                    return ('regeneralize', 'gate: label derivation unsupported')
                return ('quarantine', 'gate: source-label conflict %s' % slc['errors'])
            if candidate.get('task') != self.seeds_by_id[
                    candidate['parent_seed_id']]['task'] and \
                    candidate.get('generation_operator') != \
                    'cross_task_projection':
                return ('regeneralize', 'gate: unsupported task for seed')
            strict = self._strict_preservation_check(
                candidate, slc_ok=slc['ok'])
            if strict:
                return strict
            return None
        if insp['route'] == 'repair':
            bad = any(d['repairability'] != 'repairable' for d in insp['defects'])
            if bad or not insp['defects']:
                return ('quarantine', 'gate: repair without bounded repairable defects')
        return None

    def _strict_preservation_check(self, candidate, slc_ok=False):
        """Check task-specific preservation of source input and reference answers."""
        policy = dict(getattr(self, 'task_operator_policy', None) or {})
        task = candidate.get('task')
        if task not in policy:
            return None
        cand_op = candidate.get('generation_operator')
        if cand_op == 'none':


            pass
        elif cand_op not in set(policy[task]):
            return ('quarantine', 'gate: %s candidate operator %r outside '
                                  'task policy' % (task, cand_op))
        from .tools import _norm
        import re as _re
        pids = candidate.get('source_record_ids') or [
            candidate['parent_seed_id']]
        parents = []
        for pid in pids:
            seed = self.seeds_by_id.get(pid)
            if seed is None:
                return ('quarantine', 'gate: %s unknown parent %s'
                        % (task, pid))
            if seed.get('task') != task:
                return ('regeneralize', 'gate: %s parent %s is not a real %s '
                                       'seed' % (task, pid, task))
            parents.append(seed)


        if cand_op == 'rule_verified_parameter_transform':
            der = candidate.get('label_derivation') or {}
            subs = der.get('substitutions')
            if (der.get('rule') != 'consistent_parameter_transform'
                    or not isinstance(subs, list) or not subs):
                return ('quarantine', 'gate: %s parameter transform without '
                                      'an executable declared map' % task)
            if not slc_ok:
                return ('quarantine', 'gate: %s parameter transform '
                                      'source_label_check not ok' % task)

            def _replay(text):
                out = str(text or '')
                for pair in subs:
                    if (not isinstance(pair, (list, tuple))
                            or len(pair) != 2
                            or not isinstance(pair[0], str)
                            or not isinstance(pair[1], str)
                            or pair[0] == pair[1] or pair[0] == ''):
                        return None
                    if pair[0] not in out:
                        return None
                    out = out.replace(pair[0], pair[1])
                return out
            for seed in parents:
                want_in = _replay(seed.get('input'))
                if want_in is None or want_in != str(
                        candidate.get('input') or ''):
                    return ('quarantine', 'gate: %s parameter transform '
                            'map does not replay the candidate input from '
                            'parent %s' % (task, seed['record_id']))
                pr_out = seed.get('output')
                if pr_out is None:
                    pr_out = seed.get('label')
                want_out = _replay(pr_out)
                cand_out = str(candidate.get('output') or '')
                if want_out != cand_out and pr_out != cand_out:
                    return ('quarantine', 'gate: %s parameter transform '
                            'map does not replay the candidate output '
                            'from parent %s' % (task, seed['record_id']))
            return None


        def lines(text):
            return [_re.sub(r'\s+', ' ', ln).strip().lower()
                    for ln in (text or '').splitlines() if ln.strip()]
        cand_lines = lines(candidate.get('input'))
        parent_lines = []
        for seed in parents:
            parent_lines.extend(lines(seed['input']))
            missing = [ln for ln in lines(seed['input'])
                       if ln not in cand_lines]
            if missing:
                return ('quarantine', 'gate: %s parent log lines dropped/'
                                      'rewritten (%s, %d lines)'
                        % (task, seed['record_id'], len(missing)))
        invented = [ln for ln in cand_lines if ln not in parent_lines]
        if invented:
            return ('quarantine', 'gate: %s student log carries non-parent '
                                  'event lines: %s' % (task,
                                                       invented[0][:80]))
        gold = _norm(parents[0].get('label') or '')
        mode = dict(getattr(self, 'task_constraint_mode', None) or {}).get(
            task, 'gold_verbatim')
        if mode == 'background_grounded':


            if not str(candidate.get('output') or '').strip():
                return ('quarantine', 'gate: %s empty recommendation' % task)
            return None
        if not gold or _norm(candidate.get('output')) != gold:
            return ('quarantine', 'gate: %s trusted template gold not '
                                  'preserved verbatim' % task)
        return None


    WEAK_EVIDENCE_CODES = frozenset((
        'unsupported_causal_inference', 'instruction_label_mismatch',
        'grounding_error', 'label_conflict'))

    def _maybe_isolate_weak_evidence(self, task, item, row):
        """Skip a seed after repeated insufficient-evidence outcomes in this run."""
        if dict(getattr(self, 'task_constraint_mode',
                        None) or {}).get(task) != 'background_grounded':
            return
        codes = row.get('codes') or ([row.get('code')]
                                     if row.get('code') else [])
        hit = [c for c in codes if c in self.WEAK_EVIDENCE_CODES]
        if not hit:
            return
        sids = self.state.data.get('plan_seed_index', {}).get(
            item['plan_id']) or []
        gpid = item['plan_id']
        log = self.state.data.setdefault('weak_parent_hits', {})
        iso = self.state.data.setdefault('seed_isolation', {})
        for sid in sids:
            if sid in iso:
                continue
            seen = log.setdefault(sid, [])
            seen.append({'plan_id': gpid, 'codes': hit})
            if len(seen) >= 2:
                iso[sid] = {'task': task, 'record_id': sid,
                            'first_plan': seen[0]['plan_id'],
                            'second_plan': seen[-1]['plan_id'],
                            'codes': sorted({c for e in seen
                                             for c in e['codes']}),
                            'note': 'current-stage evidence cannot resolve '
                                    'this parent (two real weak-evidence '
                                    'quarantines on the SAME seed): skipped '
                                    'in future menus ONLY - original input/'
                                    'gold untouched, accepted samples never '
                                    'deleted',
                            'at': time.strftime('%Y%m%dT%H%M%SZ',
                                                time.gmtime())}
                self.state.data['failures'].append(
                    dict(iso[sid], kind='seed_isolated_weak_evidence'))
                self.state.save()

    def build_plan_item(self, task, seed_id, plan_id=None, operator=None):
        """Build a schema-valid plan item for local pipeline tests."""
        return {'plan_id': plan_id or ('fix-' + seed_id),
                'seed_ids': [seed_id], 'target_task': task,
                'target_cell': {'source': seed_source_name(
                    self.seeds_by_id[seed_id]),
                    'scenario': 'fixture',
                    'difficulty': 'basic'},
                'operator': operator or OPERATORS[0],
                'evidence_ids': [seed_id],
                'invariants': ['label preserved'],
                'requested_count': 1, 'attempt_budget': PER_PLAN_ATTEMPT_CAP}

    def _record_failure(self, row):
        if isinstance(row, _Outcome):
            row = row.as_row()
        self.state.data['failures'].append(row)
        self.state.save()

    def _dup_corpus(self, cand, item, sigs):
        """Build the duplicate comparison pool.

        For an identity operation, omit its own parent from exact-pair comparison.
        Keep other source records and all comparisons for generated variations."""
        if not isinstance(cand, dict) or \
                cand.get('generation_operator') != 'none':
            return sigs
        ids = {cand.get('parent_seed_id')} | set(
            cand.get('source_record_ids') or []) | set(
            (item or {}).get('seed_ids') or [])
        ids.discard(None)
        return [x for x in sigs if x.get('record_id') not in ids]

    def _process_item(self, item, pool_signatures, max_items=None,
                      remaining_total=None, remaining_task=None):


        counts = self.state.data.setdefault('candidate_counts', {})


        _batch_start_total = int(self.state.data.get(
            'batch_start_total_candidates',
            self.state.data.get('total_candidates_generated', 0)))
        _batch_start_task = int(self.state.data.get(
            'batch_start_task_candidates', {}).get(item['target_task'], 0))
        caps = [int(item.get('attempt_budget') or PER_PLAN_ATTEMPT_CAP)]
        if max_items is not None:
            caps.append(int(max_items))
        if remaining_total is not None:
            caps.append(int(remaining_total))
        if remaining_task is not None:
            caps.append(int(remaining_task))
        cap = min(caps)
        try:
            item = dict(item, seed_ids=self.canon_refs(item['seed_ids']))
        except UnknownParentError as exc:
            row = _Outcome('quarantine', code='unknown_parent',
                           reason=str(exc)[:200],
                           plan_id=item['plan_id']).as_row()
            row['zero_http_precheck'] = True
            self._record_failure(row)
            return row
        if item['seed_ids'] and item['seed_ids'][0] not in self.seeds_by_id:
            row = _Outcome('quarantine', code='unknown_parent',
                           reason='plan selects unavailable seed',
                           plan_id=item['plan_id']).as_row()
            row['zero_http_precheck'] = True
            self._record_failure(row)
            return row
        if not pool_signatures:


            raise ValueError('pool_signatures required for production gates')


        attempts = int(self.state.data.get('item_attempts', {})
                       .get(item['plan_id'], 0))
        if counts.get(item['plan_id'], 0) >= cap:


            return _Outcome('candidate_cap_exhausted',
                            plan_id=item['plan_id'],
                            candidates_generated=counts[item['plan_id']],
                            cap=cap).as_row()
        last_outcome = None
        while attempts < cap:


            st_now = self.state.data
            gt_all = int(st_now.get('total_candidates_generated', 0))
            rem_t = None if remaining_total is None else (
                _batch_start_total + int(remaining_total) - gt_all)
            tmap = st_now.get('plan_task_index', {})
            task_all = sum(int(n) for pid, n in
                           st_now.get('candidate_counts', {}).items()
                           if tmap.get(pid, item['target_task'])
                           == item['target_task'])
            rem_k = None if remaining_task is None else (
                _batch_start_task + int(remaining_task) - task_all)
            if counts.get(item['plan_id'], 0) >= cap or \
                    (rem_t is not None and rem_t <= 0) or \
                    (rem_k is not None and rem_k <= 0):
                break
            attempts += 1
            self.state.data.setdefault('item_attempts', {})[item['plan_id']] \
                = attempts
            self.state.save()
            try:
                packet = self.generalizer_packet(item)
            except (UnknownParentError, RoleContractError) as exc:
                row = _isolate(exc, 'generalizer').as_row()
                row['zero_http_precheck'] = True

                self._record_failure(row)
                return row


            counts[item['plan_id']] = counts.get(item['plan_id'], 0) + 1
            self.state.data['total_candidates_generated'] = \
                self.state.data.get('total_candidates_generated', 0) + 1
            self.state.save()

            try:
                gen = self._call('generalizer', packet)
            except (UnknownParentError, RoleContractError) as exc:
                row = _isolate(exc, 'generalizer').as_row()
                self._record_failure(row)
                return row
            except Exception as exc:
                last_outcome = _isolate(exc, 'generalizer')
                self._record_failure(last_outcome)
                break
            if gen['status'] == 'needs_evidence':


                row = _Outcome('regeneralize_deferred',
                               missing_requirements=gen['missing_requirements'],
                               plan_id=item['plan_id']).as_row()
                self._record_failure(row)
                return row
            raw = dict(gen['candidate'])
            try:
                raw['source_record_ids'] = self.canon_refs(
                    raw.get('source_record_ids') or [raw['parent_seed_id']])
                raw['parent_seed_id'] = self.canon_refs(
                    [raw['parent_seed_id']])[0]
            except UnknownParentError as exc:
                raise UnknownParentError(
                    '%s in candidate of plan %s' % (exc, item['plan_id']))
            for pid in raw['source_record_ids']:
                if pid not in self.seeds_by_id:
                    raise UnknownParentError(
                        'unknown parent %s in candidate of plan %s'
                        % (pid, item['plan_id']))
            cand = dict(raw)
            cand.setdefault('source_record_ids', [cand['parent_seed_id']])


            if self.state.already_accepted(self.stable_sample_id(item, cand)):
                return _Outcome('already_accepted_skip',
                                note='resume: settled content never '
                                     're-inserted').as_row()
            accepted_echo = accepted_pool_confirms_duplicate(
                cand, self.state.data.get('records', []))
            sigs = list(pool_signatures or []) + [
                {'record_id': rec['sample_id'],
                 'instruction': rec.get('instruction'),
                 'input': rec.get('input')}
                for rec in self.state.data.get('records', [])]
            dup = near_duplicate_search(cand, self._dup_corpus(
                cand, item, sigs))


            if accepted_echo or dup['confirmed_prohibited']:
                row = _Outcome('quarantine', plan_id=item['plan_id'],
                               code='confirmed_prohibited_duplicate',
                               reason='echoes an already accepted record',
                               attempts=attempts).as_row()
                self._record_failure(row)
                return row
            try:
                insp = self._evaluate_inspection(cand, dup, item)
            except Exception as exc:
                last_outcome = _isolate(exc, 'inspector')
                self._record_failure(last_outcome)
                break
            route = insp['route']


            if dup.get('confirmed_prohibited') and route == 'accept':
                last_outcome = _Outcome('quarantine', plan_id=item['plan_id'],
                                        code='confirmed_prohibited_duplicate',
                                        reason='exact pair match against '
                                               'accepted pool + seed parents',
                                        attempts=attempts)
                self._record_failure(last_outcome)
                return last_outcome.as_row()
            try:
                override = self._route_gates(insp, cand)
            except UnknownParentError as exc:
                last_outcome = _Outcome('quarantine', code='unknown_parent',
                                        reason=str(exc)[:160])
                break
            if override:
                route, code = override
                insp = dict(insp, route=route, route_reason=code)
            if route == 'accept':
                return self._finalize(cand, item, insp, gen, attempts)
            if route == 'repair':
                used_before = self.repair_rounds
                final = self._repair_flow(cand, insp, item, pool_signatures,
                                          base_rounds=used_before)
                self.repair_rounds = max(getattr(self, '_rounds_after', 0),
                                         used_before + 1)
                if final is None:


                    continue
                if final.get('kind') == 'regeneralize' and \
                        attempts < cap and \
                        self.repair_rounds < MAX_REPAIR_ROUNDS:
                    continue
                return final
            last_outcome = _Outcome(route, plan_id=item['plan_id'],
                                    codes=[d['code'] for d in insp['defects']],
                                    reason=insp['route_reason'][:160],
                                    attempts=attempts)
            self._record_failure(last_outcome)

            if route.startswith('quarantine'):
                break
        return (last_outcome or _Outcome('exhausted',
                                        plan_id=item['plan_id'])).as_row()

    def _evaluate_inspection(self, cand, dup, item):
        """Inspect a candidate and return the validated response."""
        return self._call('inspector', self.inspector_packet(cand, dup))

    def _repair_and_reinspect(self, cand, insp, item, pool_signatures,
                              rounds_used):

        """Pass diagnosed defects to the Optimizer and inspect the revision.

        Preserve record identity and enforce the edit scope and repair-round limit."""
        prev_edits = []
        current = cand
        self._rounds_after = rounds_used
        for rnd in range(rounds_used, MAX_REPAIR_ROUNDS):
            self._rounds_after = rnd + 1
            packet = self.optimizer_packet(current, insp, rnd, prev_edits)
            try:
                opt = self._call('optimizer', packet)
            except Exception as exc:
                outcome = _isolate(exc, 'optimizer')
                self._record_failure(outcome)
                return ((outcome, False), current, rnd)
            if opt['status'] != 'revised':
                route = opt['requested_route']


                regen = (route == 'reinspect' or
                         opt['status'] == 'needs_evidence')
                outcome = _Outcome('quarantine' if not regen else route,
                                   plan_id=item['plan_id'],
                                   code='optimizer_%s' % opt['status'],
                                   unresolved=opt['unresolved_defects'])
                return ((outcome, regen), current, rnd)
            revised = dict(opt['revised_candidate'])
            revised.setdefault('source_record_ids',
                               current.get('source_record_ids'))
            guard = self._identity_guard(current, revised, packet)
            if guard:
                return ((_Outcome('quarantine', plan_id=item['plan_id'],
                                  code=guard), False), current, rnd)
            prev_edits.extend(opt['defect_to_edit'])
            self.recent_edits.extend(opt['defect_to_edit'])
            current = revised
            dup = near_duplicate_search(current, self._dup_corpus(
                current, item, pool_signatures))
            if dup['confirmed_prohibited']:
                outcome = _Outcome('quarantine', plan_id=item['plan_id'],
                                   code='gate: confirmed duplicate after repair')
                return ((outcome, False), current, rnd)
            try:
                insp = self._evaluate_inspection(
                    current, near_duplicate_search(
                        current, self._dup_corpus(current, item,
                                                  pool_signatures)),
                    item)
            except Exception as exc:
                outcome = _isolate(exc, 'inspector_recheck')
                return ((outcome, False), current, rnd)
            override = self._route_gates(insp, current)
            if override:
                insp = dict(insp, route=override[0], route_reason=override[1])
            if insp['route'] == 'accept':
                self.accept_insp = insp
                return ('ACCEPT', None), current, rnd
            if insp['route'] != 'repair':
                return ((_Outcome(insp['route'], plan_id=item['plan_id'],
                                  code='after_repair_round_%d' % rnd,
                                  reasons=[insp['route_reason']]),
                         insp['route'] == 'regeneralize'), current, rnd)

        return ((_Outcome('quarantine', plan_id=item['plan_id'],
                          code='max_repair_rounds_stopped'), False),
                current, MAX_REPAIR_ROUNDS)

    def _identity_guard(self, before, after, packet):
        """Check changed fields against the permitted edit scope and identity rules."""
        for k in ('sample_id', 'parent_seed_id', 'source_record_ids',
                  'source_id', 'source_revision', 'task'):
            if before.get(k) != after.get(k):
                return 'identity_changed:%s' % k
        if before.get('task') == 'AD' and before.get('output') != \
                after.get('output'):
            return 'gold_label_overwritten'

        scope = set(packet['allowed_edit_scope'])
        changed = [k for k in set(before) | set(after)
                   if before.get(k) != after.get(k)]
        illegal = [k for k in changed if k not in scope]
        return ('edit_outside_allowed_scope:%s' % ','.join(sorted(illegal))) \
            if illegal else None

    def _repair_flow(self, cand, insp, item, pool_signatures, base_rounds=0):
        """Run bounded repair rounds and return the outcome or request regeneration."""
        if base_rounds >= MAX_REPAIR_ROUNDS:
            row = _Outcome('quarantine', plan_id=item['plan_id'],
                           code='max_repair_rounds_stopped').as_row()
            self._record_failure(row)
            return row
        result = self._repair_and_reinspect(cand, insp, item, pool_signatures,
                                            base_rounds)
        (payload, current, rnd) = result
        outcome, regeneralize = payload
        if isinstance(outcome, str) and outcome == 'ACCEPT':
            return self._finalize(current, item, self.accept_insp, None, rnd)
        self._record_failure(outcome.as_row())
        return None if regeneralize else outcome.as_row()

    @staticmethod
    def stable_sample_id(item, cand):
        """Create a content-based identifier for a training example."""
        payload = json.dumps({k: cand.get(k) for k in
                              ('task', 'instruction', 'input', 'output',
                               'reasoning')}, sort_keys=True)
        return 'syn-' + hashlib.sha256(payload.encode()).hexdigest()[:16]

    def _finalize(self, cand, item, insp, gen, attempts):
        tcc = task_contract_check(cand)
        if not tcc['valid']:
            row = _Outcome('quarantine', plan_id=item['plan_id'],
                           code='format_error', detail=tcc['errors']).as_row()
            self._record_failure(row)
            return row
        for e in insp.get('defects', []):
            _validate_defect(e)
        sid = self.stable_sample_id(item, cand)
        if self.state.already_accepted(sid):
            return _Outcome('already_accepted_skip', sample_id=sid,
                            note='resume: duplicate accepted insertion '
                                 'refused').as_row()


        record = {k: v for k, v in cand.items()}
        record['sample_id'] = sid
        audit = {'sample_id': sid, 'status': 'accepted',
                 'inspector_route': insp['route'], 'attempts': attempts,
                 'candidate_sha256': _sha(json.dumps(cand, sort_keys=True)),
                 'review': {'inspector': insp}, 'edits': [],
                 'operator': cand['generation_operator'],
                 'optimizer_edits': list(self.recent_edits),
                 'parent_seed_id': cand['parent_seed_id'],
                 'group_ids': source_label_check(
                     cand, self.seeds_by_id, self.groups)['group_ids']}
        self.recent_edits = []


        inserted = self.state.accept(record, audit)
        if inserted:
            for gid in audit['group_ids']:
                self.state.data.setdefault('groups', {}).setdefault(
                    gid, []).append(sid)
        self.state.save()
        return _Outcome('accepted', sample_id=sid, reused=not inserted,
                        plan_id=item['plan_id']).as_row()


def _validate_defect(d):
    """Validate a defect description before routing the candidate."""
    for k in ('defect_id', 'code', 'location', 'description'):
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise RoleContractError('defect.%s required' % k)
    if d.get('severity') not in ('minor', 'major', 'critical'):
        raise RoleContractError('invalid severity')
    if d.get('repairability') not in ('repairable', 'needs_evidence',
                                      'unrepairable'):
        raise RoleContractError('invalid repairability')


def _cell_key(task, item):
    return '%s|%s|%s' % (task, (item.get('target_cell') or {}).get('source', ''),
                         (item.get('target_cell') or {}).get('difficulty', ''))


if __name__ == 'lr.pipeline.module':
    pass


def run_batch(pipe, dispatcher_state=None, targets=None, max_candidates=None,
              batch_limit=10, max_rounds=6, wall_sec=600,
              first_g_accept_gate=None, generation_caps=None,
              task_no_accept_gate=None):
    """Run planning and generation rounds within task, attempt, and time limits.

    Each round gives the Planner current coverage and inspection feedback.
    Targets count accepted examples; generation_caps count generation attempts.
    Optional no-accept limits stop tasks that repeatedly produce no usable data."""

    pipe._generation_caps = dict(generation_caps or {}) or None
    targets = targets or TASK_TARGETS
    gen_caps = dict(generation_caps or {})

    def _gen_cap(t):
        if t in gen_caps:
            return int(gen_caps[t])
        return int(targets.get(t) or QUOTA_TOTAL)


    accept_gate = None if task_no_accept_gate is None else int(
        task_no_accept_gate)
    streak = defaultdict(int)
    if accept_gate is not None:
        for t, n in (pipe.state.data.get('task_no_accept_streak') or {}).items():
            streak[t] = int(n)
    accepted_counts = defaultdict(int)


    for task, n in usable_accepted_counts(
            pipe.state.data.get('records') or [],
            pipe.state.data.get('accepted_by_task') or {}).items():
        accepted_counts[task] = n
    attempts = defaultdict(int)
    for ck, n in pipe.state.data.get('cell_attempts', {}).items():
        attempts[ck] = n


    gen_total = int(pipe.state.data.get('total_candidates_generated', 0))


    pipe.state.data['batch_start_total_candidates'] = gen_total
    task_gen = defaultdict(int)
    cand_counts = pipe.state.data.get('candidate_counts', {})
    plan_task = pipe.state.data.get('plan_task_index', {})
    for pid, n in cand_counts.items():
        t = plan_task.get(pid)
        if t:
            task_gen[t] += n
    pipe.state.data['batch_start_task_candidates'] = dict(task_gen)

    batch_start_gen_total = gen_total
    batch_start_task_gen = defaultdict(int, task_gen)


    acc_start_total = sum(accepted_counts.values())
    gate_live = first_g_accept_gate is not None
    gate_stop = False
    closed_tasks = set()
    pipe._closed_tasks_view = closed_tasks


    menu_base = int(pipe.state.data.get('menu_round_offset', 0))

    def _post_row(item, row, new_g):
        """Update coverage, attempt counts, stop conditions, and saved progress."""
        nonlocal stop_reason, gate_stop, gate_live
        tsk = item['target_task']
        if row and row['kind'] == 'accepted':
            accepted_counts[tsk] += 1
            streak[tsk] = 0
        elif new_g > 0:


            streak[tsk] += new_g
        if accept_gate is not None and streak[tsk] >= accept_gate and \
                tsk not in closed_tasks:


            closed_tasks.add(tsk)
            gen_caps[tsk] = task_gen.get(tsk, 0)
            pipe._generation_caps = dict(gen_caps)
            pipe.state.data['failures'].append(
                {'kind': 'task_no_accept_gate', 'task': tsk,
                 'consecutive_new_g_without_accept': int(streak[tsk]),
                 'gate': accept_gate,
                 'note': 'task generation CLOSED for the stage after '
                         'gate-worth consecutive NEW G dispatches with '
                         'ZERO accepts: other tasks CONTINUE THE BATCH'})
        pipe.state.data['task_no_accept_streak'] = dict(streak)
        if gate_live and (gen_total - batch_start_gen_total) >= \
                int(first_g_accept_gate):
            if sum(accepted_counts.values()) == acc_start_total:
                stop_reason = 'first_g_accept_gate_zero_accept'
                pipe.state.data['failures'].append(
                    {'kind': 'first_g_accept_gate_zero_accept',
                     'gate': int(first_g_accept_gate),
                     'new_g_dispatches': gen_total -
                     batch_start_gen_total,
                     'new_accepted': 0,
                     'note': 'first new G dispatches of this batch yielded '
                             'ZERO accepts: DISCLOSED early stop'})
                pipe.state.save()
                gate_stop = True
            else:
                gate_live = False
        if max_candidates is not None and gen_total >= max_candidates:
            stop_reason = 'candidate_cap_reached'
            return 'cap'


        elif row and row.get('plan_id') is None:
            entry = dict(row, plan_id=item['plan_id'])
            pipe.state.data['failures'].append(entry)
            pipe.state.save()
        pipe.state.data['processed_plans'].append(item['plan_id'])
        pipe.state.data['accepted_by_task'] = {
            k: pipe.state.data.get('accepted_by_task', {}).get(k, [])
            for k in targets}
        pipe.state.save()
        if gate_stop:
            return 'stop'
        return None

    rows = []
    t0 = time.time()
    stop_reason = 'filled_or_exhausted'
    planner_snapshot = None
    pool_sig = [clean_seed_view(s) for s in pipe.seeds]

    def _request_plan(r_idx):
        """Request a plan and exclude plan identifiers that have already been processed."""
        nonlocal planner_snapshot
        planner_snapshot = pipe.planner_packet(targets, batch_limit,
                                               rnd=menu_base + r_idx)
        try:
            plan = pipe._call('planner', planner_snapshot)
        except (RoleContractError, AssertionError) as exc:
            rows.append({'kind': 'planner_error', 'reason': str(exc)[:200]})
            pipe.state.data['failures'].append({'kind': 'planner_error',
                                                'reason': str(exc)[:200]})
            pipe.state.save()
            return None
        items = [it for it in plan['plan_items']
                 if it['plan_id'] not in pipe.state.data['processed_plans']]
        return plan, items

    for rnd in range(max_rounds):
        pipe._batch_round = rnd


        quota_left = sum(max(0, targets[t] - accepted_counts.get(t, 0))
                         for t in targets)
        cand_left = (max_candidates - gen_total) if max_candidates is not None \
            else float('inf')
        if quota_left <= 0 or cand_left <= 0:


            stop_reason = ('candidate_cap_reached' if cand_left <= 0
                           and quota_left > 0 else 'quota_reached')
            break
        if time.time() - t0 >= wall_sec:
            stop_reason = 'wall_clock_stop'
            break
        got = _request_plan(rnd)
        if got is None:
            break
        plan, items = got
        if not items:
            stop_reason = 'filled_or_exhausted'
            break
        gen_at_round_start = gen_total
        for item in items:
            ck = _cell_key(item['target_task'], item)
            task_cap = _gen_cap(item['target_task'])
            if max_candidates is not None and gen_total >= max_candidates:
                stop_reason = 'candidate_cap_reached'
                break
            if task_gen[item['target_task']] >= task_cap:


                continue
            if item['target_task'] in gen_caps:


                room = task_cap - task_gen.get(item['target_task'], 0)
            else:
                room = (targets[item['target_task']] * ATTEMPT_FACTOR
                        - attempts.get(ck, 0))
            if room <= 0 or accepted_counts[item['target_task']] >= \
                    targets[item['target_task']]:
                continue
            attempts[ck] = attempts.get(ck, 0) + 1
            pipe.state.data['cell_attempts'] = dict(attempts)
            pipe.state.data.setdefault('plan_task_index', {})[item['plan_id']] \
                = item['target_task']
            pipe.state.data.setdefault('plan_seed_index', {})[
                item['plan_id']] = list(item['seed_ids'] or ())
            pipe.repair_rounds = 0
            before = int(cand_counts.get(item['plan_id'], 0))
            after = before
            regens = max(1, min(item['requested_count'] * 2,
                                PER_PLAN_ATTEMPT_CAP, room))


            rem_total = (max_candidates - batch_start_gen_total) \
                if max_candidates is not None else None
            rem_task = task_cap - batch_start_task_gen[item['target_task']]
            before = int(cand_counts.get(item['plan_id'], 0))
            after = before
            row = pipe._process_item(item, pool_sig, max_items=regens,
                                     remaining_total=rem_total,
                                     remaining_task=rem_task)
            after = int(pipe.state.data.get(
                'candidate_counts', {}).get(item['plan_id'], before))
            gen_total += after - before
            task_gen[item['target_task']] += after - before
            pipe.state.data['total_candidates_generated'] = gen_total


            rows.append(row)
            if _post_row(item, row, max(0, after - before)):
                break
            if row and row.get('kind') == 'quarantine':


                pipe._maybe_isolate_weak_evidence(item['target_task'], item,
                                                  row)
        if gate_stop or not items:
            break


        if gen_total == gen_at_round_start:
            idle = getattr(pipe, '_idle_rounds', 0) + 1
            pipe._idle_rounds = idle
            if idle >= 2:
                stop_reason = 'planner_offtarget_idle_stop'
                pipe.state.data['failures'].append(
                    {'kind': 'planner_offtarget_idle_stop',
                     'remaining_generation': {
                         t: max(0, _gen_cap(t) - task_gen.get(t, 0))
                         for t in targets}})
                pipe.state.save()
                break
        else:
            pipe._idle_rounds = 0


    pipe.state.data['menu_round_offset'] = menu_base + (rnd + 1)
    pipe.state.save()
    stats = {
        'accepted_by_task': dict(accepted_counts),
        'attempts_by_cell': dict(attempts),
        'deficits': {t: targets[t] - accepted_counts.get(t, 0)
                     for t in targets},
        'new_generation': max(0, gen_total - batch_start_gen_total),
        'generation_by_task': {t: task_gen.get(t, 0) for t in targets},
        'generation_caps': dict(gen_caps) or None,
        'task_no_accept_streak': dict(streak) or None,
        'tasks_closed_no_accept': sorted(closed_tasks) or None,
        'ask_required': bool(closed_tasks) or bool(gate_stop),
        'menu_round_offset': int(pipe.state.data['menu_round_offset']),
        'n_rows': len(rows), 'rounds': rnd + 1,
        'stop_reason': stop_reason,
        'wall_sec': round(time.time() - t0, 1),
        'consumed_prompt_hashes': sorted(pipe.consumed_hashes),
    }
    return {'rows': rows, 'stats': stats,
            'planner_packet': planner_snapshot}
