# -*- coding: utf-8 -*-
"""Распознавание и обработка антибот-защиты ВсеИнструменты (Servicepipe).

ВсеИнструменты закрыты Servicepipe: при подозрении на бота сервер отдаёт не
каталог, а JS-challenge. Если challenge не проходит — показывается капча
(обычно с картинкой) внутри `#id_captcha_frame_div`.

Модуль НЕ пытается распознавать картинку автоматически. Вместо этого он:
  1) различает «идёт JS-проверка» / «показана капча» / «страница в порядке»;
  2) при капче делает скриншот, сохраняет HTML и присылает фото в Telegram;
  3) ждёт, пока человек пройдёт капчу в браузере или пришлёт код ответом
     в Telegram (тогда код вводится в поле капчи автоматически);
  4) после решения возвращает управление — прогон продолжается без перезапуска.
"""
import re
import time

from common import CAPTCHA_DIR, CAPTCHA_TG_SOLVE, log, tg_poll_replies, tg_send, tg_send_photo

# Маркеры именно блокировки (не путать со словом «проверка» в тексте товаров).
BLOCK_MARKERS = (
    'доступ ограничен',
    'доступ запрещён',
    'доступ запрещен',
    'подтвердите, что вы не робот',
    'подтвердите что вы не робот',
    'too many requests',
    'access denied',
    'request blocked',
)

