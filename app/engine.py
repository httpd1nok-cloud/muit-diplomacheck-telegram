"""Preliminary rules-based verification of DOCX/PDF, bilingual output.

Not a university-certified norm control, anti-plagiarism system, or academic grading engine.
Only unequivocal checks count toward the diagnostic compliance percentage.
"""
from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / 'config' / 'rules.json').read_text(encoding='utf-8'))
MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 400
MAX_UNPACKED = 100 * 1024 * 1024


class InvalidDocument(ValueError):
    pass


TITLES = {
    'cover':('Обложка','Мұқаба'), 'title':('Титульный лист','Титулдық бет'),
    'assignment':('Задание','Тапсырма'), 'calendar':('Календарный план','Күнтізбелік жоспар'),
    'annotations':('Аннотации на 3 языках','Үш тілдегі аннотация'), 'toc':('Содержание','Мазмұн'),
    'intro':('Введение','Кіріспе'), 'main':('Основная часть','Негізгі бөлім'),
    'conclusion':('Заключение','Қорытынды'), 'references':('Список литературы','Әдебиеттер тізімі'),
    'appendix':('Приложения','Қосымшалар'), 'margins':('Поля страницы','Бет жиектері'),
    'font':('Шрифт и размер','Қаріп пен өлшемі'), 'spacing':('Межстрочный интервал','Жоларалық интервал'),
    'pages':('Объём работы','Жұмыс көлемі'), 'page_format':('Формат страниц','Бет пішімі'),
    'intro_len':('Объём введения','Кіріспе көлемі'), 'conclusion_len':('Объём заключения','Қорытынды көлемі'),
    'ieee':('Ссылки и IEEE','Сілтемелер және IEEE'), 'figures':('Ссылки на рисунки','Суреттерге сілтемелер'),
    'tables':('Ссылки на таблицы','Кестелерге сілтемелер')
}

HEADINGS = {
 'assignment':r'(?:задание\s+(?:на\s+)?диплом\w*(?:\s+(?:работ\w*|проект\w*))?|диплом\w*\s+тапсырм\w*|дипломдық\s+жұмыс(?:қа)?\s+тапсырма)',
 'calendar':r'(?:календарный\s+(?:план|график)|күнтізбелік\s+жоспар)',
 'ann_kk':r'(?:аңдатпа|түйіндеме)', 'ann_ru':r'(?:аннотация|реферат)', 'ann_en':r'(?:abstract|annotation)',
 'toc':r'(?:содержание|мазмұны|мазмұн|contents)',
 'intro':r'(?:введение|кіріспе|introduction)',
 'conclusion':r'(?:заключение|қорытынды|выводы|conclusion)',
 'references':r'(?:список\s+(?:использованной|используемой)?\s*литературы|список\s+литературы|использованные\s+источники|пайдаланылған\s+әдебиеттер(?:\s+тізімі)?|әдебиеттер\s+тізімі|references|bibliography)',
 'appendix':r'(?:приложени\w*(?:\s+[а-яa-z0-9])?|қосымшалар?(?:\s+[а-яa-z0-9])?|appendix(?:\s+[a-z0-9])?)'
}


def _clean(text: str) -> str:
    return re.sub(r'\s+',' ',text.strip(' \n\t:.-–—')).lower()


def _match_heading(line: str, pat: str) -> bool:
    return len(line) <= 110 and not re.search(r'\.{3,}\s*\d+\s*$',line) and bool(re.fullmatch(pat,_clean(line),flags=re.I))


def _font_of(para, run):
    if run.font.name and run.font.size:
        return (run.font.name, run.font.size.pt)
    for sty in (getattr(run,'style',None), para.style):
        for _ in range(10):
            if sty is None: break
            font = getattr(sty,'font',None)
            if font and font.name and font.size:
                return (run.font.name or font.name, run.font.size.pt if run.font.size else font.size.pt)
            sty = getattr(sty,'base_style',None)
    return (run.font.name or None,run.font.size.pt if run.font.size else None)


def _spacing(para):
    v=para.paragraph_format.line_spacing
    if v is not None:
        return v if isinstance(v,(int,float)) else None
    sty=para.style
    for _ in range(10):
        if sty is None:break
        v=sty.paragraph_format.line_spacing
        if v is not None:
            return v if isinstance(v,(int,float)) else None
        sty=sty.base_style
    return None


