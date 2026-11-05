# -*- coding: utf-8 -*-
"""
Приёмный сервис (ingest) для данных ВсеИнструменты, собранных на внешнем хосте
с «хорошим» (российским/жилым) IP. Слушает 127.0.0.1:8077, за nginx на
vi-ingest.vm.internal (публично vi-ingest.abacusai.cloud).

Эндпоинты:
  GET  /health  -> 200 {"ok":true}
  POST /ingest  -> требует Authorization: Bearer <token> (token из .ingest_token).
                   Тело JSON: {generated_at, blocked, products[], mono_codes[]}.
                   Сырое тело атомарно сохраняется в /home/ubuntu/tmp/vi_remote.json.
"""
import os, json, tempfile, datetime

from flask import Flask, request, jsonify

REP = '/home/ubuntu/monolit_vi_report'
TMP = '/home/ubuntu/tmp'
TOKEN_FILE = os.path.join(REP, '.ingest_token')
OUT_FILE = os.path.join(TMP, 'vi_remote.json')
LOG_FILE = os.path.join(REP, 'ingest.log')

app = Flask(__name__)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception as e:
        log(f"Не удалось прочитать токен: {type(e).__name__}")
        return None


def _atomic_write(path, raw_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.vi_remote_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw_bytes)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


@app.get('/health')
def health():
    return jsonify(ok=True), 200


@app.post('/ingest')
def ingest():
    expected = _read_token()
    auth = request.headers.get('Authorization', '')
    given = auth[7:].strip() if auth.startswith('Bearer ') else ''
    if not expected or not given or given != expected:
        log(f"401 неавторизованный POST /ingest с {request.remote_addr}")
        return jsonify(ok=False, error='unauthorized'), 401

    raw = request.get_data()  # сырое тело, сохраняем как есть
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception as e:
        log(f"400 невалидный JSON: {type(e).__name__}")
        return jsonify(ok=False, error='invalid json'), 400

    products = data.get('products') or []
    blocked = bool(data.get('blocked'))
    gen = data.get('generated_at')
    mono = data.get('mono_codes') or []

    try:
        _atomic_write(OUT_FILE, raw)
    except Exception as e:
        log(f"500 не удалось сохранить файл: {type(e).__name__} {str(e)[:120]}")
        return jsonify(ok=False, error='write failed'), 500

    log(f"OK /ingest: products={len(products)} mono_codes={len(mono)} "
        f"blocked={blocked} generated_at={gen} -> {OUT_FILE}")
    return jsonify(ok=True, received=len(products)), 200


if __name__ == '__main__':
    log("=== ingest_service старт на 127.0.0.1:8077 ===")
    app.run(host='127.0.0.1', port=8077, threaded=True)
