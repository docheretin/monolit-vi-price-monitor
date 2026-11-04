# -*- coding: utf-8 -*-
"""Общие настройки, пути, логирование и уведомления в Telegram.

Все пути можно переопределить переменными окружения — это позволяет запускать
проект и на Ubuntu-сервере (значения по умолчанию), и на Windows/Mac:

    MONO_REP          — рабочая папка отчёта      (по умолчанию /home/ubuntu/monolit_vi_report)
    MONO_TMP          — папка обмена JSON-файлами (по умолчанию /home/ubuntu/tmp)
    MONO_XLSX         — путь к итоговому Excel
    MONO_SECRETS      — путь к JSON-хранилищу секретов
    MONO_LOG          — путь к логу
    MONO_CDP_PORT     — порт отладки Chrome (по умолчанию 9222)
    MONO_CHROME_PROFILE — профиль Chrome (для автозапуска)
    MONO_CAPTCHA_WAIT_MIN — сколько минут ждать решения капчи человеком (по умолчанию 20)
"""
import json
import os
import sys
import uuid
import urllib.error
import urllib.parse
import urllib.request
import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
IS_WINDOWS = os.name == 'nt'

DEFAULT_REP = Path(os.environ.get('MONO_REP') or
                   (Path.home() / 'monolit_vi_report' if IS_WINDOWS
                    else Path('/home/ubuntu/monolit_vi_report')))
DEFAULT_TMP = Path(os.environ.get('MONO_TMP') or
                   (Path(os.environ.get('TEMP', str(APP_DIR / 'tmp'))) / 'monolit_vi_tmp'
                    if IS_WINDOWS else Path('/home/ubuntu/tmp')))

REP = DEFAULT_REP
TMP = DEFAULT_TMP
LOG = Path(os.environ.get('MONO_LOG') or (REP / 'daily_run.log'))
XLSX = Path(os.environ.get('MONO_XLSX') or
            ((REP if IS_WINDOWS else Path('/home/ubuntu'))
             / 'Сравнение_цен_МОНОЛИТ-РУС_vs_ВсеИнструменты.xlsx'))
CAPTCHA_DIR = REP / 'captcha'
TG_STATE = REP / 'tg_state.json'

CDP_PORT = int(os.environ.get('MONO_CDP_PORT') or 9222)
CDP = f'http://localhost:{CDP_PORT}'
CHROME_PROFILE = Path(os.environ.get('MONO_CHROME_PROFILE') or
                      (Path.home() / '.chrome-mono' if IS_WINDOWS else '/home/ubuntu/.chrome-mono'))
CHROME_LOG = Path(os.environ.get('MONO_CHROME_LOG') or
                  (REP / 'chrome_cdp.log' if IS_WINDOWS else '/home/ubuntu/chrome_cdp.log'))

CAPTCHA_WAIT_MIN = int(os.environ.get('MONO_CAPTCHA_WAIT_MIN') or 20)
CAPTCHA_TG_SOLVE = (os.environ.get('MONO_CAPTCHA_TG_SOLVE') or '1').lower() not in ('0', 'false', 'no', 'off')

SITE = 'https://www.vseinstrumenti.ru'


def ensure_dirs():
    for d in (REP, TMP, CAPTCHA_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def log(message, path=None):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    try:
        with open(path or LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


# ---------------------------------------------------------------- секреты
def secret_file():
    candidates = [
        os.environ.get('MONO_SECRETS'),
        APP_DIR / 'secrets.json',
        Path.home() / '.config' / 'abacusai_auth_secrets.json',
        '/home/ubuntu/.config/abacusai_auth_secrets.json',
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _load_secrets():
    path = secret_file()
    if not path:
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def get_secret(block, key):
    data = _load_secrets()
    blk = data.get(block) or data.get(block.upper()) or {}
    sec = blk.get('secrets', blk)
    val = sec.get(key)
    if isinstance(val, dict):
        val = val.get('value')
    return val


# ---------------------------------------------------------------- Telegram
def tg_creds():
    token = get_secret('telegram', 'bot_token')
    chat = get_secret('telegram', 'chat_id')
    return (token, chat) if token and chat else (None, None)


def _tg_call(method, fields=None, files=None, timeout=40):
    token, _ = tg_creds()
    if not token:
        log('TG: нет bot_token — уведомление не отправлено')
        return None
    url = f'https://api.telegram.org/bot{token}/{method}'
    fields = fields or {}
    if files:
        boundary = '----mono' + uuid.uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n'.encode('utf-8'))
        for name, (filename, data, ctype) in files.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode('utf-8'))
            chunks.append(data)
            chunks.append(b'\r\n')
        chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
        body = b''.join(chunks)
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    else:
        body = urllib.parse.urlencode(fields).encode('utf-8')
        req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        log(f'TG: {method} не удалось: {type(e).__name__} {str(e)[:140]}')
        return None


def tg_send(text, disable_preview=True):
    _, chat = tg_creds()
    if not chat:
        log('TG: нет chat_id — уведомление не отправлено')
        return
    res = _tg_call('sendMessage', {
        'chat_id': chat,
        'text': text,
        'disable_web_page_preview': 'true' if disable_preview else 'false',
    })
    if res and res.get('ok'):
        log('TG: уведомление отправлено')


def tg_send_photo(path, caption=''):
    _, chat = tg_creds()
    if not chat:
        return
    p = Path(path)
    try:
        data = p.read_bytes()
    except Exception as e:
        log(f'TG: не прочитать скриншот {p}: {e}')
        return
    res = _tg_call('sendPhoto', {
        'chat_id': chat,
        'caption': caption[:1024],
    }, files={'photo': (p.name, data, 'image/png')})
    if res and res.get('ok'):
        log('TG: скриншот капчи отправлен')


def _tg_offset():
    try:
        return int(json.loads(TG_STATE.read_text(encoding='utf-8')).get('offset') or 0)
    except Exception:
        return 0


def _tg_save_offset(offset):
    try:
        TG_STATE.write_text(json.dumps({'offset': offset}), encoding='utf-8')
    except Exception:
        pass


def tg_poll_replies(timeout=0, min_date=0):
    """Возвращает новые текстовые сообщения от chat_id (свежие — первыми).

    min_date — unix-время начала события: более старые сообщения (в т.ч.
    накопившиеся до капчи) игнорируются.
    """
    token, chat = tg_creds()
    if not token or not chat or not CAPTCHA_TG_SOLVE:
        return []
    params = {'timeout': int(timeout), 'allowed_updates': '["message"]'}
    offset = _tg_offset()
    if offset:
        params['offset'] = offset
    res = _tg_call('getUpdates', params, timeout=max(15, int(timeout) + 10))
    if not res or not res.get('ok'):
        return []
    messages = []
    for upd in res.get('result') or []:
        _tg_save_offset(int(upd['update_id']) + 1)
        msg = upd.get('message') or {}
        text = (msg.get('text') or '').strip()
        if not text:
            continue
        if str(msg.get('chat', {}).get('id')) != str(chat):
            continue
        if min_date and int(msg.get('date') or 0) < int(min_date):
            continue
        messages.append(text)
    messages.reverse()
    return messages
