# -*- coding: utf-8 -*-
import ast, re, json, pickle, os, datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import LineChart, Reference

from common import TMP, REP, XLSX

HIST=os.path.join(REP,'price_history.json')
# Дата замера: берётся из окружения (ежедневный прогон) либо = сегодня
_env=os.environ.get('REPORT_DATE')
TODAY=datetime.date.fromisoformat(_env) if _env else datetime.date.today()
DATE=TODAY.isoformat()
# Свежие данные ВИ по коду (обновляются ежедневным прогоном daily_run.py)
VI_FRESH={}
_vf=os.path.join(TMP,'vi_fresh.json')
if os.path.exists(_vf):
    try: VI_FRESH=json.load(open(_vf))
    except Exception: VI_FRESH={}
RU_MONTHS={'января':1,'февраля':2,'марта':3,'апреля':4,'мая':5,'июня':6,'июля':7,'августа':8,'сентября':9,'октября':10,'ноября':11,'декабря':12}
def litr(f): return ast.literal_eval(open(f).read())
def price_int(s):
    d=re.sub(r'[^\d]','',s or ''); return int(d) if d else None

def parse_delivery(status):
    if not status: return None,'нет данных'
    s=status.lower()
    if 'нет в наличии' in s: return None,'нет в наличии'
    if 'на складе' in s or 'в магазин' in s: return 0,'в наличии'
    m=re.search(r'c\s+(\d+)\s+([а-я]+)',s)
    if m:
        d=int(m.group(1)); mon=RU_MONTHS.get(m.group(2))
        if mon:
            dt=datetime.date(TODAY.year,mon,d)
            if dt<TODAY: dt=datetime.date(TODAY.year+1,mon,d)
            return (dt-TODAY).days,'под заказ'
    return None,'нет данных'

# ===== availability + url maps =====
av={}; url={}
for f in ['leiya_av_p1.json','leiya_av_p2.json','leiya_av_p3.json']:
    for d in litr(os.path.join(TMP,f)): av[d['code']]=d['status']
av.update({'40157597':'21 шт. в магазинах сегодня / 21 шт. есть на складе','39880683':'56 шт. в магазинах сегодня / 56 шт. есть на складе','39880689':'15 шт. в магазинах сегодня / 15 шт. есть на складе','39880677':'24 шт. в магазинах сегодня / 24 шт. есть на складе','39880665':'Нет в наличии'})
for f in ['leiya_url_p1.json','leiya_url_p2.json','leiya_url_p3.json','mol_url.json']:
    for d in litr(os.path.join(TMP,f)): url[d['code']]=d['url'].split('#')[0]
# Monolit tools: build av/url/price from site files
mono_site=litr(os.path.join(TMP,'mono_tool_p1.json'))+litr(os.path.join(TMP,'mono_tool_p2.json'))
for d in mono_site:
    if d['code']:
        av[d['code']]=d['status']; url[d['code']]=d['url']

cur=[]
# ---- LEIYA ----
site,sup,hits=pickle.load(open(os.path.join(TMP,'xref2.pkl'),'rb'))
def art_ly(s):
    m=re.search(r'LY[-\s]?[A-Z0-9]+',s.upper()); return m.group(0) if m else ''
for su,si in hits:
    code=si.get('code',''); sp=round(su['price']); vp=si['price']; status=av.get(code,'')
    if code in VI_FRESH:
        fp=VI_FRESH[code].get('price'); 
        if fp: vp=fp
        status=VI_FRESH[code].get('status',status) or status
        if VI_FRESH[code].get('url'): url[code]=VI_FRESH[code]['url']
    dd,dt=parse_delivery(status)
    cur.append(dict(sect='LEIYA',art=art_ly(su.get('art','') or su.get('name','')),sup_name=su.get('name',''),
        sup_price=sp,sup_unit=su.get('unit',''),vi_name=si.get('name',''),vi_price=vp,
        diff=round((sp-vp)/vp*100),status=status,dd=dd,dtxt=dt,url=url.get(code,''),code=code,flag=''))
