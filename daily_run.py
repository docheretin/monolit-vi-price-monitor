# -*- coding: utf-8 -*-
"""Ежедневный прогон сравнения МОНОЛИТ РУС vs ВсеИнструменты.

Сбор данных идёт через уже открытый Chrome с CDP (порт 9222), но НЕ через
навигацию по каждой странице: страницы забираются обычным `fetch()` изнутри
уже «прогретой» вкладки. Для Servicepipe это обычный запрос фронтенда, поэтому
JS-challenge и капча не перезапускаются на каждой странице (раньше новый
`page.goto` на каждый URL провоцировал проверку заново).

Если доступ всё-таки потерян:
  * JS-challenge ждём до 45 секунд — обычно он проходит сам;
  * при капче присылаем скриншот в Telegram, ждём человека (до MONO_CAPTCHA_WAIT_MIN
    минут) и продолжаем прогон, не перезаписывая данные неполным снимком.

Коды выхода: 0 — успех; 2 — капча/блокировка; 1 — иная ошибка.
"""
import argparse
import datetime
import json
import os
import random
import subprocess
import sys
import time
import traceback
from pathlib import Path

from common import (APP_DIR, CAPTCHA_WAIT_MIN, CDP, CDP_PORT, CHROME_LOG,
                    CHROME_PROFILE, IS_WINDOWS, REP, SITE, TMP, XLSX,
                    ensure_dirs, log as _log, secret_file, tg_send)
from captcha import (BLOCKED, CAPTCHA, CHALLENGE, OK, classify_page,
                     handle_captcha, wait_until_ready)

LEIYA_PAGES = [f'{SITE}/brand/leiya-2104793/',
               f'{SITE}/brand/leiya-2104793/page2/',
               f'{SITE}/brand/leiya-2104793/page3/']
MOL_PAGES = [f'{SITE}/search/?what=Monolit+MO-L']
MONO_PAGES = [f'{SITE}/category/stroitelnyj-instrument-6474/monolit--2106803/',
              f'{SITE}/category/stroitelnyj-instrument-6474/monolit--2106803/page2/']

GROUPS = (('LEIYA', LEIYA_PAGES, False),
          ('MO-L', MOL_PAGES, False),
          ('MONO', MONO_PAGES, True))


def log(message):
    _log(message)


# ---------------------------------------------------------------- браузер
def _cdp_alive():
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json/version', timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def _chrome_binary():
    if not IS_WINDOWS:
        return 'google-chrome-stable'
    candidates = [
        Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Google/Chrome/Application/chrome.exe',
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return 'chrome.exe'


def ensure_chrome():
    """Поднимает Chrome с CDP, если он ещё не запущен. True — CDP отвечает."""
    if _cdp_alive():
        log(f'Chrome(CDP:{CDP_PORT}) уже запущен — переиспользую сессию')
        return True

    if not IS_WINDOWS:
        # Основной путь: постоянный systemd-сервис с «прогретым» профилем.
        try:
            subprocess.run(['sudo', 'systemctl', 'start', 'mono-chrome'], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(30):
                time.sleep(1)
                if _cdp_alive():
                    log('mono-chrome (systemd) поднят, CDP отвечает')
                    return True
        except Exception as e:
            log(f'systemctl start mono-chrome не сработал: {type(e).__name__} {str(e)[:120]}')

    log(f'Chrome(CDP:{CDP_PORT}) не запущен — поднимаю сам')
    env = dict(os.environ)
    if not IS_WINDOWS and not env.get('DISPLAY'):
        disp = ':0'
        try:
            for n in sorted(os.listdir('/tmp/.X11-unix')):
                if n.startswith('X') and n[1:].isdigit():
                    disp = ':' + n[1:]
                    break
        except Exception:
            pass
        env['DISPLAY'] = disp
        log(f"DISPLAY={env.get('DISPLAY')}")

    cmd = [_chrome_binary(), '--disable-gpu',
           f'--remote-debugging-port={CDP_PORT}',
           f'--user-data-dir={CHROME_PROFILE}',
           '--no-first-run', '--no-default-browser-check',
           '--disable-background-networking',
           '--disable-blink-features=AutomationControlled',
           '--lang=ru-RU', '--window-size=1280,900']
    if not IS_WINDOWS:
        cmd.insert(1, '--no-sandbox')
    try:
        CHROME_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CHROME_LOG, 'a', encoding='utf-8') as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=lf, env=env,
                             start_new_session=not IS_WINDOWS)
    except Exception as e:
        log(f'Не удалось запустить Chrome: {type(e).__name__} {str(e)[:150]}')
        return False
    for _ in range(30):
        time.sleep(1)
        if _cdp_alive():
            log('Chrome поднят, CDP отвечает')
            return True
    log('Chrome запущен, но CDP не ответил за 30 c')
    return False


