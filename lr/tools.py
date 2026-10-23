"""Task constraints, expansion operators, source checks, and coverage tools."""
import re

from .asset_audit import input_hash
from .assets import SOURCE_REVISIONS, TASKS
from .source_groups import UnknownParentError, load_group_view


QUOTA_TOTAL = 100
TASK_TARGETS = {t: QUOTA_TOTAL // len(TASKS) for t in TASKS}


OPERATOR_DEFINITIONS = {
    'question_constraint_expansion':
        'SAME log/input; add or tighten the question constraints the '
        'answer must satisfy. Input stays the seed log.',
    'evidence_grounded_depth':
        'SAME log/input; deepen the analysis/answer grounded in this '
        'log only. Input stays the seed log (input-preserving operator).',
    'cross_task_projection':
        'Project the seed scenario onto a different task family over a '
        'NEW derived log; the new input must contain what its target '
        'asserts.',
    'rule_verified_parameter_transform':
        'Transform declared parameters (ids, hosts, paths, versions, '
        'counts) consistently through the WHOLE log and answer; every '
        'occurrence must move together. Input changes. '
        'The candidate must carry a '
        'COMPLETE label_derivation object or the schema validator '
        'refuses it: {"rule": "consistent_parameter_transform", '
        '"substitutions": [["<the exact parameter token copied from the '
        'seed input>", "<its replacement>"]], "evidence_ids": '
        '["<a canonical parent ID from plan_item.seed_ids / '
        'evidence_ids>"]}. '
        'PARAMETER FIELD restriction: substitute ONLY instance-level '
        'identifiers and quantities (request ids, block ids, pids, IPs, '
        'ports, byte counts, timestamps, file paths as instance names, '
        'hostnames as instance names). FORBIDDEN substitutions: the '
        'software product (Apache/Nginx/Hadoop/Spark/PHP/...), error '
        'actions or codes, anomaly conditions, event mechanisms, log '
        'formats. CAUTION: ports/counts/timestamps/paths are not '
        'automatically label-safe - the transform is valid ONLY when it '
        "does not change THIS record's anomaly condition, suggested fix "
        'or root cause; if the label would move, keep the original value '
        'or switch to a seed whose transform is label-safe. The input '
        'AND the output transform with the SAME map; the checker '
        're-executes the map over the parent log and parent answer and '
        'refuses any mismatch or leftover.',
    'supported_scenario_expansion':
        'Write a genuinely NEW log scenario (different event sequence, '
        'actors, failure mechanism) supported by the same task contract; '
        'derive instruction+input+output self-consistently. Input MUST '
        'be a new log, not the seed log.',
}
INPUT_CHANGING_OPS = ('rule_verified_parameter_transform',
                      'supported_scenario_expansion',
                      'cross_task_projection')
OPERATOR_INVARIANTS = {
    'question_constraint_expansion': [
        'parent log input kept byte-identical',
        'trusted gold kept byte-identical',
        'change ONLY the explicit question/constraints'],
    'evidence_grounded_depth': [
        'parent log input kept byte-identical',
        'deepen the answer grounded ONLY in this log'],
    'rule_verified_parameter_transform': [
        'transform the declared parameter consistently through the WHOLE '
        'log AND the whole answer',
        'the candidate MUST carry the COMPLETE label_derivation object: '
        '{"rule": "consistent_parameter_transform", "substitutions": '
        '[["<exact parameter token from the seed input>", "<its '
        'replacement>"]], "evidence_ids": ["<a canonical parent ID '
        'from plan_item.seed_ids / evidence_ids>"]} - a partial object '
        '(missing rule or evidence_ids) fails the schema validator',
        'fails the schema validator',
        'parameter fields = instance-level identifiers and quantities '
        'ONLY (request ids, block ids, pids, IPs, ports, byte counts, '
        'timestamps, paths/hostnames as instance names); the software '
        'product, error actions/codes, anomaly conditions and event '
        'mechanisms are FORBIDDEN substitution sources',
        'the input AND the output transform with the SAME map; no other '
        'content change; the hard check re-executes the map mechanically '
        'and refuses any mismatch or leftover old token'],
    'supported_scenario_expansion': [
        'write a genuinely NEW log scenario (new event sequence/actors/'
        'mechanism); the input MUST be a new log, not the seed log',
        'derive instruction+input+output self-consistently from the new '
        'scenario; do not inherit the parent answer',
        'the new input must contain what the target asserts'],
    'cross_task_projection': [
        'project the seed scenario onto the target task family over a '
        'NEW derived log',
        'the new input must contain what the target asserts'],
}
OPERATORS = ('question_constraint_expansion', 'evidence_grounded_depth',
             'cross_task_projection', 'rule_verified_parameter_transform',
             'supported_scenario_expansion')
DIFFICULTY_LEVELS = ('basic', 'intermediate', 'advanced')
SOURCE_NAMES = tuple(SOURCE_REVISIONS)


def _trusted_output_values(parents):
    import re as _re

    def toks(t):
        return set(_re.findall(r'[0-9a-z]+', (t or '').lower()))
    golds = [((pr.get('ad_view') or {}).get('gold') or pr.get('label'))
             for pr in parents]


    if any(len(_norm(g)) <= 24 for g in golds):
        return {_norm(g) for g in golds}, 'exact'
    union = set().union(*[toks(g) for g in golds]) if golds else set()
    return union, 'tokens'


def _tokenize(t):
    import re as _re
    return set(_re.findall(r'[0-9a-z]+', (t or '').lower()))


def _rule_identity_transform(candidate, parents):
    """Check a candidate against parent answers and recorded evidence.

    Short labels require an exact match. Longer answers use token containment.
    Input changes must be covered by the parent answer or recorded evidence."""
    ins = [_norm(pr.get('input')) for pr in parents]
    cin = _norm(candidate.get('input'))
    trusted, mode = _trusted_output_values(parents)
    out = _norm(candidate.get('output'))
    if mode == 'exact':
        ok_out = bool(out) and out in trusted
    else:
        ok_out = bool(_tokenize(out)) and _tokenize(out) <= trusted
    if not ok_out:
        return {'applicable': False,
                'reason': 'invented_output_not_a_trusted_lineage_value'}
    if cin in ins:
        return {'applicable': True, 'reason': None}


    if mode == 'exact':
        return {'applicable': False,
                'reason': 'changed_event_conditions_unsupported_by_exact_'
                          'trusted_label'}
    import json as _json
    base = set()
    for pr in parents:
        gold = ((pr.get('ad_view') or {}).get('gold') or pr.get('label'))
        base |= _tokenize(pr.get('input')) | _tokenize(gold)
    grounded = set(base) | _tokenize(candidate.get('output')) | {out}
    der = candidate.get('label_derivation') or {}
    ae = der.get('answer_evidence')
    if isinstance(ae, str):
        grounded |= _tokenize(ae)
    for pr in parents:
        ev = pr.get('evidence_packet') or pr.get('evidence') or []
        items = [ev] if isinstance(ev, str) else list(ev)
        for e in items:
            grounded |= _tokenize(e if isinstance(e, str) else
                                  _json.dumps(e, ensure_ascii=False))
    added = [t for t in _tokenize(cin) if t not in grounded and len(t) > 2]
    if added:
        return {'applicable': False,
                'reason': 'changed_event_conditions_unverified_new_tokens:'
                          + ','.join(sorted(set(added))[:6])}
    return {'applicable': True, 'reason': None}


def _consistent_param_self_test(view):
    """Create a consistent parameter substitution for testing a transformation."""
    import re as _re
    inp = str(view.get('input') or '')
    lab = str(view.get('label') or '')
    toks = _re.findall(r'[A-Za-z0-9_./:-]{4,}', inp)
    _bad = {'error', 'success', 'normal', 'abnormal', 'warning',
            'failed', 'failure', 'ok', 'critical', 'anomaly'}
    for tok in toks:
        if tok.lower() in _bad:
            continue
        new = ('9' * len(tok) if any(c.isdigit() for c in tok)
               else 'ZQX' + tok[::-1])
        cand = {'input': inp.replace(tok, new),
                'output': lab.replace(tok, new) if tok in lab else lab,
                'label_derivation': {
                    'rule': 'consistent_parameter_transform',
                    'substitutions': [[tok, new]]}}


        r = _rule_consistent_parameter_transform(cand, [view])
        if r.get('applicable'):
            return cand
    return {'input': inp, 'output': lab,
            'label_derivation': {'rule': 'consistent_parameter_transform',
                                 'substitutions': []}}


def _rule_consistent_parameter_transform(candidate, parents):
    """Validate a declared parameter substitution in both input and answer.

    Applying the ordered substitution map to a parent must produce the candidate
    input and output exactly, with no remaining substituted tokens."""
    import re as _re


    if len(parents) == 1 and \
            str(candidate.get('input') or '') == str(parents[0].get('input') or '') and \
            str(candidate.get('output') or '') in (str(parents[0].get('output') or ''),
                                                   str(parents[0].get('label') or '')):
        return {'applicable': True, 'reason': 'identity passthrough'}
    der = candidate.get('label_derivation') or {}
    subs = der.get('substitutions')
    if not isinstance(subs, list) or not subs:
        return {'applicable': False,
                'reason': 'no substitution map declared'}


    _LABEL_WORDS = {'error', 'success', 'normal', 'abnormal', 'warning',
                    'failed', 'failure', 'ok', 'critical', 'anomaly'}
    for pair in subs:
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and str(pair[0]).strip().lower() in _LABEL_WORDS):
            return {'applicable': False,
                    'reason': 'substitution source is a label/mechanism '
                              'word: never a valid parameter'}
    if not parents:
        return {'applicable': False, 'reason': 'no resolved parent'}
    def applied(text):
        out = str(text or '')
        for pair in subs:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not isinstance(pair[0], str)
                    or not isinstance(pair[1], str)
                    or pair[0] == pair[1] or pair[0] == ''):
                return None
            if pair[0] not in out:
                return None
            out = out.replace(pair[0], pair[1])
        return out
    for pr in parents:
        want_in = applied(pr.get('input'))
        if want_in is None or want_in != str(candidate.get('input') or ''):
            return {'applicable': False,
                    'reason': 'substitution does not reproduce the '
                              'candidate input from the parent log'}
        cand_out = str(candidate.get('output') or '')
        pr_out = pr.get('output')
        if pr_out is None:
            pr_out = pr.get('label')
        want_out = applied(pr_out)


        if want_out != cand_out and pr_out != cand_out:
            return {'applicable': False,
                    'reason': 'substitution does not reproduce the '
                              'candidate output from the parent answer'}
    return {'applicable': True, 'reason': None}