# ---- MO-L ----
mol_sup=json.load(open(os.path.join(TMP,'mol_sup.json')))
mol_site={50:('Прожектор светодиодный Monolit MO-L 50 (50W) 6500К 0122',589,'39880677'),100:('Прожектор светодиодный Monolit MO-L 100 (100W) 6500К 0115',895,'39880665'),200:('Прожектор светодиодный Monolit MO-L 200 (200W) 6500К 1192',2344,'39880683'),300:('Прожектор светодиодный Monolit MO-L 300 (300W) 6500К 1026',4033,'39880689'),500:('Прожектор светодиодный Monolit MO-L 500 (500W) 6500К 1193',12150,'40157597')}
for s in mol_sup:
    w=int(re.search(r'MO-L\s*(\d+)',s['name']).group(1)); sp=round(s['price'],2)
    if w in mol_site:
        nm,vp,code=mol_site[w]; status=av.get(code,'')
        if code in VI_FRESH:
            fp=VI_FRESH[code].get('price')
            if fp: vp=fp
            status=VI_FRESH[code].get('status',status) or status
            if VI_FRESH[code].get('url'): url[code]=VI_FRESH[code]['url']
        dd,dt=parse_delivery(status)
        cur.append(dict(sect='MO-L',art=f'MO-L {w}',sup_name=s['name'],sup_price=sp,sup_unit='шт',vi_name=nm,vi_price=vp,
            diff=round((sp-vp)/vp*100),status=status,dd=dd,dtxt=dt,url=url.get(code,''),code=code,flag=''))
    else:
        cur.append(dict(sect='MO-L',art=f'MO-L {w}',sup_name=s['name'],sup_price=sp,sup_unit='шт',vi_name='не найден на ВсеИнструменты',vi_price=None,diff=None,status='',dd=None,dtxt='не найден',url='',code='',flag=''))
# ---- Monolit tools ----
mono_sup=json.load(open(os.path.join(TMP,'mono_tool_sup.json')))
supcode={str(s['code']).strip():s for s in mono_sup}
def art_tail(n):
    nums=re.findall(r'(?<!\d)(\d{3,5})(?!\d)',n); return nums[-1] if nums else None
TYPE=['пистолет','молоток','топор','уровень','рулетка','бучарда','пика','зубило','кисть','лопата','ведро','нож','маркер','карандаш','дюбель','замок','гайка','винт','стрейч','скотч','лента','лом','кельма','шпатель','валик','терка','тёрка','правило','пена','клин','штроборез','бур','коронка','сверло','чашка','диск','ковш','совок','мастерок','гвоздь','изолента','пленка','плёнка','пила']
SYN={'скотч','лента','клейкая','изолента'}
def tw(n):
    n=n.lower()
    for t in TYPE:
        if t in n: return t
    return None
for it in mono_site:
    a=art_tail(it['name'])
    if not a or a not in supcode: continue
    s=supcode[a]; t1,t2=tw(it['name']),tw(s['name'])
    if t1 and t2 and t1!=t2 and not({t1,t2}<=SYN): continue
    vp=price_int(it['cur']); sp=round(s['price'],2); dd,dt=parse_delivery(it['status'])
    diff=round((sp-vp)/vp*100) if vp else None
    flag='⚠ проверить ед. изм.' if (diff is not None and diff<=-70) else ''
    cur.append(dict(sect='MONO',art=a,sup_name=s['name'],sup_price=sp,sup_unit=s.get('unit',''),vi_name=it['name'],
        vi_price=vp,diff=diff,status=it['status'],dd=dd,dtxt=dt,url=it['url'],code=it['code'],flag=flag))