@dataclass
class Evidence:
    kind: str
    lines: list[str] = field(default_factory=list)
    page_texts: list[str] = field(default_factory=list)
    margins: list[dict] = field(default_factory=list)
    fonts: list[dict] = field(default_factory=list)
    spacings: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract(path: Path, name: str) -> Evidence:
    suffix = Path(name).suffix.lower()
    if suffix not in ('.docx','.pdf'):
        raise InvalidDocument('Only DOCX or PDF are supported.')
    if path.stat().st_size < 100 or path.stat().st_size > MAX_BYTES:
        raise InvalidDocument('File is empty or too large (20 MB maximum).')
    if suffix == '.docx':
        try:
            with zipfile.ZipFile(path) as z:
                names=z.namelist()
                if 'word/document.xml' not in names or '[Content_Types].xml' not in names:
                    raise InvalidDocument('Not a valid Word DOCX file.')
                if sum(i.file_size for i in z.infolist())>MAX_UNPACKED:
                    raise InvalidDocument('DOCX uncompressed size is too large.')
                if z.testzip() is not None:
                    raise InvalidDocument('DOCX ZIP archive is damaged.')
            doc=Document(path)
        except (zipfile.BadZipFile,OSError,ValueError) as exc:
            raise InvalidDocument('Unable to open DOCX.') from exc
        ev=Evidence(kind='docx')
        for sec in doc.sections:
            ev.margins.append({k:round(getattr(sec,k+'_margin').mm,2) for k in ('left','right','top','bottom')})
        # Main paragraphs + cell text, but format samples only from main non-heading paragraphs.
        for p in doc.paragraphs:
            v=p.text.strip()
            if not v: continue
            ev.lines.append(v)
            style=p.style.name.lower() if p.style else ''
            if len(v)<18 or 'heading' in style or 'заголов' in style: continue
            for run in p.runs:
                if run.text.strip():
                    font,size=_font_of(p,run)
                    ev.fonts.append({'name':font,'size':size,'chars':len(run.text.strip())})
            gap=_spacing(p)
            if gap is not None: ev.spacings.append({'line':gap,'chars':len(v)})
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    ev.lines.extend(p.text.strip() for p in cell.paragraphs if p.text.strip())
        return ev
    with path.open('rb') as f:
        if f.read(5)!=b'%PDF-':
            raise InvalidDocument('Invalid PDF signature.')
    try:
        pdf=fitz.open(str(path))
    except Exception as exc:
        raise InvalidDocument('Unable to open PDF.') from exc
    with pdf:
        if pdf.is_encrypted: raise InvalidDocument('Password-protected PDF not supported.')
        if len(pdf)>MAX_PAGES: raise InvalidDocument('PDF has more than 400 pages.')
        ev=Evidence(kind='pdf')
        for page in pdf:
            text=page.get_text('text',sort=True)
            ev.page_texts.append(text)
            ev.lines.extend(x.strip() for x in text.splitlines() if x.strip())
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines',[]):
                    for span in line.get('spans',[]):
                        if len(span['text'].strip())>=3:
                            ev.fonts.append({'name':span['font'],'size':span['size'],'chars':len(span['text'].strip())})
        if sum(len(t.strip()) for t in ev.page_texts)<100:
            ev.warnings.append('PDF may be a scan; OCR is not included.')
        return ev


def _status_message(lang: str, status: str) -> str:
    return ({'pass':'Талапқа сай','fail':'Сәйкес емес','warning':'Нақтылау керек','manual':'Қолмен тексеру керек'} if lang=='kk' else
            {'pass':'Соответствует','fail':'Не соответствует','warning':'Нужна проверка','manual':'Проверить вручную'})[status]


