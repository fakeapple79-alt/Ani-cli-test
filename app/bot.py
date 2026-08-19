from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from html import escape
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
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

HOME_TEXT = (
    "<b>◈ NOOBIE ANIME HUB ◈</b>\n\n"
    "Your personal English-dub anime finder.\n\n"
    "<b>🎙 English Dub</b>  •  <b>⚡ Fast Search</b>  •  <b>🔗 Fresh Links</b>\n\n"
    "Choose an option below, or send an anime title to search."
)

ABOUT_TEXT = (
    "<b>ℹ️ ABOUT NOOBIE</b>\n\n"
    "Search anime, browse episodes, and receive a fresh English-dub link on demand.\n\n"
    "Links may expire because they are generated when requested. No video files are stored by this bot."
)

POPULAR_TITLES = [
    "One Piece",
    "Naruto",
    "Bleach",
    "Attack on Titan",
    "Black Clover",
    "My Hero Academia",
]

SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]


def _progress_bar(current: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "░" * width
    ratio = min(1.0, max(0.0, current / total))
    filled = min(width, max(0, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def _fetch_animation_lines(title: str, episode: int) -> list[str]:
    return [
        f"🎬 {title} — Episode {episode}\n\nChecking English-dub availability…",
        f"🎬 {title} — Episode {episode}\n\nResolving a fresh stream…",
        f"🎬 {title} — Episode {episode}\n\nPreparing your watch link…",
        f"🎬 {title} — Episode {episode}\n\nAlmost ready…",
    ]


MENU_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("☰  Menu")]], resize_keyboard=True, is_persistent=True
)


def _clear_transient_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("query", "results", "last_results", "selected", "episode_page", "ui_state", "last_link", "last_episode"):
        context.user_data.pop(key, None)



async def _animate(message: Any, lines: list[str], stop: asyncio.Event) -> None:
    index = 0
    while not stop.is_set():
        frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
        with suppress(Exception):
            await message.edit_text(f"{frame} {lines[index % len(lines)]}")
        index += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.75)
        except asyncio.TimeoutError:
            pass


async def _start_animation(message: Any, lines: list[str]) -> tuple[asyncio.Event, asyncio.Task[Any]]:
    stop = asyncio.Event()
    task = asyncio.create_task(_animate(message, lines, stop))
    return stop, task


