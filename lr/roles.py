"""Role prompt composition, response validation, and dispatch.

Each request combines shared.md and the selected role prompt as its system
message, with a JSON context packet as its user message. Prompt checksums
are checked against the manifest before sending. Responses are parsed and
validated against the role schema."""
import hashlib
import json
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent / 'prompts' / 'v1'


def messages(role, packet):
    if role not in ('planner', 'generalizer', 'inspector', 'optimizer'):
        raise ValueError('unknown role')
    system = (PROMPTS / 'shared.md').read_text(encoding='utf-8') + \
        '\n\n' + (PROMPTS / (role + '.md')).read_text(encoding='utf-8')
    return [{'role': 'system', 'content': system},
            {'role': 'user',
             'content': json.dumps(packet, ensure_ascii=False)}]


TASKS = ('Parsing', 'AD', 'Interpretation', 'RCA', 'SR')
QUALITY_CHECKS = ('task_validity', 'correctness', 'grounding',
                  'completeness', 'usability')
TRISTATE = ('pass', 'fail', 'unknown')
_SEVERITY = ('minor', 'major', 'critical')
_REPAIRABILITY = ('repairable', 'needs_evidence', 'unrepairable')
_INPUT_MODES = ('log_only', 'evidence_augmented')
_NOVELTY = ('paraphrase', 'parameter', 'task_projection', 'scenario')


def manifest_hashes():
    return json.loads((PROMPTS / 'manifest.json')
                      .read_text(encoding='utf-8'))['files_sha256']


def consumed_hashes(role):
    """Return checksums for the shared and selected role prompt files."""
    names = ['shared.md', role + '.md']
    return {n: hashlib.sha256((PROMPTS / n).read_bytes()).hexdigest()
            for n in names}


class RoleContractError(ValueError):
    pass


def _need(cond, msg):
    if not cond:
        raise RoleContractError(msg)


def _str(v, field):
    _need(isinstance(v, str) and v.strip(), '%s must be non-empty string' % field)


def dispatch(role, packet, client, purpose, max_tokens=None, pin=None):
    """Verify prompts, send the role request, and validate the response.

    Return the parsed response and request receipt."""
    _need(role in ('planner', 'generalizer', 'inspector', 'optimizer'),
          'dispatch covers the four synthesis roles only')
    msgs = messages(role, packet)
    consumed = consumed_hashes(role)
    pinned = pin or manifest_hashes()
    for name, digest in consumed.items():
        _need(pinned.get(name) == digest,
              'prompt drift: %s consumed %s != pinned %s'
              % (name, digest[:12], str(pinned.get(name))[:12]))
    outcome = client.chat(msgs, purpose, max_tokens=max_tokens, json_mode=True)
    try:
        result = json.loads(outcome['content'])
    except (TypeError, ValueError):
        raise RoleContractError('role %s returned malformed JSON' % role)
    validate_role_v1(role, result, packet)
    return result, {'request_id': outcome['request_id'],
                    'attempts': outcome['attempts'], 'usage': outcome['usage'],
                    'consumed_hashes': consumed}


