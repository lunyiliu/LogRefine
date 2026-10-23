#!/usr/bin/env python3
"""Test the client, role dispatcher, and repair loop with a local HTTP server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lr import roles as R                          # noqa: E402
from lr.cli import _dispatcher                     # noqa: E402
from lr.client import ChatClient                   # noqa: E402
from lr.pipeline import FourRolePipeline, run_batch  # noqa: E402
from lr.seed_pool import load_seed_pool            # noqa: E402

FIXTURE = json.loads((Path(__file__).resolve().parent / 'fixtures' /
                      'offline_full_loop.json').read_text(encoding='utf-8'))
STATE = {'counts': {}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(length))
        assert self.path.endswith('/chat/completions')
        role = req['messages'][0]['content'].split('ROLE: ', 1)[1].split(
            ' ')[0].strip().lower()
        STATE['counts'][role] = STATE['counts'].get(role, 0) + 1
        seq = FIXTURE.get(role, [])
        idx = min(STATE['counts'][role] - 1, len(seq) - 1)
        reply = json.dumps(seq[idx])
        body = json.dumps({'model': req['model'],
                           'choices': [{'message': {'role': 'assistant',
                                                    'content': reply},
                                        'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 100,
                                     'completion_tokens': 50,
                                     'total_tokens': 150}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)


class LiveHttpPath(unittest.TestCase):
    def test_real_sockets_through_the_released_stack(self):
        import tempfile
        srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = 'http://127.0.0.1:%d/v1' % port
            seeds, view = load_seed_pool(
                str(Path(__file__).resolve().parents[1] / 'examples' /
                    'demo_seeds.jsonl'))
            client = ChatClient(base_url=base, model='test-model',
                                api_key='sk-fixtureonlykey')
            pipe = FourRolePipeline(_dispatcher(client), seeds=seeds,
                                    run_root=tempfile.mkdtemp())
            res = run_batch(pipe, targets={'AD': 1}, batch_limit=2,
                             max_rounds=3, wall_sec=15)
            self.assertEqual([r['kind'] for r in res['rows'] if r],
                             ['accepted'])
            st = STATE['counts']
            self.assertEqual(st.get('planner'), 1)
            self.assertEqual(st.get('generalizer'), 1)
            self.assertEqual(st.get('inspector'), 2)
            self.assertEqual(st.get('optimizer'), 1)

            from lr.client import gate_stats
            gs = gate_stats()
            self.assertGreaterEqual(gs['http_attempts'], 4)

            self.assertTrue(('inspector', 'inspector.md',
                             R.consumed_hashes('inspector')['inspector.md'])
                            in pipe.consumed_hashes)
        finally:
            srv.shutdown()


if __name__ == '__main__':
    unittest.main(verbosity=2)
