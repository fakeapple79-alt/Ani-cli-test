from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from html import escape
from pathlib import Path
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
    "<b>◈ ANISIGNAL / ANIME HUB</b>\n"
    "<code>ENGLISH DUB DEFAULT · JAPANESE ORIGINAL OPTIONAL · ON AIR</code>\n\n"
    "<b>Your next binge is already tuned in.</b>\n"
    "Search a title, choose an episode, and keep the next one close.\n\n"
    "<b>⚡ FAST SEARCH</b>  •  <b>🎙 AUDIO CHOICE</b>  •  <b>🔗 FRESH LINKS</b>"
)

ABOUT_TEXT = (
    "<b>◈ ABOUT / ANISIGNAL</b>\n"
    "<code>YOUR ANIME AUDIO CONTROL ROOM</code>\n\n"
    "Search anime, browse the real episode range, and receive a fresh English-dub or Japanese-original link on demand.\n\n"
    "<b>HOW IT WORKS</b>\n"
    "1. Tune into an anime.\n"
    "2. Pick an indexed episode.\n"
    "3. Get a fresh link, then keep binging.\n\n"
    "<i>Links can expire because they are generated when requested. No video files are stored by AniSignal.</i>"
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
CONTACT_DEV_URL = "https://t.me/prithvirajnagvanshi"
WELCOME_ART_PATH = Path(__file__).parent / "assets" / "anisignal-welcome.jpg"
AUDIO_MODE_DUB = "dub"
AUDIO_MODE_SUB = "sub"


def _audio_mode(context: ContextTypes.DEFAULT_TYPE | None = None) -> str:
    if context is not None and context.user_data.get("audio_mode") == AUDIO_MODE_SUB:
        return AUDIO_MODE_SUB
    return AUDIO_MODE_DUB


def _audio_label(audio_mode: str) -> str:
    return "Japanese Original" if audio_mode == AUDIO_MODE_SUB else "English Dub"


def _audio_short_label(audio_mode: str) -> str:
    return "JP Original" if audio_mode == AUDIO_MODE_SUB else "English Dub"


def _audio_settings_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    audio_mode = _audio_mode(context)
    detail = (
        "English-dub streams are checked first by default."
        if audio_mode == AUDIO_MODE_DUB
        else "Japanese-original streams are now selected for new episode links."
    )
    return (
        "<b>◈ AUDIO SIGNAL</b>\n"
        "<code>VOICE TRACK / YOUR CHOICE</code>\n\n"
        f"<b>ACTIVE:</b> 🎙 {_audio_label(audio_mode)}\n\n"
        f"{detail}\n\n"
        "Choose the audio track you want AniSignal to use."
    )


def _audio_settings_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    audio_mode = _audio_mode(context)
    dub_label = "✓  English Dub" if audio_mode == AUDIO_MODE_DUB else "English Dub"
    sub_label = "✓  Japanese Original" if audio_mode == AUDIO_MODE_SUB else "Japanese Original"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🎙  {dub_label}", callback_data="audio:dub"),
            InlineKeyboardButton(f"🎧  {sub_label}", callback_data="audio:sub"),
        ],
        [InlineKeyboardButton("⌂  Back to Menu", callback_data="home:main")],
        [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
    ])


