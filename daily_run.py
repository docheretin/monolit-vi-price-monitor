# -*- coding: utf-8 -*-
"""
Ежедневный прогон сравнения МОНОЛИТ РУС vs ВсеИнструменты.
Скрейпит сайт ЧЕРЕЗ УЖЕ ОТКРЫТЫЙ в этой машине Chrome (порт 9222),
переиспользуя живую сессию (куки), где капча уже пройдена человеком.
Если натыкаемся на капчу/блокировку — НЕ ломаем отчёт: оставляем прежние данные,
пишем в лог и выходим с кодом 2 (чтобы планировщик сообщил вам «нужно пройти капчу»).
Иначе — обновляем цены/наличие ВИ и пересобираем отчёт (новый снимок в истории).
"""
import os, re, json, ast, subprocess, datetime, traceback, time

TMP='/home/ubuntu/tmp'; REP='/home/ubuntu/monolit_vi_report'
LOG=os.path.join(REP,'daily_run.log')
CDP="http://localhost:9222"
CDP_PORT=9222
CHROME_PROFILE='/home/ubuntu/.chrome-mono'   # постоянный «прогретый» профиль (куки/капча)
CHROME_LOG='/home/ubuntu/chrome_cdp.log'

# Приоритетный источник данных ВИ: пуш с внешнего «хорошего-IP» хоста (ingest_service).
REMOTE_FILE=os.path.join(TMP,'vi_remote.json')
REMOTE_MAX_AGE_HOURS=20
REMOTE_BLOCKED=object()   # маркер: пуш пришёл, но хост поймал капчу/блок

LEIYA_PAGES=["https://www.vseinstrumenti.ru/brand/leiya-2104793/",
             "https://www.vseinstrumenti.ru/brand/leiya-2104793/page2/",
             "https://www.vseinstrumenti.ru/brand/leiya-2104793/page3/"]
MOL_PAGES=["https://www.vseinstrumenti.ru/search/?what=Monolit+MO-L"]
MONO_PAGES=["https://www.vseinstrumenti.ru/category/stroitelnyj-instrument-6474/monolit--2106803/",
            "https://www.vseinstrumenti.ru/category/stroitelnyj-instrument-6474/monolit--2106803/page2/"]

def log(m):
    line=f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {m}"
    print(line)
    try:
        with open(LOG,'a') as f: f.write(line+"\n")
    except Exception: pass

def tg_alert(msg):
    """Шлёт уведомление в личку в Telegram (сбой капчи). Токен/chat_id — из хранилища секретов."""
    try:
        import urllib.request, urllib.parse
        d=json.load(open('/home/ubuntu/.config/abacusai_auth_secrets.json'))
        blk=(d.get('telegram') or {}).get('secrets',{})
        g=lambda k:(blk.get(k,{}).get('value') if isinstance(blk.get(k),dict) else blk.get(k))
        tok=g('bot_token'); chat=g('chat_id')
        if not (tok and chat):
            log('TG: нет токена/chat_id — уведомление не отправлено'); return
        data=urllib.parse.urlencode({'chat_id':chat,'text':msg,'disable_web_page_preview':'true'}).encode()
        req=urllib.request.Request(f'https://api.telegram.org/bot{tok}/sendMessage',data=data)
        with urllib.request.urlopen(req,timeout=20) as r: r.read()
        log('TG: уведомление о капче отправлено')
    except Exception as e:
        log(f'TG: не удалось отправить уведомление: {type(e).__name__} {str(e)[:120]}')

def _cdp_alive():
    """True, если CDP на порту 9222 отвечает."""
    try:
        import urllib.request
        with urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json/version', timeout=5) as r:
            return r.status==200
    except Exception:
        return False

