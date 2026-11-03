# -*- coding: utf-8 -*-
"""
Отправляет Excel-отчёт сравнения МОНОЛИТ РУС vs ВсеИнструменты на список адресов
с сопроводительным письмом в теле (HTML): что внутри, что улучшилось, что ухудшилось.
Данные для письма берутся из summary.json, который пишет build_report.py.
Отправка: SMTP с реального ящика kontur@super-stroi.ru (smtp.mail.ru, SSL 465).
Письмо идёт «от нас», поэтому не попадает в спам Gmail (в отличие от служебного
домена платформы). Учётные данные читаются из хранилища секретов, в коде/логах
пароль не фигурирует.
"""
import os, json, smtplib, ssl, imaplib, time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr, formatdate, make_msgid

REP='/home/ubuntu/monolit_vi_report'
REPORT='/home/ubuntu/Сравнение_цен_МОНОЛИТ-РУС_vs_ВсеИнструменты.xlsx'
SUMMARY=os.path.join(REP,'summary.json')
# ВТОРОЙ адрес (burkov@monolita.net) добавим только после подтверждённого
# успешного прогона с основным адресом. Пока рассылка — только на основной.
RECIPIENTS=['docheretin@gmail.com','burkov@monolita.net','max@superstroy.ru','dolgoff@superstroy.ru']

FROM_NAME='МОНОЛИТ РУС — мониторинг цен'
ATTACH_NAME='Сравнение_цен_МОНОЛИТ-РУС_vs_ВсеИнструменты.xlsx'
SECRETS='/home/ubuntu/.config/abacusai_auth_secrets.json'

def _creds():
    d=json.load(open(SECRETS))
    blk=d.get('mailrusmtp') or d.get('MAILRUSMTP') or {}
    sec=blk.get('secrets',{})
    def g(k):
        v=sec.get(k); return (v.get('value') if isinstance(v,dict) else v)
    return g('smtp_host'), g('smtp_user'), g('smtp_password')

def _imap_creds():
    """IMAP-доступ для сохранения копий в «Отправленные».
    Хост: imap_host или imap.mail.ru. Пароль: отдельный imap_password (пароль
    приложения с доступом IMAP), иначе — тот же smtp_password. Пользователь — smtp_user."""
    d=json.load(open(SECRETS))
    blk=d.get('mailrusmtp') or d.get('MAILRUSMTP') or {}
    sec=blk.get('secrets',{})
    def g(k):
        v=sec.get(k); return (v.get('value') if isinstance(v,dict) else v)
    host=g('imap_host') or 'imap.mail.ru'
    user=g('smtp_user')
    pwd=g('imap_password') or g('smtp_password')
    return host, user, pwd

def _find_sent_folder(M):
    """Находит папку «Отправленные» по флагу \\Sent, иначе по известным именам."""
    try:
        typ,data=M.list()
        flagged=None; named=None
        for raw in (data or []):
            line=raw.decode('utf-8','replace') if isinstance(raw,bytes) else str(raw)
            low=line.lower()
            name=line.split(' "/" ')[-1].strip().strip('"') if ' "/" ' in line else line.split('"')[-2] if '"' in line else line
            if '\\sent' in low: flagged=name
            if name.lower() in ('sent','отправленные','&BB4EEgRABD4EMgQ7BDUEPdDA-','sent messages','sent items'): named=named or name
        return flagged or named or 'Отправленные'
    except Exception:
        return 'Отправленные'

def save_to_sent(raw_message):
    """Кладёт копию отправленного письма в папку «Отправленные» через IMAP APPEND.
    Возвращает (ok, note). Любая ошибка IMAP НЕ влияет на факт отправки письма."""
    try:
        host,user,pwd=_imap_creds()
        if not (host and user and pwd):
            return False, 'нет IMAP-учётных данных'
        M=imaplib.IMAP4_SSL(host,993,timeout=30)
        M.login(user,pwd)
        folder=_find_sent_folder(M)
        try:
            M.append(folder, '\\Seen', imaplib.Time2Internaldate(time.time()),
                     raw_message if isinstance(raw_message,bytes) else raw_message.encode('utf-8'))
        finally:
            try: M.logout()
            except Exception: pass
        return True, folder
    except Exception as e:
        return False, f'{type(e).__name__} {str(e)[:140]}'

# ── палитра письма ────────────────────────────────────────────────────────
BLUE='#2F5496'   # шапка
LT='#eef2fb'     # светлая подложка блоков
GRN='#1a7a3c'    # улучшение
RED='#c0392b'    # ухудшение
GRY='#7a7a7a'    # вспомогательный текст
INK='#222222'    # основной текст