_rule_consistent_parameter_transform.self_test_candidate = \
    _consistent_param_self_test
REGISTERED_DERIVATION_RULES = {
    'registered_parameter_transform': _rule_identity_transform,
    'consistent_parameter_transform': _rule_consistent_parameter_transform}


def _verify_registered_rules(seeds_by_id):
    """Check registered rules with matching and mismatching examples."""
    results = {}
    for name, fn in REGISTERED_DERIVATION_RULES.items():
        pos = neg = total = 0
        builder = getattr(fn, 'self_test_candidate', None)
        for seed in seeds_by_id.values():
            total += 1
            view = clean_seed_view(seed)
            if builder is not None:
                good = fn(builder(view), [seed])
            else:
                good = fn(dict(view, output=view['label']), [seed])
            bad = fn(dict(view, input=view['input'] + ' ERR fabricated tail',
                          output='absolutely invented target'), [seed])


            bad2 = fn(dict(view, output='absolutely invented target'), [seed])
            pos += bool(good['applicable'])
            neg += (not bad['applicable']) and (not bad2['applicable'])
        results[name] = {'verified': bool(total and pos == total
                                          and neg == total),
                         'executed_over_seeds': total,
                         'passed_positive': pos, 'rejected_negative': neg,
                         'claim': 'deterministic rule re-executed per seed; '
                                  'only an actually-applicable transform can '
                                  'authorize a label'}
    return results