def ensure_chrome():
    """Если Chrome с CDP на 9222 не поднят — запускаем его сами (headful, постоянный профиль).
    Возвращает True, если к концу CDP отвечает. Не роняет прогон — только логирует."""
    if _cdp_alive():
        log("Chrome(CDP:9222) уже запущен — переиспользую сессию")
        return True
    # Основной путь: поднять постоянный systemd-сервис mono-chrome (профиль .chrome-mono,
    # в котором человек проходит капчу; сессия сохраняется и переиспользуется).
    try:
        subprocess.run(['sudo','systemctl','start','mono-chrome'], timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            time.sleep(1)
            if _cdp_alive():
                log("mono-chrome (systemd) поднят, CDP отвечает")
                return True
    except Exception as e:
        log(f"systemctl start mono-chrome не сработал: {type(e).__name__} {str(e)[:120]}")
    log("Chrome(CDP:9222) не запущен — поднимаю сам (fallback)")
    env=dict(os.environ)
    if not env.get('DISPLAY'):
        # выбираем активный X-дисплей из /tmp/.X11-unix (например, :99), иначе :0
        disp=':0'
        try:
            for n in sorted(os.listdir('/tmp/.X11-unix')):
                if n.startswith('X') and n[1:].isdigit():
                    disp=':'+n[1:]; break
        except Exception: pass
        env['DISPLAY']=disp
    log(f"DISPLAY={env.get('DISPLAY')}")
    cmd=['google-chrome-stable','--no-sandbox','--disable-gpu',
         f'--remote-debugging-port={CDP_PORT}', f'--user-data-dir={CHROME_PROFILE}',
         '--no-first-run','--no-default-browser-check','--disable-background-networking']
    try:
        with open(CHROME_LOG,'a') as lf:
            subprocess.Popen(cmd, stdout=lf, stderr=lf, env=env,
                             start_new_session=True)
    except Exception as e:
        log(f"Не удалось запустить Chrome: {type(e).__name__} {str(e)[:150]}")
        return False
    # ждём до ~30 c, пока CDP оживёт
    for _ in range(30):
        time.sleep(1)
        if _cdp_alive():
            log("Chrome поднят, CDP отвечает")
            return True
    log("Chrome запущен, но CDP не ответил за 30 c")
    return False

# Сбор данных делаем НЕ навигацией (page.goto), а внутристраничным fetch() на том же
# origin — обычный XHR в уже «прогретой» (пройдена капча) сессии капчу не триггерит.
# fetch возвращает серверный HTML страницы, который тут же парсим через DOMParser.
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
  // признаки анти-бота/капчи ВИ (ServicePipe js-challenge + Yandex SmartCaptcha)
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

def _vi_page(ctx):
    """Берём уже открытую вкладку на vseinstrumenti.ru (её origin/куки нужны для fetch)."""
    for pg in ctx.pages:
        try:
            if 'vseinstrumenti.ru' in (pg.url or ''):
                return pg
        except Exception:
            pass
    return ctx.pages[0] if ctx.pages else ctx.new_page()

def _scrape_dom(ctx, url):
    """Резерв: читаем товары из DOM через навигацию (goto). Безопасно только в уже
    пройденной (cleared) сессии — там goto не вызывает новой капчи."""
    pg = ctx.new_page()
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3500)
        for _ in range(6):
            pg.mouse.wheel(0, 2200); pg.wait_for_timeout(600)
        title = (pg.title() or '').lower(); body = pg.inner_text("body")[:3000].lower()
        if any(k in title or k in body for k in
               ('captcha','доступ ограничен','подтвердите','robot','проверка','разверните картинк')):
            return [], True
        out = []
        for t in pg.query_selector_all('[data-qa="products-tile"]'):
            def q(sel):
                e = t.query_selector(sel); return e.inner_text().strip() if e else ''
            code = q('[data-qa="product-code-value"]')
            if not code: continue
            price = q('[data-qa="product-price-current"]')
            link = t.query_selector('a[href*="/product"]')
            href = link.get_attribute('href') if link else ''
            if href and href.startswith('/'): href = 'https://www.vseinstrumenti.ru'+href
            pint = re.sub(r'[^\d]','',price or ''); pint = int(pint) if pint else None
            out.append(dict(code=code, name=q('[data-qa="product-name"]'), price=pint,
                            status=(q('[data-qa="product-availability"]') or 'Нет в наличии'),
                            url=(href or '').split('#')[0]))
        return (out, False) if out else ([], True)
    finally:
        pg.close()

