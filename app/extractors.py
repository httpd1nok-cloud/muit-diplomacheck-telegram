"""Extract evidence without pretending DOCX has intrinsic pages or PDF has editable styles."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz
from docx import Document

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 400
MAX_UNZIPPED = 100 * 1024 * 1024


class InvalidDocument(ValueError):
    pass


@dataclass
class DocumentEvidence:
    kind: str
    text: str
    page_texts: list[str] = field(default_factory=list)
    paragraph_texts: list[str] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)
    font_samples: list[dict] = field(default_factory=list)
    section_margins: list[dict] = field(default_factory=list)
    body_fonts: list[dict] = field(default_factory=list)
    body_spacing: list[dict] = field(default_factory=list)
    pdf_pages: list[dict] = field(default_factory=list)
    converted_for_pages: bool = False
    page_source: str | None = None
    warnings: list[str] = field(default_factory=list)


def validate_file(path: Path, filename: str) -> str:
    if not path.exists() or path.stat().st_size > MAX_BYTES or path.stat().st_size < 100:
        raise InvalidDocument('Файл пуст или превышает лимит 20 МБ.')
    suffix = Path(filename).suffix.lower()
    if suffix not in ('.pdf', '.docx'):
        raise InvalidDocument('Допустимы только PDF и DOCX; формат .doc не поддерживается.')
    if suffix == '.pdf':
        with path.open('rb') as f:
            if not f.read(5) == b'%PDF-':
                raise InvalidDocument('Недопустимая сигнатура PDF.')
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                total_unpacked = sum(x.file_size for x in entries)
                if total_unpacked > MAX_UNZIPPED or any(x.file_size > MAX_UNZIPPED for x in entries):
                    raise InvalidDocument('Внутренние данные DOCX превышают допустимый объём.')
                bad = archive.testzip()
                if bad:
                    raise InvalidDocument('Файл DOCX повреждён.')
                if '[Content_Types].xml' not in archive.namelist() or 'word/document.xml' not in archive.namelist():
                    raise InvalidDocument('Файл не является допустимым документом DOCX.')
        except (zipfile.BadZipFile, OSError) as exc:
            raise InvalidDocument('Невозможно прочитать ZIP-контейнер DOCX.') from exc
    return suffix[1:]


def _heading_like(text: str, style: str = '') -> bool:
    t = text.strip()
    if not t or len(t) > 130 or re.search(r'\.{4,}\s*\d+\s*$', t):
        return False
    if re.match(r'^(\d{1,2}(?:\.\d{1,2}){0,2})\s+\S+', t) and len(t) < 120:
        return True
    if ('heading' in style.lower() or 'заголов' in style.lower()) and len(t) < 130:
        return True
    return bool(re.fullmatch(r'[\s\dА-ЯЁӘІҢҒҮҰҚӨҺA-Z\-–()«».,]+', t) and len(t)>3)


def _effective_attr(obj: Any, attribute: str):
    """Walk character/paragraph style chain; None means there is no declared value."""
    current = obj
    for _ in range(12):
        if current is None:
            break
        val = getattr(current, attribute, None)
        if val is not None:
            return val
        current = getattr(current, 'base_style', None)
    return None


def _font_name(para, run):
    if run.font.name:
        return run.font.name
    n = _effective_attr(getattr(run, 'style', None), 'font')
    if n is not None and n.name:
        return n.name
    style = para.style
    for _ in range(12):
        if style is None:
            break
        if style.font.name:
            return style.font.name
        style = style.base_style
    return None


def _font_size(para, run):
    if run.font.size:
        return run.font.size.pt
    style = getattr(run, 'style', None)
    for _ in range(12):
        if style is None:
            break
        if style.font.size:
            return style.font.size.pt
        style = style.base_style
    style = para.style
    for _ in range(12):
        if style is None:
            break
        if style.font.size:
            return style.font.size.pt
        style = style.base_style
    return None


def _spacing(para):
    if para.paragraph_format.line_spacing is not None:
        return para.paragraph_format.line_spacing
    style = para.style
    for _ in range(12):
        if style is None:
            break
        v = style.paragraph_format.line_spacing
        if v is not None:
            return v
        style = style.base_style
    return None


def extract_docx(path: Path, use_libreoffice: bool = True) -> DocumentEvidence:
    try:
        doc = Document(path)
    except Exception as exc:
        raise InvalidDocument('Не удалось открыть DOCX.') from exc
    ev = DocumentEvidence(kind='docx', text='')
    for i, sec in enumerate(doc.sections):
        mm = lambda l: round(l.mm, 2) if l is not None else None
        ev.section_margins.append({
            'section': i + 1, 'left': mm(sec.left_margin), 'right': mm(sec.right_margin),
            'top': mm(sec.top_margin), 'bottom': mm(sec.bottom_margin),
            'page_width': mm(sec.page_width), 'page_height': mm(sec.page_height),
        })
    blocks = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                ev.paragraph_texts.extend(p.text.strip() for p in cell.paragraphs if p.text.strip())
    for idx, para in enumerate(blocks):
        t = para.text.strip()
        if not t:
            continue
        ev.paragraph_texts.append(t)
        style_name = para.style.name if para.style is not None else ''
        if _heading_like(t, style_name):
            ev.headings.append({'text':t, 'paragraph':idx, 'style':style_name})
        if len(t) < 10 or ('heading' in style_name.lower() or 'заголов' in style_name.lower()):
            continue
        spacing = _spacing(para)
        ev.body_spacing.append({'paragraph':idx, 'value':round(spacing, 3) if isinstance(spacing, float) else str(spacing) if spacing else None,'weight':len(t)})
        for run in para.runs:
            if not run.text.strip():
                continue
            ev.body_fonts.append({'paragraph':idx,'font':_font_name(para, run), 'pt':_font_size(para, run), 'weight':len(run.text.strip())})
    ev.text = '\n'.join(ev.paragraph_texts)
    if use_libreoffice and shutil.which('libreoffice'):
        try:
            with tempfile.TemporaryDirectory(prefix='muit_render_') as td:
                cmd = [shutil.which('libreoffice'), '-env:UserInstallation=file://'+td+'/lo-profile',
                    '--headless', '--convert-to', 'pdf', '--outdir', td, str(path)]
                subprocess.run(cmd, check=True, capture_output=True, timeout=55)
                converted = Path(td) / (path.stem + '.pdf')
                if converted.is_file():
                    pdf_ev = extract_pdf(converted)
                    ev.page_texts = pdf_ev.page_texts
                    ev.pdf_pages = pdf_ev.pdf_pages
                    ev.converted_for_pages = True
                    ev.page_source = 'LibreOffice PDF render (DOCX pagination may differ from MS Word)'
                else:
                    ev.warnings.append('LibreOffice Не удалось преобразовать DOCX в PDF: число страниц не подтверждено.')
        except Exception as exc:
            ev.warnings.append(f'Не удалось определить число страниц DOCX: {type(exc).__name__}.')
    else:
        ev.warnings.append('Число страниц не хранится в DOCX; LibreOffice отсутствует или отключён.')
    return ev


def extract_pdf(path: Path) -> DocumentEvidence:
    try:
        pdf = fitz.open(str(path))
    except Exception as exc:
        raise InvalidDocument('Не удалось открыть PDF.') from exc
    with pdf:
        if pdf.is_encrypted:
            raise InvalidDocument('PDF, защищённый паролем, не поддерживается.')
        if pdf.page_count > MAX_PAGES:
            raise InvalidDocument(f'Документ превышает лимит {MAX_PAGES} страниц.')
        ev = DocumentEvidence(kind='pdf', text='', page_source='PDF')
        for page_i, page in enumerate(pdf):
            text = page.get_text('text', sort=True)
            ev.page_texts.append(text)
            ev.paragraph_texts.extend(x.strip() for x in text.splitlines() if x.strip())
            w,h = page.rect.width, page.rect.height
            words = page.get_text('words')
            body_words = [v for v in words if 0.07*h <= v[1] <= 0.91*h and v[4].strip()]
            bbox = (
                [min(p[0] for p in body_words), min(p[1] for p in body_words),
                 max(p[2] for p in body_words), max(p[3] for p in body_words)] if body_words else None
            )
            # A footer page number is meaningful only if printed near center and at bottom.
            footer = []
            for x0,y0,x1,y1,word,*_ in words:
                if y0 >= h*0.90 and abs((x0+x1)/2-w/2)<w*.14 and re.fullmatch(r'\d{1,3}',word):
                    footer.append(word)
            ev.pdf_pages.append({'page':page_i+1, 'width_pt':round(w,1),'height_pt':round(h,1),
                'bbox':bbox, 'footer_numbers':footer, 'text_chars':len(text.strip())})
            block = page.get_text('dict')
            for b in block['blocks']:
                if 'lines' not in b:
                    continue
                for line in b['lines']:
                    for span in line['spans']:
                        txt = span['text'].strip()
                        if txt and len(txt) >= 2 and 0.07*h < span['bbox'][1] < 0.91*h:
                            ev.font_samples.append({'page':page_i+1, 'font':span['font'],
                                'pt':round(span['size'],1), 'weight':len(txt)})
            # Preserve page-local candidate headings for boundary checks.
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            for line_i, line in enumerate(lines):
                if _heading_like(line) and len(line)<140:
                    ev.headings.append({'text':line,'page':page_i+1,'line':line_i})
        ev.text = '\n'.join(ev.page_texts)
        if sum(p['text_chars'] for p in ev.pdf_pages) < 100:
            ev.warnings.append('В PDF мало машиночитаемого текста: возможно, это скан. OCR в данной версии нет.')
    return ev