_RULE_EXECUTION = {}


def derivation_rule_status(rule, seeds_by_id=None):
    seeds = seeds_by_id or {}
    if seeds:
        _RULE_EXECUTION.update(_verify_registered_rules(seeds))
    return _RULE_EXECUTION.get(rule)


def apply_registered_rule(rule, candidate, parents, seeds_by_id=None):
    """Apply a verified transformation rule to a candidate and its parents."""
    fn = REGISTERED_DERIVATION_RULES.get(rule)
    if fn is None:
        return {'applicable': False,
                'reason': 'unregistered_deterministic_rule'}
    status = derivation_rule_status(rule, seeds_by_id or {})
    if status is None or not status.get('verified'):
        return {'applicable': False,
                'reason': 'rule_registration_not_executed_and_verified'}
    out = dict(fn(candidate, parents))
    out.setdefault('reason', None)
    return out

_RE_LEAK = re.compile(r'redacted|audit_raw|\[\s*REDACTED\s*\]', re.IGNORECASE)


def inventory(seeds):
    """Summarize available seeds by task and source."""
    counts = {t: sum(1 for s in seeds if s['task'] == t) for t in TASKS}
    return {'canonical_seed_counts': counts, 'total': len(seeds),
            'source_revisions': dict(SOURCE_REVISIONS),
            'group_view_version': load_group_view()['group_version']}


