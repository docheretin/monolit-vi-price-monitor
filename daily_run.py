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

def looks_blocked(title, body):
    t=(title or '').lower(); b=(body or '').lower()
    for k in ('captcha','доступ ограничен','подтвердите','robot','проверка','ddos','too many requests'):
        if k in t or k in b: return True
    return False

def scrape_page(ctx, url):
    """Возвращает (list_of_products, blocked_bool). product={code,name,price,status,url}"""
    pg=ctx.new_page()
    try:
        # Маскируем признаки автоматизации (webdriver и пр.) — ведём себя как обычный браузер.
        try:
            pg.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        except Exception: pass
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(3500)
        # прокрутка для подгрузки ленивых плиток
        for _ in range(6):
            pg.mouse.wheel(0, 2200); pg.wait_for_timeout(600)
        title=pg.title(); body=pg.inner_text("body")[:3000]
        tiles=pg.query_selector_all('[data-qa="products-tile"]')
        if looks_blocked(title, body):
            return [], True
        out=[]
        for t in tiles:
            def q(sel):
                e=t.query_selector(sel); return e.inner_text().strip() if e else ''
            name=q('[data-qa="product-name"]')
            price=q('[data-qa="product-price-current"]')
            code=q('[data-qa="product-code-value"]')
            avail=q('[data-qa="product-availability"]')
            link=t.query_selector('a[href*="/product"]')
            href=link.get_attribute('href') if link else ''
            if href and href.startswith('/'): href='https://www.vseinstrumenti.ru'+href
            if not code: continue
            pint=re.sub(r'[^\d]','',price or ''); pint=int(pint) if pint else None
            out.append(dict(code=code.strip(), name=name, price=pint,
                            status=(avail or 'Нет в наличии'), url=(href or '').split('#')[0]))
        # пустая выдача там, где товары должны быть — тоже считаем блокировкой
        if not out:
            return [], True
        return out, False
    finally:
        pg.close()

def main():
    from playwright.sync_api import sync_playwright
    log("=== старт ежедневного прогона ===")
    # Автоподъём браузера: если Chrome с CDP на 9222 не запущен (типично при
    # автозапуске по расписанию) — поднимаем сами, чтобы не падать с ECONNREFUSED.
    if not ensure_chrome():
        log("Браузер недоступен (CDP:9222 не отвечает). Прекращаю без перезаписи данных.")
        tg_alert("\u26a0\ufe0f Отчёт МОНОЛИТ vs ВсеИнструменты: не удалось запустить браузер "
                 "для сбора цен (CDP:9222 не поднялся). Зайдите на рабочую машину и проверьте Chrome.")
        return 2
    all_prod={}; mono_prod=[]
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
