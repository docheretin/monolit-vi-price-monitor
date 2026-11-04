# -*- coding: utf-8 -*-
"""Тесты логики сбора без браузера: fetch -> разбор -> аварийный путь."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('MONO_LOG', str(Path(tempfile.gettempdir()) / 'mono_test.log'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily_run import collect_page, write_json  # noqa: E402


PRODUCT = {'code': '33750301', 'name': 'Циркулярная пила LEIYA LY212-01',
           'price': 17555, 'status': '2 шт. в магазинах сегодня',
           'url': 'https://www.vseinstrumenti.ru/product/x-14282267/'}


class FetchPage:
    """Заглушка: fetch-вызовы отдаёт из очереди, состояние — из state."""

    def __init__(self, fetch_results, state=None, goto_status=200):
        self.fetch_results = list(fetch_results)
        self.state = state or {'tiles': 40}
        self.goto_status = goto_status
        self.calls = []

    def evaluate(self, script, arg=None):
        if arg is not None:  # FETCH_JS
            self.calls.append('fetch')
            return self.fetch_results.pop(0)
        if 'js-challenge-loader' in script:
            self.calls.append('classify')
            return self.state
        self.calls.append('dom')
        return []

    def goto(self, url, **kw):
        self.calls.append('goto')

        class Resp:
            def __init__(self, status):
                self.status = status

        return Resp(self.goto_status)

    def wait_for_timeout(self, ms):
        pass


class CollectTests(unittest.TestCase):
    def test_fetch_success(self):
        page = FetchPage([{'status': 200, 'challenge': False, 'products': [PRODUCT], 'count': 1}])
        self.assertEqual(collect_page(page, 'https://x/', 0), [PRODUCT])
        self.assertEqual(page.calls, ['fetch'])

    def test_fetch_zero_products_then_empty_page(self):
        page = FetchPage([
            {'status': 200, 'challenge': False, 'products': [], 'count': 0},
            {'status': 200, 'challenge': False, 'products': [], 'count': 0},
        ])
        self.assertEqual(collect_page(page, 'https://x/', 0), [])
        self.assertEqual(page.calls.count('fetch'), 2)
        self.assertEqual(page.calls.count('goto'), 1)

    def test_blocked_returns_none(self):
        page = FetchPage(
            [{'status': 403, 'challenge': True, 'products': [], 'count': 0}],
            state={'tiles': 0, 'loader': False, 'capVisible': False, 'spFrames': [],
                   'title': '', 'url': 'https://x/', 'text': 'доступ ограничен'},
            goto_status=403)
        self.assertIsNone(collect_page(page, 'https://x/', 0))

    def test_write_json_is_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'vi_fresh.json'
            write_json(p, {'1': PRODUCT})
            self.assertEqual(json.loads(p.read_text(encoding='utf-8'))['1']['code'], '33750301')
            self.assertFalse((Path(d) / 'vi_fresh.json.tmp').exists())


if __name__ == '__main__':
    unittest.main()