def pick_page(ctx):
    """Берём уже открытую вкладку ВИ, чтобы не плодить новые документы.

    Чужие вкладки пользователя не трогаем: если вкладки ВИ нет — открываем одну.
    """
    pages = [p for p in ctx.pages if not (p.url or '').startswith(('devtools://', 'chrome-extension://'))]
    for p in pages:
        if 'vseinstrumenti.ru' in (p.url or ''):
            return p
    for p in pages:
        if (p.url or '') in ('about:blank', ''):
            return p
    return ctx.new_page()


# ---------------------------------------------------------------- сбор
# Забираем HTML через fetch() изнутри вкладки и сразу парсим его DOMParser'ом.
# Это ключевой момент: fetch не является навигацией, поэтому Servicepipe
# не запускает новую проверку.
FETCH_JS = r"""
async ({url, timeout}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      cache: 'no-store',
      signal: ctrl.signal,
      headers: {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'
      }
    });
    const html = await resp.text();
    if (resp.status !== 200 || /js-challenge-loader|id_captcha_frame_div/.test(html)) {
      return {status: resp.status, challenge: true, count: 0, products: [],
              title: '', text: html.slice(0, 500)};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const tiles = [...doc.querySelectorAll('[data-qa="products-tile"]')];
    const products = [];
    for (const t of tiles) {
      const get = (s) => { const e = t.querySelector(s); return e ? (e.textContent || '').trim() : ''; };
      const code = get('[data-qa="product-code-value"]');
      if (!code) continue;
      const a = t.querySelector('a[href*="/product"]');
      let href = a ? (a.getAttribute('href') || '') : '';
      if (href.startsWith('/')) href = location.origin + href;
      const digits = get('[data-qa="product-price-current"]').replace(/[^\d]/g, '');
      products.push({
        code: code,
        name: get('[data-qa="product-name"]'),
        price: digits ? parseInt(digits, 10) : null,
        status: get('[data-qa="product-availability"]') || 'Нет в наличии',
        url: href.split('#')[0]
      });
    }
    return {
      status: resp.status,
      challenge: false,
      count: products.length,
      title: (doc.querySelector('title') || {}).textContent || '',
      text: ((doc.body && doc.body.textContent) || '').slice(0, 500),
      products: products
    };
  } catch (e) {
    return {status: 0, challenge: false, error: String(e), count: 0, products: []};
  } finally {
    clearTimeout(timer);
  }
}
"""


def fetch_products(page, url, timeout_ms=45000):
    try:
        return page.evaluate(FETCH_JS, {'url': url, 'timeout': timeout_ms}) or {}
    except Exception as e:
        return {'status': 0, 'error': f'{type(e).__name__}: {str(e)[:160]}',
                'count': 0, 'products': []}


def _goto(page, url):
    try:
        resp = page.goto(url, wait_until='domcontentloaded', timeout=60000)
        return resp.status if resp else None
    except Exception as e:
        log(f'Навигация на {url} не удалась: {type(e).__name__} {str(e)[:140]}')
        return None