def analyze(path: Path, filename: str, lang: str = 'ru') -> dict:
    lang='kk' if lang=='kk' else 'ru'
    ev=extract(path,filename)
    lines=ev.lines
    checks=[]
    def add(key: str, ref: str, status: str, ru: str, kk: str):
        title=TITLES[key][1 if lang=='kk' else 0]
        checks.append({'id':key,'title':title,'ref':ref,'status':status,
                       'status_label':_status_message(lang,status),'detail':kk if lang=='kk' else ru})
    hits={k:[] for k in HEADINGS}
    for i,line in enumerate(lines):
        for k,p in HEADINGS.items():
            if _match_heading(line,p):hits[k].append(i)
    has_diplom=bool(re.search(r'диплом|дипломдық|diploma', '\n'.join(lines[:40]),re.I))
    add('cover','6.5–6.6','manual','Проверьте обложку вручную: организация, автор, тема, специальность, город, год.',
        'Мұқабаны қолмен тексеріңіз: университет, автор, тақырып, мамандық, қала, жыл.')
    add('title','6.5–6.7','warning' if not has_diplom else 'pass',
        'В начале найдено упоминание дипломной работы; реквизиты титульного листа стоит проверить вручную.' if has_diplom else 'Титульный лист не удалось обнаружить уверенно.',
        'Құжат басында диплом тақырыбы байқалды; титулдық беттің деректемелерін қолмен тексеріңіз.' if has_diplom else 'Титулдық бет анық табылмады.')
    required=[('assignment','6.8'),('calendar','6.9'),('toc','6.11'),('intro','6.12'),('conclusion','6.18'),('references','6.19'),('appendix','6.20')]
    for k,ref in required:
        found=bool(hits[k]); add(k,ref,'pass' if found else 'fail',
           'Заголовок раздела найден.' if found else 'Заголовок обязательного раздела не найден (возможна нестандартная формулировка).',
           'Бөлімнің атауы табылды.' if found else 'Міндетті бөлім атауы табылмады (өзгеше атау болуы мүмкін).')
    ann=[x for x in ('ann_kk','ann_ru','ann_en') if hits[x]]
    add('annotations','6.10','pass' if len(ann)==3 else 'fail',
        f'Обнаружены заголовки аннотаций: {len(ann)} из 3 (казахский, русский, английский).',
        f'Анықталған аннотация атаулары: {len(ann)}/3 (қазақша, орысша, ағылшынша).')
    intro_i=hits['intro'][-1] if hits['intro'] else -1
    conc_i=hits['conclusion'][-1] if hits['conclusion'] else len(lines)
    main_headings=[x for i,x in enumerate(lines) if intro_i<i<conc_i and len(x)<110 and re.match(r'^\s*(?:\d{1,2}\s+[А-Яа-яA-Za-zӘІҢҒҮҰҚӨҺәіңғүұқөһЁё]|ГЛАВА\s+\d+|ТАРАУ\s+\d+)',x)]
    add('main','6.13–6.16','pass' if main_headings else 'warning',
        f'Нумерованных глав найдено: {len(main_headings)}.' if main_headings else 'Не удалось уверенно обнаружить нумерованные главы.',
        f'Нөмірленген тараулар саны: {len(main_headings)}.' if main_headings else 'Нөмірленген тараулар анық табылмады.')
    expected=RULES['margins_mm']
    if ev.kind=='docx':
        wrong=[f'{k}={m[k]:.1f} мм' for m in ev.margins for k in expected if abs(m[k]-expected[k])>0.7]
        add('margins','7.2','fail' if wrong else 'pass',
            'Неверные поля: '+', '.join(wrong[:6]) if wrong else 'Поля DOCX: 30/10/20/25 мм (левое/правое/верхнее/нижнее).',
            'Қате жиектер: '+', '.join(wrong[:6]) if wrong else 'DOCX жиектері: 30/10/20/25 мм (сол/оң/жоғары/төмен).')
        known=[v for v in ev.fonts if v['name'] and v['size'] is not None]
        total=sum(s['chars'] for s in known)
        wrong_count=sum(s['chars'] for s in known if 'timesnewroman' not in s['name'].lower().replace(' ','') or abs(s['size']-14)>.5)
        rate=round(100*wrong_count/total) if total else None
        fontstatus='warning' if rate is None else ('fail' if rate>5 else 'pass')
        add('font','7.1',fontstatus,
            f'Несоответствие Times New Roman 14 пт: {rate}% основного текста.' if rate is not None else 'Размер/имя шрифта не удалось установить.',
            f'Times New Roman 14 пт талабына сәйкес емес мәтін үлесі: {rate}%.' if rate is not None else 'Қаріптің атауы/өлшемі анықталмады.')
        known_sp=ev.spacings
        total=sum(v['chars'] for v in known_sp)
        gap_bad=sum(v['chars'] for v in known_sp if abs(v['line']-1.0)>.03)
        gap_rate=round(100*gap_bad/total) if total else None
        add('spacing','7.1','warning' if gap_rate is None else ('fail' if gap_rate>5 else 'pass'),
            f'Доля текста с не-одинарным интервалом: {gap_rate}%.' if gap_rate is not None else 'Интервал из стилей не удалось подтвердить.',
            f'Бірлік емес интервалмен жазылған мәтін үлесі: {gap_rate}%.' if gap_rate is not None else 'Стильден интервал мәні анықталмады.')
        add('pages','6.4','manual','DOCX не хранит достоверное число страниц. Загрузите PDF для контроля объёма.',
            'DOCX ішінде нақты бет саны сақталмайды. Көлемін тексеру үшін PDF жіберіңіз.')
        add('page_format','7.1','manual','Размер листа в DOCX лучше подтвердить в Word.',
            'DOCX парағының өлшемін Word-та қосымша тексеріңіз.')
        add('intro_len','6.12','manual','Границы страниц неизвестны. Введение — максимум 2 страницы.',
            'Бет шекаралары белгісіз. Кіріспе — ең көбі 2 бет.')
        add('conclusion_len','6.18','manual','Границы страниц неизвестны. Заключение — максимум 1 страница.',
            'Бет шекаралары белгісіз. Қорытынды — ең көбі 1 бет.')
    else:
        count=len(ev.page_texts)
        appendix_page=None
        if hits['appendix']:
            for p,text in enumerate(ev.page_texts,1):
                if re.search(r'(?m)^\s*(?:приложени\w*|қосымшалар?|appendix)(?:\s+\w+)?\s*$',text,re.I):
                    appendix_page=p
                    break
        body_pages=appendix_page-1 if appendix_page else count
        add('pages','6.4','pass' if body_pages>=60 else 'fail',
            f'Примерно {body_pages} страниц без приложений; нужно не менее 60. Всего PDF-страниц: {count}.',
            f'Қосымшасыз шамамен {body_pages} бет; кемінде 60 бет қажет. PDF-та барлығы {count} бет.')
        add('page_format','7.1','warning','Геометрия листов в PDF требует отдельной проверки; поля по PDF могут быть неточными.',
            'PDF парағының өлшемі мен жиектерін қосымша тексеру керек.')
        add('margins','7.2','manual','Поля по одному PDF достоверно проверить нельзя.',
            'PDF арқылы бет жиектерін дәл тексеру мүмкін емес.')
        add('font','7.1','warning','PDF шрифты могут быть заменены при экспорте; лучше загрузить DOCX для проверки Times New Roman 14 пт.',
            'Экспорт кезінде PDF қаріптері ауысуы мүмкін; Times New Roman 14 пт талаптарын DOCX арқылы тексеріңіз.')
        add('spacing','7.1','manual','Исходный межстрочный интервал PDF не хранит. Проверьте в DOCX.',
            'PDF бастапқы жоларалық интервалды сақтамайды. DOCX арқылы тексеріңіз.')
        # A heading can share a page with the next chapter. Report a range conservatively.
        page_map={}
        for k in ('intro','conclusion','references'):
            if hits[k]:
                pat=HEADINGS[k]
                for p,text in enumerate(ev.page_texts,1):
                    if any(_match_heading(line,pat) for line in text.splitlines()):
                        page_map[k]=p
                        break
        for check, ref, start, end, maxpages in (
             ('intro_len','6.12','intro','conclusion',2),('conclusion_len','6.18','conclusion','references',1)):
            # For introduction, next section must be the first numbered chapter, not conclusion.
            if check=='intro_len':
                startpage=page_map.get('intro')
                endpage=None
                if startpage:
                    for p in range(startpage,len(ev.page_texts)+1):
                        if any(re.match(r'^\s*1\s+[А-Яа-яA-Za-zӘІҢҒҮҰҚӨҺәіңғүұқөһЁё]',x.strip()) for x in ev.page_texts[p-1].splitlines()):
                            endpage=p;break
            else:
                startpage=page_map.get(start)
                endpage=page_map.get(end)
            if startpage and endpage and endpage>=startpage:
                length=endpage-startpage
                st='fail' if length>maxpages else 'warning'
                add(check,ref,st,
                    f'Начало на стр. {startpage}, следующий раздел на стр. {endpage}. Оценка объёма: {length}–{length+1} стр., максимум {maxpages}. Проверьте переходную страницу.',
                    f'{startpage}-беттен басталады, келесі бөлім {endpage}-бетте. Шамамен {length}–{length+1} бет, ең көбі {maxpages}. Шекара бетін тексеріңіз.')
            else:
                add(check,ref,'manual','Не удалось уверенно определить границы страниц разделов.',
                    'Бөлімдердің бет шекарасы дәл анықталмады.')
    # IEEE numbered citations: search only body before the bibliography where possible.
    ref_pos=hits['references'][-1] if hits['references'] else None
    body='\n'.join(lines[:ref_pos]) if ref_pos is not None else '\n'.join(lines)
    bib='\n'.join(lines[ref_pos+1:]) if ref_pos is not None else ''
    citations=re.findall(r'\[(\d+)\]',body)
    entries=re.findall(r'(?m)^\s*\[(\d+)\]\s+',bib)
    missing=sorted(set(citations)-set(entries))
    if missing:
        ieee_status='fail'
    elif citations and entries:
        ieee_status='warning' # bibliography fields not fully validated
    else:
        ieee_status='warning'
    add('ieee','6.19, 7.39',ieee_status,
        f'Ссылок [n] в тексте: {len(citations)}, записей списка: {len(entries)}; отсутствуют в списке: {", ".join(missing[:8]) or "не обнаружены"}. Полную правильность IEEE нужно проверить вручную.',
        f'Мәтіндегі [n] сілтемелер: {len(citations)}, тізімдегі жазбалар: {len(entries)}; тізімде жоқ нөмірлер: {", ".join(missing[:8]) or "табылмады"}. IEEE толықтығын қолмен тексеру керек.')
    for key,word,ref in [('figures','(?:рисунок|сурет|figure)','7.19, 7.21–7.23'),('tables','(?:таблица|кесте|table)','7.24–7.27')]:
        caps=[];refs=[]
        for line in lines:
            m=re.match(rf'^\s*{word}\s+(\d+(?:\.\d+)?)\s*[-–—:.]\s*\S+',line,re.I)
            if m: caps.append(m.group(1))
            else: refs.extend(re.findall(rf'{word}\s+(\d+(?:\.\d+)?)',line,re.I))
        without=sorted(set(caps)-set(refs))
        add(key,ref,'warning' if without or not caps else 'pass',
            f'Подписей: {len(caps)}, упоминаний в тексте: {len(refs)}; без ссылок: {", ".join(without[:8]) or "нет"}.',
            f'Атаулары: {len(caps)}, мәтіндегі сілтемелер: {len(refs)}; сілтемесіз нөмірлер: {", ".join(without[:8]) or "жоқ"}.')
    counts=Counter(c['status'] for c in checks)
    checked=counts['pass']+counts['fail']
    rate=round(100*counts['pass']/checked) if checked else None
    return {
        'filename':Path(filename).name,
        'language':lang,
        'methodology':{'title':RULES['methodology'],'version':RULES['edition'],'code':RULES['code']},
        'summary':{'pass':counts['pass'],'fail':counts['fail'],'warning':counts['warning'],'manual':counts['manual'],
                   'total_checks':len(checks),'verified_compliance_percent':rate,
                   'page_count':len(ev.page_texts) if ev.page_texts else None},
        'checks':checks,
        'rubric':[{'title':x[lang],'max_points':x['points']} for x in RULES['rubric']],
        'warnings':ev.warnings,
        'limitations':(['Бұл көрсеткіш ресми 100 балдық диплом бағасы емес.','Плагиатты және қорғауды университет тексереді.'] if lang=='kk' else
                       ['Процент соответствия не является официальной оценкой комиссии.','Плагиат и качество выступления проверяет университет.'])
    }
