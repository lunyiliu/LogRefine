"""Group records by parent links and canonical input hashes.

Explicit parent and source-record references connect derived records to
their sources. Shared input hashes connect otherwise unlinked copies.
Group-aware splitting keeps connected records together."""
import json

from .assets import canon
from .asset_audit import input_hash

_CURRENT_VIEW = None


class UnknownParentError(KeyError):
    pass


def set_group_view(view):
    """Install the source-group view for the current seed pool."""
    global _CURRENT_VIEW
    _CURRENT_VIEW = view
    return view


def load_group_view(path=None):
    global _CURRENT_VIEW
    if path is not None:
        with open(path, 'r', encoding='utf-8') as f:
            return set_group_view(json.load(f))
    if _CURRENT_VIEW is None:
        raise RuntimeError('no group view installed: load a seed pool first '
                           '(lr.seed_pool.load_seed_pool) or pass an explicit '
                           'view path')
    return _CURRENT_VIEW


class UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, x):
        self.parent.setdefault(x, x)

    def find(self, x):
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def build_group_index(records):
    """Map record identifiers to groups sharing canonical input hashes."""
    uf = UnionFind()
    by_hash = {}
    for r in records:
        h = input_hash(r['input'])
        uf.add(r['record_id'])
        if h in by_hash:
            uf.union(r['record_id'], by_hash[h])
        else:
            by_hash[h] = r['record_id']
    return {r['record_id']: uf.find(r['record_id']) for r in records}, \
        {h: uf.find(mid) for h, mid in by_hash.items()}


def group_of(record_id, groups=None):
    g = groups if groups is not None else load_group_view()['record_to_group']
    return g.get(record_id)


def same_parent(a, b, groups=None):
    g = groups if groups is not None else load_group_view()['record_to_group']
    return g.get(a) is not None and g.get(a) == g.get(b)


def split_by_group(records, frac_train, seed=0, groups=None):
    """Split records while keeping each connected source group together."""
    gmap = groups if groups is not None else load_group_view()['record_to_group']
    comp = {}
    for r in records:
        comp.setdefault(gmap[r['record_id']], []).append(r)
    keys = sorted(comp)
    total = sum(len(v) for v in comp.values())
    train, dev, n = [], [], 0
    for k in keys:
        if n < frac_train * total:
            train.extend(comp[k])
            n += len(comp[k])
        else:
            dev.extend(comp[k])
    return train, dev


def build_group_index_with_refs(records, parent_refs=None):
    """Build connected source groups from input hashes and parent references.

    A record with multiple parents joins their groups. Unknown parent identifiers
    raise UnknownParentError."""
    uf = UnionFind()
    by_hash = {}
    ids = set()
    for r in records:
        rid = r['record_id']
        ids.add(rid)
        uf.add(rid)
        h = input_hash(r.get('input', ''))
        if h in by_hash:
            uf.union(rid, by_hash[h])
        else:
            by_hash[h] = rid
    refs = dict(parent_refs or {})
    for r in records:
        for key in ('source_record_ids', 'parent_record_id'):
            val = r.get(key)
            if val is None:
                continue
            kids = refs.setdefault(r['record_id'], [])
            kids.extend([val] if isinstance(val, str) else list(val))
    for child, parents in refs.items():
        if not isinstance(parents, (list, tuple, set)):
            parents = [parents]
        for p in parents:
            if p is None:
                raise UnknownParentError('%s: null parent reference' % child)
            if p not in uf.parent:
                raise UnknownParentError(
                    '%s references unknown parent %s' % (child, p))
            uf.union(child, p)
    return {r['record_id']: uf.find(r['record_id']) for r in records}, uf


def descendants(group_id, records, groups):
    """Return record identifiers connected through parent or shared-input links."""
    return sorted(r['record_id'] for r in records
                  if groups.get(r['record_id'], r['record_id']) == group_id)