def esc(s): return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def coeff_row(label, cur, prev, unit='', lower_better=True):
    """Строка таблицы сводного индекса: значение + цветная дельта со стрелкой и «было»."""
    if prev is None:
        delta=f"<span style='color:{GRY}'>первый замер</span>"
    else:
        d=round(cur-prev,1)
        if abs(d)<1e-9:
            delta=f"<span style='color:{GRY}'>без изменений</span>"
        else:
            good=(d<0) if lower_better else (d>0)
            col=GRN if good else RED
            arrow='▲' if d>0 else '▼'
            delta=(f"<span style='color:{col};font-weight:bold'>{arrow} {abs(d)}{unit}</span>"
                   f" <span style='color:{GRY}'>(было {prev}{unit})</span>")
    return (f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e3e8f2'>{label}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e3e8f2;text-align:right;font-weight:bold;white-space:nowrap'>{cur}{unit}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #e3e8f2;white-space:nowrap'>{delta}</td>"
            f"</tr>")

def item_table(lst, cap, accent):
    """Таблица изменившихся позиций с цветной левой полосой и серыми деталями динамики."""
    rows=''
    for x in lst[:cap]:
        nm=esc(x.get('name',''))
        if len(nm)>70: nm=nm[:70]+'…'
        det=' · '.join(d for d in (x.get('dyn_price',''), x.get('dyn_deliv','')) if d)
        det=f"<div style='color:{GRY};font-size:12px;margin-top:2px'>{esc(det)}</div>" if det else ''
        rows+=(f"<tr><td style='padding:7px 10px;border-left:3px solid {accent};"
               f"border-bottom:1px solid #eee'><span style='color:{INK}'>{nm}</span>{det}</td></tr>")
    if not rows:
        rows=f"<tr><td style='padding:7px 10px;color:{GRY}'>нет</td></tr>"
    return f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;background:#fff'>{rows}</table>"

def build_body(s, cap=1000):
    date=s['date']; c=s['cur']; p=s.get('prev')
    # шапка-баннер
    head=(f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse'>"
          f"<tr><td style='background:{BLUE};padding:18px 22px;border-radius:8px 8px 0 0'>"
          f"<div style='color:#fff;font-size:18px;font-weight:bold'>Мониторинг цен · МОНОЛИТ РУС vs ВсеИнструменты</div>"
          f"<div style='color:#cdd8ef;font-size:13px;margin-top:4px'>Ежедневный отчёт за {esc(date)}</div>"
          f"</td></tr></table>")
    intro=(f"<p style='margin:16px 0 10px'>Здравствуйте!</p>"
           f"<p style='margin:0 0 14px'>Во вложении — ежедневный отчёт сравнения цен поставщика "
           f"<b>МОНОЛИТ РУС</b> с сайтом <b>ВсеИнструменты</b> (vseinstrumenti.ru, Москва).</p>")
    inside=(f"<div style='background:{LT};border-radius:8px;padding:12px 16px;margin:0 0 16px'>"
            f"<b>Что внутри (Excel, 3 листа):</b> «Сравнение цен» — {s['n_total']} позиций "
            f"(LEIYA {s['n_leiya']}, MO-L {s['n_mol']}, Monolit {s['n_mono']}) с ценой поставщика и ВИ, "
            f"разницей %, наличием, сроком, динамикой и ссылкой; «Сводный индекс» — коэффициенты и "
            f"графики; «История замеров» — все снимки по датам.</div>")
    # сводный индекс — таблица
    coeff=(f"<div style='font-weight:bold;margin:0 0 8px'>Сводный индекс (средняя температура по больнице)</div>"
           f"<table cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
           f"border:1px solid #e3e8f2;border-radius:8px;overflow:hidden;font-size:13px'>"
           +coeff_row('Индекс наличия (выше — лучше)', c['idx'], (p['idx'] if p else None), lower_better=False)
           +coeff_row('Доля в наличии', c['p_ins'], (p['p_ins'] if p else None), unit='%', lower_better=False)
           +coeff_row('Доля «нет в наличии»', c['p_none'], (p['p_none'] if p else None), unit='%')
           +coeff_row('Средний срок поставки', c['avgdd'], (p['avgdd'] if p else None), unit=' дн')
           +coeff_row('Средняя разница цены vs ВИ', c['avgdiff'], (p['avgdiff'] if p else None), unit='%')
           +"</table>")
    # вердикт — цветная плашка
    if 'УЛУЧШ' in s['verdict']:   vbg='#e7f5ec'; vcol=GRN; vbd=GRN
    elif 'УХУДШ' in s['verdict']: vbg='#fcebe9'; vcol=RED; vbd=RED
    else:                         vbg=LT;        vcol=BLUE; vbd='#c3cee6'
    verdict=(f"<div style='background:{vbg};border-left:4px solid {vbd};border-radius:6px;"
             f"padding:12px 16px;margin:14px 0;color:{vcol};font-weight:bold'>{esc(s['verdict'])}</div>")
    # что изменилось
    if not s['has_prev']:
        changes=(f"<div style='background:{LT};border-radius:8px;padding:12px 16px;margin:0 0 16px'>"
                 f"<b>Что изменилось:</b> это первый замер — базовая точка отсчёта. "
                 f"Со следующего прогона здесь появятся конкретные товары, по которым стало "
                 f"лучше или хуже, а на графиках протянется динамика.</div>")
    else:
        if s['n_improved']:
            imp=(f"<div style='color:{GRN};font-weight:bold;margin:14px 0 6px'>▲ Стало лучше ({s['n_improved']})</div>"
                 +item_table(s['improved'], cap, GRN))
        else:
            imp=f"<p style='color:{GRY};margin:14px 0 6px'>Улучшений по позициям нет.</p>"
        if s['n_worsened']:
            wrs=(f"<div style='color:{RED};font-weight:bold;margin:14px 0 6px'>▼ Стало хуже ({s['n_worsened']})</div>"
                 +item_table(s['worsened'], cap, RED))
        else:
            wrs=f"<p style='color:{GRY};margin:14px 0 6px'>Ухудшений по позициям нет.</p>"
        extra=(f"<p style='font-size:12px;color:{GRY};margin:8px 0 0'>Показаны первые {cap} в каждой группе; "
               f"полный список — в файле.</p>") if (s['n_improved']>cap or s['n_worsened']>cap) else ''
        changes=f"<div style='font-weight:bold;margin:4px 0'>Что изменилось по позициям</div>{imp}{wrs}{extra}"
    caveat=(f"<hr style='border:none;border-top:1px solid #e3e8f2;margin:18px 0 10px'>"
            f"<p style='color:{GRY};font-size:12px;margin:0'>Цены и наличие ВИ — базовая розница Москвы, "
            f"по регионам и с промокодами могут отличаться. {s['n_flags']} позиций Monolit помечены "
            f'«проверить ед. изм.»: цена поставщика может быть за штуку из упаковки, а ВИ продаёт '
            f"поштучно. Отчёт формируется автоматически ежедневно.</p>")
    return (f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:13px;color:{INK};"
            f"max-width:720px;margin:0 auto;line-height:1.5'>"
            f"{head}<div style='padding:4px 2px'>{intro}{inside}{coeff}{verdict}{changes}{caveat}</div></div>")