def _progress_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    ratio = min(1.0, max(0.0, current / total))
    filled = min(width, max(1 if current > 0 else 0, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def _fetch_animation_lines(title: str, episode: int, audio_mode: str) -> list[str]:
    safe_title = escape(title)
    audio_label = _audio_label(audio_mode)
    return [
        f"<b>🎬 {safe_title} / EP. {episode:02d}</b>\n<code>SECURE FETCH / 01</code>\n\nChecking {audio_label.lower()} availability…",
        f"<b>🎬 {safe_title} / EP. {episode:02d}</b>\n<code>SECURE FETCH / 02</code>\n\nResolving a fresh stream…",
        f"<b>🎬 {safe_title} / EP. {episode:02d}</b>\n<code>SECURE FETCH / 03</code>\n\nPreparing your watch link…",
        f"<b>🎬 {safe_title} / EP. {episode:02d}</b>\n<code>SECURE FETCH / 04</code>\n\nSignal almost ready…",
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
            await message.edit_text(
                f"{frame} {lines[index % len(lines)]}",
                parse_mode=ParseMode.HTML,
            )
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
        text += f"\n\n<b>🎙 AUDIO:</b> {_audio_short_label(_audio_mode(context))}"
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
                "\n\n──────────────\n"
                "<b>▶ ON DECK / CONTINUE WATCHING</b>\n"
                f"🎬 <b>{escape(str(selected['title']))}</b>{progress}"
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
            InlineKeyboardButton("🎙  Audio", callback_data="home:audio"),
        ],
        [InlineKeyboardButton("ℹ️  About", callback_data="home:about")],
        [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
    ])
    return InlineKeyboardMarkup(rows)


def _back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⌂  Home", callback_data="home:main"),
            InlineKeyboardButton("🎙  Audio", callback_data="home:audio"),
        ],
        [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
    ])


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
    rows.append([InlineKeyboardButton("🎙  Audio", callback_data="home:audio")])
    rows.append([InlineKeyboardButton("⌂  Back to Menu", callback_data="home:main")])
    rows.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
    return InlineKeyboardMarkup(rows)


def _episode_keyboard(
    page: int = 0,
    max_episode: int = 1,
    audio_mode: str = AUDIO_MODE_DUB,
) -> InlineKeyboardMarkup:
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
    rows.append([InlineKeyboardButton(f"🎙  Audio: {_audio_short_label(audio_mode)}", callback_data="home:audio")])
    rows.append(
        [
            InlineKeyboardButton("‹  Search Results", callback_data="home:results"),
            InlineKeyboardButton("⌂  Home", callback_data="home:main"),
        ]
    )
    rows.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
    return InlineKeyboardMarkup(rows)


def _selected_text(selected: dict[str, Any], audio_mode: str = AUDIO_MODE_DUB) -> str:
    max_episode = int(selected.get("episode_count", 0) or 0)
    episode_line = f"Episodes 1–{max_episode}" if max_episode else "Episode guide loaded"
    return (
        f"<b>🎬 {escape(str(selected['title']))}</b>\n"
        "<code>ANIME INDEX / VERIFIED RANGE</code>\n\n"
        f"<b>🎙 {_audio_label(audio_mode).upper()}</b>\n"
        f"<b>▦ {episode_line.upper()}</b>\n\n"
        "Choose an episode to generate a fresh watch link."
    )


def _favorite_key(item: dict[str, Any]) -> str:
    return f"{item.get('query', '')}|{item.get('result_index', '')}"


