# -*- coding: utf-8 -*-
"""Диагностика доступа к ВсеИнструменты в живом Chrome.

Запуск:
    python captcha_debug.py [URL]

Открывает URL в уже запущенном Chrome (CDP 9222), печатает состояние
(ok / challenge / captcha / blocked), сохраняет скриншот и HTML в папку
`captcha/` рабочей папки отчёта. Удобно, если сайт поменял разметку капчи.
"""
import argparse
import json

from common import CDP, SITE, ensure_dirs, log
from captcha import (BLOCKED, CAPTCHA, CHALLENGE, OK, UNKNOWN, classify_page,
                     save_debug, wait_until_ready)

DEFAULT_URL = f'{SITE}/brand/leiya-2104793/'

DESCRIPTIONS = {
    OK: 'страница в порядке, каталог получен',
    CHALLENGE: 'идёт JS-проверка Servicepipe (обычно проходит сама за 5–20 с)',
    CAPTCHA: 'показана капча — нужен человек (скриншот сохранён)',
    BLOCKED: 'сайт вернул блокировку (403)',
    UNKNOWN: 'страница открыта, но каталога не видно (проверьте URL)',
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?', default=DEFAULT_URL)
    ap.add_argument('--wait', type=int, default=45, help='секунд ждать JS-проверку')
    args = ap.parse_args(argv)

    ensure_dirs()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        pages = [pg for pg in ctx.pages if not (pg.url or '').startswith('devtools://')]
        page = pages[0] if pages else ctx.new_page()
        page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
        state, info = wait_until_ready(page, wait_challenge=args.wait)

        print(f'URL:      {args.url}')
        print(f'STATE:    {state} — {DESCRIPTIONS.get(state, "?")}')
        print('INFO:')
        print(json.dumps({k: v for k, v in info.items() if k != 'text'},
                         ensure_ascii=False, indent=2))
        if info.get('text'):
            print('TEXT:', (info['text'][:400]).replace('\n', ' '))

        if state in (CAPTCHA, BLOCKED, UNKNOWN):
            png, html = save_debug(page, f'debug-{state}')
            print(f'Скриншот: {png}')
            print(f'HTML:     {html}')
    return 0 if state == OK else 2


if __name__ == '__main__':
    raise SystemExit(main())
