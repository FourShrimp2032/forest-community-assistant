"""Telegram transport (python-telegram-bot, long polling). Private chats only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

from .conversation import Conversation, Reply

log = logging.getLogger(__name__)

MAX_MESSAGE = 4000
COMMANDS = ["start", "help", "reset", "my_documents", "pending"]


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    rows = []
    for row in reply.buttons:
        rows.append([InlineKeyboardButton(b.label, url=b.url) if b.url else
                     InlineKeyboardButton(b.label, callback_data=b.data) for b in row])
    return InlineKeyboardMarkup(rows)


def _split(text: str) -> list[str]:
    if len(text) <= MAX_MESSAGE:
        return [text]
    parts, current = [], ""
    for para in text.split("\n"):
        if len(current) + len(para) + 1 > MAX_MESSAGE and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + para
    if current:
        parts.append(current)
    return parts


def _strip_html(text: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class TelegramBot:
    def __init__(self, token: str, conversation: Conversation, on_startup=None, on_shutdown=None):
        self.conversation = conversation
        builder = Application.builder().token(token).concurrent_updates(True)
        if on_startup:
            builder = builder.post_init(on_startup)
        if on_shutdown:
            builder = builder.post_shutdown(on_shutdown)
        self.app: Application = builder.build()
        private = filters.ChatType.PRIVATE
        for command in COMMANDS:
            self.app.add_handler(CommandHandler(command, self._command, filters=private))
        self.app.add_handler(MessageHandler(private & filters.Document.ALL, self._document))
        self.app.add_handler(MessageHandler(private & filters.PHOTO, self._photo))
        self.app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self._text))
        self.app.add_handler(MessageHandler(private & filters.COMMAND, self._command))
        self.app.add_handler(CallbackQueryHandler(self._callback))
        self.app.add_error_handler(self._error)
        conversation.downloader = self._download

    def run(self) -> None:
        self.app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)

    async def _download(self, file_id: str) -> bytes:
        tg_file = await self.app.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    # ------------------------------------------------------------------ handlers
    async def _command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        text = update.message.text or ""
        command, _, args = text.partition(" ")
        async with self.conversation.user_lock(user_id):
            replies = await self.conversation.on_command(user_id, command, args)
        await self._send(update, replies)

    async def _text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        async with self.conversation.user_lock(user_id), self._typing(update):
            replies = await self.conversation.on_text(user_id, update.message.text or "")
        await self._send(update, replies)

    async def _document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        doc = update.message.document
        async with self.conversation.user_lock(user_id), self._typing(update):
            replies = await self.conversation.on_document(
                user_id=user_id, chat_id=update.effective_chat.id, file_id=doc.file_id,
                filename=doc.file_name or "document", size=doc.file_size, received_at=update.message.date)
        await self._send(update, replies)

    async def _photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from . import messages
        await self._send(update, [Reply(messages.PHOTO_NOT_SUPPORTED)])

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user_id = query.from_user.id
        # Location buttons may trigger a full answer, so show "typing…" here too.
        async with self.conversation.user_lock(user_id), self._typing(update):
            replies = await self.conversation.on_callback(user_id, query.data or "")
        toast = next((r.toast for r in replies if r.toast), None)
        with contextlib.suppress(TelegramError):
            await query.answer(toast)
        await self._send(update, [r for r in replies if r.text or r.document_file_id])

    async def _error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error while processing update: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            with contextlib.suppress(TelegramError):
                await context.bot.send_message(update.effective_chat.id,
                                               "Сталася технічна помилка. Спробуйте, будь ласка, ще раз.")

    # ------------------------------------------------------------------ sending
    @contextlib.asynccontextmanager
    async def _typing(self, update: Update):
        chat_id = update.effective_chat.id

        async def keep_typing():
            while True:
                with contextlib.suppress(TelegramError):
                    await self.app.bot.send_chat_action(chat_id, ChatAction.TYPING)
                await asyncio.sleep(4)

        task = asyncio.create_task(keep_typing())
        try:
            yield
        finally:
            task.cancel()

    async def _send(self, update: Update, replies: list[Reply]) -> None:
        chat_id = update.effective_chat.id
        for reply in replies:
            for attempt in range(3):
                try:
                    await self._send_one(update, chat_id, reply)
                    break
                except RetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                except NetworkError as exc:
                    log.warning("Telegram network error (attempt %d): %s", attempt + 1, exc)
                    await asyncio.sleep(2 ** attempt)
                except Forbidden:
                    log.info("User %s blocked the bot", chat_id)
                    return

    async def _send_one(self, update: Update, chat_id: int, reply: Reply) -> None:
        bot = self.app.bot
        if reply.document_file_id:
            await bot.send_document(chat_id, reply.document_file_id)
            return
        markup = _markup(reply)
        if reply.edit and update.callback_query and update.callback_query.message:
            try:
                await update.callback_query.edit_message_text(reply.text, parse_mode=ParseMode.HTML,
                                                              reply_markup=markup, disable_web_page_preview=True)
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                log.info("Edit failed (%s); sending a new message", exc)
        parts = _split(reply.text)
        for i, part in enumerate(parts):
            part_markup = markup if i == len(parts) - 1 else None
            try:
                await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML, reply_markup=part_markup,
                                       disable_web_page_preview=True)
            except BadRequest as exc:
                if "parse" in str(exc).lower() or "entit" in str(exc).lower():
                    log.warning("HTML rejected by Telegram (%s); sending plain text", exc)
                    await bot.send_message(chat_id, _strip_html(part), reply_markup=part_markup,
                                           disable_web_page_preview=True)
                else:
                    raise