# ===== history (idempotent per day) =====
hist=json.load(open(HIST)) if os.path.exists(HIST) else {}
def dyn(code,cp,cdd,cdt):
    recs=hist.get(code,[]); prev=[r for r in recs if r['date']!=DATE]
    if not prev: return 'первый замер','первый замер','-'
    p=prev[-1]
    if cp is None or p.get('vi_price') is None: pr='-'
    elif cp<p['vi_price']: pr=f"↓ дешевле на {round((p['vi_price']-cp)/p['vi_price']*100)}% (было {p['vi_price']})"
    elif cp>p['vi_price']: pr=f"↑ дороже на {round((cp-p['vi_price'])/p['vi_price']*100)}% (было {p['vi_price']})"
    else: pr='- без изменений'
    def nz(d): return 9999 if d is None else d
    cd,pd=nz(cdd),nz(p.get('dd'))
    if cd<pd: dl=f"↓ быстрее (было: {p.get('dtxt')}"+(f", {p.get('dd')} дн.)" if p.get('dd') is not None else ")")
    elif cd>pd: dl=f"↑ дольше (было: {p.get('dtxt')}"+(f", {p.get('dd')} дн.)" if p.get('dd') is not None else ")")
    else: dl='- без изменений'
    sc=0
    if cp is not None and p.get('vi_price') is not None: sc+=-1 if cp<p['vi_price'] else(1 if cp>p['vi_price'] else 0)
    sc+=-1 if cd<pd else(1 if cd>pd else 0)
    return pr,dl,('ЛУЧШЕ' if sc<0 else('ХУЖЕ' if sc>0 else 'без изменений'))
for r in cur:
    r['dyn_price'],r['dyn_deliv'],r['verdict']=dyn(r['code'],r['vi_price'],r['dd'],r['dtxt'])
    if r['code']:
        hist.setdefault(r['code'],[])
        hist[r['code']]=[x for x in hist[r['code']] if x['date']!=DATE]  # drop today dupes
        hist[r['code']].append(dict(date=DATE,vi_price=r['vi_price'],status=r['status'],dd=r['dd'],dtxt=r['dtxt'],vi_name=r['vi_name'],url=r['url'],sup_price=r['sup_price'],art=r['art']))
json.dump(hist,open(HIST,'w'),ensure_ascii=False,indent=1)

# ===== Excel =====
def fmt_dd(r):
    if r['dtxt']=='в наличии': return 'В наличии (0 дн.)'
    if r['dtxt']=='под заказ': return f"Под заказ (~{r['dd']} дн.)"
    if r['dtxt']=='нет в наличии': return 'Нет в наличии'
    return '-'
wb=Workbook(); ws=wb.active; ws.title='Сравнение цен'
hdr=['Артикул','Наименование (МОНОЛИТ РУС)','Цена МОНОЛИТ РУС, ₽','Ед.','Наименование на ВсеИнструменты','Цена ВИ, ₽','Разница %','Наличие на ВсеИнструменты','Срок поставки','Динамика цены ВИ','Динамика срока','Итог vs прошлый замер','Ссылка на товар','Код ВИ']
hfill=PatternFill('solid',fgColor='2F5496');hfont=Font(bold=True,color='FFFFFF',size=10)
sec=PatternFill('solid',fgColor='D9E1F2');secf=Font(bold=True,size=11)
thin=Side(style='thin',color='BBBBBB');bd=Border(left=thin,right=thin,top=thin,bottom=thin)
r=1
ws.cell(r,1,'Сравнение цен поставщика МОНОЛИТ РУС и ВсеИнструменты (vseinstrumenti.ru, Москва)').font=Font(bold=True,size=13);r+=1
ws.cell(r,1,f'Замер {DATE}. Цены/наличие ВИ - базовая розница. Отрицательная разница = поставщик дешевле. «⚠ проверить ед. изм.» - цена поставщика за шт. из упаковки, сравнение с розничной шт. может быть некорректным. Динамика - к предыдущему замеру (история ведётся автоматически).').font=Font(italic=True,size=9,color='666666');r+=2
def cdyn(c,v):
    if isinstance(v,str):
        if v.startswith('↓') or v=='ЛУЧШЕ': c.font=Font(color='006100',bold=(v=='ЛУЧШЕ'))
        elif v.startswith('↑') or v=='ХУЖЕ': c.font=Font(color='C00000',bold=(v=='ХУЖЕ'))
        elif 'первый замер' in v or 'без изменений' in v or v.startswith('='): c.font=Font(color='808080')
