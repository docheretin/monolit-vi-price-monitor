# -*- coding: utf-8 -*-
"""
Автономный скрейпер ВсеИнструменты (ВИ) для запуска на внешнем «хорошем-IP»
хосте (лучше всего российский/жилой always-on компьютер).

Собирает товары групп LEIYA + MO-L + MONO тем же способом, что и daily_run на VM
(внутристраничный fetch() в живой «прогретой» сессии, парс плиток data-qa), и
ПУШИТ собранные данные на приёмный сервис VM (ingest) по HTTP.

Если наткнулись на капчу / 403 / js-challenge — всё равно пушим payload с
blocked=true, чтобы VM прислала вам Telegram-уведомление о капче.

Запуск:
  python scrape_and_push.py          # как в .env (HEADLESS=0 — с окном браузера)
  HEADLESS=1 python scrape_and_push.py   # фоном (после разового прогрева профиля)
"""
import os, re, sys, json, time, datetime

# --- .env (без обязательной зависимости python-dotenv) --------------------
def _load_env():
    here=os.path.dirname(os.path.abspath(__file__))
    envf=os.path.join(here,'.env')
    if os.path.exists(envf):
        for line in open(envf,encoding='utf-8'):
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k,v=line.split('=',1)
            k=k.strip(); v=v.strip().strip('"').strip("'")
            os.environ.setdefault(k,v)

_load_env()

INGEST_URL   = os.environ.get('INGEST_URL','').strip()
INGEST_TOKEN = os.environ.get('INGEST_TOKEN','').strip()
HEADLESS     = os.environ.get('HEADLESS','0').strip() in ('1','true','True','yes')
PROFILE_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)),'rf_profile')

# --- прокси (жилой/мобильный РФ-IP) — обходит блокировку ServicePipe по -----
# --- датацентровому IP. Пусто = без прокси (прямое подключение). ------------
PROXY_SERVER = os.environ.get('PROXY_SERVER','').strip()   # напр. http://host:port или socks5://host:port
PROXY_USER   = os.environ.get('PROXY_USER','').strip()
PROXY_PASS   = os.environ.get('PROXY_PASS','').strip()

def _proxy_cfg():
    if not PROXY_SERVER:
        return None
    cfg = {"server": PROXY_SERVER}
    if PROXY_USER:
        cfg["username"] = PROXY_USER
    if PROXY_PASS:
        cfg["password"] = PROXY_PASS
    return cfg

# --- те же страницы, что и на VM (дублируем для автономности пакета) -------
LEIYA_PAGES=["https://www.vseinstrumenti.ru/brand/leiya-2104793/",
             "https://www.vseinstrumenti.ru/brand/leiya-2104793/page2/",
             "https://www.vseinstrumenti.ru/brand/leiya-2104793/page3/"]
MOL_PAGES=["https://www.vseinstrumenti.ru/search/?what=Monolit+MO-L"]
MONO_PAGES=["https://www.vseinstrumenti.ru/category/stroitelnyj-instrument-6474/monolit--2106803/",
            "https://www.vseinstrumenti.ru/category/stroitelnyj-instrument-6474/monolit--2106803/page2/"]

# --- тот же внутристраничный fetch, что и на VM ---------------------------
FETCH_JS = r"""
async ({url, timeout}) => {
  const ctrl = new AbortController();
  const tid = setTimeout(() => ctrl.abort(), timeout || 45000);
  let html = '', status = 0, ferr = '';
  try {
    const r = await fetch(url, {credentials:'include', redirect:'follow',
                               headers:{'Accept':'text/html,application/xhtml+xml'}, signal:ctrl.signal});
    status = r.status; html = await r.text();
  } catch(e) { ferr = String(e); }
  finally { clearTimeout(tid); }
  const low = html.toLowerCase();
  const challenge = ['js-challenge','servicepipe','smartcaptcha','checkbox-captcha',
                     'spinner-loader','разверните картинк','подтвердите, что вы не робот',
                     'доступ ограничен'].some(k => low.includes(k));
  let products = [], tiles = -1, perr = '';
  try {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const nodes = Array.from(doc.querySelectorAll('[data-qa="products-tile"]'));
    tiles = nodes.length;
    const q = (t,s) => { const e = t.querySelector(s); return e ? e.textContent.trim() : ''; };
    for (const t of nodes) {
      const link = t.querySelector('a[href*="/product"]');
      let href = link ? link.getAttribute('href') : '';
      if (href && href.startsWith('/')) href = 'https://www.vseinstrumenti.ru' + href;
      products.push({code:q(t,'[data-qa="product-code-value"]'),
                     name:q(t,'[data-qa="product-name"]'),
                     price:q(t,'[data-qa="product-price-current"]'),
                     status:q(t,'[data-qa="product-availability"]') || 'Нет в наличии',
                     url:(href || '').split('#')[0]});
    }
  } catch(e) { perr = String(e); }
  const blocked = ferr !== '' || status === 403 || status === 429 || status === 0 || challenge;
  return {blocked, status, tiles, len:html.length, ferr, perr, products};
}
"""


