"""Stand-alone Russian DiplomaCheck Telegram bot for an always-on worker.

No local HTTP server, public website, WhatsApp account, or AI API key needed.
Keep exactly ONE worker instance online per Telegram token (long polling).
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
from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .engine import analyze
from .extractors import InvalidDocument, MAX_BYTES
from .rules import ROOT, rubric_view

load_dotenv(ROOT / '.env')
logger = logging.getLogger('muit_bot')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')

BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
ALLOWED_IDS = set()
for val in os.getenv('ALLOWED_TELEGRAM_USER_IDS', '').split(','):
    if val.strip():
        try:
            ALLOWED_IDS.add(int(val.strip()))
        except ValueError:
            raise SystemExit('ALLOWED_TELEGRAM_USER_IDS должен содержать только числа, разделённые запятыми.')

# Limit one user to 5 scans per hour, to reduce excessive resource usage.
_last_scans: dict[int, list[float]] = defaultdict(list)
MAX_SCANS_PER_HOUR = 5

WELCOME = (
    '🎓 MUIT DiplomaCheck\n\n'
    'Пришлите диплом в формате PDF или DOCX. Бот проверит структуру и '
    'оформление по методическим указаниям МУИТ от 18.11.2020.\n\n'
    'Проверка предварительная и не заменяет решение комиссии. '
    'Отправляйте только документы, которые вправе передавать через Telegram.\n\n'
    '/criteria — критерии защиты; /help — помощь'
)


def _allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not ALLOWED_IDS or user.id in ALLOWED_IDS))


async def _guard(update: Update) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if chat is None or chat.type != 'private':
        if message:
            await message.reply_text('Пожалуйста, откройте личный чат с ботом.')
        return False
    if not _allowed(update):
        await message.reply_text('Этот бот пока доступен только тестовым пользователям.')
        return False
    return True


def _rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    stamps = _last_scans[user_id]
    stamps[:] = [t for t in stamps if now - t < 3600]
    if len(stamps) >= MAX_SCANS_PER_HOUR:
        return False
    stamps.append(now)
    # Avoid unbounded accumulation of inactive IDs in long-running process.
    if len(_last_scans) > 10000:
        for uid in list(_last_scans):
            _last_scans[uid][:] = [t for t in _last_scans[uid] if now-t < 3600]
            if not _last_scans[uid]:
                del _last_scans[uid]
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update):
        await update.effective_message.reply_text(WELCOME)


async def criteria(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    rubric = rubric_view()
    parts = ['📋 Критерии комиссии (Приложение 2):', '']
    parts.extend(f'• {item["name"]}: {item["max_points"]} баллов' for item in rubric['rubric'])
    parts.append(f'\nИтого: {rubric["possible_total"]} баллов.')
    parts.append('Бот не ставит официальную оценку за защиту.')
    await update.effective_message.reply_text('\n'.join(parts))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    message = update.effective_message
    doc = message.document
    if not doc:
        return
    # Support backslash paths embedded in filenames without allowing path traversal.
    name = Path((doc.file_name or 'document').replace('\\', '/')).name
    extension = Path(name).suffix.lower()
    if extension not in {'.pdf', '.docx'}:
        await message.reply_text('Допустимы только PDF и DOCX.')
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await message.reply_text('Максимальный размер файла — 20 МБ.')
        return
    if not _rate_limit(update.effective_user.id):
        await message.reply_text('Лимит: не более 5 документов за час на пользователя.')
        return
    await message.reply_text('📄 Документ получен. Проверяю…')
    try:
        with tempfile.TemporaryDirectory(prefix='muit_cloud_') as folder:
            path = Path(folder) / ('upload' + extension)
            file = await doc.get_file()
            await file.download_to_drive(custom_path=path)
            if path.stat().st_size > MAX_BYTES:
                await message.reply_text('Максимальный размер файла — 20 МБ.')
                return
            # The uploaded file is deleted automatically when TemporaryDirectory exits.
            # No external AI is used. Without LibreOffice DOCX page count may be manual.
            report = await asyncio.wait_for(
                asyncio.to_thread(analyze, path, name, False), timeout=180
            )
        summary = report['summary']
        score = summary.get('verified_compliance_percent')
        heading = f'📑 {name}\n\n'
        heading += f'Соответствие проверенным требованиям: {score}%\n' if score is not None else 'Соответствие: не определено\n'
        heading += (
            f'✅ Соответствует: {summary["pass"]}\n'
            f'❌ Ошибки: {summary["fail"]}\n'
            f'⚠️ Предупреждения: {summary["warning"]}\n'
            f'👤 Нужна ручная проверка: {summary["manual"]}\n\n'
        )
        failed = [c for c in report.get('checks', []) if c['status'] == 'fail'][:5]
        if failed:
            heading += 'Основные замечания:\n'
            heading += '\n'.join(f'• {c["title"]} (п. {c["ref"]}): {c["detail"][:110]}' for c in failed)
        heading += '\n\nЭто не официальная оценка аттестационной комиссии.'
        await message.reply_text(heading[:3900])
        payload = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
        if len(payload) <= 8 * 1024 * 1024:
            await message.reply_document(
                InputFile(io.BytesIO(payload), filename='muit_report.json'),
                caption='Полный отчёт проверки по методичке (JSON).'
            )
        else:
            await message.reply_text('Подробный отчёт слишком велик для отправки.')
    except InvalidDocument as exc:
        await message.reply_text(f'Файл не подходит для проверки: {exc}')
    except asyncio.TimeoutError:
        await message.reply_text('Время проверки превышено. Попробуйте более короткий документ.')
    except Exception:
        logger.exception('Document processing error')
        await message.reply_text('Не удалось обработать документ. Попробуйте другой PDF/DOCX.')


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit('TELEGRAM_BOT_TOKEN не задан. Укажите переменную на сервере.')
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', start))
    application.add_handler(CommandHandler('criteria', criteria))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info('Telegram worker starting (polling; no website required).')
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