def coverage_stats(accepted=None, targets=None):
    accepted = accepted or {}
    targets = targets or TASK_TARGETS
    return {'targets': dict(targets),
            'accepted_counts': {t: len(accepted.get(t, [])) for t in targets},
            'deficits': {t: max(0, targets[t] - len(accepted.get(t, [])))
                         for t in targets}}


def seed_search(seeds, task, limit=8):
    from .seed_pool import select_seeds
    return [{'record_id': s['record_id'], 'task': s['task'],
             'group_id': s['group_id'], 'input_sha256': input_hash(s['input']),
             'ad_domain': (s.get('ad_view') or {}).get('domain_exact')}
            for s in select_seeds(seeds, task, limit)]


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').lower().strip())


def near_duplicate_search(candidate, pool_records, jaccard_threshold=0.8, k=8):
    """Find exact instruction/input matches and surface-similar neighbors.

    Surface similarity provides context for the Inspector; exact matches
    identify duplicate pairs."""
    sig = (_norm(candidate.get('instruction')), _norm(candidate.get('input')))
    shingle = lambda t: {t[i:i + k] for i in range(max(1, len(t) - k + 1))}
    sh = shingle(sig[0] + ' ' + sig[1])
    hits = []
    for rec in pool_records:
        rsig = (_norm(rec.get('instruction')), _norm(rec.get('input')))
        if rsig == sig:
            hits.append({'record_id': rec.get('record_id'), 'kind': 'exact_pair'})
            continue
        rsh = shingle(rsig[0] + ' ' + rsig[1])
        if len(sh) >= 12 and len(rsh) >= 12:
            inter = len(sh & rsh)
            jac = inter / (len(sh) + len(rsh) - inter) if inter else 0.0
            if jac >= jaccard_threshold:
                hits.append({'record_id': rec.get('record_id'),
                             'kind': 'near_surface', 'jaccard': round(jac, 3),
                             'advisory_only_for_inspector': True})
    confirmed = any(h['kind'] == 'exact_pair' for h in hits) \
        and bool(pool_records)
    return {'threshold': jaccard_threshold, 'pool_size': len(pool_records or []),
            'confirmed_prohibited': bool(confirmed),
            'note': 'exact pair only is a confirmed duplicate; Jaccard '
                    'near_surface is an unconfirmed surface heuristic',
            'hits': hits[:5]}


def export_input_norm(s):
    """Strip surrounding whitespace and collapse spaces and tabs.

    Preserve letter case and line breaks."""
    t = ' '.join(str(s or '').split(' '))
    t = re.sub(r'[ \t]{2,}', ' ', t).strip(' \t')
    return t


def accepted_pool_confirms_duplicate(candidate, accepted_records):
    """Check accepted examples for matching instruction/input pairs or task/input keys."""
    sig_a = (_norm(candidate.get('instruction')),
             _norm(candidate.get('input')))
    tin_a = (candidate.get('task'), export_input_norm(candidate.get('input')))
    for rec in list(accepted_records or []):
        if sig_a == (_norm(rec.get('instruction')),
                     _norm(rec.get('input'))):
            return True
        if tin_a == (rec.get('task'),
                     export_input_norm(rec.get('input'))):
            return True
    return False