def log(m):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {m}", flush=True)


def _vi_page(ctx):
    for pg in ctx.pages:
        try:
            if 'vseinstrumenti.ru' in (pg.url or ''):
                return pg
        except Exception:
            pass
    pg = ctx.new_page()
    try:
        pg.goto('https://www.vseinstrumenti.ru/', wait_until='domcontentloaded', timeout=60000)
    except Exception as e:
        log(f"стартовая навигация не удалась: {type(e).__name__} {str(e)[:120]}")
    return pg


def scrape_page(pg, url):
    """Возвращает (list_of_products, blocked_bool)."""
    try:
        res = pg.evaluate(FETCH_JS, {"url": url, "timeout": 45000})
    except Exception as e:
        log(f"fetch-исключение на {url}: {e}")
        return [], True
    if res is None or res.get('blocked'):
        log(f"fetch заблокирован на {url}: status={(res or {}).get('status')} "
            f"tiles={(res or {}).get('tiles')} ferr={(res or {}).get('ferr')}")
        return [], True
    out = []
    for pr in res.get('products', []):
        code = (pr.get('code') or '').strip()
        if not code:
            continue
        pint = re.sub(r'[^\d]', '', pr.get('price') or ''); pint = int(pint) if pint else None
        out.append(dict(code=code, name=pr.get('name', ''), price=pint,
                        status=(pr.get('status') or 'Нет в наличии'),
                        url=(pr.get('url') or '').split('#')[0]))
    if not out:
        log(f"fetch 0 товаров на {url} (status={res.get('status')}, len={res.get('len')}) — считаю как блок")
        return [], True
    return out, False


def collect():
    """Собирает товары. Возвращает (all_prod_list, mono_codes, blocked_bool)."""
    from playwright.sync_api import sync_playwright
    all_prod = {}; mono_codes = []
    with sync_playwright() as p:
        _px = _proxy_cfg()
        if _px:
            log(f"используется прокси: {PROXY_SERVER}")
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=HEADLESS, proxy=_px,
            args=['--no-first-run','--no-default-browser-check','--disable-background-networking'])
        try:
            pg = _vi_page(ctx)
            pg.wait_for_timeout(2500)
            for grp, pages, is_mono in (('LEIYA', LEIYA_PAGES, False),
                                        ('MO-L', MOL_PAGES, False),
                                        ('MONO', MONO_PAGES, True)):
                for u in pages:
                    prods, blocked = scrape_page(pg, u)
                    if blocked:
                        log(f"БЛОКИРОВКА/КАПЧА на {u} (группа {grp})")
                        return list(all_prod.values()), mono_codes, True
                    log(f"{grp}: {u} -> {len(prods)} товаров")
                    for pr in prods:
                        all_prod[pr['code']] = pr
                        if is_mono and pr['code'] not in mono_codes:
                            mono_codes.append(pr['code'])
                    pg.wait_for_timeout(800)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    return list(all_prod.values()), mono_codes, False


def push(payload):
    """POST payload на ingest. 3 попытки с паузой. True при HTTP 200."""
    import requests
    if not INGEST_URL or not INGEST_TOKEN:
        log("ОШИБКА: INGEST_URL/INGEST_TOKEN не заданы (.env). Пуш невозможен.")
        return False
    headers = {'Authorization': f'Bearer {INGEST_TOKEN}', 'Content-Type': 'application/json'}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    for attempt in range(1, 4):
        try:
            r = requests.post(INGEST_URL, data=body, headers=headers, timeout=60)
            if r.status_code == 200:
                log(f"пуш OK (HTTP 200): {r.text[:200]}")
                return True
            log(f"пуш попытка {attempt}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            log(f"пуш попытка {attempt}: ошибка {type(e).__name__} {str(e)[:150]}")
        if attempt < 3:
            time.sleep(5 * attempt)
    log("пуш НЕ удался после 3 попыток")
    return False


def main():
    log("=== rf_host_scraper: старт ===")
    log(f"HEADLESS={HEADLESS}, профиль={PROFILE_DIR}")
    blocked = False; products = []; mono_codes = []
    try:
        products, mono_codes, blocked = collect()
    except Exception as e:
        import traceback
        log("ИСКЛЮЧЕНИЕ при сборе:\n" + traceback.format_exc())
        blocked = True  # не можем собрать -> сигналим как блок, чтобы VM уведомила
    payload = {
        'generated_at': datetime.datetime.now(datetime.timezone.utc)
                        .replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'blocked': bool(blocked),
        'products': products,
        'mono_codes': mono_codes,
    }
    log(f"собрано товаров: {len(products)}, mono_codes: {len(mono_codes)}, blocked={blocked}")
    ok = push(payload)
    log("=== rf_host_scraper: завершено ===")
    # Код возврата: 0 успех (пуш дошёл, данные собраны); 2 — был блок/капча; 1 — пуш не дошёл.
    if not ok:
        return 1
    return 2 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