def section(title,rows):
    global r
    ws.cell(r,1,title).font=secf
    for c in range(1,len(hdr)+1): ws.cell(r,c).fill=sec
    r+=1
    for c,h in enumerate(hdr,1):
        cell=ws.cell(r,c,h);cell.fill=hfill;cell.font=hfont;cell.border=bd;cell.alignment=Alignment(wrap_text=True,vertical='center',horizontal='center')
    r+=1
    for row in rows:
        diff_txt=(f"{row['diff']}%" if row['diff'] is not None else '')
        vals=[row['art'],row['sup_name'],row['sup_price'],row.get('sup_unit',''),row['vi_name'],row['vi_price'],diff_txt,row['status'],fmt_dd(row),row['dyn_price'],row['dyn_deliv'],(row['verdict']+(('  '+row['flag']) if row['flag'] else '')),'',row['code']]
        for c,v in enumerate(vals,1):
            cell=ws.cell(r,c,v);cell.border=bd;cell.alignment=Alignment(vertical='center',wrap_text=(c in(2,5,8,10,11,12)))
            if c==7 and isinstance(v,str) and v.endswith('%'): cell.font=Font(color='C00000' if v.startswith('-') else '006100',bold=True)
            if c==8 and isinstance(v,str):
                if 'Нет в наличии' in v: cell.font=Font(color='C00000')
                elif 'от поставщика' in v: cell.font=Font(color='BF8F00')
                elif 'склад' in v or 'магазин' in v: cell.font=Font(color='006100')
            if c in(10,11,12): cdyn(cell,v)
        if row['url']:
            lc=ws.cell(r,13,'открыть ↗');lc.hyperlink=row['url'];lc.font=Font(color='0563C1',underline='single');lc.border=bd
        r+=1
    r+=1
leiya=[x for x in cur if x['sect']=='LEIYA']; mol=[x for x in cur if x['sect']=='MO-L']; mono=[x for x in cur if x['sect']=='MONO']
for g in (leiya,mol,mono): g.sort(key=lambda x:x['diff'] if x['diff'] is not None else 999)
section('ЭЛЕКТРОИНСТРУМЕНТ LEIYA',leiya)
section('ПРОЖЕКТОРЫ Monolit MO-L',mol)
section('ИНСТРУМЕНТ Monolit (собственный бренд)',mono)
for c,w in enumerate([12,40,16,6,40,11,9,28,18,26,26,24,13,10],1):
    ws.column_dimensions[chr(64+c)].width=w
ws.freeze_panes='A6'

# ===== Сводный индекс (общая температура по наличию) =====
from collections import defaultdict
byd=defaultdict(list)
for code,recs in hist.items():
    for rec in recs: byd[rec['date']].append(rec)
agg=[]
for d in sorted(byd):
    rs=byd[d]; n=len(rs)
    ins=sum(1 for x in rs if x.get('dd')==0)
    order=sum(1 for x in rs if x.get('dd') is not None and x.get('dd')>0)
    none=sum(1 for x in rs if x.get('dd') is None)
    dds=[x['dd'] for x in rs if x.get('dd') is not None]
    diffs=[(x['sup_price']-x['vi_price'])/x['vi_price']*100 for x in rs if x.get('vi_price') and x.get('sup_price')]
    agg.append(dict(date=d,n=n,
        idx=(100*ins+50*order)/n if n else 0,
        p_ins=ins/n*100 if n else 0, p_order=order/n*100 if n else 0, p_none=none/n*100 if n else 0,
        avgdd=sum(dds)/len(dds) if dds else 0,
        avgdiff=sum(diffs)/len(diffs) if diffs else 0))
cu=agg[-1]; pv=agg[-2] if len(agg)>=2 else None

sm=wb.create_sheet('Сводный индекс')
GRN='006100';RED='C00000';GRY='808080'
def delta_cell(cur_v,prev_v,lower_better,fmt='{:+.1f}'):
    if prev_v is None: return 'первый замер',GRY
    d=cur_v-prev_v
    if abs(d)<1e-9: return '- без изменений',GRY
    good=(d<0) if lower_better else (d>0)
    arrow='↓' if d<0 else '↑'
    return f"{arrow} {fmt.format(d)}",(GRN if good else RED)
R=1
sm.cell(R,1,'Сводный индекс наличия и цен - МОНОЛИТ РУС vs ВсеИнструменты').font=Font(bold=True,size=13);R+=1
sm.cell(R,1,f'Замер {DATE}. Индекс наличия 0–100: «в наличии»=100, «под заказ»=50, «нет в наличии»=0 (выше - лучше). Δ - изменение к предыдущему замеру; история и графики заполняются автоматически при каждом запуске.').font=Font(italic=True,size=9,color='666666');R+=2
# dashboard
dhdr=['Показатель','Текущий замер','Прошлый замер','Δ к прошлому','Куда лучше']
hfill2=PatternFill('solid',fgColor='2F5496')
for c,h in enumerate(dhdr,1):
    cell=sm.cell(R,c,h);cell.fill=hfill2;cell.font=Font(bold=True,color='FFFFFF');cell.border=bd;cell.alignment=Alignment(horizontal='center',wrap_text=True)