def _v_planner(r, packet):
    from .tools import OPERATORS
    _need(isinstance(r.get('plan_items'), list)
          and isinstance(r.get('unfilled_cells'), list),
          'planner: plan_items/unfilled_cells must be lists')
    allowed = set(packet.get('available_seed_ids', ()))
    menu = packet.get('seed_task_menu') or {}
    for cell in menu.values():
        ids = cell.get('record_ids') if isinstance(cell, dict) else cell
        allowed |= set(ids or ())
    catalog = set(packet.get('operator_catalog', ()))
    _need(catalog <= set(OPERATORS), 'planner: unpinned operator catalog')
    for it in r['plan_items']:
        _need(isinstance(it, dict), 'planner: plan item must be object')
        for k in ('plan_id', 'target_task', 'operator'):
            _str(it.get(k), 'planner.%s' % k)
        _need(it['target_task'] in TASKS, 'planner: unknown task enum')
        _need(it['operator'] in catalog, 'planner: operator outside frozen catalog')
        ids = it.get('seed_ids')
        _need(isinstance(ids, list) and ids
              and all(isinstance(s, str) for s in ids),
              'planner: seed_ids must be a non-empty string list')
        _need(set(ids) <= allowed, 'planner: invented/unavailable seed id')
        ev = it.get('evidence_ids', [])
        _need(isinstance(ev, list) and all(isinstance(s, str) for s in ev),
              'planner: evidence_ids must be strings')
        _need(isinstance(it.get('invariants'), list), 'planner: invariants list')
        rc = it.get('requested_count')
        _need(type(rc) is int and 1 <= rc <= 8, 'planner: requested_count int 1..8')
        ab = it.get('attempt_budget')
        _need(type(ab) is int and 1 <= ab <= 4, 'planner: attempt_budget int 1..4')
        cell = it.get('target_cell')
        _need(isinstance(cell, dict) and isinstance(cell.get('source'), str)
              and cell['source'].strip(), 'planner: target_cell needs source')


def _v_generalizer(r, packet):
    _need(r.get('status') in ('candidate', 'needs_evidence'),
          'generalizer: unknown status')
    if r['status'] == 'needs_evidence':
        _need(r.get('candidate') is None
              and isinstance(r.get('missing_requirements'), list)
              and r['missing_requirements'],
              'generalizer: needs_evidence needs candidate=null plus exact '
              'missing_requirements')
        return
    c = r.get('candidate')
    _need(isinstance(c, dict), 'generalizer: candidate object required')
    for k in ('sample_id', 'parent_seed_id', 'source_id', 'source_revision',
              'instruction', 'input', 'output', 'task',
              'generation_operator', 'source_label_version', 'input_mode'):
        _str(c.get(k), 'candidate.%s' % k)
    _need(c['task'] in TASKS, 'candidate: unknown task enum')
    _need(c['input_mode'] in _INPUT_MODES, 'candidate: invalid input_mode')
    _need(c['reasoning'] is None or isinstance(c['reasoning'], str),
          'candidate.reasoning must be null or string')
    _need(isinstance(c.get('evidence_refs'), list), 'candidate.evidence_refs list')
    _need(isinstance(c.get('scenario_tags'), list), 'candidate.scenario_tags list')
    _str(c.get('difficulty'), 'candidate.difficulty')
    der = c.get('label_derivation')
    if der is not None:
        _need(isinstance(der, dict)
              and isinstance(der.get('rule'), str) and der['rule'].strip()
              and isinstance(der.get('evidence_ids'), list)
              and all(isinstance(x, str) for x in der['evidence_ids']),
              'candidate.label_derivation needs rule + evidence_ids')
    _need(r.get('novelty_kind') in _NOVELTY, 'generalizer: invalid novelty_kind')
    _need(isinstance(r.get('preserved_invariants'), list),
          'generalizer: preserved_invariants list')
    seeds = packet.get('seeds_by_id') or {}
    pids = c.get('source_record_ids') or [c['parent_seed_id']]
    _need(isinstance(pids, list) and pids, 'candidate: source_record_ids required')
    for pid in pids:
        _need(pid in seeds, 'candidate references unavailable seed %r' % pid)
    _need(seeds[c['parent_seed_id']].get('task') == c['task'],
          'candidate task unsupported by its parent seed')


