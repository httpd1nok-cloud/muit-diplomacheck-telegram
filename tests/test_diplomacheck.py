import asyncio
import io
import json
import shutil
import sys
import types
from pathlib import Path

import fitz
import pytest
from docx import Document
from docx.shared import Cm, Pt

from app.engine import RULES, analyze
from app.knowledge import clauses, find_topic, offline_answer, retrieve
from app.ai_advisor import enabled


def sample_docx(path, good=True):
    doc=Document()
    sec=doc.sections[0]
    sec.left_margin=Cm(3 if good else 2)
    sec.right_margin=Cm(1)
    sec.top_margin=Cm(2)
    sec.bottom_margin=Cm(2.5)
    sty=doc.styles['Normal']
    sty.font.name='Times New Roman' if good else 'Arial'
    sty.font.size=Pt(14 if good else 12)
    sty.paragraph_format.line_spacing=1 if good else 1.5
    items=['ДИПЛОМНЫЙ ПРОЕКТ','ЗАДАНИЕ НА ДИПЛОМНЫЙ ПРОЕКТ','КАЛЕНДАРНЫЙ ПЛАН',
        'АҢДАТПА','АННОТАЦИЯ','ABSTRACT','СОДЕРЖАНИЕ','ВВЕДЕНИЕ',
        '1 ТЕОРЕТИЧЕСКАЯ ЧАСТЬ','ЗАКЛЮЧЕНИЕ','СПИСОК ИСПОЛЬЗОВАННОЙ ЛИТЕРАТУРЫ','ПРИЛОЖЕНИЕ А']
    for name in items:
        doc.add_paragraph(name)
        doc.add_paragraph('Дипломный проект содержит исследование методов телекоммуникационных систем. '*5)
    doc.save(path)


def sample_pdf(path):
    doc=fitz.open()
    headings={0:'ДИПЛОМНЫЙ ПРОЕКТ',1:'ЗАДАНИЕ НА ДИПЛОМНЫЙ ПРОЕКТ',
        2:'КАЛЕНДАРНЫЙ ПЛАН',3:'АҢДАТПА',4:'АННОТАЦИЯ',5:'ABSTRACT',
        6:'СОДЕРЖАНИЕ',7:'ВВЕДЕНИЕ',8:'1 ОСНОВНАЯ ЧАСТЬ',
        61:'ЗАКЛЮЧЕНИЕ',62:'СПИСОК ИСПОЛЬЗОВАННОЙ ЛИТЕРАТУРЫ',64:'ПРИЛОЖЕНИЕ А'}
    for i in range(66):
        page=doc.new_page(width=595,height=842)
        page.insert_text((80,90),headings.get(i,'Телекоммуникационные системы: методы и результаты.'),fontsize=14)
    doc.save(path);doc.close()


def by_id(report, key):
    return next(x for x in report['checks'] if x['id']==key)


def test_guide_retrieval_and_precise_quotations():
    db=clauses()
    assert len(db)>=110
    assert '60 страниц' in db['6.4']['text']
    assert 'Times New Roman' in db['7.1']['text']
    assert any(x['id']=='7.39' for x in retrieve('Как оформить источники IEEE'))
    assert any(x['id']=='6.12' for x in retrieve('§6.12 что в введении'))
    assert '30 мм' in offline_answer('Қаріп пен жиектер қандай?','kk')
    assert '60 страниц' in offline_answer('Объем в страницах','ru')
    assert sum(x['points'] for x in RULES['rubric'])==100


def test_bilingual_docx_checks(tmp_path):
    p=tmp_path/'example.docx'
    sample_docx(p, True)
    ru=analyze(p,'example.docx','ru')
    kk=analyze(p,'example.docx','kk')
    assert by_id(ru,'margins')['status']=='pass'
    assert by_id(ru,'font')['status']=='pass'
    assert by_id(ru,'spacing')['status']=='pass'
    assert by_id(ru,'annotations')['status']=='pass'
    assert by_id(ru,'pages')['status']=='manual'
    assert by_id(ru,'font')['title']=='Шрифт и размер'
    assert by_id(kk,'font')['title']=='Қаріп пен өлшемі'
    assert ru['summary']==kk['summary']
    assert 'комиссии' in ru['limitations'][0]
    assert 'ресми' in kk['limitations'][0]