def usable_accepted_counts(records, accepted_by_task=None):
    """Count distinct task/input keys among accepted examples."""
    keep = None
    if accepted_by_task:
        keep = {sid for ids in accepted_by_task.values() for sid in ids}
    seen, out = set(), {}
    for rec in list(records or []):
        if keep is not None and rec.get('sample_id') not in keep:
            continue
        t = rec.get('task')
        k = (t, export_input_norm(rec.get('input')))
        if k in seen:
            continue
        seen.add(k)
        out[t] = out.get(t, 0) + 1
    return out


def task_contract_check(candidate):
    """Check task values, training fields, and separation of review metadata."""
    errs = []
    if candidate.get('task') not in TASKS:
        errs.append('unknown_task')
    for k in ('instruction', 'input', 'output'):
        v = candidate.get(k)
        if not isinstance(v, str) or not v.strip():
            errs.append('missing_%s' % k)
    if candidate.get('input_mode') not in ('log_only', 'evidence_augmented'):
        errs.append('invalid_input_mode')
    text = ' '.join(str(candidate.get(k) or '')
                    for k in ('instruction', 'input', 'reasoning', 'output'))
    if _RE_LEAK.search(text):
        errs.append('audit_or_review_material_in_student_target')
    if re.search(r'\b(self[_ -]?eval|acceptance label|quality score)\b',
                 text.lower()):
        errs.append('quality_label_in_student_target')
    return {'valid': not errs, 'errors': errs,
            'note': 'format validity is not semantic correctness'}


def alias_to_record(group_view, record_id):
    """Resolve a canonical or alias identifier in the current source-group view."""
    if record_id in group_view['record_to_group']:
        return record_id
    for alias, canonical in (group_view.get('canonical_alias_links') or []):
        if record_id == alias:
            return canonical
    return None


def seed_parent_refs(seed):
    """Return a seed identifier and its duplicate aliases."""
    return [seed['record_id']] + list(seed.get('pair_alias_record_ids', []))


_SOURCE_PREFIX_OF = {'R-Log': 'rlog', 'LogLM': 'loglm',
                     'RationAnomaly': 'rationanomaly'}
_SEED_SOURCE_PREFIXES = {'rlog': 'R-Log', 'loglm': 'LogLM',
                         'rationanomaly': 'RationAnomaly'}


def seed_source_name(seed):
    """Read source identity from seed metadata or its record identifier."""
    src = seed.get('source')
    rid = seed.get('record_id') or ''
    prefix = rid.split('-', 1)[0].lower()
    mapped = _SEED_SOURCE_PREFIXES.get(prefix)
    if src is not None:
        if mapped is not None and str(src).lower() != prefix:
            raise ValueError('seed %s: source %r contradicts its record-id '
                             'prefix' % (rid[:40], src))
        return src
    if mapped is None:
        raise ValueError('unknown seed source for record %r' % rid[:40])
    return mapped


def actual_cross_task_projection(candidate, seeds_by_id, gview):
    """Check that both tasks have seed records in the same source group."""
    parent = seeds_by_id.get(alias_to_record(gview, candidate['parent_seed_id'])
                              or candidate['parent_seed_id'])
    if parent is None:
        return {'ok': False, 'reason': 'unknown_parent'}
    gid = gview['record_to_group'].get(parent['record_id'])
    task = candidate.get('task')
    has_target = any(seeds_by_id[r]['task'] == task
                     for r, g in gview['record_to_group'].items()
                     if g == gid and r in seeds_by_id)
    return {'ok': bool(has_target),
            'route_supported': bool(has_target),
            'reason': None if has_target else
            'no_real_same_group_target_task_seed: pending end-to-end support'}