def prepare_page(page, url, wait_min):
    """Гарантирует, что вкладка открыта на сайте и проверка не висит."""
    if 'vseinstrumenti.ru' not in (page.url or ''):
        log('Открываю ВсеИнструменты в существующей вкладке…')
        status = _goto(page, f'{SITE}/')
        state, info = wait_until_ready(page, status=status)
        if state == CAPTCHA:
            if not handle_captcha(page, page.url or url, wait_min):
                return False
        elif state in (BLOCKED, CHALLENGE):
            log(f'Сайт не открылся: {state} (HTTP {status})')
            return False
        return True

    state, info = classify_page(page)
    if state == CHALLENGE:
        log('Servicepipe выполняет JS-проверку — жду…')
        state, info = wait_until_ready(page)
    if state == CAPTCHA:
        return handle_captcha(page, page.url or url, wait_min)
    return True


def human_scroll(page, steps=6):
    """Плавная прокрутка: если плитки когда-нибудь станут ленивыми, они догрузятся."""
    try:
        for _ in range(steps):
            page.mouse.wheel(0, random.randint(600, 1200))
            page.wait_for_timeout(random.randint(250, 500))
    except Exception:
        pass


def recover(page, url, wait_min):
    """Аварийный путь: открываем страницу документом и проходим проверку."""
    log(f'Прямой fetch не дал каталог — открываю {url} документом')
    status = _goto(page, url)
    state, info = wait_until_ready(page, wait_challenge=60, status=status)

    if state == CHALLENGE:
        state, info = wait_until_ready(page, wait_challenge=45, status=status)
    if state == CAPTCHA:
        if not handle_captcha(page, url, wait_min):
            return False
        status = _goto(page, url)
        state, info = wait_until_ready(page, status=status)
    if state in (BLOCKED, CHALLENGE):
        log(f'Страница не открылась: {state} (HTTP {status})')
        return False
    if state == OK:
        human_scroll(page)
    return True


def collect_page(page, url, wait_min):
    """Возвращает список товаров или None, если страницу не удалось получить."""
    for attempt in (1, 2):
        data = fetch_products(page, url)
        if data.get('status') == 200 and not data.get('challenge'):
            products = data.get('products') or []
            if products:
                return products
            if attempt == 2:
                log(f'{url}: страница открылась, но товаров нет — считаю выдачу пустой')
                return []
            log(f'{url}: fetch вернул 0 товаров (попытка {attempt}) — проверяю документом')
        else:
            log(f'{url}: fetch -> HTTP {data.get("status")}'
                f'{" (challenge)" if data.get("challenge") else ""}'
                f'{" " + str(data.get("error"))[:120] if data.get("error") else ""}')

        if not recover(page, url, wait_min):
            return None
        # после навигации сначала пробуем разобрать уже открытый документ
        try:
            dom = page.evaluate(r"""() => {
              const out = [];
              for (const t of document.querySelectorAll('[data-qa="products-tile"]')) {
                const get = (s) => { const e = t.querySelector(s); return e ? (e.textContent || '').trim() : ''; };
                const code = get('[data-qa="product-code-value"]');
                if (!code) continue;
                const a = t.querySelector('a[href*="/product"]');
                let href = a ? (a.getAttribute('href') || '') : '';
                if (href.startsWith('/')) href = location.origin + href;
                const digits = get('[data-qa="product-price-current"]').replace(/[^\d]/g, '');
                out.push({code, name: get('[data-qa="product-name"]'),
                          price: digits ? parseInt(digits, 10) : null,
                          status: get('[data-qa="product-availability"]') || 'Нет в наличии',
                          url: href.split('#')[0]});
              }
              return out;
            }""") or []
            if dom:
                return dom
        except Exception:
            pass
    return []


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, p)


def write_repr(path, obj):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(repr(obj), encoding='utf-8')
    os.replace(tmp, p)