def _v_inspector(r, packet):
    ch = r.get('checks')
    _need(isinstance(ch, dict) and set(ch) == set(QUALITY_CHECKS + ('novelty',)),
          'inspector: checks need exactly the five quality dims plus novelty')
    for k, v in ch.items():
        _need(v in TRISTATE, 'inspector: %s must be pass|fail|unknown (string '
              'false is not a bool tri-state)' % k)
    route = r.get('route')
    _need(route in ('accept', 'repair', 'regeneralize', 'quarantine'),
          'inspector: unknown route enum')
    _str(r.get('route_reason'), 'inspector.route_reason')
    defects = r.get('defects')
    _need(isinstance(defects, list), 'inspector.defects must be a list')
    ids = set()
    for d in defects:
        _need(isinstance(d, dict), 'inspector: defect must be object')
        for k in ('defect_id', 'code', 'location', 'description'):
            _str(d.get(k), 'defect.%s' % k)
        _need(d['defect_id'] not in ids, 'inspector: duplicate defect_id')
        ids.add(d['defect_id'])
        _need(d.get('severity') in _SEVERITY, 'inspector: invalid severity')
        _need(d.get('repairability') in _REPAIRABILITY,
              'inspector: invalid repairability')
        for k in ('evidence_ids', 'edit_scope', 'preserve'):
            val = d.get(k)
            _need(isinstance(val, list) and all(isinstance(x, str) for x in val),
                  'inspector: defect.%s must be string list' % k)
    five_pass = all(ch[k] == 'pass' for k in QUALITY_CHECKS)
    confirmed_dup = any(d['code'].startswith('duplicate_')
                        and d['severity'] in ('major', 'critical')
                        for d in defects)
    if route == 'accept':
        _need(five_pass and not confirmed_dup,
              'inspector: accept requires all five quality checks pass and no '
              'confirmed prohibited duplicate')
    elif route == 'repair':
        _need(any(d['repairability'] == 'repairable' for d in defects)
              and ch['task_validity'] != 'fail',
              'inspector: repair needs a bounded repairable defect and a '
              'valid task; wrong-task failures go regeneralize')
    elif route == 'quarantine':
        _need(not five_pass or bool(defects) or not any(
            ch[k] == 'pass' for k in QUALITY_CHECKS),
              'inspector: quarantine without defect/failed check unexplained')


def _v_optimizer(r, packet):
    st = r.get('status')
    _need(st in ('revised', 'needs_evidence', 'unrepairable'),
          'optimizer: unknown status')
    _need(r.get('requested_route') in ('reinspect', 'regeneralize', 'quarantine'),
          'optimizer: invalid requested_route')
    mapping = r.get('defect_to_edit')
    unresolved = r.get('unresolved_defects')
    _need(isinstance(mapping, list) and isinstance(unresolved, list),
          'optimizer: defect_to_edit/unresolved_defects lists required')
    report_ids = {d['defect_id'] for d in
                  (packet.get('inspection_report') or {}).get('defects', [])}
    cand = r.get('revised_candidate')
    if st == 'revised':
        _need(isinstance(cand, dict) and cand,
              'optimizer: revised needs the entire candidate object')
        _need(bool(mapping), 'optimizer: revised without defect-to-edit mapping')
    else:
        _need(cand is None, 'optimizer: non-revised status must carry null candidate')
        _need(bool(unresolved), 'optimizer: failed repair needs explicit '
              'unresolved_defects')
    for m in mapping:
        _need(isinstance(m, dict) and m.get('defect_id') in report_ids,
              'optimizer: edit mapped to an unknown defect id')
        for k in ('fields_changed', 'evidence_ids'):
            val = m.get(k)
            _need(isinstance(val, list) and all(isinstance(x, str) for x in val),
                  'optimizer: %s must be string list' % k)
        for k in ('before', 'after', 'why_resolved'):
            _str(m.get(k), 'optimizer.defect_to_edit.%s' % k)


_VALIDATORS = {'planner': _v_planner, 'generalizer': _v_generalizer,
               'inspector': _v_inspector, 'optimizer': _v_optimizer}


def validate_role_v1(role, result, packet=None):
    """Validate the fields and values of a role response."""
    packet = packet or {}
    _need(isinstance(result, dict), '%s: reply must be a JSON object' % role)
    _need(result.get('version') == 'logrefine-role-v1', '%s: version mismatch' % role)
    _need(result.get('role') == role, '%s: role field mismatch' % role)
    _VALIDATORS[role](result, packet)
    return result