def test_docx_detect_errors(tmp_path):
    p=tmp_path/'bad.docx';sample_docx(p,False)
    report=analyze(p,p.name,'kk')
    assert by_id(report,'margins')['status']=='fail'
    assert by_id(report,'font')['status']=='fail'
    assert by_id(report,'spacing')['status']=='fail'
    assert 'қате' in by_id(report,'margins')['detail'].lower()


def test_pdf_detect_page_count(tmp_path):
    p=tmp_path/'demo.pdf';sample_pdf(p)
    for lang in ('ru','kk'):
        r=analyze(p,p.name,lang)
        assert by_id(r,'pages')['status']=='pass'
        assert r['summary']['page_count']==66
        assert by_id(r,'pages')['ref']=='6.4'


def test_reject_untrusted_input(tmp_path):
    p=tmp_path/'fake.pdf';p.write_bytes(b'not-a-pdf'*40)
    with pytest.raises(ValueError):analyze(p,'fake.pdf','ru')
    with pytest.raises(ValueError):analyze(p,'fake.exe','ru')


def install_fake_telegram(monkeypatch):
    from dataclasses import dataclass
    telegram=types.ModuleType('telegram')
    class FakeButton:
        def __init__(self,label,callback_data):self.text=label;self.callback_data=callback_data
    class FakeMarkup:
        def __init__(self,rows):self.inline_keyboard=rows
    class FakeInput:
        def __init__(self,data,filename):self.data=data;self.filename=filename
    telegram.InlineKeyboardButton=FakeButton
    telegram.InlineKeyboardMarkup=FakeMarkup
    telegram.InputFile=FakeInput
    telegram.Update=object
    ext=types.ModuleType('telegram.ext')
    ext.Application=object; ext.CallbackQueryHandler=object;ext.CommandHandler=object
    ext.ContextTypes=types.SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler=object
    ext.filters=types.SimpleNamespace(Document=types.SimpleNamespace(ALL=object()),TEXT=1,COMMAND=2)
    monkeypatch.setitem(sys.modules,'telegram',telegram)
    monkeypatch.setitem(sys.modules,'telegram.ext',ext)
    sys.modules.pop('app.bot',None)
    import app.bot as bot
    return bot


class FakeMessage:
    def __init__(self,text=None,document=None):
        self.text=text;self.document=document;self.responses=[];self.files=[]
    async def reply_text(self,text,**kw):self.responses.append((text,kw))
    async def reply_document(self,file,**kw):self.files.append((file,kw))


class FakeContext:
    def __init__(self):self.user_data={}


class FakeUpdate:
    def __init__(self,msg,callback=None):
        self.effective_message=msg
        self.effective_user=types.SimpleNamespace(id=12345)
        self.effective_chat=types.SimpleNamespace(type='private')
        self.callback_query=callback


class FakeCallback:
    def __init__(self,data):self.data=data;self.edits=[]
    async def answer(self):pass
    async def edit_message_text(self,text,**kw):self.edits.append((text,kw))


def test_bot_language_flow_and_offline_answer(monkeypatch):
    bot=install_fake_telegram(monkeypatch)
    async def run():
        ctx=FakeContext();msg=FakeMessage();u=FakeUpdate(msg)
        await bot.start(u,ctx)
        assert '🇰🇿 Қазақша' in [b.text for b in msg.responses[-1][1]['reply_markup'].inline_keyboard[0]]
        q=FakeCallback('lang:kk')
        await bot.buttons(FakeUpdate(msg,q),ctx)
        assert ctx.user_data['lang']=='kk'
        assert 'Сәлем' in q.edits[0][0]
        question=FakeMessage('Кіріспе неше бет?')
        await bot.question(FakeUpdate(question),ctx)
        assert '§6.12' in question.responses[0][0]
        q2=FakeCallback('lang:ru')
        await bot.buttons(FakeUpdate(msg,q2),ctx)
        assert 'Здравствуйте' in q2.edits[0][0]
        question2=FakeMessage('Как оформить IEEE?')
        await bot.question(FakeUpdate(question2),ctx)
        assert 'IEEE' in question2.responses[0][0]
        await bot.clear(FakeUpdate(msg),ctx)
        assert not ctx.user_data['ai_consent']
    asyncio.run(run())


