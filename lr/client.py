"""OpenAI-compatible chat client with connection pooling and bounded retries.

Configuration uses LOGREFINE_API_BASE, LOGREFINE_API_KEY, and LOGREFINE_MODEL.
Optional settings control temperature (default 0.7), timeout (120 seconds),
concurrency (up to 20), and request logging. Authentication values are
removed from error messages and request logs."""
import hashlib
import json
import os
import re
import threading
import time
import uuid
from urllib import parse

import requests

DEFAULT_MODEL = None
DEFAULT_BASE = None
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
VALID_FINISH_REASONS = ('stop', 'length', 'tool_calls', None)
MAX_CONCURRENCY_CAP = 20

_SENTINEL = re.compile(
    r'(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]{6,}|'
    r'"api[_-]?key"\s*:\s*"[^"]+")', re.IGNORECASE)
REDACTION = '[REDACTED]'


def _redact(obj):
    if isinstance(obj, str):
        return _SENTINEL.sub(REDACTION, obj)
    if isinstance(obj, dict):
        return {k: _redact(_redact(v) if isinstance(v, str) else v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


class APIError(RuntimeError):
    def __init__(self, code, message):
        super().__init__('%s: %s' % (code, message))
        self.code = code
        self.message = message


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


_lock = threading.Lock()
_SEM = None


def _gate():
    global _SEM
    with _lock:
        if _SEM is None:
            n = min(_env_int('LOGREFINE_MAX_CONCURRENCY', MAX_CONCURRENCY_CAP),
                    MAX_CONCURRENCY_CAP)
            _SEM = threading.BoundedSemaphore(max(1, n))
        return _SEM


_POOL = None
_GATE_STATS_LOCK = threading.Lock()
_GATE_STATS = {'active': 0, 'peak': 0, 'calls': 0, 'http_attempts': 0,
               'http_4xx': 0, 'http_5xx': 0, 'network_errors': 0,
               'prompt_tokens': 0, 'completion_tokens': 0,
               'total_tokens': 0}


def gate_stats():
    with _GATE_STATS_LOCK:
        return dict(_GATE_STATS)


def _note_http(status):
    with _GATE_STATS_LOCK:
        _GATE_STATS['http_attempts'] += 1
        if status is not None and 400 <= status < 500:
            _GATE_STATS['http_4xx'] += 1
        elif status is not None and 500 <= status < 600:
            _GATE_STATS['http_5xx'] += 1


def _get_pool():
    global _POOL
    with _lock:
        if _POOL is None:
            s = requests.Session()
            limit = min(_env_int('LOGREFINE_MAX_CONCURRENCY',
                                 MAX_CONCURRENCY_CAP), MAX_CONCURRENCY_CAP)
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=limit, pool_maxsize=limit,
                max_retries=0)
            s.mount('http://', adapter)
            s.mount('https://', adapter)
            _POOL = s
    return _POOL


class ChatClient:
    """OpenAI-compatible endpoint client with a shared connection pool."""

    def __init__(self, base_url=None, model=None, api_key=None,
                 temperature=None, timeout=None, max_retries=None,
                 verify=True):
        self.base_url = (base_url if base_url is not None
                         else os.environ.get('LOGREFINE_API_BASE', DEFAULT_BASE))
        self.model = model or os.environ.get('LOGREFINE_MODEL') or DEFAULT_MODEL
        self.api_key = (api_key if api_key is not None
                        else os.environ.get('LOGREFINE_API_KEY'))
        self.temperature = float(temperature if temperature is not None else
                                 os.environ.get('LOGREFINE_TEMPERATURE',
                                                DEFAULT_TEMPERATURE))
        self.timeout = timeout or float(os.environ.get('LOGREFINE_TIMEOUT',
                                                       DEFAULT_TIMEOUT))
        self.max_retries = (DEFAULT_MAX_RETRIES if max_retries is None
                            else int(max_retries))
        self.verify = verify
        if self.base_url and not str(self.base_url).startswith(
                ('http://', 'https://')):
            raise APIError('config', 'base_url must be an absolute http(s) '
                                     'OpenAI-compatible base URL; refusing '
                                     'before any socket')
        self.session = _get_pool()

    def _check_config(self):
        if not self.base_url or not str(self.base_url).startswith(
                ('http://', 'https://')):
            raise APIError('config', 'LOGREFINE_API_BASE must be an absolute '
                                     'http(s) OpenAI-compatible base URL')
        if not self.api_key:
            raise APIError('config', 'LOGREFINE_API_KEY is not set; refusing '
                                      '(real HTTP only, no silent fallback)')
        if not self.model:
            raise APIError('config', 'no model: pass model= or set '
                                      'LOGREFINE_MODEL; refusing before any '
                                      'socket (no implied model choice)')

    def chat(self, msgs, purpose, max_tokens=None, json_mode=True):
        """Send a chat completion request and return its message.

        Retry temporary HTTP and connection failures within the configured limit.
        Raise APIError for unsuccessful requests; purpose identifies the call in logs."""
        self._check_config()
        url = str(self.base_url).rstrip('/') + '/chat/completions'
        payload = {'model': self.model, 'messages': msgs,
                   'temperature': self.temperature, 'stream': False}
        if max_tokens:
            payload['max_tokens'] = int(max_tokens)
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        rid = uuid.uuid4().hex
        last = 'unreachable'
        attempts = 0
        with _gate():
            with _GATE_STATS_LOCK:
                _GATE_STATS['calls'] += 1
                _GATE_STATS['active'] += 1
                if _GATE_STATS['active'] > _GATE_STATS['peak']:
                    _GATE_STATS['peak'] = _GATE_STATS['active']
            try:
                for attempt in range(self.max_retries + 1):
                    attempts += 1
                    try:
                        resp = self.session.post(
                            url, json=payload,
                            headers={'Authorization': 'Bearer %s'
                                            % self.api_key},
                            timeout=self.timeout, verify=self.verify,
                            allow_redirects=False)
                    except (requests.ConnectionError, requests.Timeout) as exc:
                        with _GATE_STATS_LOCK:
                            _GATE_STATS['network_errors'] += 1
                        last = _redact('%s: %s' % (type(exc).__name__, exc))
                        if attempt < self.max_retries:
                            time.sleep(1.0 * 2 ** attempt)
                            continue
                        raise APIError('api', 'connection failed after %d '
                                               'attempts (redacted)'
                                       % attempts)
                    _note_http(resp.status_code)
                    if resp.status_code in RETRYABLE_STATUS:
                        last = _redact('HTTP %d' % resp.status_code)[:200]
                        if attempt < self.max_retries:
                            time.sleep(1.0 * 2 ** attempt)
                            continue
                        raise APIError('api', 'retryable HTTP status %d '
                                              'exhausted after %d attempts'
                                       % (resp.status_code, attempts))
                    if resp.status_code >= 400:
                        raise APIError('api', _redact(
                            'HTTP %d: %s' % (resp.status_code,
                                             resp.text[:300])))
                    try:
                        body = resp.json()
                    except ValueError:
                        raise APIError('api', 'non-JSON response body')
                    choices = body.get('choices') or []
                    if not choices:
                        raise APIError('api', 'no choices in response')
                    msg = (choices[0] or {}).get('message') or {}
                    finish = choices[0].get('finish_reason')
                    if finish not in VALID_FINISH_REASONS:
                        raise APIError('api', 'invalid finish_reason %r'
                                       % finish)
                    usage = body.get('usage') or {}
                    with _GATE_STATS_LOCK:
                        for k in ('prompt_tokens', 'completion_tokens',
                                  'total_tokens'):
                            if isinstance(usage.get(k), int):
                                _GATE_STATS[k] += usage[k]
                    content = msg.get('content')
                    if not isinstance(content, str) or not content.strip():
                        raise APIError('api', 'empty assistant content '
                                              '(never a fake success row)')
                    self._traj({'rid': rid, 'purpose': _redact(purpose),
                                'model': body.get('model', self.model),
                                'status': resp.status_code, 'usage': usage,
                                'attempts': attempts,
                                'content_sha256': hashlib.sha256(
                                    content.encode()).hexdigest()[:16]})
                    msg['usage'] = usage
                    msg['request_id'] = rid
                    msg['attempts'] = attempts
                    return msg
            finally:
                with _GATE_STATS_LOCK:
                    _GATE_STATS['active'] -= 1
        raise APIError('api', 'unreachable dispatch state: %s' % last)

    @staticmethod
    def _traj(row):
        path = os.environ.get('LOGREFINE_TRAJECTORY')
        if not path:
            return
        with _lock:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(_redact(row), ensure_ascii=False) + '\n')
