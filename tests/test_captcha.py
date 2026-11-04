# -*- coding: utf-8 -*-
"""Юнит-тесты распознавания антибот-защиты (без браузера)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('MONO_LOG', str(Path(tempfile.gettempdir()) / 'mono_test.log'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captcha import (BLOCKED, CAPTCHA, CHALLENGE, OK, UNKNOWN,  # noqa: E402
                     classify_page, wait_until_ready)


class FakePage:
    """Минимальная заглушка playwright.Page для classify_page."""

    def __init__(self, info, sequence=None):
        self.info = info
        self.sequence = list(sequence or [])
        self.evaluations = 0

    def evaluate(self, script, arg=None):
        self.evaluations += 1
        if self.sequence:
            value = self.sequence.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return self.info

    def wait_for_timeout(self, ms):
        pass


def info(**kw):
    base = {
        'tiles': 0,
        'loader': False,
        'capVisible': False,
        'spFrames': [],
        'title': '',
        'url': 'https://www.vseinstrumenti.ru/brand/leiya-2104793/',
        'text': '',
    }
    base.update(kw)
    return base


class ClassifyTests(unittest.TestCase):
    def test_catalog_is_ok(self):
        state, _ = classify_page(FakePage(info(tiles=40)))
        self.assertEqual(state, OK)

    def test_js_challenge(self):
        state, _ = classify_page(FakePage(info(loader=True)))
        self.assertEqual(state, CHALLENGE)

    def test_captcha_visible(self):
        state, _ = classify_page(FakePage(info(capVisible=True, loader=True)))
        self.assertEqual(state, CAPTCHA)

    def test_captcha_iframe(self):
        state, _ = classify_page(FakePage(info(spFrames=['https://x.servicepipe.tech/captcha'])))
        self.assertEqual(state, CAPTCHA)

    def test_http_403_is_blocked(self):
        state, _ = classify_page(FakePage(info()), status=403)
        self.assertEqual(state, BLOCKED)

    def test_block_marker_in_text(self):
        state, _ = classify_page(FakePage(info(text='Доступ ограничен. Подтвердите, что вы не робот')))
        self.assertEqual(state, BLOCKED)

    def test_word_proverka_alone_is_not_block(self):
        # «проверка» встречается в обычных текстах — не должна считаться капчей
        state, _ = classify_page(FakePage(info(text='Проверка наличия в магазинах')))
        self.assertEqual(state, UNKNOWN)

    def test_destroyed_context_means_challenge(self):
        page = FakePage(info(), sequence=[RuntimeError('Execution context was destroyed')])
        state, _ = classify_page(page)
        self.assertEqual(state, CHALLENGE)

    def test_wait_until_ready_polls_challenge(self):
        page = FakePage(info(), sequence=[info(loader=True), info(loader=True), info(tiles=10)])
        state, _ = wait_until_ready(page, wait_challenge=5)
        self.assertEqual(state, OK)
        self.assertEqual(page.evaluations, 3)

    def test_wait_until_ready_returns_captcha(self):
        page = FakePage(info(capVisible=True))
        state, _ = wait_until_ready(page, wait_challenge=5)
        self.assertEqual(state, CAPTCHA)

    def test_wait_until_ready_passes_http_status(self):
        page = FakePage(info())
        state, _ = wait_until_ready(page, wait_challenge=0, status=403)
        self.assertEqual(state, BLOCKED)


if __name__ == '__main__':
    unittest.main()