def test_bot_document_e2e(tmp_path,monkeypatch):
    bot=install_fake_telegram(monkeypatch)
    p=tmp_path/'test.docx';sample_docx(p)
    class FakeTelegramFile:
        async def download_to_drive(self,custom_path):shutil.copy2(p,custom_path)
    class FakeDoc:
        file_name='test.docx';file_size=p.stat().st_size
        async def get_file(self):return FakeTelegramFile()
    async def run():
        ctx=FakeContext();ctx.user_data['lang']='kk'
        msg=FakeMessage(document=FakeDoc())
        await bot.docs(FakeUpdate(msg),ctx)
        assert len(msg.responses)>=2
        assert 'Дипломды тексеру нәтижесі' in msg.responses[1][0]
        assert len(msg.files)==1
        f=msg.files[0][0]
        report=json.loads(f.data.getvalue().decode('utf-8'))
        assert report['language']=='kk'
        assert 'last_report' in ctx.user_data
        assert 'checks' in report
    asyncio.run(run())


def test_ai_off_by_default(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.setenv('ENABLE_AI_CHAT','true')
    assert not enabled()
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    assert enabled()


def test_ai_consent_required_and_reset(monkeypatch):
    bot=install_fake_telegram(monkeypatch)
    monkeypatch.setattr(bot,'enabled',lambda:True)
    async def run():
        ctx=FakeContext();ctx.user_data['lang']='ru'
        m=FakeMessage()
        cb=FakeCallback('menu:advice')
        await bot.buttons(FakeUpdate(m,cb),ctx)
        assert 'OpenAI' in cb.edits[0][0]
        assert not ctx.user_data.get('ai_consent')
        yes=FakeCallback('ai:yes')
        await bot.buttons(FakeUpdate(m,yes),ctx)
        assert ctx.user_data['ai_consent'] is True
        await bot.clear(FakeUpdate(m),ctx)
        assert not ctx.user_data['ai_consent']
        change=FakeCallback('lang:kk')
        await bot.buttons(FakeUpdate(m,change),ctx)
        assert ctx.user_data['lang']=='kk'
        assert not ctx.user_data['ai_consent']
    asyncio.run(run())


def test_ai_sends_only_summary_not_raw_document(monkeypatch):
    monkeypatch.setenv('ENABLE_AI_CHAT','true')
    monkeypatch.setenv('OPENAI_API_KEY','mock-secret')
    from app import ai_advisor
    sent=[]
    class ChatCompletions:
        @staticmethod
        def create(**kwargs):
            sent.extend(kwargs['messages'])
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content='См. §6.12.'))])
    class FakeClient:
        def __init__(self,**kwargs):pass
        self_chat=types.SimpleNamespace(completions=ChatCompletions())
        chat=self_chat
    fake_openai=types.ModuleType('openai')
    fake_openai.OpenAI=FakeClient
    monkeypatch.setitem(sys.modules,'openai',fake_openai)
    report={'summary':{'fail':2},'checks':[{'status':'fail','title':'Поля страницы','ref':'7.2','detail':'Левое поле 20мм'}],
            'raw_file_text':'СЕКРЕТНАЯ_ПОЛНАЯ_ДИПЛОМКА_НЕ_ПОСЫЛАТЬ'}
    out=ai_advisor.ai_reply('Помоги исправить поля','ru',[],report)
    assert out=='См. §6.12.'
    full=' '.join(x['content'] for x in sent)
    assert 'СЕКРЕТНАЯ_ПОЛНАЯ_ДИПЛОМКА_НЕ_ПОСЫЛАТЬ' not in full
    assert 'Левое поле 20мм' in full
    assert 'МУИТ' in full


def test_compatible_railway_entrypoint():
    from app import bot_cloud
    assert callable(bot_cloud.run)
