"""Source revisions, task names, and content normalization helpers."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SOURCE_REVISIONS = {
    'LogLM': '6b563becae259cc7bc1fd3dfaccfe517ae2ca2b1',
    'R-Log': '51d58fa60b39f38bcd8d497a6ddaa899c151127c',
    'RationAnomaly': 'ca217708f6d85ba0f7e4574cb9776dc3d2d483f6',
}

TASKS = ('Parsing', 'AD', 'Interpretation', 'RCA', 'SR')


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def canon(s):
    """Collapse whitespace and lowercase text for content comparison."""
    return re.sub(r'\s+', ' ', (s or '')).strip().lower()


def pair_key(rec):
    return (canon(rec.get('instruction', '')), canon(rec.get('input', '')))


def input_key(rec):
    return canon(rec.get('input', ''))


def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