# ---------------------------------------------------------------- прогон
def main(argv=None):
    ap = argparse.ArgumentParser(description='Сбор цен ВсеИнструменты + отчёт')
    ap.add_argument('--check', action='store_true',
                    help='только проверить доступ и собрать одну страницу, без отчёта')
    ap.add_argument('--no-send', action='store_true', help='собрать отчёт, но не отправлять письмо')
    ap.add_argument('--wait-captcha', type=int, default=CAPTCHA_WAIT_MIN,
                    help='сколько минут ждать решения капчи (0 — не ждать)')
    args = ap.parse_args(argv)

    ensure_dirs()
    log('=== старт ежедневного прогона ===')
    if not ensure_chrome():
        log('Браузер недоступен (CDP не отвечает). Прекращаю без перезаписи данных.')
        tg_send('\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: не удалось запустить браузер '
                'для сбора цен (CDP не поднялся). Проверьте Chrome на рабочей машине.')
        return 2

    from playwright.sync_api import sync_playwright
    all_prod = {}
    mono_prod = []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = pick_page(ctx)

        if not prepare_page(page, LEIYA_PAGES[0], args.wait_captcha):
            log('CAPTCHA')
            log('Доступ к сайту не восстановлен. Данные не перезаписаны.')
            return 2

        if args.check:
            prods = collect_page(page, LEIYA_PAGES[0], args.wait_captcha)
            if prods is None:
                log('CHECK: страница недоступна')
                return 2
            if not prods:
                log('CHECK: страница открылась, но товаров нет')
                return 2
            log(f'CHECK OK: {len(prods)} товаров; пример: {prods[0]}')
            return 0

        for grp, pages, is_mono in GROUPS:
            for url in pages:
                prods = collect_page(page, url, args.wait_captcha)
                if prods is None:
                    log(f'БЛОКИРОВКА/КАПЧА на {url} (группа {grp}). '
                        f'Прекращаю без перезаписи данных.')
                    log('CAPTCHA')
                    tg_send('\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: сайт vseinstrumenti.ru '
                            'не отдаёт каталог даже после проверки. Прогон остановлен, '
                            'старые данные сохранены.')
                    return 2
                log(f'{grp}: {url} -> {len(prods)} товаров')
                for pr in prods:
                    all_prod[pr['code']] = pr
                    if is_mono:
                        mono_prod.append(pr)
                # человекоподобная пауза между запросами
                time.sleep(random.uniform(2.0, 4.5))

    if not all_prod:
        log('Не собрано ни одного товара — данные не перезаписываю.')
        return 2

    write_json(TMP / 'vi_fresh.json', all_prod)

    def mono_fmt(pr):
        return dict(name=pr['name'], cur=str(pr['price'] or ''), code=pr['code'],
                    status=pr['status'], url=pr['url'])

    half = (len(mono_prod) + 1) // 2
    write_repr(TMP / 'mono_tool_p1.json', [mono_fmt(x) for x in mono_prod[:half]])
    write_repr(TMP / 'mono_tool_p2.json', [mono_fmt(x) for x in mono_prod[half:]])
    log(f'Собрано кодов ВИ: {len(all_prod)}; Monolit-инструмент: {len(mono_prod)}')

    env = dict(os.environ,
               REPORT_DATE=datetime.date.today().isoformat(),
               PYTHONIOENCODING='utf-8',
               MONO_REP=str(REP), MONO_TMP=str(TMP), MONO_XLSX=str(XLSX))
    if secret_file():
        env['MONO_SECRETS'] = str(secret_file())

    r = subprocess.run([sys.executable, str(APP_DIR / 'build_report.py')],
                       cwd=str(APP_DIR), env=env, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    log((r.stdout or '').strip())
    if r.returncode != 0:
        log('ОШИБКА build_report:\n' + (r.stderr or '')[-1500:])
        return 1
    log('=== отчёт пересобран успешно ===')

    if args.no_send:
        log('--no-send: письмо не отправляю')
        return 0

    try:
        sr = subprocess.run([sys.executable, str(APP_DIR / 'send_report.py')],
                            cwd=str(APP_DIR), env=env, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
        log('send_report:\n' + (sr.stdout or '').strip() +
            (('\n' + (sr.stderr or '')[-800:]) if sr.returncode != 0 else ''))
        if sr.returncode != 0:
            log('ВНИМАНИЕ: отчёт пересобран, но письмо отправлено не всем получателям')
    except Exception:
        log('ОШИБКА отправки письма:\n' + traceback.format_exc())
    return 0


if __name__ == '__main__':
    try:
        code = main()
    except Exception:
        log('ИСКЛЮЧЕНИЕ:\n' + traceback.format_exc())
        code = 1
    raise SystemExit(code)