async def _finish_animation(stop: asyncio.Event, task: asyncio.Task[Any]) -> None:
    stop.set()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _continue_watching(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    item = context.user_data.get("continue_watching")
    if not isinstance(item, dict):
        return None
    if not isinstance(item.get("selected"), dict) or not isinstance(item.get("episode"), int):
        return None
    return item


def _home_text(context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
    text = HOME_TEXT
    if context is not None:
        watching = _continue_watching(context)
        if watching:
            selected = watching["selected"]
            current_episode = watching["episode"]
            max_episode = int(selected.get("episode_count", 0) or 0)
            progress = (
                f"\n📺 Episode {current_episode} / {max_episode}"
                f"\nProgress  {_progress_bar(current_episode, max_episode)}"
                if max_episode
                else f"\n📺 Episode {current_episode}"
            )
            text += (
                f"\n\n<b>▶ CONTINUE WATCHING</b>\n"
                f"🎬 {escape(str(selected['title']))}{progress}"
            )
    return text


def _home_keyboard(context: ContextTypes.DEFAULT_TYPE | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    watching = _continue_watching(context) if context is not None else None
    if watching:
        selected = watching["selected"]
        max_episode = int(selected.get("episode_count", 0) or 0)
        suffix = (
            f"Episode {watching['episode']} / {max_episode}"
            if max_episode
            else f"Episode {watching['episode']}"
        )
        rows.append([
            InlineKeyboardButton(
                f"▶  Continue {selected['title'][:24]} — {suffix}",
                callback_data="binge:continue",
            )
        ])
    rows.extend([
        [InlineKeyboardButton("🔎  Search Anime", callback_data="home:search")],
        [
            InlineKeyboardButton("🔥  Popular", callback_data="home:popular"),
            InlineKeyboardButton("⭐  Favorites", callback_data="home:favorites"),
        ],
        [
            InlineKeyboardButton("🕘  Recent", callback_data="home:recent"),
            InlineKeyboardButton("ℹ️  About", callback_data="home:about"),
        ],
    ])
    return InlineKeyboardMarkup(rows)


def _back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⌂  Home", callback_data="home:main")]])


def _results_keyboard(results: list[AnimeResult]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{result.index:02d}  {result.title[:52]}",
                callback_data=f"pick:{result.index}",
            )
        ]
        for result in results[:MAX_SEARCH_RESULTS]
    ]
    rows.append([InlineKeyboardButton("✕  Cancel", callback_data="home:main")])
    return InlineKeyboardMarkup(rows)


def _episode_keyboard(page: int = 0, max_episode: int = 1) -> InlineKeyboardMarkup:
    max_episode = max(1, int(max_episode))
    max_page = max(0, (max_episode - 1) // 20)
    page = min(max(0, page), max_page)
    start = page * 20 + 1
    end = min(start + 19, max_episode)
    rows = []
    for row_start in range(start, end + 1, 5):
        row_end = min(row_start + 4, end)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{episode:02d}", callback_data=f"episode:{episode}"
                )
                for episode in range(row_start, row_end + 1)
            ]
        )
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("‹  Previous", callback_data="episodes:prev"))
    navigation.append(InlineKeyboardButton(f"Page {page + 1} / {max_page + 1}", callback_data="episodes:noop"))
    if page < max_page:
        navigation.append(InlineKeyboardButton("Next  ›", callback_data="episodes:next"))
    rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton("🔢  Jump to Episode", callback_data="episodes:jump"),
            InlineKeyboardButton("⭐  Favorite", callback_data="favorite:add"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("‹  Back", callback_data="home:results"),
            InlineKeyboardButton("⌂  Home", callback_data="home:main"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _selected_text(selected: dict[str, Any]) -> str:
    max_episode = int(selected.get("episode_count", 0) or 0)
    episode_line = f"Episodes 1–{max_episode}" if max_episode else "Episode guide loaded"
    return (
        f"<b>📺 {escape(str(selected['title']))}</b>\n\n"
        "<b>🎙 English Dub</b>\n"
        f"<b>📚 {episode_line}</b>\n"
        "Choose an episode to generate a fresh link."
    )


def _favorite_key(item: dict[str, Any]) -> str:
    return f"{item.get('query', '')}|{item.get('result_index', '')}"


def _binge_keyboard(episode: int, max_episode: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if episode < max_episode:
        rows.append([InlineKeyboardButton(f"⏭️  Episode {episode + 1}", callback_data="binge:next")])
    if episode > 1:
        rows.append([InlineKeyboardButton(f"⬅️  Episode {episode - 1}", callback_data="binge:prev")])
    rows.extend([
        [InlineKeyboardButton("📋  All Episodes", callback_data="episodes:show")],
        [
            InlineKeyboardButton("⭐  Favorite", callback_data="favorite:add"),
            InlineKeyboardButton("🏠  Menu", callback_data="home:main"),
        ],
    ])
    return InlineKeyboardMarkup(rows)


def _unavailable_keyboard(episode: int, final: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if final:
        rows.append([InlineKeyboardButton("🔎  Find Another Anime", callback_data="home:search")])
    else:
        rows.append([InlineKeyboardButton("🔄  Check Again", callback_data="binge:retry")])
        rows.append([InlineKeyboardButton("📋  Episodes", callback_data="episodes:show")])
    rows.append([InlineKeyboardButton("🏠  Menu", callback_data="home:main")])
    return InlineKeyboardMarkup(rows)


def _source_keyboard(episode: int, url: str, max_episode: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"▶  Watch Episode {episode}", url=url)],
        [InlineKeyboardButton("🔗  Show Link", callback_data="source:show")],
    ]
    if episode < max_episode:
        rows.append([InlineKeyboardButton(f"⏭️  Episode {episode + 1}", callback_data="binge:next")])
    if episode > 1:
        rows.append([InlineKeyboardButton(f"⬅️  Episode {episode - 1}", callback_data="binge:prev")])
    rows.extend([
        [InlineKeyboardButton("📋  All Episodes", callback_data="episodes:show")],
        [
            InlineKeyboardButton("⭐  Favorite", callback_data="favorite:add"),
            InlineKeyboardButton("🏠  Menu", callback_data="home:main"),
        ],
    ])
    return InlineKeyboardMarkup(rows)


async def _search(
    query_text: str,
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    status = await message.reply_text("◐ Connecting to the anime index…")
    stop, task = await _start_animation(
        status,
        [
            "◐ Searching the anime index…",
            "◓ Matching anime titles…",
            "◑ Checking English-dub results…",
            "◒ Preparing your results…",
        ],
    )
    try:
        results = await resolver.search(query_text)
    except ResolverError as exc:
        await _finish_animation(stop, task)
        await status.edit_text(f"⚠️ {escape(str(exc))}", reply_markup=_home_keyboard())
        return
    finally:
        if not stop.is_set():
            await _finish_animation(stop, task)

    context.user_data["query"] = query_text
    context.user_data["results"] = {
        str(result.index): {
            "anime_id": result.anime_id,
            "title": result.title,
        }
        for result in results
    }
    context.user_data["last_results"] = results
    context.user_data["ui_state"] = "results"

    await status.edit_text(
        f"<b>🔎 RESULTS FOR</b>  <code>{escape(query_text)}</code>\n\n"
        "Choose an anime to continue:",
        parse_mode=ParseMode.HTML,
        reply_markup=_results_keyboard(results),
    )


def _store_recent(context: ContextTypes.DEFAULT_TYPE, selected: dict[str, Any]) -> None:
    recent = context.user_data.setdefault("recent", [])
    recent[:] = [item for item in recent if _favorite_key(item) != _favorite_key(selected)]
    recent.insert(0, selected.copy())
    del recent[8:]


def _store_favorite(context: ContextTypes.DEFAULT_TYPE, selected: dict[str, Any]) -> None:
    favorites = context.user_data.setdefault("favorites", [])
    if not any(_favorite_key(item) == _favorite_key(selected) for item in favorites):
        favorites.append(selected.copy())


async def _load_episode_count(
    context: ContextTypes.DEFAULT_TYPE, selected: dict[str, Any]
) -> int:
    cached = selected.get("episode_count")
    if isinstance(cached, int) and cached > 0:
        return cached
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    count = await resolver.get_episode_count(selected["anime_id"])
    selected["episode_count"] = count
    return count


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await update.effective_message.reply_text(
        _home_text(context),
        parse_mode=ParseMode.HTML,
        reply_markup=_home_keyboard(context),
    )
    await update.effective_message.reply_text(
        "Use the menu below anytime.", reply_markup=MENU_REPLY_KEYBOARD
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await update.effective_message.reply_text(
        "<b>Selection cleared.</b>\n\nBack at the Anime Hub.",
        parse_mode=ParseMode.HTML,
        reply_markup=_home_keyboard(context),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await update.effective_message.reply_text(
        _home_text(context),
        parse_mode=ParseMode.HTML,
        reply_markup=_home_keyboard(context),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text in {"☰ Menu", "☰  Menu"}:
        await menu(update, context)
        return
    selected = context.user_data.get("selected")
    if selected is not None:
        try:
            episode = int(text)
        except ValueError:
            await message.reply_text("🔢 Send an episode number, for example <b>12</b>.", parse_mode=ParseMode.HTML)
            return
        await _resolve_episode(message, context, selected, episode, from_callback=False)
        return

    if len(text) > MAX_QUERY_LENGTH:
        await message.reply_text("Please keep the anime title under 100 characters.")
        return

    await _search(text, message, context)


async def pick_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer("Opening anime card…")

    try:
        result_index = int((query.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        await query.edit_message_text("That selection is invalid.", reply_markup=_home_keyboard())
        return

    result = context.user_data.get("results", {}).get(str(result_index))
    search_text = context.user_data.get("query")
    if not result or not search_text:
        await query.edit_message_text("That search expired. Start again.", reply_markup=_home_keyboard())
        return

    selected = {
        "query": search_text,
        "result_index": result_index,
        "anime_id": result["anime_id"],
        "title": result["title"],
    }
    stop, task = await _start_animation(
        query.message,
        [
            f"🎬 Opening {selected['title']}…",
            "📡 Loading episode guide…",
            "◓ Counting available episodes…",
            "✓ Building your episode browser…",
        ],
    )
    try:
        await _load_episode_count(context, selected)
    except ResolverError as exc:
        await _finish_animation(stop, task)
        await query.edit_message_text(
            f"⚠️ {escape(str(exc))}\n\nPlease try selecting the anime again.",
            parse_mode=ParseMode.HTML,
            reply_markup=_home_keyboard(context),
        )
        return
    finally:
        if not stop.is_set():
            await _finish_animation(stop, task)

    context.user_data["selected"] = selected
    context.user_data["episode_page"] = 0
    context.user_data["ui_state"] = "episodes"
    _store_recent(context, selected)

    await query.edit_message_text(
        _selected_text(selected),
        parse_mode=ParseMode.HTML,
        reply_markup=_episode_keyboard(0, selected["episode_count"]),
    )


async def _resolve_episode(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    selected: dict[str, Any],
    episode: int,
    from_callback: bool,
) -> None:
    try:
        max_episode = await _load_episode_count(context, selected)
    except ResolverError as exc:
        text = f"⚠️ {escape(str(exc))}\n\nThe episode guide could not be loaded."
        if from_callback:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
        return

    if episode < 1 or episode > max_episode:
        text = (
            f"⚠️ Episode {episode} does not exist for {escape(str(selected['title']))}.\n\n"
            f"This anime has episodes 1–{max_episode}."
        )
        if from_callback:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=_episode_keyboard((max_episode - 1) // 20, max_episode))
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_episode_keyboard((max_episode - 1) // 20, max_episode))
        return

    title = str(selected["title"])
    if from_callback:
        work_message = message
        await work_message.edit_text(
            f"<b>🎬 {escape(title)} — Episode {episode}</b>\n\n◐ Starting secure fetch…",
            parse_mode=ParseMode.HTML,
        )
    else:
        work_message = await message.reply_text(
            f"<b>🎬 {escape(title)} — Episode {episode}</b>\n\n◐ Starting secure fetch…",
            parse_mode=ParseMode.HTML,
        )

    stop, task = await _start_animation(work_message, _fetch_animation_lines(title, episode))
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    try:
        url = await resolver.resolve_english_dub(
            query=selected["query"],
            result_index=selected["result_index"],
            episode=episode,
        )
    except ResolverError as exc:
        await _finish_animation(stop, task)
        context.user_data["binge_pending"] = {"selected": selected.copy(), "episode": episode}
        error_text = str(exc).lower()
        final = episode > max_episode or "final" in error_text or "last episode" in error_text
        if final:
            body = (
                f"<b>🏁 You've reached the final episode!</b>\n\n"
                f"🎬 {escape(str(selected['title']))}\n"
                f"Episode {episode} is the end of the available run."
            )
        else:
            body = (
                f"<b>⚠️ Episode {episode} isn't available yet</b>\n\n"
                f"🎬 {escape(str(selected['title']))}\n"
                "There is no usable English-dub stream right now."
            )
        await work_message.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=_unavailable_keyboard(episode, final=final),
        )
        return
    finally:
        if not stop.is_set():
            await _finish_animation(stop, task)

    context.user_data["last_link"] = url
    context.user_data["last_episode"] = episode
    context.user_data["continue_watching"] = {"selected": selected.copy(), "episode": episode}
    context.user_data.pop("binge_pending", None)
    context.user_data["ui_state"] = "source"
    await work_message.edit_text(
        f"<b>🎬 {escape(title)}</b>\n"
        f"📺 Episode <b>{episode}</b> / {max_episode}\n"
        "🎙 <b>English Dub</b>  •  <b>✓ READY</b>\n"
        f"Progress  {_progress_bar(episode, max_episode)}\n\n"
        f"▶️ Watch Episode {episode}\n"
        "──────────────\n"
        "<b>Continue watching?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_source_keyboard(episode, url, max_episode),
    )


def _current_binge_target(context: ContextTypes.DEFAULT_TYPE) -> tuple[dict[str, Any], int] | None:
    watching = _continue_watching(context)
    if watching:
        return watching["selected"], watching["episode"]
    selected = context.user_data.get("selected")
    episode = context.user_data.get("last_episode")
    if isinstance(selected, dict) and isinstance(episode, int):
        return selected, episode
    return None


async def binge_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    data = query.data or ""
    target = _current_binge_target(context)
    if data == "binge:continue":
        watching = _continue_watching(context)
        if not watching:
            await query.answer("Nothing to continue yet.", show_alert=False)
            await query.edit_message_text(HOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
            return
        await query.answer("Continuing…")
        selected, episode = watching["selected"], watching["episode"]
        context.user_data["selected"] = selected.copy()
        await _resolve_episode(query.message, context, selected, episode, from_callback=True)
        return

    if not target:
        await query.answer("Your session expired. Search the anime again.", show_alert=True)
        await query.edit_message_text(HOME_TEXT, parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
        return

    selected, current_episode = target
    try:
        max_episode = await _load_episode_count(context, selected)
    except ResolverError as exc:
        await query.answer("Episode guide unavailable.", show_alert=True)
        await query.edit_message_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
        return

    if data == "binge:next":
        if current_episode >= max_episode:
            await query.answer("This is the final episode.", show_alert=False)
            await query.edit_message_text(
                f"<b>🏁 You've reached the final episode!</b>\n\n"
                f"🎬 {escape(str(selected['title']))}\n"
                f"Episode {max_episode} is the final indexed episode.",
                parse_mode=ParseMode.HTML,
                reply_markup=_unavailable_keyboard(max_episode, final=True),
            )
            return
        episode = current_episode + 1
        await query.answer(f"Fetching Episode {episode}…")
    elif data == "binge:prev":
        if current_episode <= 1:
            await query.answer("This is the first episode.", show_alert=False)
            return
        episode = current_episode - 1
        await query.answer(f"Fetching Episode {episode}…")
    elif data == "binge:retry":
        pending = context.user_data.get("binge_pending")
        if not isinstance(pending, dict) or not isinstance(pending.get("episode"), int):
            await query.answer("There is no episode waiting to retry.", show_alert=False)
            return
        selected = pending["selected"]
        episode = pending["episode"]
        await query.answer(f"Checking Episode {episode} again…")
    else:
        return

    context.user_data["selected"] = selected.copy()
    await _resolve_episode(query.message, context, selected, episode, from_callback=True)


async def episode_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    data = query.data or ""
    selected = context.user_data.get("selected")
    if not selected:
        await query.edit_message_text("This selection expired.", reply_markup=_home_keyboard())
        return

    try:
        max_episode = await _load_episode_count(context, selected)
    except ResolverError as exc:
        await query.edit_message_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
        return

    if data == "episodes:noop":
        return
    if data == "episodes:next":
        max_page = max(0, (max_episode - 1) // 20)
        context.user_data["episode_page"] = min(max_page, context.user_data.get("episode_page", 0) + 1)
        await query.edit_message_reply_markup(_episode_keyboard(context.user_data["episode_page"], max_episode))
        return
    if data == "episodes:prev":
        context.user_data["episode_page"] = max(0, context.user_data.get("episode_page", 0) - 1)
        await query.edit_message_reply_markup(_episode_keyboard(context.user_data["episode_page"], max_episode))
        return
    if data == "episodes:jump":
        context.user_data["ui_state"] = "jump"
        await query.edit_message_text(
            f"<b>🔢 JUMP TO EPISODE</b>\n\n{escape(str(selected['title']))}\n\nType an episode number.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✕  Cancel", callback_data="episodes:show")]]),
        )
        return
    if data == "episodes:show":
        context.user_data["ui_state"] = "episodes"
        await query.edit_message_text(_selected_text(selected), parse_mode=ParseMode.HTML, reply_markup=_episode_keyboard(context.user_data.get("episode_page", 0), max_episode))
        return
    if data == "favorite:add":
        _store_favorite(context, selected)
        await query.answer("Added to Favorites ⭐", show_alert=False)
        return
    if data.startswith("episode:"):
        try:
            episode = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid episode", show_alert=True)
            return
        await _resolve_episode(query.message, context, selected, episode, from_callback=True)


async def source_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    if query.data == "source:show":
        url = context.user_data.get("last_link")
        if not url:
            await query.answer("That link expired from this session.", show_alert=True)
            return
        await query.message.reply_text(
            f"<b>🔗 Episode link</b>\n\n<code>{escape(url)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def home_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()
    action = (query.data or "").split(":", 1)[1]

    if action == "main":
        await query.edit_message_text(_home_text(context), parse_mode=ParseMode.HTML, reply_markup=_home_keyboard(context))
    elif action == "search":
        await query.edit_message_text("<b>🔎 SEARCH ANIME</b>\n\nType the anime title below.", parse_mode=ParseMode.HTML, reply_markup=_back_home_keyboard())
        context.user_data["selected"] = None
    elif action == "about":
        await query.edit_message_text(ABOUT_TEXT, parse_mode=ParseMode.HTML, reply_markup=_back_home_keyboard())
    elif action == "popular":
        keyboard = [[InlineKeyboardButton(f"🔥  {title}", callback_data=f"popular:{index}")] for index, title in enumerate(POPULAR_TITLES)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        await query.edit_message_text("<b>🔥 POPULAR ANIME</b>\n\nChoose a title to search:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "favorites":
        favorites = context.user_data.get("favorites", [])
        if not favorites:
            await query.edit_message_text("<b>⭐ FAVORITES</b>\n\nNo favorites yet. Add one from an episode screen.", parse_mode=ParseMode.HTML, reply_markup=_back_home_keyboard())
            return
        keyboard = [[InlineKeyboardButton(f"⭐  {item['title'][:55]}", callback_data=f"favorite:{index}")] for index, item in enumerate(favorites)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        await query.edit_message_text("<b>⭐ MY ANIME</b>\n\nChoose a favorite:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "recent":
        recent = context.user_data.get("recent", [])
        if not recent:
            await query.edit_message_text("<b>🕘 RECENT</b>\n\nYour recent anime will appear here.", parse_mode=ParseMode.HTML, reply_markup=_back_home_keyboard())
            return
        keyboard = [[InlineKeyboardButton(f"🕘  {item['title'][:55]}", callback_data=f"recent:{index}")] for index, item in enumerate(recent)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        await query.edit_message_text("<b>🕘 RECENT ANIME</b>\n\nChoose a title:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
    elif action == "results":
        results = context.user_data.get("last_results", [])
        await query.edit_message_text("<b>🔎 SEARCH RESULTS</b>\n\nChoose an anime:", parse_mode=ParseMode.HTML, reply_markup=_results_keyboard(results))


async def list_item_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    data = query.data or ""
    kind, raw_index = data.split(":", 1)
    try:
        index = int(raw_index)
    except ValueError:
        return
    items = context.user_data.get("favorites" if kind == "favorite" else "recent", [])
    if index < 0 or index >= len(items):
        await query.edit_message_text("That item expired.", reply_markup=_home_keyboard())
        return
    selected = items[index].copy()
    try:
        await _load_episode_count(context, selected)
    except ResolverError as exc:
        await query.edit_message_text(
            f"⚠️ {escape(str(exc))}",
            parse_mode=ParseMode.HTML,
            reply_markup=_home_keyboard(context),
        )
        return
    context.user_data["selected"] = selected
    context.user_data["episode_page"] = 0
    context.user_data["ui_state"] = "episodes"
    await query.edit_message_text(
        _selected_text(selected),
        parse_mode=ParseMode.HTML,
        reply_markup=_episode_keyboard(0, selected["episode_count"]),
    )


async def popular_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer("Searching…")
    try:
        index = int((query.data or "").split(":", 1)[1])
        title = POPULAR_TITLES[index]
    except (IndexError, ValueError):
        await query.edit_message_text("That title is unavailable.", reply_markup=_home_keyboard())
        return
    await query.message.edit_text("◐ Searching the anime index…")
    status = query.message
    stop, task = await _start_animation(status, ["Searching…", "Matching titles…", "Preparing results…"])
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    try:
        results = await resolver.search(title)
    except ResolverError as exc:
        await _finish_animation(stop, task)
        await status.edit_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML, reply_markup=_home_keyboard())
        return
    finally:
        if not stop.is_set():
            await _finish_animation(stop, task)
    context.user_data["query"] = title
    context.user_data["results"] = {str(result.index): {"anime_id": result.anime_id, "title": result.title} for result in results}
    context.user_data["last_results"] = results
    await status.edit_text(f"<b>🔎 RESULTS FOR</b>  <code>{escape(title)}</code>", parse_mode=ParseMode.HTML, reply_markup=_results_keyboard(results))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


def build_application(token: str, resolver: AniCliResolver) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["resolver"] = resolver

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(home_action, pattern=r"^home:(main|search|popular|favorites|recent|about|results)$"))
    application.add_handler(CallbackQueryHandler(binge_action, pattern=r"^binge:(next|prev|continue|retry)$"))
    application.add_handler(CallbackQueryHandler(popular_action, pattern=r"^popular:\d+$"))
    application.add_handler(CallbackQueryHandler(list_item_action, pattern=r"^(favorite|recent):\d+$"))
    application.add_handler(CallbackQueryHandler(episode_action, pattern=r"^(episode:\d+|episodes:(next|prev|jump|show|noop)|favorite:add)$"))
    application.add_handler(CallbackQueryHandler(source_action, pattern=r"^source:show$"))
    application.add_handler(CallbackQueryHandler(pick_result, pattern=r"^pick:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application
