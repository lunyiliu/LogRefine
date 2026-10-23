"""Load, deduplicate, and group seed records.

Each unique task/instruction/input combination has a canonical seed.
Duplicate record identifiers remain available as aliases. Optional input
hashes exclude designated records from the seed pool."""
import json
from pathlib import Path

from .assets import canon, pair_key
from .asset_audit import input_hash
from . import source_groups

TASKS = ('Parsing', 'AD', 'Interpretation', 'RCA', 'SR')


class TestAccessError(RuntimeError):
    pass


def load_forbidden(path):
    if path is None:
        return set()
    p = Path(path)
    if p.is_symlink():
        raise TestAccessError('symlink entry rejected by generator guard: %s'
                              % p)
    data = json.loads(p.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        return set(data['forbidden_input_sha256'])
    return set(data)


def _required_seed(s, ln):
    if s.get('exclusion_reason') or s.get('pair_quarantine_reason'):
        return None
    for k in ('task', 'instruction', 'input', 'label'):
        v = s.get(k)
        if not isinstance(v, str) or not v.strip():
            raise TestAccessError('seed line %d: %s must be a non-empty '
                                  'string (train schema is frozen)' % (ln, k))
    if s['task'] not in TASKS:
        raise TestAccessError('seed line %d: unknown task %r' % (ln, s['task']))
    return s


def build_group_view(records, alias_links):
    """Build and install source groups for the loaded seed pool."""
    rows = [{'record_id': r['record_id'], 'input': r.get('input', '')}
            for r in records]
    refs = {}
    for alias, canonical in alias_links:
        refs.setdefault(canonical, []).append(alias)
    gidx, _ = source_groups.build_group_index_with_refs(rows, refs)
    view = {'group_version': 'seed-pool-groups-v1',
            'record_to_group': gidx,
            'canonical_alias_links': [list(l) for l in alias_links],
            'identity_evidence': 'canonical input hash; record_id alone is '
                                 'not a source',
            'source_event_id_available': False,
            'total_records': len(rows),
            'groups_total': len(set(gidx.values()))}
    return source_groups.set_group_view(view)


def load_seed_pool(seeds_path, forbidden_hashes=None):
    """Return deduplicated seeds and their source-group view."""
    forbidden_in = load_forbidden(forbidden_hashes)
    seen_pair, order, by_id = {}, [], {}
    with open(seeds_path, 'r', encoding='utf-8') as f:
        for ln, line in enumerate(f, 1):
            if not line.strip():
                continue
            s = json.loads(line)
            if _required_seed(s, ln) is None:
                continue
            rid = s.get('record_id')
            if not rid:
                raise TestAccessError('seed line %d: record_id required' % ln)
            if rid in by_id:
                raise TestAccessError('duplicate record_id %s in seed pool'
                                      % rid)
            pk = (s['task'],) + pair_key(s)
            if input_hash(s['input']) in forbidden_in:
                raise TestAccessError('forbidden input hash present in pool '
                                      'record %s (train/test leakage guard)'
                                      % rid)
            s.setdefault('source', rid.split('-', 1)[0])
            s.setdefault('reasoning', None)
            s.setdefault('evidence_packet', s.get('evidence'))
            s.setdefault('ad_view', None)
            by_id[rid] = s
            if pk not in seen_pair:
                seen_pair[pk] = rid
                order.append((pk, rid))
            else:
                canon_id = seen_pair[pk]
                alias = by_id[canon_id].setdefault('pair_alias_record_ids',
                                                   [])
                if rid not in alias:
                    alias.append(rid)
    seeds = []
    alias_links = []
    for pk, rid in order:
        s = by_id[rid]
        for al in s.get('pair_alias_record_ids', []):
            alias_links.append([al, rid])
            if al in by_id and by_id[al] is not s:
                by_id[al]['exclusion_reason'] = 'pair_dedup_alias'
        seed = dict(s)
        seed['record_id'] = rid
        seed['canonical_pair_record_id'] = rid
        seeds.append(seed)
    view = build_group_view(list(by_id.values()), alias_links)
    for s in seeds:
        s['group_id'] = view['record_to_group'][s['record_id']]
    for r in by_id.values():
        if r.setdefault('pair_alias_record_ids', None):
            r['canonical_pair_record_id'] = next(
                c for a, c in alias_links if a == r['record_id']) \
                if r['record_id'] in [a for a, _c in alias_links] else \
                r['record_id']
    return seeds, view


def _guard_seed_views(path):
    """Reject seed paths marked as test data."""
    raw = Path(path)
    if raw.is_symlink():
        raise TestAccessError('symlink entry rejected by generator guard: %s'
                              % raw)
    if 'test' in raw.name.lower():
        raise TestAccessError('generator-side code must not read test assets: '
                              '%s' % raw.name)
    return str(raw.resolve())


def select_seeds(pool, task, limit):
    """Select a bounded number of seeds per task in source-group order."""
    groups, order = {}, []
    for s in pool:
        if s['task'] != task:
            continue
        if s['group_id'] not in groups:
            groups[s['group_id']] = []
            order.append(s['group_id'])
        groups[s['group_id']].append(s)
    out = []
    for gid in order:
        if len(out) >= limit:
            break
        out.extend(groups[gid][:limit - len(out)])
    return out