def source_label_check(candidate, seeds_by_id, groups=None):
    """Check parent identity and reference-answer constraints for a candidate."""
    gview = groups or load_group_view()
    errs = []
    raw_pids = (candidate.get('source_record_ids')
                or [candidate.get('parent_seed_id')])


    pids = []
    for pid in raw_pids:
        canon = alias_to_record(gview, pid)
        if canon is not None:
            pids.append(canon)
        else:
            raise UnknownParentError(
                'candidate %s references unknown parent %s'
                % (candidate.get('sample_id'), pid))
    if not pids:
        errs.append('no_parent_reference')
    resolved = []
    for seed in seeds_by_id.values():
        for pid in seed_parent_refs(seed):
            if alias_to_record(gview, pid) is None:
                raise UnknownParentError(
                    'seed %s carries unknown parent %s'
                    % (seed['record_id'], pid))
    for pid in pids:
        real = alias_to_record(gview, pid)
        if real is None:
            raise UnknownParentError('candidate %s references unknown parent %s'
                                     % (candidate.get('sample_id'), pid))
        resolved.append(real)
        seed = seeds_by_id.get(real)
        if seed is None:
            raise UnknownParentError('parent %s not in available train seeds' % real)
    parents = [seeds_by_id[r] for r in resolved]
    parent = parents[0]
    if parent['task'] != candidate.get('task'):
        if candidate.get('generation_operator') == 'cross_task_projection':
            proj = actual_cross_task_projection(candidate, seeds_by_id, gview)
            if not proj['ok']:
                errs.append('label_derivation_needs_evidence:cross_task_'
                            'projection_pending_%s' % proj['reason'])
        else:
            errs.append('parent_task_mismatch')
    parent_rev = clean_seed_view(parent).get('source_revision')
    if candidate.get('source_revision') != parent_rev:
        errs.append('unpinned_source_revision')

    if candidate.get('source_label_version') != parent.get('label_version',
                                                           'train-view-v1'):
        errs.append('source_label_version_drift')
    same_input = _norm(parent.get('input')) == _norm(candidate.get('input'))
    if not same_input:


        der = candidate.get('label_derivation')
        if not isinstance(der, dict) or not der.get('rule'):
            errs.append('changed_event_conditions_require_explicit_label_derivation')
        else:
            rule_status = derivation_rule_status(der['rule'], seeds_by_id)
            if rule_status is None or not rule_status.get('verified'):


                errs.append('label_derivation_needs_evidence:unregistered_'
                            'deterministic_rule')
            else:
                applied = apply_registered_rule(der['rule'], candidate,
                                                parents, seeds_by_id)
                if not applied['applicable']:


                    errs.append('label_derivation_needs_evidence:%s'
                                % applied['reason'])
            refs = der.get('evidence_ids') or []


            allowed_ids = {seeds_by_id[r]['record_id'] for r in resolved
                           if r in seeds_by_id}
            if not refs:
                errs.append('label_derivation_needs_evidence:no_evidence_ids')
            elif not set(refs) <= allowed_ids:
                errs.append('label_derivation_evidence_unavailable')
            if der.get('parent_label_inherited') is True:
                errs.append('label_derivation_is_bare_parent_inheritance')
    if same_input and candidate.get('task') == 'AD':
        gold = (parent.get('ad_view') or {}).get('gold') or parent.get('label')
        if _norm(candidate.get('output')) != _norm(gold):
            errs.append('trusted_label_changed_without_derivation')
    return {'ok': not errs, 'errors': errs,
            'group_ids': sorted({gview['record_to_group'][r] for r in resolved}),
            'note': 'mechanical lineage/label consistency, not semantic truth'}


def failure_summary(failures, limit=20):
    """Summarize inspection routes, defect counts, and recent cases for the Planner."""
    by = {}
    for f in failures:
        key = '%s:%s' % (f.get('route', f.get('kind', 'error')),
                         f.get('code', ''))
        by[key] = by.get(key, 0) + 1
    return {'counts': by, 'recent': failures[-limit:],
            'total_failures': len(failures)}


def packet_for(role, **kw):
    """Build a JSON role context from supplied fields, omitting absent values."""
    base = {'role': role}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def clean_seed_view(seed):
    """Select instruction, log input, reference answer, and seed metadata for agents."""
    src = seed_source_name(seed)
    return {'record_id': seed['record_id'], 'task': seed['task'],
            'group_id': seed['group_id'], 'instruction': seed['instruction'],
            'input': seed['input'], 'label': seed['label'],
            'label_version': seed.get('label_version', 'train-view-v1'),
            'source_revision': seed.get('source_revision')
            or SOURCE_REVISIONS.get(src) or 'unpinned-%s' % src}
