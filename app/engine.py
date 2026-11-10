"""Transparent, rules-based checks based on clauses 6 and 7 of the uploaded 2020 MУИТ guide.

Never interpret the formatting compliance percentage as the commission's official 100-point grade.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from .extractors import DocumentEvidence, extract_docx, extract_pdf, validate_file
from .rules import get_rules, rubric_view

# Anchored headings (TOC entries with dot leaders are ignored). Alternate languages are supported.
HEADING_PATTERNS = {
    'assignment': r'(?:задание\s+(?:на\s+)?диплом\w*(?:\s+(?:работ\w*|проект\w*))?|диплом(?:дық|дық\s+жұмыс)\s+тапсырм\w*|тапсырма\s+диплом\w*)',
    'calendar': r'(?:календарный\s+(?:план|график)|күнтізбелік\s+жоспар)',
    'annotation_kz': r'(?:аңдатпа|түйіндеме)',
    'annotation_ru': r'(?:аннотация|реферат)',
    'annotation_en': r'(?:abstract|annotation)',
    'toc': r'(?:содержание|мазмұны|contents)',
    'intro': r'(?:введение|кіріспе|introduction)',
    'conclusion': r'(?:заключение|қорытынды|conclusion|выводы)',
    'references': r'(?:список\s+(?:использованной|используемой)\s+литературы|список\s+литературы|использованные\s+источники|пайдаланылған\s+әдебиеттер|әдебиеттер\s+тізімі|references|bibliography)',
    'appendix': r'(?:приложение(?:\s+[а-яa-z0-9])?|қосымша(?:лар)?(?:\s+[а-яa-z0-9])?|appendix(?:\s+[a-z0-9])?)',
}


def _clean(t: str) -> str:
    return re.sub(r'\s+', ' ', t.strip()).strip(' :.-–—\t').lower()


def _is_anchor(t: str, pattern: str) -> bool:
    if len(t) > 110 or re.search(r'\.{3,}\s*\d+\s*$', t):
        return False
    return bool(re.fullmatch(pattern, _clean(t), re.IGNORECASE))


def _detect_anchors(ev: DocumentEvidence) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {k:[] for k in HEADING_PATTERNS}
    # Use paragraphs/lines rather than generic occurrence inside ordinary body text.
    for i, line in enumerate(ev.paragraph_texts):
        for key, pattern in HEADING_PATTERNS.items():
            if _is_anchor(line, pattern):
                out[key].append({'index':i, 'label':line})
    # Determine PDF page for each anchor, using page-local exact line matches.
    if ev.page_texts:
        for key, matches in out.items():
            for match in matches:
                for page_no, page_text in enumerate(ev.page_texts, 1):
                    if any(_clean(x) == _clean(match['label']) for x in page_text.splitlines()):
                        match['page'] = page_no
                        break
    return out


def _put(checks, key, title, ref, status, detail, suggestion=None, **extras):
    checks.append({'id':key, 'title':title, 'ref':ref, 'status':status, 'detail':detail,
                   'suggestion':suggestion, **extras})


def _group_sections(ev: DocumentEvidence, checks: list, anchors: dict):
    required = get_rules()['sections']
    text_prefix = '\n'.join(ev.paragraph_texts[:60]).lower()
    title_hint = bool(re.search(r'дипломн(?:ая|ый|ой|ого)|дипломдық|diploma\s+(?:project|thesis)', text_prefix))
    _put(checks, 'cover','Обложка','6.5–6.6','manual',
         'Обложку и титульный лист нельзя надёжно различить автоматически; проверьте вручную.')
    _put(checks,'title','Титульный лист','6.5, 6.7', 'pass' if title_hint else 'warning',
         'В начале документа найдено упоминание дипломной работы или проекта.' if title_hint else 'Не удалось однозначно определить титульный лист.',
         None if title_hint else 'Проверьте наличие в начале работы организации, кафедры, темы, автора и руководителя.')
    names = {s['key']:s for s in required}
    for key in ('assignment','calendar','toc','intro','conclusion','references','appendix'):
        present = bool(anchors[key])
        _put(checks,key,names[key]['title'],names[key]['ref'], 'pass' if present else 'fail',
             ('Заголовок найден: '+anchors[key][-1]['label']) if present else 'Заголовок обязательного раздела не обнаружен.',
             None if present else 'Добавьте нужный раздел или используйте общепринятое название по методичке.')
    detected_langs = [code for code in ('kz','ru','en') if anchors['annotation_'+code]]
    _put(checks,'annotations','Аннотации на трёх языках','6.5, 6.10',
         'pass' if len(detected_langs)==3 else 'fail',
         'Обнаружены заголовки на языках: '+(', '.join(detected_langs) or 'ни один')+'.',
         None if len(detected_langs)==3 else 'Убедитесь, что есть разделы «АҢДАТПА», «АННОТАЦИЯ» и «ABSTRACT».')
    # Main section marker after the introduction; section headings can use 1, 1.1 or ГЛАВА 1.
    lines = ev.paragraph_texts
    intro_i = anchors['intro'][-1]['index'] if anchors['intro'] else -1
    conclusion_i = anchors['conclusion'][-1]['index'] if anchors['conclusion'] else len(lines)
    main_markers = [i for i,x in enumerate(lines) if intro_i < i < conclusion_i and
        (re.match(r'^(?:ГЛАВА|ТАРАУ|CHAPTER)\s+[1-9ivx]+\b',x,re.I) or
         re.match(r'^\d{1,2}\s+[^\W\d_]{2}',x,re.UNICODE)) and
        not re.search(r'\.{3,}\d+\s*$',x) and len(x)<130]
    _put(checks,'main',names['main']['title'],names['main']['ref'],
         'pass' if main_markers else 'warning',
         f'Найдено заголовков нумерованных разделов: {len(main_markers)}.' if main_markers else 'Не удалось автоматически выделить нумерованные разделы основной части.',
         None if main_markers else 'Используйте нумерованные заголовки вида «1 ...», «2 ...».')


def _check_margins(ev: DocumentEvidence, checks: list, expected: dict):
    if ev.kind == 'docx':
        mismatches = []
        for s in ev.section_margins:
            for side in ('left','top','right','bottom'):
                actual = s[side]
                if actual is None or abs(actual - expected[side]) > 0.7:
                    mismatches.append(f'{s["section"]}-й раздел, { {"left": "левое", "right": "правое", "top": "верхнее", "bottom": "нижнее"}[side] } поле: {actual} мм (требуется {expected[side]} мм)')
        _put(checks,'margins','Поля страницы','7.2', 'fail' if mismatches else 'pass',
             '; '.join(mismatches[:7]) if mismatches else 'Поля всех разделов DOCX соответствуют нормативу.',
             'Исправьте поля в Word → Макет → Поля.' if mismatches else None)
    else:
        _put(checks,'margins','Поля страницы','7.2','manual',
             'Координаты текста в PDF не позволяют точно определить поля: проверьте их визуально.')


def _check_docx_fonts(ev, checks, rules):
    samples = ev.body_fonts
    known = [x for x in samples if x['font'] and x['pt'] is not None]
    if len(known)==0:
        _put(checks,'font','Шрифт и размер','7.1','warning',
             'Не удалось определить фактический шрифт и размер основного текста DOCX.')
    else:
        total = sum(x['weight'] for x in known)
        wrong = [x for x in known if x['font'].lower().replace(' ','') != 'timesnewroman' or abs(x['pt']-14)>0.4]
        pct = round(100*sum(x['weight'] for x in wrong)/total,1)
        _put(checks,'font','Шрифт и размер','7.1', 'fail' if pct>5 else 'pass',
             f'Проверено фрагментов основного текста: {len(known)}. Доля несоответствий: {pct}%. ' +
             ('Примеры: '+', '.join(f'абзац {x["paragraph"]+1} — {x["font"]}, {x["pt"]} pt' for x in wrong[:4]) if wrong else 'Times New Roman 14 pt.'),
             'Установите для основного текста Times New Roman, 14 пт.' if pct>5 else None)
    total = sum(x['weight'] for x in ev.body_spacing)
    known = [x for x in ev.body_spacing if isinstance(x['value'],float)]
    if not known:
        _put(checks,'spacing','Межстрочный интервал','7.1','warning',
             'Не удалось достоверно определить межстрочный интервал основного текста: параметр может наследоваться из стиля.')
    else:
        wrong = [x for x in known if abs(x['value']-1.0)>0.03]
        pct=round(sum(x['weight'] for x in wrong)*100/max(1,sum(x['weight'] for x in known)),1)
        _put(checks,'spacing','Межстрочный интервал','7.1','fail' if pct>5 else 'pass',
             f'Проверено абзацев (межстрочный интервал): {len(known)}. Доля несоответствий: {pct}%.',
             'Word: Межстрочный интервал = Одинарный (1.0).' if pct>5 else None)


def _check_pdf_fonts(ev, checks):
    samples=ev.font_samples
    if not samples:
        _put(checks,'font','Шрифт и размер','7.1','warning', 'Не удалось определить шрифт в текстовых фрагментах PDF.')
    else:
        total=sum(s['weight'] for s in samples)
        wrong=[s for s in samples if not ('times' in s['font'].lower() and 'roman' in s['font'].lower()) or abs(s['pt']-14) > .6]
        pct=round(100*sum(x['weight'] for x in wrong)/total,1)
        common=sorted({(x['font'],x['pt']) for x in samples},key=lambda x: x[0])[:5]
        _put(checks,'font','Шрифт и размер','7.1',
             'warning' if pct>5 else 'pass',
             f'В PDF доля фрагментов с отличающимся шрифтом ≈{pct}%; примеры шрифтов: {common}. Метаданные о шрифтах PDF могут отличаться от оригинала.',
             'Для более точной проверки загрузите оригинал DOCX.' if pct>5 else None)
    _put(checks,'spacing','Межстрочный интервал','7.1','manual',
         'В PDF не сохраняется исходный параметр межстрочного интервала Word; нужен DOCX.')


def _first_page(anchors, key, prefer_last: bool = True):
    a=anchors.get(key, [])
    return (a[-1] if prefer_last else a[0]).get('page') if a else None


def _page_bounds(ev, checks, anchors, rules):
    pages=ev.pdf_pages
    if not pages:
        _put(checks,'page_count','Объём основной работы','6.4','manual','Не удалось определить число страниц DOCX; загрузите PDF.')
        _put(checks,'intro_length','Объём введения','6.12','manual','Нет данных о количестве страниц.')
        _put(checks,'conclusion_length','Объём заключения','6.18','manual','Нет данных о количестве страниц.')
        _put(checks,'page_size','Формат страниц','7.1','manual','DOCX не преобразован в PDF; проверьте формат страниц в Word.')
        return
    appendix = _first_page(anchors, 'appendix')
    # Ignore an appendix listed in an early table of contents; appendix starts after conclusion.
    conclusion = _first_page(anchors, 'conclusion')
    if appendix is not None and conclusion is not None and appendix <= conclusion:
        appendix = None
    count = appendix-1 if appendix else len(pages)
    _put(checks,'page_count','Объём основной работы','6.4', 'pass' if count>=rules['min_body_pages'] else 'fail',
         f'Основная часть: примерно {count} стр.; требование ≥{rules["min_body_pages"]} (приложения исключены). Всего: {len(pages)} стр.' +
         (' По конвертации DOCX через LibreOffice.' if ev.converted_for_pages else ''),
         None if count>=rules['min_body_pages'] else 'Увеличьте объём работы до 60 страниц, не учитывая приложения.')
    non_a4=[]
    for p in pages:
        w,h=p['width_pt'],p['height_pt']
        a4 = abs(w-595.3)<5 and abs(h-841.9)<5
        # A3 diagrams are allowed under clause 7.14.
        a3 = abs(w-841.9)<5 and abs(h-1190.6)<5 or abs(w-1190.6)<5 and abs(h-841.9)<5
        if not (a4 or a3):non_a4.append(p['page'])
    _put(checks,'page_size','Форматы A4 / A3','7.1, 7.14',
         'warning' if non_a4 else 'pass',
         f'Страницы нестандартного размера: {non_a4[:12]}' if non_a4 else 'Страницы формата A4 (чертежи A3 могут быть допустимы).')
    # Page spans: measure from the heading to the next chapter start; warn when unclear.
    intro = _first_page(anchors,'intro')
    concl = _first_page(anchors,'conclusion')
    refs = _first_page(anchors,'references')
    # 'Main' starts with numbered section; extract a page index if available.
    main_candidates=[h['page'] for h in ev.headings if 'page' in h and
        re.match(r'^\d{1,2}\s+\S+',h['text']) and intro and h['page']>=intro and
        (not concl or h['page']<concl)]
    first_chapter=min(main_candidates) if main_candidates else None
    for key,start,end,limit,ref,label in (
       ('intro_length',intro,first_chapter,rules['max_intro_pages'],'6.12','Объём введения'),
       ('conclusion_length',concl,refs,rules['max_conclusion_pages'],'6.18','Объём заключения')):
        if not start or not end or end<start:
            _put(checks,key,label,ref,'manual','Не удалось уверенно определить границы разделов по страницам.')
            continue
        delta=end-start
        if delta>=limit+1:
            status='fail'
        elif delta+1<=limit:
            status='pass'
        else:
            status='warning'  # may share the transition page
        _put(checks,key,label,ref,status,
             f'{label}: начало на странице {start}, следующий раздел — на странице {end}. Примерно {max(1,delta)}–{delta+1} стр.; допустимо не более {limit} стр.',
             'Уточните границы разделов вручную.' if status=='warning' else None)


def _refs_and_captions(ev,checks,anchors):
    lines=ev.paragraph_texts
    ref_idx=anchors['references'][-1]['index'] if anchors['references'] else None
    body='\n'.join(lines[:ref_idx]) if ref_idx is not None else ev.text
    bib='\n'.join(lines[ref_idx+1:]) if ref_idx is not None else ''
    intext=re.findall(r'\[(\d+)\]',body)
    entries=re.findall(r'(?m)^\s*\[(\d+)\]\s+',bib)
    if ref_idx is None:
        _put(checks,'ieee','Список литературы и ссылки IEEE','6.19, 7.39','warning','Раздел литературы не найден.')
    elif not entries or not intext:
        _put(checks,'ieee','Список литературы и ссылки IEEE','6.19, 7.39','warning',
             f'Ссылок вида [n] в тексте: {len(intext)}; записей [n] в списке: {len(entries)}. Нельзя проверить полноту оформления IEEE по этим данным.',
             'Пронумеруйте источники по порядку упоминания [1], [2], ... и поставьте ссылки в тексте.')
    else:
        absent=sorted(set(intext)-set(entries),key=int)
        _put(checks,'ieee','Список литературы и ссылки IEEE','6.19, 7.39',
             'fail' if absent else 'warning',
             f'В тексте {len(intext)} ссылок, в списке {len(entries)} записей. ' +
             (f'Номера, отсутствующие в списке: {absent[:10]}.' if absent else 'Номера ссылок сопоставлены; полный формат IEEE требует дополнительной проверки.'),
             'Согласуйте номера ссылок в тексте со списком литературы.' if absent else None)
    for kind,word,ref in (('figure','рисунок|сурет|figure','7.19, 7.21–7.23'),
                          ('table','таблица|кесте|table','7.24–7.27')):
        caption=re.compile(rf'^\s*(?:{word})\s+(\d+(?:\.\d+)?)\s*[-–—:]\s*\S+',re.I)
        refs=re.compile(rf'(?:{word})\s+(\d+(?:\.\d+)?)',re.I)
        caption_nums=[]
        ref_nums=[]
        for line in lines:
            m=caption.search(line)
            if m:caption_nums.append(m.group(1))
            else:ref_nums.extend(refs.findall(line))
        missing=sorted(set(caption_nums)-set(ref_nums))
        label='Ссылки на рисунки' if kind=='figure' else 'Ссылки на таблицы'
        status='warning' if missing or not caption_nums else 'pass'
        _put(checks,kind+'_links',label,ref,status,
             f'Подписей: {len(caption_nums)}, ссылок в тексте: {len(ref_nums)}. ' +
             (f'Нет ссылок на номера: {missing[:10]}' if missing else 'Базовая проверка нумерации выполнена.'),
             'Упомяните каждый рисунок и каждую таблицу по номеру в тексте.' if missing else None)


def analyze(path: Path, filename: str, use_libreoffice: bool = True) -> dict:
    kind=validate_file(path,filename)
    ev=extract_docx(path,use_libreoffice) if kind=='docx' else extract_pdf(path)
    rules=get_rules()
    anchors=_detect_anchors(ev)
    checks=[]
    _group_sections(ev,checks,anchors)
    _check_margins(ev,checks,rules['format']['margins_mm'])
    if kind=='docx':_check_docx_fonts(ev,checks,rules['format'])
    else:_check_pdf_fonts(ev,checks)
    _page_bounds(ev,checks,anchors,rules['format'])
    _refs_and_captions(ev,checks,anchors)
    tally={k:sum(c['status']==k for c in checks) for k in ('pass','fail','warning','manual')}
    denom=tally['pass']+tally['fail']
    compliance=round(100*tally['pass']/denom) if denom else None
    return {
        'filename':Path(filename).name,
        'format':kind.upper(),
        'methodology':{'name':rules['document'],'edition':rules['version_date'],
                       'document_code':rules['document_code'],'note':rules['reference_note']},
        'summary':{**tally,'total_checks':len(checks), 'verified_compliance_percent':compliance,
                   'scoring_note':'Это доля выполненных проверяемых требований, а не официальная оценка аттестационной комиссии.',
                   'pages':len(ev.page_texts) if ev.page_texts else None},
        'checks':checks,
        'rubric':rubric_view(),
        'warnings':ev.warnings,
        'ai_review':None,
        'limitations':['Метаданные PDF о шрифтах и интервалах могут быть неточными.',
                       'Научную новизну, плагиат, выступление и командную работу нельзя достоверно оценить по одному файлу.',
                       'Итоговую оценку и допуск к защите определяет университет и комиссия.'],
        '_ai_text':ev.text[:24000],
    }