R+=1
def pf(v): return f"{v:.1f}"
rows_db=[
 ('Индекс наличия (0–100)', cu['idx'], pv['idx'] if pv else None, pf(cu['idx']), False, 'выше'),
 ('Доля «в наличии», %', cu['p_ins'], pv['p_ins'] if pv else None, f"{cu['p_ins']:.0f}%", False, 'выше'),
 ('Доля «под заказ», %', cu['p_order'], pv['p_order'] if pv else None, f"{cu['p_order']:.0f}%", None, '-'),
 ('Доля «нет в наличии», %', cu['p_none'], pv['p_none'] if pv else None, f"{cu['p_none']:.0f}%", True, 'ниже'),
 ('Средний срок поставки, дн.', cu['avgdd'], pv['avgdd'] if pv else None, f"{cu['avgdd']:.1f}", True, 'ниже'),
 ('Средняя разница цены vs ВИ, %', cu['avgdiff'], pv['avgdiff'] if pv else None, f"{cu['avgdiff']:.1f}%", True, 'ниже (поставщик дешевле)'),
]
for name,cv,prv,curtxt,lb,direction in rows_db:
    sm.cell(R,1,name).border=bd
    sm.cell(R,2,curtxt).border=bd
    sm.cell(R,3,('%.1f'%prv if prv is not None else '-')).border=bd
    if lb is None:
        dtxt,col=('первый замер' if prv is None else f"{cv-prv:+.1f}"),GRY
    else:
        dtxt,col=delta_cell(cv,prv,lb,'{:+.1f}%' if '%' in curtxt else '{:+.1f}')
    dc=sm.cell(R,4,dtxt);dc.border=bd;dc.font=Font(color=col,bold=True)
    sm.cell(R,5,direction).border=bd
    R+=1
R+=1
# overall verdict
if pv is None:
    verdict='Первый замер - это базовая точка. Со следующего запуска здесь появится оценка «ситуация улучшилась / ухудшилась».';vcol=GRY
else:
    sc=0
    sc+= -1 if cu['idx']>pv['idx'] else(1 if cu['idx']<pv['idx'] else 0)
    sc+= -1 if cu['avgdd']<pv['avgdd'] else(1 if cu['avgdd']>pv['avgdd'] else 0)
    sc+= -1 if cu['p_none']<pv['p_none'] else(1 if cu['p_none']>pv['p_none'] else 0)
    verdict=('ОБЩАЯ СИТУАЦИЯ ПО НАЛИЧИЮ УЛУЧШИЛАСЬ' if sc<0 else('ОБЩАЯ СИТУАЦИЯ ПО НАЛИЧИЮ УХУДШИЛАСЬ' if sc>0 else 'ОБЩАЯ СИТУАЦИЯ БЕЗ СУЩЕСТВЕННЫХ ИЗМЕНЕНИЙ'))
    vcol=(GRN if sc<0 else(RED if sc>0 else GRY))
sm.cell(R,1,verdict).font=Font(bold=True,size=11,color=vcol);R+=2
# per-date data table (charts reference it)
sm.cell(R,1,'Динамика по замерам').font=Font(bold=True,size=11);R+=1
thdr=['Дата','Индекс наличия','Доля в наличии, %','Доля под заказ, %','Доля нет в наличии, %','Ср. срок, дн.','Ср. разница цены, %']
thead_row=R
for c,h in enumerate(thdr,1):
    cell=sm.cell(R,c,h);cell.fill=hfill2;cell.font=Font(bold=True,color='FFFFFF',size=9);cell.border=bd;cell.alignment=Alignment(horizontal='center',wrap_text=True)
R+=1
data_first=R
for a in agg:
    for c,v in enumerate([a['date'],round(a['idx'],1),round(a['p_ins'],1),round(a['p_order'],1),round(a['p_none'],1),round(a['avgdd'],1),round(a['avgdiff'],1)],1):
        sm.cell(R,c,v).border=bd
    R+=1