def scrape_page(ctx, url):
    """Возвращает (list_of_products, blocked_bool). product={code,name,price,status,url}
    Сбор через fetch() внутри живой сессии — без навигации, чтобы не ловить капчу."""
    pg = _vi_page(ctx)
    try:
        res = pg.evaluate(FETCH_JS, {"url": url, "timeout": 45000})
    except Exception as e:
        log(f"fetch-исключение на {url}: {e}")
        res = None
    # Реальная капча/блокировка (403/429/js-challenge) — не ломаем отчёт, сигналим.
    if res is None or res.get('blocked'):
        st = res.get('status') if res else 'ERR'
        log(f"fetch заблокирован на {url}: status={st} "
            f"tiles={(res or {}).get('tiles')} len={(res or {}).get('len')} ferr={(res or {}).get('ferr')}")
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
    if out:
        return out, False
    # fetch вернул 200 без ошибок, но товаров нет: страница могла отрисоваться
    # на клиенте. Пробуем DOM-резерв (сессия уже пройдена — goto не даст капчи).
    log(f"fetch 0 товаров (status={res.get('status')}, len={res.get('len')}) — пробую DOM-резерв на {url}")
    return _scrape_dom(ctx, url)

def load_remote_vi():
    """Пытается прочитать данные ВИ, присланные (пуш) с внешнего хоста, из vi_remote.json.

    Возвращает:
      * (all_prod_dict_by_code, mono_prod_list) — если пуш валиден (товары собраны):
          all_prod — dict {code: {code,name,price(int|null),status,url}} (как локальный скрейп),
          mono_prod — список тех же product-словарей, чьи коды входят в mono_codes.
      * REMOTE_BLOCKED — если пуш пришёл, но хост пометил blocked==True (капча/блок).
      * None — если файла нет / протух (>20ч) / невалиден / товаров нет.
    """
    try:
        if not os.path.exists(REMOTE_FILE):
            return None
        data=json.load(open(REMOTE_FILE,encoding='utf-8'))
    except Exception as e:
        log(f"remote: не удалось прочитать {REMOTE_FILE}: {type(e).__name__} {str(e)[:120]}")
        return None
    gen=data.get('generated_at')
    # разбор generated_at (ISO8601, возможен суффикс Z)
    ts=None
    if gen:
        try:
            s=str(gen).strip().replace('Z','+00:00')
            dt=datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt=dt.replace(tzinfo=datetime.timezone.utc)
            ts=dt
        except Exception:
            ts=None
    # если хост поймал капчу/блок — это сигнал (даже если данные устарели/пусты)
    if data.get('blocked'):
        log(f"remote: пуш помечен blocked=true (generated_at={gen}) — хост поймал капчу/блок")
        return REMOTE_BLOCKED
    if ts is None:
        log(f"remote: generated_at не распарсился ({gen!r}) — игнорирую пуш"); return None
    age_h=(datetime.datetime.now(datetime.timezone.utc)-ts).total_seconds()/3600.0
    if age_h>REMOTE_MAX_AGE_HOURS:
        log(f"remote: пуш протух ({age_h:.1f}ч > {REMOTE_MAX_AGE_HOURS}ч) — игнорирую"); return None
    if age_h<0:
        log(f"remote: generated_at в будущем ({gen}) — игнорирую пуш"); return None
    products=data.get('products') or []
    if not products:
        log("remote: список products пуст — игнорирую пуш"); return None
    mono_codes=set(str(c) for c in (data.get('mono_codes') or []))
    all_prod={}; mono_prod=[]
    for pr in products:
        code=(pr.get('code') or '').strip()
        if not code:
            continue
        price=pr.get('price')
        if isinstance(price,str):
            pint=re.sub(r'[^\d]','',price); price=int(pint) if pint else None
        norm=dict(code=code, name=pr.get('name',''), price=price,
                  status=(pr.get('status') or 'Нет в наличии'),
                  url=(pr.get('url') or '').split('#')[0])
        all_prod[code]=norm
        if code in mono_codes:
            mono_prod.append(norm)
    if not all_prod:
        log("remote: после нормализации нет ни одного товара с кодом — игнорирую пуш"); return None
    log(f"remote: валиден — товаров {len(all_prod)}, Monolit-инструмент {len(mono_prod)}, "
        f"возраст {age_h:.1f}ч, generated_at={gen}")
    return all_prod, mono_prod


