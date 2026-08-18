from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import MAX_QUERY_LENGTH, MAX_SEARCH_RESULTS
from .resolver import AniCliResolver, AnimeResult, ResolverError

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Send an anime title and I will show matching results.\n\n"
        "This bot returns English-dub episode links only.\n"
        "Use /cancel to clear the current selection."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text("Selection cleared. Send another anime title.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    selected = context.user_data.get("selected")

    if selected is not None:
        try:
            episode = int(text)
        except ValueError:
            await message.reply_text("Please send the episode number, for example: 12")
            return

        resolver: AniCliResolver = context.application.bot_data["resolver"]
        await message.chat.send_action(ChatAction.TYPING)
        try:
            url = await resolver.resolve_english_dub(
                query=selected["query"],
                result_index=selected["result_index"],
                episode=episode,
            )
        except ResolverError as exc:
            await message.reply_text(str(exc))
            return

        context.user_data.clear()
        await message.reply_text(
            f"English dub — episode {episode}:\n\n{url}\n\n"
            "The link may expire; request it again if it stops working."
        )
        return

    if len(text) > MAX_QUERY_LENGTH:
        await message.reply_text("Please keep the anime title under 100 characters.")
        return

    resolver = context.application.bot_data["resolver"]
    await message.chat.send_action(ChatAction.TYPING)
    try:
        results = await resolver.search(text)
    except ResolverError as exc:
        await message.reply_text(str(exc))
        return

    context.user_data["query"] = text
    context.user_data["results"] = {
        str(result.index): {
            "anime_id": result.anime_id,
            "title": result.title,
        }
        for result in results
    }

    keyboard = [
        [
            InlineKeyboardButton(
                f"{result.index}. {result.title[:55]}",
                callback_data=f"pick:{result.index}",
            )
        ]
        for result in results[:MAX_SEARCH_RESULTS]
    ]
    await message.reply_text(
        "Choose an anime:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pick_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    try:
        result_index = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("That selection is invalid. Search again.")
        return

    result = context.user_data.get("results", {}).get(str(result_index))
    search_text = context.user_data.get("query")
    if not result or not search_text:
        await query.edit_message_text("That selection expired. Send the anime title again.")
        return

    context.user_data["selected"] = {
        "query": search_text,
        "result_index": result_index,
        "anime_id": result["anime_id"],
        "title": result["title"],
    }
    await query.edit_message_text(
        f"Selected: {escape(result['title'])}\n\n"
        "Send the episode number. Only English-dub links will be returned."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


def build_application(token: str, resolver: AniCliResolver) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["resolver"] = resolver

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(pick_result, pattern=r"^pick:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application