def _binge_keyboard(
    episode: int,
    max_episode: int,
    audio_mode: str = AUDIO_MODE_DUB,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if episode < max_episode:
        rows.append([InlineKeyboardButton(f"⏭️  Episode {episode + 1}", callback_data="binge:next")])
    if episode > 1:
        rows.append([InlineKeyboardButton(f"⬅️  Episode {episode - 1}", callback_data="binge:prev")])
    rows.extend([
        [InlineKeyboardButton("📋  All Episodes", callback_data="episodes:show")],
        [InlineKeyboardButton(f"🎙  Audio: {_audio_short_label(audio_mode)}", callback_data="home:audio")],
        [
            InlineKeyboardButton("⭐  Favorite", callback_data="favorite:add"),
            InlineKeyboardButton("🏠  Menu", callback_data="home:main"),
        ],
        [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
    ])
    return InlineKeyboardMarkup(rows)


def _unavailable_keyboard(
    episode: int,
    final: bool = False,
    audio_mode: str = AUDIO_MODE_DUB,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if final:
        rows.append([InlineKeyboardButton("🔎  Find Another Anime", callback_data="home:search")])
    else:
        rows.append([InlineKeyboardButton("🔄  Check Again", callback_data="binge:retry")])
        if audio_mode == AUDIO_MODE_DUB:
            rows.append([InlineKeyboardButton("🎧  Try Japanese Original", callback_data="binge:japanese")])
        rows.append([InlineKeyboardButton("📋  Episodes", callback_data="episodes:show")])
    rows.append([InlineKeyboardButton(f"🎙  Audio: {_audio_short_label(audio_mode)}", callback_data="home:audio")])
    rows.append([InlineKeyboardButton("🏠  Menu", callback_data="home:main")])
    rows.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
    return InlineKeyboardMarkup(rows)


def _source_keyboard(
    episode: int,
    url: str,
    max_episode: int,
    audio_mode: str = AUDIO_MODE_DUB,
) -> InlineKeyboardMarkup:
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
        [InlineKeyboardButton(f"🎙  Audio: {_audio_short_label(audio_mode)}", callback_data="home:audio")],
        [
            InlineKeyboardButton("⭐  Favorite", callback_data="favorite:add"),
            InlineKeyboardButton("🏠  Menu", callback_data="home:main"),
        ],
        [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
    ])
    return InlineKeyboardMarkup(rows)


async def _search(
    query_text: str,
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    status = await message.reply_text(
        "<b>◈ ANISIGNAL / SIGNAL SCAN</b>\n"
        "<code>ANIME INDEX / INITIALIZING</code>\n\n"
        "◐ Tuning into the anime index…",
        parse_mode=ParseMode.HTML,
    )
    stop, task = await _start_animation(
        status,
        [
            "<b>◈ ANISIGNAL / SIGNAL SCAN</b>\n<code>SEARCH / 01</code>\n\nSearching the anime index…",
            "<b>◈ ANISIGNAL / SIGNAL SCAN</b>\n<code>SEARCH / 02</code>\n\nMatching anime titles…",
            "<b>◈ ANISIGNAL / SIGNAL SCAN</b>\n<code>SEARCH / 03</code>\n\nChecking the English-dub signal…",
            "<b>◈ ANISIGNAL / SIGNAL SCAN</b>\n<code>SEARCH / 04</code>\n\nPreparing your choices…",
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
        "<b>◈ SIGNAL MATCHES</b>\n"
        f"<code>QUERY / {escape(query_text.upper())}</code>\n\n"
        "Pick a title to load its real episode range.",
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


async def _send_home_card(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    use_welcome_cover: bool = False,
) -> None:
    """Send the home screen, using the branded cover only for a deliberate start moment."""
    if use_welcome_cover and WELCOME_ART_PATH.is_file():
        try:
            with WELCOME_ART_PATH.open("rb") as cover:
                await message.reply_photo(
                    photo=cover,
                    caption=(
                        "<b>◈ ANISIGNAL / SIGNAL LIVE</b>\n"
                        "<code>ENGLISH DUB DEFAULT · JP ORIGINAL OPTIONAL</code>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            logger.exception("Unable to send AniSignal welcome cover; using text card instead.")

    await message.reply_text(
        _home_text(context),
        parse_mode=ParseMode.HTML,
        reply_markup=_home_keyboard(context),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await _send_home_card(update.effective_message, context, use_welcome_cover=True)
    await update.effective_message.reply_text(
        "◈ ANISIGNAL CONTROL RAIL ACTIVE\nUse the menu below anytime.",
        reply_markup=MENU_REPLY_KEYBOARD,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await update.effective_message.reply_text(
        "<b>◈ SIGNAL RESET</b>\n<code>SESSION CLEARED</code>\n\nBack at the Anime Hub.",
        parse_mode=ParseMode.HTML,
        reply_markup=_home_keyboard(context),
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_transient_state(context)
    await _send_home_card(update.effective_message, context)


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
            f"<b>🎬 {escape(str(selected['title']))}</b>\n<code>EPISODE GUIDE / 01</code>\n\nOpening anime card…",
            f"<b>🎬 {escape(str(selected['title']))}</b>\n<code>EPISODE GUIDE / 02</code>\n\nLoading indexed episode guide…",
            f"<b>🎬 {escape(str(selected['title']))}</b>\n<code>EPISODE GUIDE / 03</code>\n\nCounting available episodes…",
            f"<b>🎬 {escape(str(selected['title']))}</b>\n<code>EPISODE GUIDE / READY</code>\n\nBuilding your episode browser…",
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
        _selected_text(selected, _audio_mode(context)),
        parse_mode=ParseMode.HTML,
        reply_markup=_episode_keyboard(0, selected["episode_count"], _audio_mode(context)),
    )


async def _resolve_episode(
    message: Any,
    context: ContextTypes.DEFAULT_TYPE,
    selected: dict[str, Any],
    episode: int,
    from_callback: bool,
) -> None:
    audio_mode = _audio_mode(context)
    audio_label = _audio_label(audio_mode)
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
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=_episode_keyboard((max_episode - 1) // 20, max_episode, audio_mode))
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_episode_keyboard((max_episode - 1) // 20, max_episode, audio_mode))
        return

    title = str(selected["title"])
    if from_callback:
        work_message = message
        await work_message.edit_text(
            f"<b>🎬 {escape(title)} / EP. {episode:02d}</b>\n"
            "<code>SECURE FETCH / INITIALIZING</code>\n\n"
            f"◐ Checking {audio_label.lower()}…",
            parse_mode=ParseMode.HTML,
        )
    else:
        work_message = await message.reply_text(
            f"<b>🎬 {escape(title)} / EP. {episode:02d}</b>\n"
            "<code>SECURE FETCH / INITIALIZING</code>\n\n"
            f"◐ Checking {audio_label.lower()}…",
            parse_mode=ParseMode.HTML,
        )

    stop, task = await _start_animation(work_message, _fetch_animation_lines(title, episode, audio_mode))
    resolver: AniCliResolver = context.application.bot_data["resolver"]
    try:
        url = await resolver.resolve_episode(
            query=selected["query"],
            result_index=selected["result_index"],
            episode=episode,
            audio_mode=audio_mode,
            source_anime_id=selected.get("anime_id"),
        )
    except ResolverError as exc:
        await _finish_animation(stop, task)
        context.user_data["binge_pending"] = {"selected": selected.copy(), "episode": episode}
        error_text = str(exc).lower()
        final = episode > max_episode or "final" in error_text or "last episode" in error_text
        if final:
            body = (
                "<b>🏁 RUN COMPLETE</b>\n"
                "<code>FINAL INDEXED EPISODE</code>\n\n"
                f"🎬 <b>{escape(str(selected['title']))}</b>\n"
                f"Episode {episode} closes the available run.\n\n"
                "Your next anime signal is waiting."
            )
        else:
            fallback_copy = (
                "<b>Try Japanese Original too?</b>\n"
                "Tap <b>🎧 Try Japanese Original</b> below to resolve this same episode without searching again."
                if audio_mode == AUDIO_MODE_DUB
                else "Try again later or return to the episode guide."
            )
            body = (
                "<b>⚠️ SIGNAL NOT READY</b>\n"
                f"<code>EPISODE {episode:02d} / {audio_label.upper()} CHECK</code>\n\n"
                f"🎬 <b>{escape(str(selected['title']))}</b>\n"
                f"There is no usable {audio_label.lower()} stream right now.\n\n"
                f"{fallback_copy}"
            )
        await work_message.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            reply_markup=_unavailable_keyboard(episode, final=final, audio_mode=audio_mode),
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
        f"<b>◈ STREAM READY / {escape(title.upper())}</b>\n"
        f"<code>EP. {episode:02d} OF {max_episode:02d} · {audio_label.upper()}</code>\n\n"
        "<b>✓ YOUR WATCH SIGNAL IS LIVE</b>\n"
        f"Progress  <code>{_progress_bar(episode, max_episode)}</code>\n\n"
        f"<b>▶ WATCH EPISODE {episode}</b>\n"
        "──────────────\n"
        "<b>ON DECK</b>  Continue the binge below.",
        parse_mode=ParseMode.HTML,
        reply_markup=_source_keyboard(episode, url, max_episode, audio_mode),
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
    if data in {"binge:retry", "binge:japanese"}:
        pending = context.user_data.get("binge_pending")
        if not isinstance(pending, dict) or not isinstance(pending.get("episode"), int):
            await query.answer("There is no episode waiting to retry.", show_alert=False)
            return
        selected = pending["selected"]
        episode = pending["episode"]
        if data == "binge:japanese":
            context.user_data["audio_mode"] = AUDIO_MODE_SUB
            await query.answer("Japanese Original selected. Retuning…")
        else:
            await query.answer(f"Checking Episode {episode} again…")
        context.user_data["selected"] = selected.copy()
        await _resolve_episode(query.message, context, selected, episode, from_callback=True)
        return

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
                "<b>🏁 RUN COMPLETE</b>\n"
                "<code>FINAL INDEXED EPISODE</code>\n\n"
                f"🎬 <b>{escape(str(selected['title']))}</b>\n"
                f"Episode {max_episode} closes the available run.\n\n"
                "Your next anime signal is waiting.",
                parse_mode=ParseMode.HTML,
                reply_markup=_unavailable_keyboard(max_episode, final=True, audio_mode=_audio_mode(context)),
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
        await query.edit_message_reply_markup(_episode_keyboard(context.user_data["episode_page"], max_episode, _audio_mode(context)))
        return
    if data == "episodes:prev":
        context.user_data["episode_page"] = max(0, context.user_data.get("episode_page", 0) - 1)
        await query.edit_message_reply_markup(_episode_keyboard(context.user_data["episode_page"], max_episode, _audio_mode(context)))
        return
    if data == "episodes:jump":
        context.user_data["ui_state"] = "jump"
        await query.edit_message_text(
            f"<b>◈ DIRECT EPISODE TUNE</b>\n"
            f"<code>{escape(str(selected['title']).upper())}</code>\n\n"
            "Send the episode number you want to watch.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("‹  Back to Episodes", callback_data="episodes:show")],
                [InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)],
            ]),
        )
        return
    if data == "episodes:show":
        context.user_data["ui_state"] = "episodes"
        await query.edit_message_text(
            _selected_text(selected, _audio_mode(context)),
            parse_mode=ParseMode.HTML,
            reply_markup=_episode_keyboard(context.user_data.get("episode_page", 0), max_episode, _audio_mode(context)),
        )
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
            "<b>◈ DIRECT WATCH SIGNAL</b>\n"
            "<code>FRESH EPISODE URL / COPY OR OPEN</code>\n\n"
            f"<code>{escape(url)}</code>",
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
    elif action == "audio":
        await query.edit_message_text(
            _audio_settings_text(context),
            parse_mode=ParseMode.HTML,
            reply_markup=_audio_settings_keyboard(context),
        )
    elif action == "search":
        await query.edit_message_text(
            "<b>◈ TUNE AN ANIME</b>\n"
            f"<code>SEARCH / {_audio_label(_audio_mode(context)).upper()} INDEX</code>\n\n"
            "Type an anime title below to start the signal scan.",
            parse_mode=ParseMode.HTML,
            reply_markup=_back_home_keyboard(),
        )
        context.user_data["selected"] = None
    elif action == "about":
        await query.edit_message_text(ABOUT_TEXT, parse_mode=ParseMode.HTML, reply_markup=_back_home_keyboard())
    elif action == "popular":
        keyboard = [[InlineKeyboardButton(f"🔥  {title}", callback_data=f"popular:{index}")] for index, title in enumerate(POPULAR_TITLES)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        keyboard.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
        await query.edit_message_text(
            "<b>◈ HOT SIGNALS</b>\n"
            "<code>POPULAR ANIME / ON AIR</code>\n\n"
            "Choose a title to tune in.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif action == "favorites":
        favorites = context.user_data.get("favorites", [])
        if not favorites:
            await query.edit_message_text(
                "<b>◈ MY SIGNAL SHELF</b>\n"
                "<code>FAVORITES / EMPTY</code>\n\n"
                "No favorites yet. Add one from an episode screen.",
                parse_mode=ParseMode.HTML,
                reply_markup=_back_home_keyboard(),
            )
            return
        keyboard = [[InlineKeyboardButton(f"⭐  {item['title'][:55]}", callback_data=f"favorite:{index}")] for index, item in enumerate(favorites)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        keyboard.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
        await query.edit_message_text(
            "<b>◈ MY SIGNAL SHELF</b>\n"
            "<code>FAVORITES / SAVED TITLES</code>\n\n"
            "Choose an anime to reopen its episode guide.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif action == "recent":
        recent = context.user_data.get("recent", [])
        if not recent:
            await query.edit_message_text(
                "<b>◈ RECENT SIGNALS</b>\n"
                "<code>WATCH HISTORY / EMPTY</code>\n\n"
                "Your recently opened anime will appear here.",
                parse_mode=ParseMode.HTML,
                reply_markup=_back_home_keyboard(),
            )
            return
        keyboard = [[InlineKeyboardButton(f"🕘  {item['title'][:55]}", callback_data=f"recent:{index}")] for index, item in enumerate(recent)]
        keyboard.append([InlineKeyboardButton("⌂  Home", callback_data="home:main")])
        keyboard.append([InlineKeyboardButton("✦  Contact Dev", url=CONTACT_DEV_URL)])
        await query.edit_message_text(
            "<b>◈ RECENT SIGNALS</b>\n"
            "<code>WATCH HISTORY / CONTINUE ANYTIME</code>\n\n"
            "Choose an anime to reopen its episode guide.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif action == "results":
        results = context.user_data.get("last_results", [])
        await query.edit_message_text(
            "<b>◈ LAST SIGNAL MATCHES</b>\n"
            "<code>SEARCH RESULTS / AVAILABLE NOW</code>\n\n"
            "Choose an anime to continue.",
            parse_mode=ParseMode.HTML,
            reply_markup=_results_keyboard(results),
        )


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
        _selected_text(selected, _audio_mode(context)),
        parse_mode=ParseMode.HTML,
        reply_markup=_episode_keyboard(0, selected["episode_count"], _audio_mode(context)),
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
    await query.message.edit_text(
        "<b>◈ ANISIGNAL / HOT SIGNAL SCAN</b>\n"
        "<code>POPULAR TITLE / INITIALIZING</code>\n\n"
        "◐ Tuning into the anime index…",
        parse_mode=ParseMode.HTML,
    )
    status = query.message
    stop, task = await _start_animation(
        status,
        [
            "<b>◈ ANISIGNAL / HOT SIGNAL SCAN</b>\n<code>SEARCH / 01</code>\n\nSearching the anime index…",
            "<b>◈ ANISIGNAL / HOT SIGNAL SCAN</b>\n<code>SEARCH / 02</code>\n\nMatching anime titles…",
            "<b>◈ ANISIGNAL / HOT SIGNAL SCAN</b>\n<code>SEARCH / 03</code>\n\nPreparing your choices…",
        ],
    )
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
    await status.edit_text(
        "<b>◈ HOT SIGNAL MATCHES</b>\n"
        f"<code>QUERY / {escape(title.upper())}</code>\n\n"
        "Pick a title to load its real episode range.",
        parse_mode=ParseMode.HTML,
        reply_markup=_results_keyboard(results),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)


async def audio_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    mode = (query.data or "").split(":", 1)[1]
    if mode not in {AUDIO_MODE_DUB, AUDIO_MODE_SUB}:
        await query.answer("That audio choice is unavailable.", show_alert=True)
        return
    context.user_data["audio_mode"] = mode
    await query.answer(f"Audio set to {_audio_label(mode)}")
    await query.edit_message_text(
        _audio_settings_text(context),
        parse_mode=ParseMode.HTML,
        reply_markup=_audio_settings_keyboard(context),
    )


def build_application(token: str, resolver: AniCliResolver) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["resolver"] = resolver

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(home_action, pattern=r"^home:(main|search|popular|favorites|recent|about|results|audio)$"))
    application.add_handler(CallbackQueryHandler(audio_action, pattern=r"^audio:(dub|sub)$"))
    application.add_handler(CallbackQueryHandler(binge_action, pattern=r"^binge:(next|prev|continue|retry|japanese)$"))
    application.add_handler(CallbackQueryHandler(popular_action, pattern=r"^popular:\d+$"))
    application.add_handler(CallbackQueryHandler(list_item_action, pattern=r"^(favorite|recent):\d+$"))
    application.add_handler(CallbackQueryHandler(episode_action, pattern=r"^(episode:\d+|episodes:(next|prev|jump|show|noop)|favorite:add)$"))
    application.add_handler(CallbackQueryHandler(source_action, pattern=r"^source:show$"))
    application.add_handler(CallbackQueryHandler(pick_result, pattern=r"^pick:\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application 
