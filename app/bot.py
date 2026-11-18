"""Railway-ready Telegram-only worker: bilingual norm control and diploma adviser.

Launch: python -m app.bot
One active replica per Telegram token; no website or webhook needed.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .ai_advisor import ai_reply, enabled
from .engine import InvalidDocument, MAX_BYTES, RULES, analyze
from .i18n import t
from .knowledge import offline_answer
from .offline_advisor import infer_domain

ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env')
log=logging.getLogger('muit_telegram')
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
ALLOWED={int(x.strip()) for x in os.getenv('ALLOWED_TELEGRAM_USER_IDS','').split(',') if x.strip().isdigit()}
TIME_WINDOW=3600
LIMIT_DOCUMENTS=5
LIMIT_QUESTIONS=20
AI_DAILY_LIMIT=int(os.getenv('AI_DAILY_LIMIT','100'))
_ai_timestamps: list[float]=[]
_rates: dict[tuple[int,str],list[float]]=defaultdict(list)


def allowed(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


def _limited(user_id: int, kind: str, count: int) -> bool:
    key=(user_id,kind)
    now=time.monotonic()
    _rates[key][:]=[ts for ts in _rates[key] if now-ts<TIME_WINDOW]
    if len(_rates[key])>=count:
        return True
    _rates[key].append(now)
    if len(_rates)>30000:
        # bounded in practice on restart; cap long-term memory if spammed
        for k in list(_rates)[:10000]:
            if all(now-ts>=TIME_WINDOW for ts in _rates[k]):del _rates[k]
    return False


def _ai_quota_exceeded() -> bool:
    """Simple per-worker safety cap; Railway restart resets this in-memory count."""
    now=time.monotonic()
    _ai_timestamps[:]=[v for v in _ai_timestamps if now-v<86400]
    if len(_ai_timestamps)>=max(0,AI_DAILY_LIMIT):
        return True
    _ai_timestamps.append(now)
    return False


def _last_report_answer(report: dict | None, question: str, lang: str) -> str | None:
    if not report:
        return None
    lowered=question.casefold()
    if not any(word in lowered for word in ('ошибк','исправ','проверил','отчёт','замечани',
               'қате','түзет','тексер','есептегі','дипломым')):
        return None
    issues=[x for x in report.get('checks',[]) if x.get('status')=='fail'][:6]
    if not issues:
        return ('Соңғы тексерісте анық қателер табылмады. Ескертулерді де қараңыз.' if lang=='kk' else
                'В последней проверке явных ошибок не найдено. Посмотрите также предупреждения.')
    header='Соңғы тексерісте табылған қателер:' if lang=='kk' else 'Ошибки последней проверки:'
    return header+'\n\n'+'\n'.join(f"• {x['title']} (§{x['ref']}): {x['detail'][:210]}" for x in issues)


def keyboard_lang():
    return InlineKeyboardMarkup([[
      InlineKeyboardButton('🇰🇿 Қазақша',callback_data='lang:kk'),
      InlineKeyboardButton('🇷🇺 Русский',callback_data='lang:ru')
    ]])


def keyboard_menu(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang,'menu_check'),callback_data='menu:check')],
        [InlineKeyboardButton(t(lang,'menu_advice'),callback_data='menu:advice')],
        [InlineKeyboardButton(t(lang,'menu_rules'),callback_data='menu:rules'),
         InlineKeyboardButton(t(lang,'menu_rubric'),callback_data='menu:rubric')],
        [InlineKeyboardButton(t(lang,'menu_clear'),callback_data='menu:clear'),
         InlineKeyboardButton(t(lang,'menu_language'),callback_data='menu:language')],
    ])


def keyboard_rules(lang: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang,'q_structure'),callback_data='faq:structure'),
         InlineKeyboardButton(t(lang,'q_format'),callback_data='faq:format')],
        [InlineKeyboardButton(t(lang,'q_intro'),callback_data='faq:intro'),
         InlineKeyboardButton(t(lang,'q_sources'),callback_data='faq:sources')],
        [InlineKeyboardButton(t(lang,'q_defense'),callback_data='faq:defense')],
        [InlineKeyboardButton(t(lang,'back'),callback_data='menu:home')]
    ])


async def _guard(update: Update, require_lang=True) -> str | None:
    chat=update.effective_chat
    message=update.effective_message
    lang=update.effective_user and (getattr(update,'_lang',None) or None)
    # language is stored in user_data, so guard is for chat/access only
    if not chat or chat.type!='private':
        if message:await message.reply_text(t('ru','private'))
        return None
    if not update.effective_user or not allowed(update.effective_user.id):
        if message:await message.reply_text(t('ru','restricted'))
        return None
    return 'ok'


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    # User requested choosing language after /start; always present this screen.
    await update.effective_message.reply_text(t('ru','choose'),reply_markup=keyboard_lang())


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    await update.effective_message.reply_text(t('ru','choose'),reply_markup=keyboard_lang())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    lang=context.user_data.get('lang')
    if lang not in ('ru','kk'):
        await start(update,context)
    else:
        await update.effective_message.reply_text(t(lang,'welcome'),reply_markup=keyboard_menu(lang))


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    lang=context.user_data.get('lang','ru')
    context.user_data['history']=[]
    context.user_data.pop('last_report',None)
    # Revoking consent also prevents future AI sends until renewed.
    context.user_data['ai_consent']=False
    context.user_data.pop('topic_domain',None)
    await update.effective_message.reply_text(t(lang,'cleared'),reply_markup=keyboard_menu(lang))


async def criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    lang=context.user_data.get('lang')
    if not lang:
        await start(update,context);return
    text=t(lang,'rubric_note')+'\n\n'+'\n'.join(
        f'• {x[lang]} — {x["points"]} '+('балл' if lang=='kk' else 'баллов') for x in RULES['rubric'])
    text+='\n\n'+('Барлығы: 100 балл.' if lang=='kk' else 'Итого: 100 баллов.')
    await update.effective_message.reply_text(text,reply_markup=keyboard_menu(lang))


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not await _guard(update):return
    await query.answer()
    data=query.data or ''
    if data in ('lang:kk','lang:ru'):
        lang=data[5:]
        old=context.user_data.get('lang')
        context.user_data['lang']=lang
        if old!=lang:
            context.user_data['history']=[]
            context.user_data.pop('last_report',None)
            context.user_data['ai_consent']=False
            context.user_data.pop('topic_domain',None)
        await query.edit_message_text(t(lang,'welcome'),reply_markup=keyboard_menu(lang))
        return
    lang=context.user_data.get('lang')
    if lang not in ('ru','kk'):
        await query.edit_message_text(t('ru','choose'),reply_markup=keyboard_lang());return
    if data=='menu:language':
        await query.edit_message_text(t(lang,'choose'),reply_markup=keyboard_lang())
    elif data=='menu:home':
        await query.edit_message_text(t(lang,'welcome'),reply_markup=keyboard_menu(lang))
    elif data=='menu:check':
        await query.edit_message_text(t(lang,'hint_file'),reply_markup=keyboard_menu(lang))
    elif data=='menu:rules':
        await query.edit_message_text(t(lang,'rules_caption'),reply_markup=keyboard_rules(lang))
    elif data=='menu:rubric':
        text=t(lang,'rubric_note')+'\n\n'+'\n'.join(
            f'• {x[lang]} — {x["points"]}' for x in RULES['rubric'])
        text+='\n\n'+('Барлығы: 100 балл.' if lang=='kk' else 'Итого: 100 баллов.')
        await query.edit_message_text(text,reply_markup=keyboard_menu(lang))
    elif data=='menu:clear':
        context.user_data['history']=[]
        context.user_data.pop('last_report',None)
        context.user_data['ai_consent']=False
        context.user_data.pop('topic_domain',None)
        await query.edit_message_text(t(lang,'cleared'),reply_markup=keyboard_menu(lang))
    elif data=='menu:advice':
        context.user_data['chat_mode']=True
        if enabled() and not context.user_data.get('ai_consent'):
            await query.edit_message_text(t(lang,'ai_consent'),reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(lang,'agree'),callback_data='ai:yes'),
                 InlineKeyboardButton(t(lang,'decline'),callback_data='ai:no')]]))
        elif enabled():
            await query.edit_message_text(t(lang,'ai_ready')+'\n\n'+t(lang,'hint_advice'),reply_markup=keyboard_menu(lang))
        else:
            await query.edit_message_text(t(lang,'hint_advice'),reply_markup=keyboard_menu(lang))
    elif data=='ai:yes':
        context.user_data['ai_consent']=True
        await query.edit_message_text(t(lang,'ai_ready')+'\n\n'+t(lang,'hint_advice'),reply_markup=keyboard_menu(lang))
    elif data=='ai:no':
        context.user_data['ai_consent']=False
        await query.edit_message_text(t(lang,'ai_declined')+'\n\n'+t(lang,'hint_advice'),reply_markup=keyboard_menu(lang))
    elif data.startswith('faq:'):
        mapping={'format':'Какие требования к оформлению?', 'structure':'Какова структура диплома?',
                 'sources':'Как оформлять литературу IEEE?', 'intro':'Что должно быть во введении?',
                 'defense':'Как проходит защита диплома?'}
        # Map topic without needing translation of the query itself.
        q=mapping.get(data.split(':',1)[-1],'Какие требования к диплому?')
        if lang=='kk':
            q={'format':'Қаріп пен жиектерге талап қандай?', 'structure':'Диплом құрылымы қандай?',
               'sources':'IEEE әдебиеттерін қалай рәсімдеймін?', 'intro':'Кіріспеге не жазамын?',
               'defense':'Дипломды қорғау тәртібі қандай?'}.get(data.split(':',1)[-1],q)
        await query.edit_message_text(offline_answer(q,lang),reply_markup=keyboard_rules(lang))


def _short_summary(report: dict, lang: str) -> str:
    s=report['summary']
    txt=(t(lang,'outcome_intro')+'\n'+report['filename']+'\n\n'
        +t(lang,'outcome_score')+': '+(str(s['verified_compliance_percent'])+'%' if s['verified_compliance_percent'] is not None else '—')+'\n'
        +f"✅ {s['pass']}  ❌ {s['fail']}  ⚠️ {s['warning']}  👤 {s['manual']}\n\n")
    failed=[x for x in report['checks'] if x['status']=='fail'][:5]
    if failed:
        txt+=t(lang,'outcome_details')+'\n'
        txt+='\n'.join(f"• {x['title']} (§{x['ref']}): {x['detail'][:115]}" for x in failed)
    txt+='\n\n'+t(lang,'outcome_safety')
    return txt[:3900]


async def docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    lang=context.user_data.get('lang')
    if lang not in ('ru','kk'):
        await start(update,context);return
    document=update.effective_message.document
    if document is None:return
    name=Path((document.file_name or 'document').replace('\\','/')).name
    ext=Path(name).suffix.lower()
    if ext not in ('.pdf','.docx'):
        await update.effective_message.reply_text(t(lang,'bad_ext'));return
    if document.file_size and document.file_size>MAX_BYTES:
        await update.effective_message.reply_text(t(lang,'big_file'));return
    if _limited(update.effective_user.id,'docs',LIMIT_DOCUMENTS):
        await update.effective_message.reply_text(t(lang,'limit'));return
    await update.effective_message.reply_text(t(lang,'report_running'))
    try:
        with tempfile.TemporaryDirectory(prefix='muit_diploma_') as td:
            p=Path(td)/('document'+ext)
            source=await document.get_file()
            await source.download_to_drive(custom_path=p)
            if p.stat().st_size>MAX_BYTES:
                await update.effective_message.reply_text(t(lang,'big_file'));return
            report=await asyncio.wait_for(asyncio.to_thread(analyze,p,name,lang),timeout=130)
        # Only the summary (no raw student content) is retained for follow-up AI questions.
        context.user_data['last_report']={'summary':report['summary'],
             'checks':[c for c in report['checks'] if c['status']=='fail'][:8]}
        await update.effective_message.reply_text(_short_summary(report,lang))
        payload=json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8')
        if len(payload)<8_000_000:
            await update.effective_message.reply_document(
                InputFile(io.BytesIO(payload),filename='muit_report_'+lang+'.json'),
                caption=t(lang,'report_caption'),reply_markup=keyboard_menu(lang))
    except (InvalidDocument,asyncio.TimeoutError) as exc:
        log.warning('Invalid document / timeout: %s',type(exc).__name__)
        await update.effective_message.reply_text(t(lang,'doc_error'))
    except Exception:
        log.exception('Document processing failed')
        await update.effective_message.reply_text(t(lang,'doc_error'))


async def question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):return
    lang=context.user_data.get('lang')
    if lang not in ('ru','kk'):
        await start(update,context);return
    content=(update.effective_message.text or '').strip()
    if not content:return
    if len(content)>2000:
        await update.effective_message.reply_text('Сұрақты 2000 таңбаға дейін қысқартыңыз.' if lang=='kk' else 'Сократите вопрос до 2000 символов.');return
    current_domain=infer_domain(content)
    if current_domain:
        context.user_data['topic_domain']=current_domain
    if _limited(update.effective_user.id,'chat',LIMIT_QUESTIONS):
        await update.effective_message.reply_text(t(lang,'limit'));return
    if enabled() and context.user_data.get('ai_consent') and _ai_quota_exceeded():
        output=('AI тәуліктік лимиті бітті. Методичка бойынша жауап:' if lang=='kk' else 'Достигнут дневной лимит AI. Ответ по методичке:')+'\n\n'+offline_answer(content,lang,domain=context.user_data.get('topic_domain'))
    elif enabled() and context.user_data.get('ai_consent'):
        try:
            output=await asyncio.wait_for(asyncio.to_thread(
                ai_reply,content,lang,context.user_data.get('history',[]),context.user_data.get('last_report')),timeout=70)
            if not output:
                raise RuntimeError('No AI answer')
            history=context.user_data.setdefault('history',[])
            history.extend([{'role':'user','content':content},{'role':'assistant','content':output}])
            del history[:-10]
        except Exception:
            log.exception('AI request failed')
            output=offline_answer(content,lang,domain=context.user_data.get('topic_domain'))
    else:
        output=_last_report_answer(context.user_data.get('last_report'),content,lang) or offline_answer(content,lang,domain=context.user_data.get('topic_domain'))
    for pos in range(0,len(output),3600):
        await update.effective_message.reply_text(output[pos:pos+3600],reply_markup=keyboard_menu(lang) if pos+3600>=len(output) else None)


def run():
    if not TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN не задан (Railway Variables).')
    application=Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler('start',start))
    application.add_handler(CommandHandler('language',language))
    application.add_handler(CommandHandler('menu',menu))
    application.add_handler(CommandHandler('help',menu))
    application.add_handler(CommandHandler('clear',clear))
    application.add_handler(CommandHandler('criteria',criteria))
    application.add_handler(CallbackQueryHandler(buttons))
    application.add_handler(MessageHandler(filters.Document.ALL,docs))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,question))
    log.info('MUIT DiplomaCheck KZ/RU worker starting. AI configured: %s',enabled())
    application.run_polling(drop_pending_updates=True)


if __name__=='__main__':
    run()