data_last=R-1
for c,w in enumerate([16,16,17,17,19,13,19],1): sm.column_dimensions[chr(64+c)].width=w
# charts
cats=Reference(sm,min_col=1,min_row=data_first,max_row=data_last)
ch1=LineChart();ch1.title='Индекс наличия (0–100)';ch1.height=7;ch1.width=14;ch1.y_axis.scaling.min=0;ch1.y_axis.scaling.max=100
ch1.add_data(Reference(sm,min_col=2,min_row=thead_row,max_row=data_last),titles_from_data=True);ch1.set_categories(cats)
sm.add_chart(ch1,f'A{data_last+3}')
ch2=LineChart();ch2.title='Структура наличия, %';ch2.height=7;ch2.width=14
ch2.add_data(Reference(sm,min_col=3,max_col=5,min_row=thead_row,max_row=data_last),titles_from_data=True);ch2.set_categories(cats)
sm.add_chart(ch2,f'A{data_last+18}')
ch3=LineChart();ch3.title='Средний срок поставки, дн. (ниже - лучше)';ch3.height=7;ch3.width=14
ch3.add_data(Reference(sm,min_col=6,min_row=thead_row,max_row=data_last),titles_from_data=True);ch3.set_categories(cats)
sm.add_chart(ch3,f'J{data_last+3}')

# History sheet
hs=wb.create_sheet('История замеров')
hh=['Дата замера','Код ВИ','Артикул','Наименование ВИ','Цена ВИ, ₽','Наличие','Срок, дн.','Цена МОНОЛИТ РУС, ₽']
for c,h in enumerate(hh,1):
    cell=hs.cell(1,c,h);cell.fill=hfill;cell.font=hfont;cell.border=bd;cell.alignment=Alignment(wrap_text=True,horizontal='center')
rr=2
for code,recs in hist.items():
    for rec in recs:
        for c,v in enumerate([rec['date'],code,rec.get('art',''),rec.get('vi_name',''),rec.get('vi_price'),rec.get('dtxt',''),rec.get('dd'),rec.get('sup_price')],1):
            hs.cell(rr,c,v).border=bd
        rr+=1
for c,w in enumerate([13,11,13,44,12,16,9,20],1): hs.column_dimensions[chr(64+c)].width=w
hs.freeze_panes='A2'
out=str(XLSX)
wb.save(out)

# ===== summary.json - для сопроводительного письма =====
def _imp(x):
    return dict(name=x.get('vi_name',''), art=x.get('art',''),
                dyn_price=x.get('dyn_price',''), dyn_deliv=x.get('dyn_deliv',''))
improved=[_imp(x) for x in cur if x.get('verdict')=='ЛУЧШЕ']
worsened=[_imp(x) for x in cur if x.get('verdict')=='ХУЖЕ']
summary=dict(
    date=DATE, has_prev=(pv is not None),
    n_leiya=len(leiya), n_mol=len(mol), n_mono=len(mono),
    n_total=len(cur), n_flags=sum(1 for x in mono if x['flag']),
    verdict=verdict,
    cur=dict(idx=round(cu['idx'],1), p_ins=round(cu['p_ins'],0), p_order=round(cu['p_order'],0),
             p_none=round(cu['p_none'],0), avgdd=round(cu['avgdd'],1), avgdiff=round(cu['avgdiff'],1)),
    prev=(dict(idx=round(pv['idx'],1), p_ins=round(pv['p_ins'],0), p_none=round(pv['p_none'],0),
               avgdd=round(pv['avgdd'],1), avgdiff=round(pv['avgdiff'],1)) if pv else None),
    n_improved=len(improved), n_worsened=len(worsened),
    improved=improved[:15], worsened=worsened[:15])
json.dump(summary, open(os.path.join(REP,'summary.json'),'w'), ensure_ascii=False, indent=1)

print('SAVED',out); print('LEIYA',len(leiya),'MO-L',len(mol),'MONO',len(mono),'| history codes',len(hist))
print('flags:',sum(1 for x in mono if x['flag']))
print('summary: improved',len(improved),'worsened',len(worsened))