def main():
    from playwright.sync_api import sync_playwright
    log("=== старт ежедневного прогона ===")
    all_prod={}; mono_prod=[]
    # ПРИОРИТЕТ 1: данные, присланные (пуш) с внешнего «хорошего-IP» хоста.
    remote=load_remote_vi()
    if remote is REMOTE_BLOCKED:
        log("БЛОКИРОВКА/КАПЧА на внешнем хосте (пуш blocked=true). Прекращаю без перезаписи данных.")
        log("CAPTCHA")
        tg_alert("\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: внешний хост сбора цен "
                 "наткнулся на капчу vseinstrumenti.ru. Зайдите в браузер на том хосте и "
                 "пройдите проверку, чтобы возобновить ежедневный сбор цен.")
        return 2
    if remote is not None:
        all_prod, mono_prod = remote
        log("источник данных ВИ: удалённый хост (пуш) — пропускаю ensure_chrome и локальный скрейп")
    else:
        # ПРИОРИТЕТ 2 (fallback): локальный сбор через уже открытый Chrome (CDP:9222).
        log("источник данных ВИ: локальный скрейп через Chrome CDP (пуш отсутствует/протух)")
        # Автоподъём браузера: если Chrome с CDP на 9222 не запущен (типично при
        # автозапуске по расписанию) — поднимаем сами, чтобы не падать с ECONNREFUSED.
        if not ensure_chrome():
            log("Браузер недоступен (CDP:9222 не отвечает). Прекращаю без перезаписи данных.")
            tg_alert("\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: не удалось запустить браузер "
                     "для сбора цен (CDP:9222 не поднялся). Зайдите на рабочую машину и проверьте Chrome.")
            return 2
        with sync_playwright() as p:
            b=p.chromium.connect_over_cdp(CDP)
            ctx=b.contexts[0] if b.contexts else b.new_context()
            for grp,pages,is_mono in (('LEIYA',LEIYA_PAGES,False),('MO-L',MOL_PAGES,False),('MONO',MONO_PAGES,True)):
                for u in pages:
                    prods,blocked=scrape_page(ctx,u)
                    if blocked:
                        log(f"БЛОКИРОВКА/КАПЧА на {u} (группа {grp}). Прекращаю без перезаписи данных.")
                        log("CAPTCHA")
                        tg_alert("\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: сайт vseinstrumenti.ru "
                                 "требует пройти капчу. Зайдите в браузер на рабочей машине и пройдите "
                                 "проверку, чтобы возобновить ежедневный сбор цен.")
                        return 2
                    log(f"{grp}: {u} -> {len(prods)} товаров")
                    for pr in prods:
                        all_prod[pr['code']]=pr
                        if is_mono: mono_prod.append(pr)
    # vi_fresh.json — свежие цены/наличие ВИ по коду (для Leiya/MO-L/Monolit)
    json.dump(all_prod, open(os.path.join(TMP,'vi_fresh.json'),'w'), ensure_ascii=False, indent=1)
    # mono_tool_p1/p2 в формате, который ждёт build_report (litr/ast)
    def mono_fmt(pr): return dict(name=pr['name'], cur=str(pr['price'] or ''), code=pr['code'],
                                  status=pr['status'], url=pr['url'])
    half=(len(mono_prod)+1)//2
    for fn,chunk in (('mono_tool_p1.json',mono_prod[:half]),('mono_tool_p2.json',mono_prod[half:])):
        open(os.path.join(TMP,fn),'w').write(repr([mono_fmt(x) for x in chunk]))
    log(f"Собрано кодов ВИ: {len(all_prod)}; Monolit-инструмент: {len(mono_prod)}")
    # пересборка отчёта (дата = сегодня)
    env=dict(os.environ, REPORT_DATE=datetime.date.today().isoformat())
    r=subprocess.run(['/opt/abacus-python/bin/python3','build_report.py'],cwd=REP,env=env,
                     capture_output=True,text=True)
    log(r.stdout.strip()); 
    if r.returncode!=0:
        log("ОШИБКА build_report:\n"+r.stderr[-1500:]); return 1
    log("=== отчёт пересобран успешно ===")
    # отправка отчёта с сопроводительным письмом на всех получателей
    try:
        sr=subprocess.run(['/opt/abacus-python/bin/python3','send_report.py'],cwd=REP,
                          env=os.environ,capture_output=True,text=True)
        log("send_report:\n"+(sr.stdout or '').strip()+("\n"+sr.stderr[-800:] if sr.returncode!=0 else ''))
        if sr.returncode!=0:
            log("ВНИМАНИЕ: отчёт пересобран, но письмо отправлено не всем получателям")
    except Exception:
        log("ОШИБКА отправки письма:\n"+traceback.format_exc())
    return 0

if __name__=='__main__':
    try:
        code=main()
    except Exception:
        log("ИСКЛЮЧЕНИЕ:\n"+traceback.format_exc()); code=1
    raise SystemExit(code)