def _smtp_connect(host, user, pwd):
    """SSL 465, при неудаче — STARTTLS 587."""
    try:
        ctx=ssl.create_default_context()
        s=smtplib.SMTP_SSL(host,465,context=ctx,timeout=40); s.login(user,pwd); return s
    except Exception:
        s=smtplib.SMTP(host,587,timeout=40); s.ehlo(); s.starttls(context=ssl.create_default_context())
        s.ehlo(); s.login(user,pwd); return s

def main():
    if not os.path.exists(SUMMARY):
        print('НЕТ summary.json — письмо не отправлено'); return 1
    if not os.path.exists(REPORT):
        print('НЕТ файла отчёта — письмо не отправлено'); return 1
    s=json.load(open(SUMMARY))
    # SMTP не ограничивает размер тела (ошибка 414 была только у платформенного API),
    # поэтому показываем полные списки изменившихся позиций.
    body=build_body(s,cap=1000)
    open(os.path.join(REP,'email_body.html'),'w').write(body)
    print(f'Размер тела письма: {len(body.encode("utf-8"))} байт')
    subj=f"МОНОЛИТ РУС vs ВсеИнструменты — отчёт за {s['date']}"
    if s['has_prev']:
        for k,v in {'УЛУЧШИЛАСЬ':'ситуация улучшилась','УХУДШИЛАСЬ':'ситуация ухудшилась'}.items():
            if k in s['verdict']: subj+=f" ({v})"; break

    host,user,pwd=_creds()
    if not (host and user and pwd):
        print('НЕТ SMTP-учётных данных в хранилище секретов — письмо не отправлено'); return 1
    with open(REPORT,'rb') as f: att_bytes=f.read()

    ok=0
    try:
        srv=_smtp_connect(host,user,pwd)
    except Exception as e:
        print(f'SMTP connect/login FAIL: {type(e).__name__} {str(e)[:200]}'); return 1
    try:
        for to in RECIPIENTS:
            try:
                msg=MIMEMultipart()
                msg['Subject']=subj; msg['From']=formataddr((FROM_NAME,user)); msg['To']=to
                msg['Date']=formatdate(localtime=True); msg['Message-ID']=make_msgid(domain=user.split('@')[-1])
                msg.attach(MIMEText(body,'html','utf-8'))
                part=MIMEApplication(att_bytes,_subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                part.add_header('Content-Disposition','attachment',filename=('utf-8','',ATTACH_NAME))
                msg.attach(part)
                raw=msg.as_string()
                srv.sendmail(user,[to],raw)
                print(f'-> {to}: OK'); ok+=1
                sv_ok,sv_note=save_to_sent(raw)
                print(f'   копия в «Отправленные»: {"OK ("+sv_note+")" if sv_ok else "не сохранена — "+sv_note}')
            except Exception as e:
                print(f'-> {to}: FAIL {type(e).__name__} {str(e)[:160]}')
    finally:
        try: srv.quit()
        except Exception: pass
    print(f'Отправлено успешно: {ok}/{len(RECIPIENTS)}')
    return 0 if ok==len(RECIPIENTS) else 1

if __name__=='__main__':
    raise SystemExit(main())