STATE_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const st = getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8;
  };
  const capDiv = document.getElementById('id_captcha_frame_div');
  const capVisible = visible(capDiv) && capDiv.children.length > 0;
  const frames = [...document.querySelectorAll('iframe')];
  const spFrames = frames
    .filter(f => visible(f))
    .map(f => f.getAttribute('src') || '')
    .filter(s => /servicepipe|captcha|challenge/i.test(s))
    .map(s => s.slice(0, 300));
  const text = ((document.body && document.body.innerText) || '').slice(0, 3000);
  return {
    tiles: document.querySelectorAll('[data-qa="products-tile"]').length,
    loader: !!document.querySelector('js-challenge-loader'),
    capVisible,
    spFrames,
    title: document.title || '',
    url: location.href,
    text,
  };
}
"""

# Состояния
OK = 'ok'
CHALLENGE = 'challenge'      # JS-проверка ещё считается, нужно подождать
CAPTCHA = 'captcha'          # нужен человек
BLOCKED = 'blocked'          # 403 / явная блокировка
UNKNOWN = 'unknown'


def classify_page(page, status=None):
    """Возвращает (state, info). info — снимок DOM для лога/диагностики."""
    info = {}
    try:
        info = page.evaluate(STATE_JS) or {}
    except Exception as e:
        # «Execution context was destroyed» — обычное дело при редиректе
        # после JS-challenge. Считаем, что проверка ещё идёт.
        return CHALLENGE, {'error': f'{type(e).__name__}: {str(e)[:120]}'}

    text = (info.get('text') or '').lower()
    # Если каталог уже отрисован — страница рабочая, даже если в DOM остался
    # скрытый/фоновый фрейм Servicepipe.
    if info.get('tiles'):
        return OK, info
    if info.get('spFrames') or info.get('capVisible'):
        return CAPTCHA, info
    if info.get('loader'):
        return CHALLENGE, info
    if status == 403:
        return BLOCKED, info
    if any(m in text for m in BLOCK_MARKERS):
        return BLOCKED, info
    if 'captcha' in text:
        return CAPTCHA, info
    return UNKNOWN, info


def wait_until_ready(page, wait_challenge=45, poll_ms=1000, status=None):
    """Ждёт, пока Servicepipe закончит JS-challenge.

    Возвращает (state, info): обычно OK, CAPTCHA (нужен человек) или BLOCKED.
    """
    deadline = time.monotonic() + wait_challenge
    state, info = classify_page(page, status=status)
    while state == CHALLENGE and time.monotonic() < deadline:
        page.wait_for_timeout(poll_ms)
        state, info = classify_page(page, status=status)
    return state, info


def _slug(text, limit=40):
    text = re.sub(r'[^0-9A-Za-zА-Яа-я_-]+', '-', text)[:limit].strip('-')
    return text or 'page'


def save_debug(page, tag):
    """Сохраняет скриншот и HTML текущей страницы. Возвращает (png_path, html_path)."""
    CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    base = CAPTCHA_DIR / f'{stamp}_{_slug(tag)}'
    png = base.with_suffix('.png')
    html = base.with_suffix('.html')
    try:
        target = page.query_selector('#id_captcha_frame_div')
        if target and target.is_visible():
            target.screenshot(path=str(png))
        else:
            page.screenshot(path=str(png), full_page=False)
    except Exception as e:
        log(f'CAPTCHA: скриншот элемента не удался ({type(e).__name__}), делаю общий')
        try:
            page.screenshot(path=str(png), full_page=False)
        except Exception as e2:
            log(f'CAPTCHA: не удалось сделать скриншот: {type(e2).__name__} {e2}')
            png = None
    try:
        html.write_text(page.content(), encoding='utf-8')
    except Exception as e:
        log(f'CAPTCHA: не удалось сохранить HTML: {type(e).__name__} {e}')
        html = None
    return png, html


def _visible(el):
    try:
        return bool(el and el.is_visible() and not el.is_disabled())
    except Exception:
        return False


def _submit_answer(page, answer):
    """Пытается ввести код капчи в первое видимое поле (включая iframe).

    Сначала смотрим фреймы Servicepipe/капчи, затем дочерние фреймы. В главном
    фрейме трогаем только поля, явно относящиеся к капче, чтобы случайно не
    вписать код в поиск по сайту.
    """
    specific = (
        'input[name*="captcha" i]',
        'input[id*="captcha" i]',
        'input[placeholder*="код" i]',
        'input[placeholder*="символ" i]',
    )
    generic = (
        'input[type="text"]',
        'input[type="tel"]',
        'input:not([type])',
        'textarea',
    )
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    main = getattr(page, 'main_frame', None)
    frames.sort(key=lambda f: (
        0 if re.search(r'servicepipe|captcha|challenge', (f.url or '')) else 1,
        0 if f is not main else 1,
    ))

    for frame in frames:
        selectors = specific if frame is main else specific + generic
        for sel in selectors:
            try:
                candidates = frame.query_selector_all(sel)
            except Exception:
                continue
            for el in candidates:
                if not _visible(el):
                    continue
                try:
                    el.click()
                    el.fill(answer)
                    pressed = False
                    for btn in frame.query_selector_all('button, input[type="submit"]'):
                        try:
                            label = ((btn.get_attribute('value') or btn.inner_text()) or '').lower()
                        except Exception:
                            label = ''
                        if any(w in label for w in ('провер', 'отправ', 'подтверд', 'войти', 'ок')):
                            if _visible(btn):
                                btn.click()
                                pressed = True
                                break
                    if not pressed:
                        el.press('Enter')
                    return True
                except Exception:
                    continue
    return False


def handle_captcha(page, url, wait_min=20, allow_telegram=True):
    """Обрабатывает показанную капчу. True — проверка пройдена, можно продолжать."""
    state, info = classify_page(page)
    if state != CAPTCHA:
        return state in (OK, UNKNOWN)
    log(f'CAPTCHA: капча на {url}; сохраняю скриншот и жду решения человеком '
        f'(до {wait_min} мин)')
    started_at = time.time()

    png, html = save_debug(page, 'captcha')
    hint = ('Сайт ВсеИнструменты просит пройти капчу.\n\n'
            '1) Откройте Chrome на рабочей машине и пройдите проверку, ИЛИ\n'
            '2) ответьте на это сообщение кодом с картинки — бот введёт его сам.\n\n'
            f'URL: {url}\n'
            f'Жду до {wait_min} мин, прогон продолжится автоматически.')
    tg_send(hint)
    if png:
        tg_send_photo(png, 'Капча ВсеИнструменты — введите код ответом на сообщение')

    deadline = time.monotonic() + max(0, wait_min) * 60
    last_check = 0.0
    next_ping = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(3000)
        except Exception:
            time.sleep(3)
        state, info = classify_page(page)
        if state in (OK, UNKNOWN):
            log('CAPTCHA: проверка пройдена, продолжаю прогон')
            return True

        if allow_telegram and CAPTCHA_TG_SOLVE and time.monotonic() - last_check >= 8:
            last_check = time.monotonic()
            for reply in tg_poll_replies(timeout=0, min_date=started_at):
                reply = reply.strip()
                if not reply or reply.startswith('/'):
                    continue
                log(f'CAPTCHA: получен ответ из Telegram ({len(reply)} симв.), ввожу в форму')
                if _submit_answer(page, reply):
                    tg_send('Код введён, проверяю результат…')
                    page.wait_for_timeout(6000)
                    state, info = classify_page(page)
                    if state in (OK, UNKNOWN):
                        log('CAPTCHA: код подошёл, проверка пройдена')
                        return True
                    tg_send('Код не подошёл или капча обновилась. '
                            'Пришлите новый код или пройдите проверку в браузере.')
                    png, _ = save_debug(page, 'captcha-retry')
                    if png:
                        tg_send_photo(png, 'Обновлённая капча')

        if time.monotonic() >= next_ping:
            next_ping = time.monotonic() + 600
            tg_send('Напоминание: капча ВсеИнструменты всё ещё не пройдена. '
                    'Прогон ждёт.')

    log('CAPTCHA: время ожидания истекло')
    tg_send('Капча ВсеИнструменты не пройдена за отведённое время. '
            'Прогон остановлен, данные не перезаписаны.')
    return False
