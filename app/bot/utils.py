from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from app.core.logging import logger
from app.db.repositories import Repository
from app.services.banner_generator import (
    get_memory_cached_file_id,
    render_banner_bytes,
    set_memory_cached_file_id,
)

MSK_TZ = timezone(timedelta(hours=3))


def format_dt_msk(dt: datetime) -> str:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    msk = aware.astimezone(MSK_TZ)
    return msk.strftime("%d.%m.%Y %H:%M МСК")


async def _resolve_banner_media(
    banner_key: str,
    lang: str,
    repo: Repository | None = None,
) -> tuple[str | BufferedInputFile, str]:
    """
    Возвращает (file_id_or_BufferedInputFile, sha256_hash).
    Сначала ищет `file_id` в памяти, затем в таблице `media_cache` PostgreSQL.
    Если ещё не кэширован — возвращает `BufferedInputFile` для первичной отправки.
    """
    png_bytes, sha_hash = render_banner_bytes(banner_key, lang)
    mem_id = get_memory_cached_file_id(banner_key, lang, sha_hash)
    if mem_id:
        return mem_id, sha_hash

    if repo is not None:
        db_id = await repo.get_cached_banner_file_id(banner_key, lang, sha_hash)
        if db_id:
            set_memory_cached_file_id(banner_key, lang, sha_hash, db_id)
            return db_id, sha_hash

    input_file = BufferedInputFile(png_bytes, filename=f"ghost_{banner_key}_{lang}.png")
    return input_file, sha_hash


async def _store_extracted_file_id(
    sent_msg: Message | bool | None,
    banner_key: str,
    lang: str,
    sha_hash: str,
    repo: Repository | None,
) -> None:
    if isinstance(sent_msg, Message) and sent_msg.photo:
        fid = sent_msg.photo[-1].file_id
        set_memory_cached_file_id(banner_key, lang, sha_hash, fid)
        if repo is not None:
            try:
                await repo.save_cached_banner_file_id(banner_key, lang, sha_hash, fid)
            except Exception as exc:
                logger.debug("Не удалось сохранить file_id баннера в БД: %s", exc)


async def send_or_edit_banner(
    target: Message | CallbackQuery,
    *,
    banner_key: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    lang: str = "ru",
    repo: Repository | None = None,
) -> None:
    """
    Плавно меняет экран внутри одного сообщения через `edit_message_media(InputMediaPhoto)`
    с автоматическим кэшированием `file_id` баннера в памяти и PostgreSQL.
    Если исходное сообщение было текстовым или редактирование невозможно — отправляет новое с баннером.
    """
    media_source, sha_hash = await _resolve_banner_media(banner_key, lang, repo=repo)

    if isinstance(target, CallbackQuery) and target.message:
        msg = target.message
        try:
            edited = await msg.edit_media(
                media=InputMediaPhoto(
                    media=media_source,
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=reply_markup,
            )
            if isinstance(media_source, BufferedInputFile):
                await _store_extracted_file_id(edited, banner_key, lang, sha_hash, repo)
            return
        except Exception:
            try:
                await msg.edit_text(
                    caption,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                pass
        sent = await msg.answer_photo(
            photo=media_source,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        if isinstance(media_source, BufferedInputFile):
            await _store_extracted_file_id(sent, banner_key, lang, sha_hash, repo)
        return

    if isinstance(target, Message):
        try:
            sent = await target.answer_photo(
                photo=media_source,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            if isinstance(media_source, BufferedInputFile):
                await _store_extracted_file_id(sent, banner_key, lang, sha_hash, repo)
        except Exception:
            await target.answer(
                caption,
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )


async def send_banner_to_chat(
    bot: Bot,
    chat_id: int,
    *,
    banner_key: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    lang: str = "ru",
    repo: Repository | None = None,
) -> None:
    """Отправляет сообщение с фирменным баннером в указанный `chat_id` (для фоновых уведомлений)."""
    media_source, sha_hash = await _resolve_banner_media(banner_key, lang, repo=repo)
    try:
        sent = await bot.send_photo(
            chat_id=chat_id,
            photo=media_source,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        if isinstance(media_source, BufferedInputFile):
            await _store_extracted_file_id(sent, banner_key, lang, sha_hash, repo)
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
