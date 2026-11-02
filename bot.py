import asyncio
import logging
import os
import shutil
import signal
import sys
import tempfile

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from database import (
    DB_PATH,
    BackupError,
    create_backup,
    inspect_backup_file,
    restore_from_file,
    init_db,

    # Users
    save_user,
    get_user,
    update_user_bio,
    get_all_users,
    get_user_stats,

    # Skills
    get_user_skills,
    replace_user_skills,

    # Problems
    save_problem,
    get_problem,
    get_open_problems,
    get_user_problems,
    update_problem_status,
    get_pending_problems,

    # Problem skills
    add_problem_skill,
    get_problem_skills,

    # Solutions
    add_solution,
    get_problem_solutions,
    get_user_solutions,
    update_solution_status,

    # Solutions — admin moderation
    get_pending_admin_solutions,
    update_solution_admin_status,

    # Collaborations
    add_collaboration,
    get_collaboration,
    get_problem_collaborators,
    update_collaboration_status,
    get_collaboration_count,
    get_user_collaborations,

    # Manual team matching
    get_accepted_solution,
    save_collaboration_rating,
    get_collaboration_ratings,

    # Matching
    find_matching_users,

    # Notifications
    add_notification,
    get_unread_notifications,
    mark_notifications_read,

    # Votes
    cast_vote,
    remove_vote,
    get_user_vote,
    get_vote_counts,

    # Admin dashboard
    get_admin_stats,
    get_all_collaborations,
)


# =========================================================
# CONFIG
# =========================================================

# On Railway, environment variables come from the service's
# "Variables" tab — load_dotenv() simply finds no .env file
# there and does nothing, so it's safe to leave in for local
# development too.
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN is not set. Set it as an environment variable "
        "(Railway → your service → Variables) or in a local .env "
        "file — see .env.example."
    )

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv(
        "ADMIN_IDS",
        ""
    ).split(",")
    if x.strip().isdigit()
}

# Telegram channel where approved problems/solutions get posted.
# Accepts either a numeric channel id (e.g. -1001234567890) or a
# public @username. The bot account must be an admin of this
# channel with "Post Messages" permission. Leave unset to disable
# channel publishing entirely (nothing breaks — posts are just
# skipped, with a log line).
#
# A bare username typed without "@" (e.g. "monopi_channel" instead
# of "@monopi_channel") is the single most common way to break
# this — Telegram's API rejects it with "chat not found". Normalize
# it here so that mistake can't happen: numeric ids (with or
# without a leading "-") pass through untouched, anything else
# gets "@" prepended if it's missing.
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip() or None

if CHANNEL_ID and not CHANNEL_ID.lstrip("-").isdigit():

    if not CHANNEL_ID.startswith("@"):
        CHANNEL_ID = f"@{CHANNEL_ID}"

# Discard updates that queued up on Telegram's side while the bot
# was offline (redeploying, crashed, etc.) instead of replaying a
# possibly large backlog on startup. Set to "false" in Railway
# Variables if you'd rather process everything that was missed.
DROP_PENDING_UPDATES = os.getenv(
    "DROP_PENDING_UPDATES",
    "true"
).strip().lower() in ("1", "true", "yes")


# How often (hours) a fresh database backup file is sent to every
# admin in Telegram. This keeps a copy of all users/problems/
# solutions OUTSIDE the hosting server, so a wiped server never
# means lost data (restore it with /restore). 0 disables it.
try:
    BACKUP_INTERVAL_HOURS = float(
        os.getenv("BACKUP_INTERVAL_HOURS", "6")
    )
except ValueError:
    BACKUP_INTERVAL_HOURS = 6.0


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS



# =========================================================
# LOGGING
# =========================================================
# Railway captures whatever the process writes to stdout/stderr
# and shows it live in the service's "Deploy Logs" — no file
# logging or extra service needed to see what the bot is doing
# in production.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)

# aiogram/aiohttp's own INFO-level chatter tends to bury the
# bot's actual logs — keep them quieter, errors still come through.
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

logger = logging.getLogger("monopi")


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()


# =========================================================
# USER STATES
# =========================================================

user_states = {}


def set_state(user_id: int, **kwargs):
    """
    Set or replace current user state.
    """

    user_states[user_id] = kwargs


def clear_state(user_id: int):
    """
    Completely remove current user state.
    """

    user_states.pop(user_id, None)


def get_state(user_id: int):
    """
    Get current user state.
    """

    return user_states.get(user_id)


# =========================================================
# SAFE TELEGRAM HELPERS
# =========================================================

async def safe_callback_answer(
    callback: CallbackQuery,
    text: str | None = None,
    show_alert: bool = False
):
    """
    Safely answer Telegram callback.

    Telegram callbacks expire quickly.
    An expired callback must NEVER crash the bot.
    """

    try:

        await callback.answer(
            text=text,
            show_alert=show_alert
        )

    except Exception as e:

        logging.debug(
            "Callback answer failed: %s",
            e
        )


async def safe_delete(message: Message | None):
    """
    Safely delete a Telegram message.
    """

    if not message:
        return

    try:

        await message.delete()

    except Exception as e:

        logging.debug(
            "Message delete failed: %s",
            e
        )


async def safe_edit(
    message: Message | None,
    text: str,
    reply_markup=None
):
    """
    Safely edit an existing bot message.
    """

    if not message:
        return False

    try:

        await message.edit_text(
            text,
            reply_markup=reply_markup
        )

        return True

    except Exception as e:

        logging.debug(
            "Message edit failed: %s",
            e
        )

        return False


async def send_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup=None
):
    """
    Update current bot screen.

    First tries to edit the current message.
    If editing is impossible, sends a new message.
    """

    edited = await safe_edit(
        callback.message,
        text,
        reply_markup
    )

    if edited:
        return

    try:

        await callback.message.answer(
            text,
            reply_markup=reply_markup
        )

    except Exception as e:

        logging.error(
            "send_screen failed: %s",
            e
        )


async def send_message_and_delete_previous(
    message: Message,
    text: str,
    reply_markup=None
):
    """
    Send a new screen and then delete user's previous command/message.
    """

    sent = await message.answer(
        text,
        reply_markup=reply_markup
    )

    await safe_delete(message)

    return sent


async def publish_to_channel(text: str):
    """
    Posts a message to the configured channel (CHANNEL_ID). If no
    channel is configured, this is a no-op — nothing about the
    bot's normal operation depends on it. If the bot isn't an
    admin of the channel (or the id is wrong), the failure is
    logged, not raised — a bad channel post must never break the
    admin's approve/reject flow for the user.
    """

    if not CHANNEL_ID:
        return

    try:

        await bot.send_message(
            CHANNEL_ID,
            text
        )

    except Exception:

        logger.exception(
            "Failed to publish to channel (%s)",
            CHANNEL_ID
        )


async def notify_user(
    telegram_id: int,
    message: str
):
    """
    The single place a notification is created. Always persists
    to the database (so 🔔 اعلان‌ها keeps working as a history/
    unread-badge even if the push below fails or the user's chat
    with the bot is gone) and — unless that's not possible —
    pushes it as a real message right now.

    A user who blocked the bot or deleted their account is a
    routine, expected case (TelegramForbiddenError), not a bug —
    logged quietly. Anything else is logged with more detail
    since it might point at a real problem. Either way this
    never raises: a failed push must not break whatever admin/
    owner action triggered the notification.
    """

    add_notification(
        telegram_id,
        message
    )

    try:

        await bot.send_message(
            telegram_id,
            message
        )

    except TelegramForbiddenError:

        logger.info(
            "Skipped push to %s — bot is blocked/chat unavailable",
            telegram_id
        )

    except Exception:

        logger.exception(
            "Failed to push notification to %s",
            telegram_id
        )


# =========================================================
# TEXT HELPERS
# =========================================================

def short_text(
    text: str | None,
    length: int = 35
):

    if not text:
        return ""

    text = str(text)

    if len(text) <= length:
        return text

    return text[:length - 3] + "..."


# =========================================================
# VOTING HELPERS
# =========================================================

def vote_summary_line(
    target_type: str,
    target_id: int
) -> str:

    counts = get_vote_counts(
        target_type,
        target_id
    )

    return (
        f"👍 {counts['up']}   "
        f"👎 {counts['down']}   "
        f"•   امتیاز: {counts['score']}"
    )


def add_vote_buttons(
    builder: InlineKeyboardBuilder,
    callback_prefix: str,
    user_vote
):
    """
    Adds a 👍/👎 row. callback_prefix must already contain
    everything the receiving handler needs except the trailing
    "_up" / "_down" token, e.g. "votepd_42" or "votebrowse_42_3".

    A ✓ marks the option the user already picked — tapping it
    again toggles the vote off (see the vote handlers below).
    """

    builder.button(
        text="👍" + (" ✓" if user_vote == 1 else ""),
        callback_data=f"{callback_prefix}_up"
    )

    builder.button(
        text="👎" + (" ✓" if user_vote == -1 else ""),
        callback_data=f"{callback_prefix}_down"
    )


async def apply_vote(
    user_id: int,
    target_type: str,
    target_id: int,
    direction: str
) -> str:
    """
    Casts, flips, or toggles-off a vote. Returns a short toast
    message describing what happened.
    """

    requested_value = 1 if direction == "up" else -1

    current = get_user_vote(
        user_id,
        target_type,
        target_id
    )

    if current == requested_value:

        remove_vote(
            user_id,
            target_type,
            target_id
        )

        return "رأی شما برداشته شد."

    cast_vote(
        user_id,
        target_type,
        target_id,
        requested_value
    )

    return (
        "👍 رأی مثبت ثبت شد."
        if requested_value == 1
        else "👎 رأی منفی ثبت شد."
    )


# =========================================================
# PERSISTENT KEYBOARD (Reply keyboard — always visible at the
# bottom of the chat, separate from the inline "screen" buttons
# above). Telegram only accepts a ReplyKeyboardMarkup on a newly
# *sent* message, never on an edit — so unlike everything routed
# through send_screen(), it's attached once (at /start) and then
# just stays put in the client regardless of what happens to the
# inline-keyboard screen above it.
# =========================================================

BTN_MAIN_MENU = "🏠 منوی اصلی"
BTN_PROFILE = "👤 پروفایل من"
BTN_SUPPORT = "☎️ ارتباط با پشتیبانی"


def persistent_keyboard():

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_MAIN_MENU),
                KeyboardButton(text=BTN_PROFILE),
            ],
            [
                KeyboardButton(text=BTN_SUPPORT),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True
    )


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():

    builder = InlineKeyboardBuilder()

    builder.button(
        text="💡 ثبت مسئله",
        callback_data="create_problem"
    )

    builder.button(
        text="🔎 کشف مسائل",
        callback_data="browse_problems"
    )

    builder.button(
        text="👤 پروفایل من",
        callback_data="profile"
    )

    builder.button(
        text="📊 فعالیت‌های من",
        callback_data="activity"
    )

    builder.button(
        text="🔔 اعلان‌ها",
        callback_data="notifications"
    )

    builder.adjust(1)

    return builder.as_markup()


# =========================================================
# CATEGORY MENU
# =========================================================

def category_menu():

    builder = InlineKeyboardBuilder()

    categories = [
        ("🏭 صنعت و تولید", "category_industry"),
        ("💻 فناوری", "category_technology"),
        ("🏠 زندگی روزمره", "category_life"),
        ("🌱 محیط زیست", "category_environment"),
        ("💰 کسب‌وکار", "category_business"),
        ("📚 آموزش", "category_education"),
    ]

    for text, callback in categories:

        builder.button(
            text=text,
            callback_data=callback
        )

    builder.button(
        text="❌ لغو",
        callback_data="cancel_action"
    )

    builder.adjust(2)

    return builder.as_markup()


# =========================================================
# IMPORTANCE MENU
# =========================================================

def importance_menu():

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔴 بسیار مهم",
        callback_data="importance_critical"
    )

    builder.button(
        text="🟠 مهم",
        callback_data="importance_high"
    )

    builder.button(
        text="🟡 متوسط",
        callback_data="importance_medium"
    )

    builder.button(
        text="🟢 کم",
        callback_data="importance_low"
    )

    builder.button(
        text="❌ لغو",
        callback_data="cancel_action"
    )

    builder.adjust(2)

    return builder.as_markup()



# =========================================================
# BACK MENU
# =========================================================

def back_menu():

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🏠 منوی اصلی",
        callback_data="main_menu"
    )

    return builder.as_markup()


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

    builder = InlineKeyboardBuilder()

    builder.button(
        text="💡 مسائل در انتظار بررسی",
        callback_data="admin_pending_problems"
    )

    builder.button(
        text="💭 راه‌حل‌های در انتظار بررسی",
        callback_data="admin_pending_solutions"
    )

    builder.button(
        text="🤝 درخواست‌های همکاری",
        callback_data="admin_collaborations"
    )

    builder.button(
        text="📊 آمار",
        callback_data="admin_stats"
    )

    builder.button(
        text="👥 لیست کاربران",
        callback_data="admin_users_0"
    )

    builder.button(
        text="💾 پشتیبان‌گیری از دیتابیس",
        callback_data="admin_backup"
    )

    builder.button(
        text="🏠 منوی اصلی",
        callback_data="main_menu"
    )

    builder.adjust(1)

    return builder.as_markup()


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start_handler(message: Message):

    user = message.from_user

    save_user(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name
    )

    # مهم:
    # این تابع الان وجود دارد و دیگر NameError نمی‌دهد.
    clear_state(user.id)

    # پیام اول: فقط برای فعال‌کردن کیبورد ثابت پایین صفحه —
    # یک ReplyKeyboardMarkup تنها روی پیام تازه‌ارسال‌شده جواب
    # می‌ده، نه روی ادیت؛ پس این تنها پیامیه که لازمه اونو حمل کنه.
    await message.answer(
        "🚀 <b>به Monopi خوش آمدی</b>\n\n"
        "Monopi جایی است برای تبدیل مسائل واقعی "
        "به راه‌حل، همکاری و در نهایت پروژه.\n\n"
        "می‌توانی یک مسئله ثبت کنی، "
        "راه‌حل دیگران را ببینی یا برای همکاری "
        "اعلام آمادگی کنی.",
        reply_markup=persistent_keyboard()
    )

    # پیام دوم: صفحه‌ی منوی اصلی با دکمه‌های شیشه‌ای —
    # از این به بعد همین پیام با send_screen ادیت می‌شه.
    await message.answer(
        "🏠 <b>منوی اصلی Monopi</b>\n\n"
        "چه کاری می‌خواهی انجام دهی؟",
        reply_markup=main_menu()
    )


# =========================================================
# MAIN MENU
# =========================================================

@dp.callback_query(F.data == "main_menu")
async def main_menu_handler(callback: CallbackQuery):

    await safe_callback_answer(callback)

    clear_state(callback.from_user.id)

    await send_screen(
        callback,

        "🏠 <b>منوی اصلی Monopi</b>\n\n"
        "چه کاری می‌خواهی انجام دهی؟",

        main_menu()
    )


# =========================================================
# CANCEL
# =========================================================

@dp.callback_query(F.data == "cancel_action")
async def cancel_action(callback: CallbackQuery):

    await safe_callback_answer(callback)

    clear_state(callback.from_user.id)

    await send_screen(
        callback,

        "❌ عملیات لغو شد.\n\n"
        "🏠 منوی اصلی",

        main_menu()
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(F.text == "/admin")
async def admin_command(message: Message):

    user_id = message.from_user.id

    if not is_admin(user_id):

        await message.answer(
            "⛔ دسترسی غیرمجاز."
        )

        return

    await message.answer(
        "🛠 <b>پنل مدیریت Monopi</b>\n\n"
        "بخش موردنظر را انتخاب کن:",
        reply_markup=admin_menu()
    )


# =========================================================
# DATABASE BACKUP / RESTORE  (admin only)
# =========================================================
# /backup   -> bot sends the current database file.
# /restore  -> send a .db backup file as a document with the
#              caption  /restore  ; the bot validates it, shows a
#              comparison and asks for confirmation.
# A fresh backup is also sent to every admin automatically every
# BACKUP_INTERVAL_HOURS (default 6). These handlers are registered
# before the bare @dp.message() catch-all on purpose.

# admin_id -> path of an uploaded backup waiting for confirmation
_pending_restore = {}


def _data_summary():

    s = get_admin_stats()

    return (
        f"👤 کاربران: {s['users']}\n"
        f"💡 مسائل: {s['problems_total']}\n"
        f"💭 راه‌حل‌ها: {s['solutions_total']}"
    )


async def send_backup_document(chat_id):

    path = await asyncio.to_thread(create_backup)

    await bot.send_document(
        chat_id,
        FSInputFile(path),
        caption=(
            "💾 <b>پشتیبان دیتابیس Monopi</b>\n\n"
            + _data_summary()
            + "\n\nاین فایل را نگه دار. برای بازیابی، همین فایل را "
              "با کپشن /restore برای ربات بفرست."
        )
    )


@dp.message(F.text == "/backup")
async def backup_command(message: Message):

    if not is_admin(message.from_user.id):

        await message.answer("⛔ دسترسی غیرمجاز.")

        return

    try:

        await send_backup_document(message.chat.id)

    except Exception:

        logger.exception("Manual backup failed")

        await message.answer("❌ ساخت فایل پشتیبان ناموفق بود.")


@dp.callback_query(F.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    try:

        await send_backup_document(callback.from_user.id)

    except Exception:

        logger.exception("Manual backup failed")

        await callback.message.answer("❌ ساخت فایل پشتیبان ناموفق بود.")


def _is_restore_upload(message: Message) -> bool:

    return bool(
        message.document
        and (message.caption or "").strip().lower().startswith("/restore")
    )


@dp.message(F.func(_is_restore_upload))
async def restore_upload_handler(message: Message):

    admin_id = message.from_user.id

    if not is_admin(admin_id):

        await message.answer("⛔ دسترسی غیرمجاز.")

        return

    doc = message.document

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:

        await message.answer(
            "❌ فایل بزرگ‌تر از ۲۰ مگابایت است و تلگرام اجازه‌ی "
            "دانلودش را به ربات نمی‌دهد."
        )

        return

    tmp_dir = tempfile.mkdtemp(prefix="monopi_restore_")
    tmp_path = os.path.join(tmp_dir, "upload.db")

    try:

        await bot.download(doc, destination=tmp_path)

        info = await asyncio.to_thread(inspect_backup_file, tmp_path)

    except BackupError as e:

        shutil.rmtree(tmp_dir, ignore_errors=True)

        await message.answer(f"❌ {e}")

        return

    except Exception:

        logger.exception("Restore upload failed")

        shutil.rmtree(tmp_dir, ignore_errors=True)

        await message.answer("❌ دریافت یا بررسی فایل ناموفق بود.")

        return

    # replace an older pending upload from the same admin
    old = _pending_restore.pop(admin_id, None)

    if old:
        shutil.rmtree(os.path.dirname(old), ignore_errors=True)

    _pending_restore[admin_id] = tmp_path

    cur = get_admin_stats()

    builder = InlineKeyboardBuilder()

    builder.button(
        text="✅ تأیید بازیابی",
        callback_data="restore_confirm"
    )

    builder.button(
        text="❌ انصراف",
        callback_data="restore_cancel"
    )

    builder.adjust(1)

    await message.answer(
        "♻️ <b>بازیابی دیتابیس</b>\n\n"
        "<b>محتوای فایل:</b>\n"
        f"👤 کاربران: {info['users']}\n"
        f"💡 مسائل: {info['problems']}\n"
        f"💭 راه‌حل‌ها: {info['solutions']}\n\n"
        "<b>دیتابیس فعلی:</b>\n"
        f"👤 کاربران: {cur['users']}\n"
        f"💡 مسائل: {cur['problems_total']}\n"
        f"💭 راه‌حل‌ها: {cur['solutions_total']}\n\n"
        "⚠️ با تأیید، داده‌ی فعلی با محتوای فایل جایگزین می‌شود "
        "(یک نسخه‌ی ایمنی از وضعیت فعلی قبلش ساخته می‌شود).",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "restore_cancel")
async def restore_cancel(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    path = _pending_restore.pop(callback.from_user.id, None)

    if path:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer("بازیابی لغو شد.")


@dp.callback_query(F.data == "restore_confirm")
async def restore_confirm(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    path = _pending_restore.pop(callback.from_user.id, None)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if not path or not os.path.exists(path):

        await callback.message.answer(
            "این درخواست منقضی شده است. فایل را دوباره با "
            "کپشن /restore بفرست."
        )

        return

    try:

        info, safety = await asyncio.to_thread(restore_from_file, path)

    except BackupError as e:

        await callback.message.answer(f"❌ {e}")

        return

    except Exception:

        logger.exception("Restore failed")

        await callback.message.answer(
            "❌ بازیابی ناموفق بود. دیتابیس فعلی دست‌نخورده می‌ماند "
            "یا از نسخه‌ی ایمنی قابل بازگشت است."
        )

        return

    finally:

        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    # conversation steps held in memory refer to the old data
    user_states.clear()

    await callback.message.answer(
        "✅ <b>دیتابیس بازیابی شد.</b>\n\n"
        + _data_summary()
    )


async def broadcast_backup_to_admins():

    s = get_admin_stats()

    # nothing worth protecting yet -> don't spam the admins
    if s["users"] == 0 and s["problems_total"] == 0:
        return

    try:

        path = await asyncio.to_thread(create_backup)

    except Exception:

        logger.exception("Scheduled backup failed")

        return

    caption = (
        "💾 <b>پشتیبان خودکار Monopi</b>\n\n"
        + _data_summary()
        + "\n\nبرای بازیابی: همین فایل را با کپشن /restore بفرست."
    )

    for admin_id in ADMIN_IDS:

        try:

            await bot.send_document(
                admin_id,
                FSInputFile(path),
                caption=caption
            )

        except Exception:

            logger.warning(
                "Could not send backup to admin %s", admin_id
            )


async def backup_loop():

    if BACKUP_INTERVAL_HOURS <= 0 or not ADMIN_IDS:

        logger.info("Automatic backups are disabled.")

        return

    logger.info(
        "Automatic backups every %s hour(s).", BACKUP_INTERVAL_HOURS
    )

    # small delay so the bot is fully up before the first send
    await asyncio.sleep(60)

    while True:

        try:
            await broadcast_backup_to_admins()
        except Exception:
            logger.exception("Backup loop iteration failed")

        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)


async def notify_admins_startup():

    try:

        text = (
            "🟢 <b>ربات Monopi راه‌اندازی شد.</b>\n\n"
            + _data_summary()
        )

        s = get_admin_stats()

        if s["users"] == 0 and s["problems_total"] == 0:

            text += (
                "\n\n⚠️ دیتابیس خالی است. اگر قبلاً داده داشتی، "
                "آخرین فایل پشتیبان را با کپشن /restore برای ربات "
                "بفرست."
            )

        for admin_id in ADMIN_IDS:

            try:
                await bot.send_message(admin_id, text)
            except Exception:
                logger.warning(
                    "Could not notify admin %s", admin_id
                )

    except Exception:

        logger.exception("Startup notification failed")


@dp.callback_query(F.data == "admin_menu")
async def admin_menu_handler(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    await send_screen(
        callback,

        "🛠 <b>پنل مدیریت Monopi</b>\n\n"
        "بخش موردنظر را انتخاب کن:",

        admin_menu()
    )


@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    s = get_admin_stats()

    text = (
        "📊 <b>آمار Monopi</b>\n\n"

        f"👤 کاربران: {s['users']}\n\n"

        "💡 <b>مسائل</b>\n"
        f"مجموع: {s['problems_total']}\n"
        f"⏳ در انتظار بررسی: {s['problems_pending']}\n"
        f"✅ منتشرشده: {s['problems_open']}\n"
        f"❌ ردشده: {s['problems_rejected']}\n\n"

        "💭 <b>راه‌حل‌ها</b>\n"
        f"مجموع: {s['solutions_total']}\n"
        f"⏳ در انتظار بررسی ادمین: "
        f"{s['solutions_pending_review']}\n"
        f"✅ تأییدشده توسط ادمین: "
        f"{s['solutions_approved']}\n"
        f"❌ ردشده توسط ادمین: "
        f"{s['solutions_rejected']}\n"
        f"🏆 انتخاب‌شده توسط صاحب مسئله: "
        f"{s['solutions_accepted_by_owner']}\n\n"

        "🤝 <b>همکاری</b>\n"
        f"مجموع درخواست‌ها: "
        f"{s['collaborations_total']}\n"
        f"⏳ در انتظار: "
        f"{s['collaborations_pending']}\n"
        f"✅ پذیرفته‌شده: "
        f"{s['collaborations_accepted']}\n\n"

        f"👍👎 مجموع رأی‌های ثبت‌شده: "
        f"{s['votes_total']}"
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 پنل مدیریت",
        callback_data="admin_menu"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(F.data == "admin_collaborations")
async def admin_collaborations(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    collaborations = get_all_collaborations(
        limit=20
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 پنل مدیریت",
        callback_data="admin_menu"
    )

    if not collaborations:

        await send_screen(
            callback,

            "🤝 <b>درخواست‌های همکاری</b>\n\n"
            "هنوز هیچ درخواست همکاری‌ای "
            "ثبت نشده.",

            builder.as_markup()
        )

        return

    status_labels = {
        "pending": "⏳ در انتظار",
        "accepted": "✅ پذیرفته‌شده",
        "rejected": "❌ ردشده",
    }

    text = (
        "🤝 <b>درخواست‌های همکاری</b>\n"
        f"(۲۰ مورد اخیر)\n\n"

        "ℹ️ این لیست فقط برای مشاهده است — "
        "تأیید یا رد هر درخواست بر عهده‌ی "
        "صاحب همان مسئله است.\n\n"
    )

    for c in collaborations:

        requester = (
            c["full_name"]
            or c["username"]
            or "کاربر ناشناس"
        )

        status = status_labels.get(
            c["status"],
            c["status"]
        )

        text += (
            f"{status}\n"
            f"مسئله #{c['problem_id']} — "
            f"{short_text(c['problem_title'], 40)}\n"
            f"👤 متقاضی: {requester}\n\n"
        )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# ADMIN — USER DIRECTORY
# =========================================================

async def show_admin_user(
    callback: CallbackQuery,
    users_list,
    index: int
):

    if not users_list:

        await send_screen(
            callback,

            "👥 <b>لیست کاربران</b>\n\n"
            "هنوز هیچ کاربری ثبت‌نام نکرده.",

            admin_menu()
        )

        return

    user = users_list[index]

    telegram_id = user["telegram_id"]

    skills = get_user_skills(
        telegram_id
    )

    skills_text = (
        "، ".join(skills)
        if skills
        else "ثبت نشده"
    )

    stats = get_user_stats(
        telegram_id
    )

    username_text = (
        f"@{user['username']}"
        if user["username"]
        else "ندارد"
    )

    bio_text = (
        user["bio"]
        if user["bio"]
        else "ننوشته"
    )

    text = (
        f"👥 <b>کاربر {index + 1} از "
        f"{len(users_list)}</b>\n\n"

        f"نام: {user['full_name']}\n"
        f"Username: {username_text}\n"
        f"Telegram ID: <code>{telegram_id}</code>\n"
        f"تاریخ عضویت: {user['created_at']}\n\n"

        f"📝 <b>درباره:</b> {bio_text}\n\n"

        f"🧠 <b>مهارت‌ها:</b> {skills_text}\n\n"

        f"💡 مسائل ثبت‌شده: {stats['problems']}\n"
        f"💭 راه‌حل‌های ارسالی: "
        f"{stats['solutions']}\n"
        f"🤝 درخواست‌های همکاری: "
        f"{stats['collaborations']}"
    )

    builder = InlineKeyboardBuilder()

    if index > 0:

        builder.button(
            text="⬅️ قبلی",
            callback_data=f"admin_users_{index - 1}"
        )

    if index < len(users_list) - 1:

        builder.button(
            text="بعدی ➡️",
            callback_data=f"admin_users_{index + 1}"
        )

    builder.button(
        text="🔙 پنل مدیریت",
        callback_data="admin_menu"
    )

    builder.adjust(2, 1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(
    F.data.startswith("admin_users_")
)
async def admin_users(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    try:

        index = int(
            callback.data.split("_")[-1]
        )

    except ValueError:

        index = 0

    users_list = get_all_users()

    if index < 0:
        index = 0

    if index >= len(users_list):
        index = max(len(users_list) - 1, 0)

    await show_admin_user(
        callback,
        users_list,
        index
    )


# =========================================================
# CREATE PROBLEM
# =========================================================

@dp.callback_query(F.data == "create_problem")
async def create_problem(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    set_state(
        user_id,
        step="problem_description"
    )

    await send_screen(
        callback,

        "💡 <b>ثبت مسئله</b>\n\n"
        "اول مسئله‌ای که با آن مواجه هستی را "
        "به زبان ساده توضیح بده.\n\n"

        "مثال:\n"
        "«در کارخانه ما مصرف انرژی موتورهای خط تولید "
        "بیش از حد معمول است و علت آن مشخص نیست.»\n\n"

        "✍️ توضیح مسئله را ارسال کن:"
    )


# =========================================================
# PERSISTENT KEYBOARD BUTTONS
# =========================================================
# Registered before the catch-all TEXT HANDLER below — aiogram
# tries message handlers in registration order and stops at the
# first filter match, so these three specific F.text filters
# must come first or the bare @dp.message() catch-all (which
# matches everything) would swallow every tap silently.

@dp.message(F.text == BTN_MAIN_MENU)
async def reply_button_main_menu(message: Message):

    clear_state(message.from_user.id)

    await message.answer(
        "🏠 <b>منوی اصلی Monopi</b>\n\n"
        "چه کاری می‌خواهی انجام دهی؟",

        reply_markup=main_menu()
    )


@dp.message(F.text == BTN_PROFILE)
async def reply_button_profile(message: Message):

    clear_state(message.from_user.id)

    text, markup = build_profile_view(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name
    )

    await message.answer(
        text,
        reply_markup=markup
    )


@dp.message(F.text == BTN_SUPPORT)
async def reply_button_support(message: Message):

    set_state(
        message.from_user.id,
        step="support_message"
    )

    await message.answer(
        "☎️ <b>ارتباط با پشتیبانی</b>\n\n"
        "پیامت رو بنویس — مستقیم برای تیم "
        "Monopi ارسال می‌شود.\n\n"
        "برای انصراف، هر کدام از دکمه‌های "
        "ثابت پایین صفحه را بزن."
    )


# =========================================================
# TEXT HANDLER
# =========================================================

@dp.message()
async def message_handler(message: Message):

    user_id = message.from_user.id

    state = get_state(user_id)

    if not state:
        return

    step = state.get("step")

    # =====================================================
    # EDIT SKILLS
    # =====================================================

    if step == "edit_skills":

        if not message.text:

            await message.answer(
                "⚠️ لطفاً مهارت‌ها رو به‌صورت "
                "متن بفرست."
            )

            return

        skills = parse_skills_input(
            message.text
        )

        clear_state(user_id)

        replace_user_skills(
            user_id,
            skills
        )

        skills_text = (
            "، ".join(skills)
            if skills
            else "(خالی — یعنی هیچ مهارتی ثبت نشد)"
        )

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 پروفایل",
            callback_data="profile"
        )

        await message.answer(
            "✅ <b>مهارت‌ها به‌روزرسانی شدند.</b>\n\n"
            f"{skills_text}",

            reply_markup=builder.as_markup()
        )

        return

    # =====================================================
    # EDIT BIO
    # =====================================================

    if step == "edit_bio":

        if not message.text:

            await message.answer(
                "⚠️ لطفاً بیو رو به‌صورت متن "
                "بفرست."
            )

            return

        bio = message.text.strip()[:MAX_BIO_LENGTH]

        clear_state(user_id)

        update_user_bio(
            user_id,
            bio
        )

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 پروفایل",
            callback_data="profile"
        )

        await message.answer(
            "✅ <b>بیو به‌روزرسانی شد.</b>\n\n"
            f"{bio}",

            reply_markup=builder.as_markup()
        )

        return

    # =====================================================
    # COLLABORATION — SELF-RATING
    # =====================================================

    if step == "collab_rating":

        if not message.text:

            await message.answer(
                "⚠️ لطفاً فقط یک عدد بین ۰ تا ۱۰ "
                "بفرست."
            )

            return

        try:

            score = int(
                message.text.strip()
            )

        except ValueError:

            await message.answer(
                "⚠️ لطفاً فقط یک عدد بین ۰ تا ۱۰ "
                "بفرست."
            )

            return

        if score < 0 or score > 10:

            await message.answer(
                "⚠️ نمره باید بین ۰ تا ۱۰ باشه."
            )

            return

        roles = state["roles"]
        index = state["rating_index"]
        role = roles[index]

        ratings = state.get("ratings", {})
        ratings[role] = score
        state["ratings"] = ratings

        index += 1
        state["rating_index"] = index

        if index < len(roles):

            next_role = roles[index]

            await message.answer(
                f"(‌{index + 1} از {len(roles)})\n"
                f"به‌عنوان «{next_role}» چه نمره‌ای "
                "از ۰ تا ۱۰ به خودت می‌دی؟"
            )

            return

        # همه‌ی نقش‌ها ارزیابی شدند — الان
        # درخواست همکاری واقعاً ثبت می‌شود.

        problem_id = state["problem_id"]

        problem = get_problem(problem_id)

        if not problem:

            clear_state(user_id)

            await message.answer(
                "❌ مسئله پیدا نشد.",
                reply_markup=main_menu()
            )

            return

        add_collaboration(
            problem_id,
            user_id
        )

        collaboration = get_collaboration(
            problem_id,
            user_id
        )

        if collaboration:

            for role, role_score in ratings.items():

                save_collaboration_rating(
                    collaboration["id"],
                    role,
                    role_score
                )

        clear_state(user_id)

        user = get_user(user_id)

        name = (
            user["full_name"]
            if user
            else "یک کاربر"
        )

        ratings_summary = "\n".join(
            f"• {role}: {role_score}/10"
            for role, role_score in ratings.items()
        )

        await notify_user(
            problem["telegram_id"],

            f"🤝 {name} برای همکاری روی "
            f"مسئله #{problem_id} اعلام آمادگی "
            "کرده است.\n\n"

            f"خودارزیابی:\n{ratings_summary}"
        )

        await message.answer(
            "✅ <b>اعلام آمادگی و خودارزیابی "
            "شما ثبت شد.</b>\n\n"

            "صاحب مسئله درخواست شما را "
            "بررسی می‌کند.",

            reply_markup=main_menu()
        )

        return

    # =====================================================
    # SUPPORT MESSAGE
    # =====================================================

    if step == "support_message":

        if not message.text:

            await message.answer(
                "⚠️ فقط پیام متنی برای پشتیبانی "
                "پذیرفته می‌شود."
            )

            return

        clear_state(user_id)

        if not ADMIN_IDS:

            logger.warning(
                "Support message from %s dropped — "
                "ADMIN_IDS is empty",
                user_id
            )

            await message.answer(
                "⚠️ در حال حاضر پشتیبانی فعال "
                "تنظیم نشده. بعداً دوباره تلاش کن."
            )

            return

        sender = message.from_user

        contact = (
            f"@{sender.username}"
            if sender.username
            else f"user id: {sender.id}"
        )

        forward_text = (
            "☎️ <b>پیام پشتیبانی جدید</b>\n\n"

            f"👤 از: {sender.full_name} "
            f"({contact})\n\n"

            f"{message.text}"
        )

        for admin_id in ADMIN_IDS:

            try:

                await bot.send_message(
                    admin_id,
                    forward_text
                )

            except Exception:

                logger.exception(
                    "Failed to forward support message "
                    "to admin %s",
                    admin_id
                )

        await message.answer(
            "✅ پیام شما برای پشتیبانی Monopi "
            "ارسال شد."
        )

        return

    # =====================================================
    # PROBLEM DESCRIPTION
    # =====================================================

    if step == "problem_description":

        if not message.text:
            return

        description = message.text.strip()

        if len(description) < 10:

            await send_message_and_delete_previous(
                message,

                "⚠️ توضیح مسئله خیلی کوتاه است.\n\n"
                "لطفاً مسئله را کمی کامل‌تر توضیح بده."
            )

            return

        state["description"] = description
        state["step"] = "problem_category"

        await send_message_and_delete_previous(
            message,

            "🏷 <b>دسته‌بندی مسئله</b>\n\n"
            "دسته‌بندی مناسب را انتخاب کن:",

            category_menu()
        )

        return

    # =====================================================
    # PROBLEM TITLE
    # =====================================================

    if step == "problem_title":

        if not message.text:
            return

        title = message.text.strip()

        if len(title) < 3:

            await send_message_and_delete_previous(
                message,

                "⚠️ عنوان خیلی کوتاه است.\n\n"
                "لطفاً یک عنوان واضح‌تر بنویس."
            )

            return

        state["title"] = title
        state["step"] = "problem_importance"

        await send_message_and_delete_previous(
            message,

            "⚠️ <b>اهمیت مسئله</b>\n\n"
            "این مسئله چقدر مهم است؟",

            importance_menu()
        )

        return

    # =====================================================
    # PROBLEM — REQUIRED ROLES
    # =====================================================

    if step == "problem_roles":

        if not message.text:

            await message.answer(
                "⚠️ لطفاً نقش‌ها رو به‌صورت متن "
                "بفرست، یا از دکمه‌ی رد کردن "
                "استفاده کن."
            )

            return

        roles = parse_skills_input(
            message.text
        )

        required_roles = (
            "، ".join(roles)
            if roles
            else None
        )

        _, text = finalize_problem_creation(
            user_id,
            state,
            required_roles
        )

        clear_state(user_id)

        await send_message_and_delete_previous(
            message,
            text,
            main_menu()
        )

        return

    # =====================================================
    # SOLUTION
    # =====================================================

    if step == "solution":

        if not message.text:
            return

        solution_text = message.text.strip()

        if len(solution_text) < 10:

            await send_message_and_delete_previous(
                message,

                "⚠️ راه‌حل خیلی کوتاه است.\n\n"
                "لطفاً ایده یا راه‌حل خودت را "
                "کمی کامل‌تر توضیح بده."
            )

            return

        problem_id = state.get("problem_id")

        problem = get_problem(problem_id)

        if not problem:

            clear_state(user_id)

            await send_message_and_delete_previous(
                message,

                "❌ مسئله پیدا نشد.",

                main_menu()
            )

            return

        state["solution_text"] = solution_text
        state["step"] = "solution_roles"

        builder = InlineKeyboardBuilder()

        builder.button(
            text="⏭ رد کردن",
            callback_data="skip_solution_roles"
        )

        await send_message_and_delete_previous(
            message,

            "🎯 <b>نقش‌های موردنیاز برای اجرا "
            "(اختیاری)</b>\n\n"
            "برای عملی‌کردن این راه‌حل چه "
            "افراد/تخصص‌هایی لازمه؟ با ویرگول "
            "(،) جدا کن. مثال:\n"
            "«برنامه‌نویس پایتون، طراح UI»\n\n"
            "لازم نداری؟ رد کن.",

            builder.as_markup()
        )

        return

    # =====================================================
    # SOLUTION — REQUIRED ROLES
    # =====================================================

    if step == "solution_roles":

        if not message.text:

            await message.answer(
                "⚠️ لطفاً نقش‌ها رو به‌صورت متن "
                "بفرست، یا از دکمه‌ی رد کردن "
                "استفاده کن."
            )

            return

        roles = parse_skills_input(
            message.text
        )

        required_roles = (
            "، ".join(roles)
            if roles
            else None
        )

        _, text = finalize_solution_creation(
            user_id,
            state,
            required_roles
        )

        clear_state(user_id)

        await send_message_and_delete_previous(
            message,
            text,
            main_menu()
        )

        return


# =========================================================
# CATEGORY SELECTED
# =========================================================

@dp.callback_query(F.data.startswith("category_"))
async def category_selected(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    state = get_state(user_id)

    if not state:
        return

    categories = {
        "category_industry": "صنعت و تولید",
        "category_technology": "فناوری",
        "category_life": "زندگی روزمره",
        "category_environment": "محیط زیست",
        "category_business": "کسب‌وکار",
        "category_education": "آموزش",
    }

    category = categories.get(
        callback.data,
        "سایر"
    )

    state["category"] = category
    state["step"] = "problem_title"

    await send_screen(
        callback,

        f"🏷 دسته‌بندی: <b>{category}</b>\n\n"
        "حالا یک عنوان کوتاه و واضح برای مسئله بنویس:"
    )


# =========================================================
# IMPORTANCE SELECTED
# =========================================================

@dp.callback_query(F.data.startswith("importance_"))
async def importance_selected(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    state = get_state(user_id)

    if not state:
        return

    importance_levels = {
        "importance_critical": "بسیار مهم",
        "importance_high": "مهم",
        "importance_medium": "متوسط",
        "importance_low": "کم",
    }

    importance = importance_levels.get(
        callback.data,
        "متوسط"
    )

    state["importance"] = importance
    state["step"] = "problem_roles"

    builder = InlineKeyboardBuilder()

    builder.button(
        text="⏭ رد کردن",
        callback_data="skip_problem_roles"
    )

    await send_screen(
        callback,

        "🎯 <b>نقش‌های موردنیاز (اختیاری)</b>\n\n"
        "چه افراد/تخصص‌هایی برای حل این مسئله "
        "لازمه؟ با ویرگول (،) جدا کن. مثال:\n"
        "«مهندس مکانیک، برنامه‌نویس، پژوهشگر»\n\n"
        "این لیست باعث می‌شه کسی که بعداً برای "
        "همکاری اعلام آمادگی می‌کنه، دقیقاً بگه "
        "تو کدوم بخش چقدر توانمنده.\n\n"
        "لازم نداری؟ رد کن.",

        builder.as_markup()
    )


def finalize_problem_creation(
    user_id: int,
    state: dict,
    required_roles
):
    """
    Shared by both the free-text roles step and the "skip" button
    — the only difference between them is what required_roles is.
    """

    problem_id = save_problem(
        telegram_id=user_id,
        title=state["title"],
        description=state["description"],
        category=state["category"],
        importance=state["importance"],
        required_roles=required_roles
    )

    roles_line = (
        f"🎯 نقش‌های موردنیاز: {required_roles}\n\n"
        if required_roles
        else ""
    )

    text = (
        "📥 <b>مسئله با موفقیت ثبت شد!</b>\n\n"

        "مسئله شما برای بررسی تیم Monopi ارسال شد.\n\n"

        f"🆔 #{problem_id}\n"
        f"📌 {state['title']}\n"
        f"🏷 {state['category']}\n"
        f"⚠️ اهمیت: {state['importance']}\n\n"

        f"{roles_line}"

        "پس از تأیید Admin، مسئله در بخش "
        "«کشف مسائل» منتشر خواهد شد."
    )

    return problem_id, text


@dp.callback_query(F.data == "skip_problem_roles")
async def skip_problem_roles(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    state = get_state(user_id)

    if not state:
        return

    _, text = finalize_problem_creation(
        user_id,
        state,
        None
    )

    clear_state(user_id)

    await send_screen(
        callback,
        text,
        main_menu()
    )


# =========================================================
# ADMIN — PENDING PROBLEMS REVIEW
# =========================================================

async def show_admin_problem(
    callback: CallbackQuery,
    problems,
    index: int
):

    if not problems:
        await send_screen(
            callback,

            "💡 <b>مسائل در انتظار بررسی</b>\n\n"
            "هیچ مسئله‌ای برای بررسی وجود ندارد.",

            admin_menu()
        )

        return

    problem = problems[index]

    text = (
        f"💡 <b>مسئله #{problem['id']}</b>\n\n"

        f"📌 <b>{problem['title']}</b>\n\n"

        f"{problem['description']}\n\n"

        f"🏷 دسته‌بندی: "
        f"{problem['category'] or 'سایر'}\n"

        f"⚠️ اهمیت: "
        f"{problem['importance'] or 'متوسط'}\n"

        f"👤 ثبت‌کننده: "
        f"{problem['telegram_id']}\n\n"

        f"📊 وضعیت: {problem['status']}\n\n"

        f"📄 {index + 1} از {len(problems)}"
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="❌ رد",
        callback_data=f"admin_reject_problem_{problem['id']}"
    )

    builder.button(
        text="✅ پذیرش",
        callback_data=f"admin_accept_problem_{problem['id']}"
    )

    if index > 0:

        builder.button(
            text="⬅️ قبلی",
            callback_data=f"admin_problem_prev_{index}"
        )

    if index < len(problems) - 1:

        builder.button(
            text="بعدی ➡️",
            callback_data=f"admin_problem_next_{index}"
        )

    builder.button(
        text="🔙 پنل مدیریت",
        callback_data="admin_menu"
    )

    builder.adjust(2, 2, 1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(F.data == "admin_pending_problems")
async def admin_pending_problems(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    problems = get_pending_problems()

    await show_admin_problem(
        callback,
        problems,
        0
    )


@dp.callback_query(
    F.data.startswith("admin_problem_prev_")
)
async def admin_problem_prev(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    index = int(
        callback.data.split("_")[-1]
    )

    problems = get_pending_problems()

    index -= 1

    if index < 0:
        index = 0

    await show_admin_problem(
        callback,
        problems,
        index
    )


@dp.callback_query(
    F.data.startswith("admin_problem_next_")
)
async def admin_problem_next(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    index = int(
        callback.data.split("_")[-1]
    )

    problems = get_pending_problems()

    index += 1

    if index >= len(problems):
        index = len(problems) - 1

    await show_admin_problem(
        callback,
        problems,
        index
    )


@dp.callback_query(
    F.data.startswith("admin_accept_problem_")
)
async def admin_accept_problem(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    problem_id = int(
        callback.data.split("_")[-1]
    )

    problem = get_problem(problem_id)

    if not problem:
        return

    update_problem_status(
        problem_id,
        "open"
    )

    await notify_user(
        problem["telegram_id"],

        f"🎉 مسئله #{problem_id} "
        f"در Monopi پذیرفته و منتشر شد."
    )

    await publish_to_channel(
        f"🧠 <b>Monopi</b>\n\n"

        f"💡 <b>مسئله #{problem_id}</b>\n\n"

        f"📌 <b>{problem['title']}</b>\n\n"

        f"{problem['description']}\n\n"

        f"🏷 دسته‌بندی: "
        f"{problem['category'] or 'سایر'}\n"

        f"⚠️ اهمیت: "
        f"{problem['importance'] or 'متوسط'}"
    )

    problems = get_pending_problems()

    if problems:

        await show_admin_problem(
            callback,
            problems,
            0
        )

    else:

        await send_screen(
            callback,

            "✅ مسئله منتشر شد.\n\n"
            "در حال حاضر مسئله دیگری "
            "برای بررسی وجود ندارد.",

            admin_menu()
        )


@dp.callback_query(
    F.data.startswith("admin_reject_problem_")
)
async def admin_reject_problem(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    problem_id = int(
        callback.data.split("_")[-1]
    )

    problem = get_problem(problem_id)

    if not problem:
        return

    update_problem_status(
        problem_id,
        "rejected"
    )

    await notify_user(
        problem["telegram_id"],

        f"❌ مسئله #{problem_id} "
        f"فعلاً توسط Monopi تأیید نشد."
    )

    await send_screen(
        callback,

        f"❌ <b>مسئله #{problem_id} رد شد.</b>",

        admin_menu()
    )


# =========================================================
# ADMIN — PENDING SOLUTIONS REVIEW
# =========================================================
#
# Content moderation gate for solutions: a solution only becomes
# visible to the problem's owner (get_problem_solutions) once an
# admin approves it here. This mirrors the problems review flow
# above one-to-one.

async def show_admin_solution(
    callback: CallbackQuery,
    solutions_list,
    index: int
):

    if not solutions_list:
        await send_screen(
            callback,

            "💭 <b>راه‌حل‌های در انتظار بررسی</b>\n\n"
            "هیچ راه‌حلی برای بررسی وجود ندارد.",

            admin_menu()
        )

        return

    solution = solutions_list[index]

    author = (
        solution["full_name"]
        or solution["username"]
        or "کاربر ناشناس"
    )

    text = (
        f"💭 <b>راه‌حل #{solution['id']}</b>\n\n"

        f"برای مسئله #{solution['problem_id']} — "
        f"{solution['problem_title']}\n\n"

        f"{solution['solution_text']}\n\n"

        f"👤 ارائه‌دهنده: {author}\n\n"

        f"📄 {index + 1} از {len(solutions_list)}"
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="❌ رد",
        callback_data=f"adminsol_reject_{solution['id']}"
    )

    builder.button(
        text="✅ پذیرش",
        callback_data=f"adminsol_approve_{solution['id']}"
    )

    if index > 0:

        builder.button(
            text="⬅️ قبلی",
            callback_data=f"adminsol_prev_{index}"
        )

    if index < len(solutions_list) - 1:

        builder.button(
            text="بعدی ➡️",
            callback_data=f"adminsol_next_{index}"
        )

    builder.button(
        text="🔙 پنل مدیریت",
        callback_data="admin_menu"
    )

    builder.adjust(2, 2, 1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(F.data == "admin_pending_solutions")
async def admin_pending_solutions(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    solutions_list = get_pending_admin_solutions()

    await show_admin_solution(
        callback,
        solutions_list,
        0
    )


@dp.callback_query(
    F.data.startswith("adminsol_prev_")
)
async def admin_solution_prev(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    index = int(
        callback.data.split("_")[-1]
    )

    solutions_list = get_pending_admin_solutions()

    index -= 1

    if index < 0:
        index = 0

    await show_admin_solution(
        callback,
        solutions_list,
        index
    )


@dp.callback_query(
    F.data.startswith("adminsol_next_")
)
async def admin_solution_next(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    index = int(
        callback.data.split("_")[-1]
    )

    solutions_list = get_pending_admin_solutions()

    index += 1

    if index >= len(solutions_list):
        index = len(solutions_list) - 1

    await show_admin_solution(
        callback,
        solutions_list,
        index
    )


@dp.callback_query(
    F.data.startswith("adminsol_approve_")
)
async def admin_approve_solution(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    solution_id = int(
        callback.data.split("_")[-1]
    )

    solutions_list = get_pending_admin_solutions()

    solution = next(
        (s for s in solutions_list if s["id"] == solution_id),
        None
    )

    if not solution:
        return

    update_solution_admin_status(
        solution_id,
        "approved"
    )

    await notify_user(
        solution["telegram_id"],

        f"🎉 راه‌حل شما برای مسئله "
        f"#{solution['problem_id']} تأیید شد و اکنون "
        f"برای صاحب مسئله قابل مشاهده است."
    )

    author = (
        solution["full_name"]
        or solution["username"]
        or "کاربر ناشناس"
    )

    await publish_to_channel(
        f"🧠 <b>Monopi</b>\n\n"

        f"💭 <b>راه‌حل جدید برای "
        f"مسئله #{solution['problem_id']}</b>\n\n"

        f"📌 {solution['problem_title']}\n\n"

        f"{solution['solution_text']}\n\n"

        f"👤 ارائه‌دهنده: {author}"
    )

    remaining = get_pending_admin_solutions()

    if remaining:

        await show_admin_solution(
            callback,
            remaining,
            0
        )

    else:

        await send_screen(
            callback,

            "✅ راه‌حل تأیید شد.\n\n"
            "در حال حاضر راه‌حل دیگری "
            "برای بررسی وجود ندارد.",

            admin_menu()
        )


@dp.callback_query(
    F.data.startswith("adminsol_reject_")
)
async def admin_reject_solution(callback: CallbackQuery):

    await safe_callback_answer(callback)

    if not is_admin(callback.from_user.id):
        return

    solution_id = int(
        callback.data.split("_")[-1]
    )

    solutions_list = get_pending_admin_solutions()

    solution = next(
        (s for s in solutions_list if s["id"] == solution_id),
        None
    )

    if not solution:
        return

    update_solution_admin_status(
        solution_id,
        "rejected"
    )

    await notify_user(
        solution["telegram_id"],

        f"❌ راه‌حل شما برای مسئله "
        f"#{solution['problem_id']} توسط ادمین "
        f"تأیید نشد."
    )

    remaining = get_pending_admin_solutions()

    if remaining:

        await show_admin_solution(
            callback,
            remaining,
            0
        )

    else:

        await send_screen(
            callback,

            "❌ راه‌حل رد شد.\n\n"
            "در حال حاضر راه‌حل دیگری "
            "برای بررسی وجود ندارد.",

            admin_menu()
        )


# =========================================================
# BROWSE PROBLEMS (single-problem carousel)
# =========================================================

async def show_problem_page(
    callback: CallbackQuery,
    problems,
    index: int
):

    if not problems:

        await send_screen(
            callback,

            "🔎 <b>کشف مسائل</b>\n\n"
            "در حال حاضر مسئله‌ای منتشر نشده است.",

            back_menu()
        )

        return

    problem = problems[index]

    problem_id = problem["id"]

    solutions = get_problem_solutions(
        problem_id
    )

    collaboration_count = (
        get_collaboration_count(
            problem_id
        )
    )

    user_id = callback.from_user.id

    is_owner = problem["telegram_id"] == user_id

    roles_line = (
        f"🎯 <b>نقش‌های موردنیاز:</b> "
        f"{problem['required_roles']}\n\n"
        if problem["required_roles"]
        else ""
    )

    text = (
        f"💡 <b>مسئله #{problem_id}</b>\n\n"

        f"📌 <b>{problem['title']}</b>\n\n"

        f"{problem['description']}\n\n"

        f"🏷 <b>دسته‌بندی:</b> "
        f"{problem['category'] or 'سایر'}\n"

        f"⚠️ <b>اهمیت:</b> "
        f"{problem['importance'] or 'متوسط'}\n\n"

        f"{roles_line}"

        f"{vote_summary_line('problem', problem_id)}\n\n"

        f"💭 راه‌حل‌ها: {len(solutions)}\n"
        f"🤝 درخواست‌های همکاری: "
        f"{collaboration_count}\n\n"

        f"📄 {index + 1} از {len(problems)}"
    )

    builder = InlineKeyboardBuilder()

    # Navigation

    if index > 0:

        builder.button(
            text="⬅️",
            callback_data=f"browse_prev_{index}"
        )

    if index < len(problems) - 1:

        builder.button(
            text="➡️",
            callback_data=f"browse_next_{index}"
        )

    # Voting — an owner can't vote on their own problem, so they
    # only see the score above, not the buttons.

    if not is_owner:

        user_vote = get_user_vote(
            user_id,
            "problem",
            problem_id
        )

        add_vote_buttons(
            builder,
            f"votebrowse_{problem_id}_{index}",
            user_vote
        )

    builder.button(
        text="💡 پیشنهاد راه‌حل",
        callback_data=f"suggest_{problem_id}"
    )

    builder.button(
        text="🤝 اعلام آمادگی برای همکاری",
        callback_data=f"collaborate_{problem_id}"
    )

    builder.button(
        text="🏠 منوی اصلی",
        callback_data="main_menu"
    )

    builder.adjust(2, 2, 1, 1, 1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(F.data == "browse_problems")
async def browse_problems(callback: CallbackQuery):

    await safe_callback_answer(callback)

    problems = get_open_problems(
        limit=1000,
        offset=0
    )

    await show_problem_page(
        callback,
        problems,
        0
    )


@dp.callback_query(
    F.data.startswith("browse_prev_")
)
async def browse_prev(callback: CallbackQuery):

    await safe_callback_answer(callback)

    index = int(
        callback.data.split("_")[-1]
    )

    problems = get_open_problems(
        limit=1000,
        offset=0
    )

    index -= 1

    if index < 0:
        index = 0

    await show_problem_page(
        callback,
        problems,
        index
    )


@dp.callback_query(
    F.data.startswith("browse_next_")
)
async def browse_next(callback: CallbackQuery):

    await safe_callback_answer(callback)

    index = int(
        callback.data.split("_")[-1]
    )

    problems = get_open_problems(
        limit=1000,
        offset=0
    )

    index += 1

    if index >= len(problems):
        index = len(problems) - 1

    await show_problem_page(
        callback,
        problems,
        index
    )


@dp.callback_query(
    F.data.startswith("votebrowse_")
)
async def vote_browse_problem(callback: CallbackQuery):
    """
    Voting from inside the "کشف مسائل" carousel — callback_data:
    votebrowse_{problem_id}_{index}_{up|down}
    """

    parts = callback.data.split("_")

    if len(parts) != 4:
        await safe_callback_answer(callback)
        return

    try:

        problem_id = int(parts[1])
        index = int(parts[2])
        direction = parts[3]

    except ValueError:

        await safe_callback_answer(callback)
        return

    problem = get_problem(problem_id)

    if not problem:
        await safe_callback_answer(callback)
        return

    if problem["telegram_id"] == callback.from_user.id:

        await safe_callback_answer(
            callback,
            "⚠️ نمی‌توانی به مسئله خودت رأی بدهی."
        )

        return

    toast = await apply_vote(
        callback.from_user.id,
        "problem",
        problem_id,
        direction
    )

    await safe_callback_answer(callback, toast)

    problems = get_open_problems(
        limit=1000,
        offset=0
    )

    if index >= len(problems):
        index = max(len(problems) - 1, 0)

    await show_problem_page(
        callback,
        problems,
        index
    )


# =========================================================
# PROBLEM DETAILS
# =========================================================

@dp.callback_query(F.data.startswith("problem_"))
async def problem_details(callback: CallbackQuery):

    await safe_callback_answer(callback)

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    await render_problem_details(
        callback,
        problem_id
    )


async def render_problem_details(
    callback: CallbackQuery,
    problem_id: int
):
    """
    Renders the single-problem detail screen. Split out from the
    problem_details handler so other handlers (e.g. voting) can
    refresh this screen without needing to fabricate a fake
    callback_data — aiogram's CallbackQuery is a frozen model and
    can't be mutated after the fact.
    """

    problem = get_problem(problem_id)

    if not problem:

        await send_screen(
            callback,
            "❌ این مسئله پیدا نشد.",
            back_menu()
        )

        return

    user_id = callback.from_user.id

    owner_id = problem["telegram_id"]

    collaboration_count = get_collaboration_count(
        problem_id
    )

    solutions = get_problem_solutions(
        problem_id
    )

    solution_count = len(solutions)

    # =====================================================
    # OWNER
    # =====================================================

    if user_id == owner_id:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🎯 پیدا کردن افراد مناسب",
            callback_data=f"matching_{problem_id}"
        )

        builder.button(
            text=f"💡 مشاهده راه‌حل‌ها ({solution_count})",
            callback_data=f"solutions_{problem_id}"
        )

        builder.button(
            text=f"👥 متقاضیان همکاری ({collaboration_count})",
            callback_data=f"applicants_{problem_id}"
        )

        builder.button(
            text="🔎 بازگشت به مسائل",
            callback_data="browse_problems"
        )

        builder.adjust(1)

        roles_line = (
            f"🎯 نقش‌های موردنیاز: "
            f"{problem['required_roles']}\n"
            if problem["required_roles"]
            else ""
        )

        text = (
            f"💡 <b>مسئله #{problem_id}</b>\n\n"

            f"📌 <b>{problem['title']}</b>\n\n"

            f"{problem['description']}\n\n"

            f"🏷 دسته‌بندی: {problem['category']}\n"
            f"⚠️ اهمیت: {problem['importance']}\n"
            f"{roles_line}"
            f"{vote_summary_line('problem', problem_id)}\n"
            f"💡 راه‌حل‌ها: {solution_count}\n"
            f"👥 متقاضیان همکاری: {collaboration_count}\n"
            f"📊 وضعیت: {problem['status']}"
        )

        await send_screen(
            callback,
            text,
            builder.as_markup()
        )

        return

    # =====================================================
    # NORMAL USER
    # =====================================================

    builder = InlineKeyboardBuilder()

    # مهم:
    # برای کاربری که صاحب مسئله نیست،
    # اولین اقدام پیشنهاد راه‌حل است.

    builder.button(
        text="💡 پیشنهاد راه‌حل",
        callback_data=f"suggest_{problem_id}"
    )

    user_vote = get_user_vote(
        user_id,
        "problem",
        problem_id
    )

    add_vote_buttons(
        builder,
        f"votepd_{problem_id}",
        user_vote
    )

    collaboration = get_collaboration(
        problem_id,
        user_id
    )

    if collaboration:

        status = collaboration["status"]

        if status == "pending":

            builder.button(
                text="⏳ درخواست همکاری در انتظار بررسی",
                callback_data="noop"
            )

        elif status == "accepted":

            builder.button(
                text="✅ همکاری پذیرفته شد",
                callback_data="noop"
            )

        elif status == "rejected":

            builder.button(
                text="❌ درخواست رد شده",
                callback_data="noop"
            )

    else:

        builder.button(
            text="🤝 اعلام آمادگی برای همکاری",
            callback_data=f"collaborate_{problem_id}"
        )

    builder.button(
        text="🔎 بازگشت به مسائل",
        callback_data="browse_problems"
    )

    builder.adjust(1, 2, 1, 1)

    roles_line = (
        f"🎯 نقش‌های موردنیاز: "
        f"{problem['required_roles']}\n\n"
        if problem["required_roles"]
        else ""
    )

    text = (
        f"💡 <b>مسئله #{problem_id}</b>\n\n"

        f"📌 <b>{problem['title']}</b>\n\n"

        f"{problem['description']}\n\n"

        f"🏷 دسته‌بندی: {problem['category']}\n"
        f"⚠️ اهمیت: {problem['importance']}\n\n"

        f"{roles_line}"

        f"{vote_summary_line('problem', problem_id)}\n\n"

        f"💡 {solution_count} راه‌حل\n"
        f"👥 {collaboration_count} درخواست همکاری"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(
    F.data.startswith("votepd_")
)
async def vote_problem_details(callback: CallbackQuery):
    """
    Voting from the direct problem detail screen — callback_data:
    votepd_{problem_id}_{up|down}
    """

    parts = callback.data.split("_")

    if len(parts) != 3:
        await safe_callback_answer(callback)
        return

    try:

        problem_id = int(parts[1])
        direction = parts[2]

    except ValueError:

        await safe_callback_answer(callback)
        return

    problem = get_problem(problem_id)

    if not problem:
        await safe_callback_answer(callback)
        return

    if problem["telegram_id"] == callback.from_user.id:

        await safe_callback_answer(
            callback,
            "⚠️ نمی‌توانی به مسئله خودت رأی بدهی."
        )

        return

    toast = await apply_vote(
        callback.from_user.id,
        "problem",
        problem_id,
        direction
    )

    await safe_callback_answer(callback, toast)

    await render_problem_details(
        callback,
        problem_id
    )


# =========================================================
# NO OP
# =========================================================

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):

    await safe_callback_answer(
        callback,
        "این گزینه در حال حاضر قابل تغییر نیست."
    )


# =========================================================
# SUGGEST SOLUTION
# =========================================================

@dp.callback_query(F.data.startswith("suggest_"))
async def suggest_solution(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    problem = get_problem(problem_id)

    if not problem:

        await send_screen(
            callback,
            "❌ مسئله پیدا نشد.",
            main_menu()
        )

        return

    if problem["telegram_id"] == user_id:

        await send_screen(
            callback,

            "⚠️ این مسئله متعلق به خودت است.\n\n"
            "برای مسئله خودت می‌توانی "
            "راه‌حل‌های دیگران را بررسی کنی.",

            back_menu()
        )

        return

    set_state(
        user_id,
        step="solution",
        problem_id=problem_id
    )

    await send_screen(
        callback,

        "💡 <b>پیشنهاد راه‌حل</b>\n\n"

        f"مسئله #{problem_id}\n"
        f"📌 {problem['title']}\n\n"

        "راه‌حل، ایده یا پیشنهادی که برای "
        "حل این مسئله داری را بنویس.\n\n"

        "✍️ راه‌حل خودت را ارسال کن:"
    )


def finalize_solution_creation(
    user_id: int,
    state: dict,
    required_roles
):
    """
    Shared by both the free-text roles step and the "skip"
    button — mirrors finalize_problem_creation() above.
    """

    solution_id = add_solution(
        problem_id=state["problem_id"],
        telegram_id=user_id,
        solution_text=state["solution_text"],
        required_roles=required_roles
    )

    roles_line = (
        f"🎯 نقش‌های موردنیاز: {required_roles}\n\n"
        if required_roles
        else ""
    )

    text = (
        "📥 <b>راه‌حل شما ثبت شد.</b>\n\n"

        f"🆔 شماره راه‌حل: #{solution_id}\n\n"

        f"{roles_line}"

        "راه‌حل شما در انتظار بررسی Admin "
        "Monopi قرار گرفت.\n\n"

        "پس از تأیید، برای صاحب مسئله قابل مشاهده خواهد بود."
    )

    return solution_id, text


@dp.callback_query(F.data == "skip_solution_roles")
async def skip_solution_roles(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    state = get_state(user_id)

    if not state:
        return

    _, text = finalize_solution_creation(
        user_id,
        state,
        None
    )

    clear_state(user_id)

    await send_screen(
        callback,
        text,
        main_menu()
    )


# =========================================================
# COLLABORATE
# =========================================================

@dp.callback_query(F.data.startswith("collaborate_"))
async def collaborate(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    problem = get_problem(problem_id)

    if not problem:

        await send_screen(
            callback,
            "❌ مسئله پیدا نشد.",
            main_menu()
        )

        return

    owner_id = problem["telegram_id"]

    if owner_id == user_id:

        await send_screen(
            callback,

            "⚠️ نمی‌توانی برای مسئله خودت "
            "درخواست همکاری ارسال کنی.",

            back_menu()
        )

        return

    existing = get_collaboration(
        problem_id,
        user_id
    )

    if existing:

        status = existing["status"]

        messages = {

            "pending":
                "⏳ درخواست همکاری شما قبلاً ثبت شده "
                "و در انتظار بررسی صاحب مسئله است.",

            "accepted":
                "✅ درخواست همکاری شما پذیرفته شده است.",

            "rejected":
                "❌ درخواست همکاری قبلی شما رد شده است.",
        }

        await send_screen(
            callback,

            messages.get(
                status,
                "ℹ️ درخواست شما قبلاً ثبت شده است."
            ),

            main_menu()
        )

        return

    # اگر برای این مسئله (یا راه‌حل پذیرفته‌شده‌اش)
    # نقش موردنیاز تعریف شده باشد، قبل از ثبت نهایی
    # از متقاضی می‌خواهیم خودش را در هر نقش
    # از ۰ تا ۱۰ ارزیابی کند — این اطلاعات دستی
    # توسط صاحب مسئله/راه‌حل برای چیدن تیم استفاده
    # می‌شود (فاز بعد: پیشنهاد خودکار با AI).

    accepted_solution = get_accepted_solution(
        problem_id
    )

    roles_source = None

    if (
        accepted_solution
        and accepted_solution["required_roles"]
    ):

        roles_source = (
            accepted_solution["required_roles"]
        )

    elif problem["required_roles"]:

        roles_source = problem["required_roles"]

    if roles_source:

        roles = parse_skills_input(
            roles_source
        )

    else:

        roles = []

    if roles:

        set_state(
            user_id,
            step="collab_rating",
            problem_id=problem_id,
            roles=roles,
            rating_index=0,
            ratings={}
        )

        first_role = roles[0]

        await send_screen(
            callback,

            "🎯 <b>خودارزیابی همکاری</b>\n\n"

            "قبل از ثبت نهایی، چند سؤال کوتاه "
            "می‌پرسیم تا صاحب مسئله بهتر بتونه "
            "تیم رو بچینه.\n\n"

            f"(۱ از {len(roles)})\n"
            f"به‌عنوان «{first_role}» چه نمره‌ای "
            "از ۰ تا ۱۰ به خودت می‌دی؟\n\n"
            "فقط عدد رو بفرست."
        )

        return

    success = add_collaboration(
        problem_id,
        user_id
    )

    if not success:

        await send_screen(
            callback,

            "ℹ️ درخواست همکاری شما قبلاً ثبت شده است.",

            main_menu()
        )

        return

    user = get_user(user_id)

    name = (
        user["full_name"]
        if user
        else "یک کاربر"
    )

    await notify_user(
        owner_id,

        f"🤝 {name} برای همکاری روی "
        f"مسئله #{problem_id} اعلام آمادگی کرده است."
    )

    await send_screen(
        callback,

        "✅ <b>اعلام آمادگی شما ثبت شد.</b>\n\n"

        "صاحب مسئله درخواست شما را بررسی می‌کند.\n\n"

        "در صورت پذیرش، وضعیت درخواست "
        "به شما اطلاع داده خواهد شد.",

        main_menu()
    )


# =========================================================
# OWNER APPLICANTS
# =========================================================

@dp.callback_query(F.data.startswith("applicants_"))
async def applicants(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != user_id:

        await send_screen(
            callback,

            "⛔ فقط صاحب مسئله می‌تواند "
            "متقاضیان را مشاهده کند.",

            main_menu()
        )

        return

    applicants_list = get_problem_collaborators(
        problem_id
    )

    if not applicants_list:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 بازگشت به مسئله",
            callback_data=f"problem_{problem_id}"
        )

        await send_screen(
            callback,

            "👥 <b>متقاضیان همکاری</b>\n\n"
            "هنوز کسی برای همکاری اعلام آمادگی نکرده است.",

            builder.as_markup()
        )

        return

    text = (
        f"👥 <b>متقاضیان همکاری "
        f"مسئله #{problem_id}</b>\n\n"
    )

    builder = InlineKeyboardBuilder()

    for applicant in applicants_list:

        applicant_id = applicant["telegram_id"]

        full_name = (
            applicant["full_name"]
            or "بدون نام"
        )

        status = applicant["status"]

        if status == "pending":
            status_text = "⏳ در انتظار بررسی"

        elif status == "accepted":
            status_text = "✅ پذیرفته شده"

        else:
            status_text = "❌ رد شده"

        skills = get_user_skills(
            applicant_id
        )

        skills_text = (
            ", ".join(skills)
            if skills
            else "مهارتی ثبت نشده"
        )

        text += (
            f"👤 <b>{full_name}</b>\n"
            f"🧠 {skills_text}\n"
            f"📊 {status_text}\n\n"
        )

        builder.button(
            text=f"👤 {short_text(full_name, 25)}",
            callback_data=(
                f"applicant_"
                f"{applicant['id']}_"
                f"{problem_id}"
            )
        )

    builder.button(
        text="🔙 بازگشت به مسئله",
        callback_data=f"problem_{problem_id}"
    )

    builder.adjust(1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# APPLICANT DETAILS
# =========================================================

@dp.callback_query(F.data.startswith("applicant_"))
async def applicant_details(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        collaboration_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:

        await send_screen(
            callback,
            "⛔ دسترسی غیرمجاز.",
            main_menu()
        )

        return

    applicants_list = get_problem_collaborators(
        problem_id
    )

    applicant = None

    for item in applicants_list:

        if item["id"] == collaboration_id:

            applicant = item
            break

    if not applicant:

        await send_screen(
            callback,
            "❌ متقاضی پیدا نشد.",
            main_menu()
        )

        return

    telegram_id = applicant["telegram_id"]

    skills = get_user_skills(
        telegram_id
    )

    skills_text = (
        "\n".join(
            f"• {skill}"
            for skill in skills
        )
        if skills
        else "مهارتی ثبت نشده است."
    )

    username = applicant["username"]

    username_text = (
        f"@{username}"
        if username
        else "ندارد"
    )

    ratings = get_collaboration_ratings(
        collaboration_id
    )

    if ratings:

        ratings_text = "\n".join(
            f"• {r['role']}: {r['score']}/10"
            for r in ratings
        )

        ratings_block = (
            f"🎯 <b>خودارزیابی:</b>\n"
            f"{ratings_text}\n\n"
        )

    else:

        ratings_block = ""

    text = (
        "👤 <b>متقاضی همکاری</b>\n\n"

        f"نام: {applicant['full_name']}\n"
        f"Username: {username_text}\n\n"

        f"🧠 <b>مهارت‌ها:</b>\n"
        f"{skills_text}\n\n"

        f"{ratings_block}"

        f"📊 وضعیت: {applicant['status']}"
    )

    builder = InlineKeyboardBuilder()

    if applicant["status"] == "pending":

        builder.button(
            text="✅ پذیرش",
            callback_data=(
                f"accept_"
                f"{collaboration_id}_"
                f"{problem_id}"
            )
        )

        builder.button(
            text="❌ رد",
            callback_data=(
                f"reject_"
                f"{collaboration_id}_"
                f"{problem_id}"
            )
        )

    builder.button(
        text="🔙 بازگشت",
        callback_data=f"applicants_{problem_id}"
    )

    builder.adjust(2, 1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# ACCEPT APPLICANT
# =========================================================

@dp.callback_query(F.data.startswith("accept_"))
async def accept_applicant(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        collaboration_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:
        return

    applicants_list = get_problem_collaborators(
        problem_id
    )

    applicant = None

    for item in applicants_list:

        if item["id"] == collaboration_id:

            applicant = item
            break

    if not applicant:
        return

    update_collaboration_status(
        collaboration_id,
        "accepted"
    )

    await notify_user(
        applicant["telegram_id"],

        f"🎉 درخواست همکاری شما برای مسئله "
        f"#{problem_id} پذیرفته شد!"
    )

    await send_screen(
        callback,

        "✅ <b>متقاضی پذیرفته شد.</b>\n\n"

        f"👤 {applicant['full_name']}\n\n"

        "یک اعلان برای او ارسال شد.",

        back_menu()
    )


# =========================================================
# REJECT APPLICANT
# =========================================================

@dp.callback_query(F.data.startswith("reject_"))
async def reject_applicant(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        collaboration_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:
        return

    applicants_list = get_problem_collaborators(
        problem_id
    )

    applicant = None

    for item in applicants_list:

        if item["id"] == collaboration_id:

            applicant = item
            break

    if not applicant:
        return

    update_collaboration_status(
        collaboration_id,
        "rejected"
    )

    await notify_user(
        applicant["telegram_id"],

        f"ℹ️ درخواست همکاری شما برای مسئله "
        f"#{problem_id} پذیرفته نشد."
    )

    await send_screen(
        callback,

        "❌ <b>درخواست همکاری رد شد.</b>\n\n"
        f"👤 {applicant['full_name']}",

        back_menu()
    )


# =========================================================
# OWNER SOLUTIONS
# =========================================================

@dp.callback_query(F.data.startswith("solutions_"))
async def solutions(callback: CallbackQuery):

    await safe_callback_answer(callback)

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:

        await send_screen(
            callback,

            "⛔ فقط صاحب مسئله می‌تواند "
            "راه‌حل‌ها را مدیریت کند.",

            main_menu()
        )

        return

    solutions_list = get_problem_solutions(
        problem_id
    )

    if not solutions_list:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 بازگشت",
            callback_data=f"problem_{problem_id}"
        )

        await send_screen(
            callback,

            "💡 <b>راه‌حل‌ها</b>\n\n"
            "هنوز راه‌حلی برای این مسئله ثبت نشده است.",

            builder.as_markup()
        )

        return

    text = (
        f"💡 <b>راه‌حل‌های "
        f"مسئله #{problem_id}</b>\n\n"
    )

    builder = InlineKeyboardBuilder()

    for solution in solutions_list:

        solution_id = solution["id"]

        author = (
            solution["full_name"]
            or "کاربر"
        )

        status = solution["status"]

        status_icon = {
            "pending": "⏳",
            "accepted": "✅",
            "rejected": "❌"
        }.get(
            status,
            "ℹ️"
        )

        text += (
            f"{status_icon} "
            f"<b>#{solution_id}</b>\n"
            f"👤 {author}\n"
            f"{short_text(solution['solution_text'], 120)}\n\n"
        )

        builder.button(
            text=f"💡 راه‌حل #{solution_id}",
            callback_data=(
                f"solution_"
                f"{solution_id}_"
                f"{problem_id}"
            )
        )

    builder.button(
        text="🔙 بازگشت به مسئله",
        callback_data=f"problem_{problem_id}"
    )

    builder.adjust(1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# SOLUTION DETAILS
# =========================================================

@dp.callback_query(F.data.startswith("solution_"))
async def solution_details(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        solution_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    await render_solution_details(
        callback,
        solution_id,
        problem_id
    )


async def render_solution_details(
    callback: CallbackQuery,
    solution_id: int,
    problem_id: int
):
    """
    Split out from the solution_details handler for the same
    reason as render_problem_details — lets the vote handler
    refresh this screen without mutating a frozen CallbackQuery.
    """

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:
        return

    solutions_list = get_problem_solutions(
        problem_id
    )

    solution = None

    for item in solutions_list:

        if item["id"] == solution_id:

            solution = item
            break

    if not solution:
        return

    roles_line = (
        f"🎯 نقش‌های موردنیاز برای اجرا: "
        f"{solution['required_roles']}\n\n"
        if solution["required_roles"]
        else ""
    )

    text = (
        f"💡 <b>راه‌حل #{solution_id}</b>\n\n"

        f"👤 ارائه‌دهنده: "
        f"{solution['full_name']}\n\n"

        f"{solution['solution_text']}\n\n"

        f"{roles_line}"

        f"{vote_summary_line('solution', solution_id)}\n\n"

        f"📊 وضعیت: {solution['status']}"
    )

    builder = InlineKeyboardBuilder()

    user_vote = get_user_vote(
        callback.from_user.id,
        "solution",
        solution_id
    )

    add_vote_buttons(
        builder,
        f"votesd_{solution_id}_{problem_id}",
        user_vote
    )

    if solution["status"] == "pending":

        builder.button(
            text="✅ پذیرش راه‌حل",
            callback_data=(
                f"acceptsolution_"
                f"{solution_id}_"
                f"{problem_id}"
            )
        )

        builder.button(
            text="❌ رد راه‌حل",
            callback_data=(
                f"rejectsolution_"
                f"{solution_id}_"
                f"{problem_id}"
            )
        )

        builder.adjust(2, 2, 1)

    else:

        builder.adjust(2, 1)

    builder.button(
        text="🔙 بازگشت",
        callback_data=f"solutions_{problem_id}"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


@dp.callback_query(
    F.data.startswith("votesd_")
)
async def vote_solution_details(callback: CallbackQuery):
    """
    Voting from the (owner-only) solution detail screen —
    callback_data: votesd_{solution_id}_{problem_id}_{up|down}
    """

    parts = callback.data.split("_")

    if len(parts) != 4:
        await safe_callback_answer(callback)
        return

    try:

        solution_id = int(parts[1])
        problem_id = int(parts[2])
        direction = parts[3]

    except ValueError:

        await safe_callback_answer(callback)
        return

    problem = get_problem(problem_id)

    if not problem:
        await safe_callback_answer(callback)
        return

    if problem["telegram_id"] != callback.from_user.id:
        await safe_callback_answer(callback)
        return

    toast = await apply_vote(
        callback.from_user.id,
        "solution",
        solution_id,
        direction
    )

    await safe_callback_answer(callback, toast)

    await render_solution_details(
        callback,
        solution_id,
        problem_id
    )


# =========================================================
# ACCEPT SOLUTION
# =========================================================

@dp.callback_query(F.data.startswith("acceptsolution_"))
async def accept_solution(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        solution_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:
        return

    solutions_list = get_problem_solutions(
        problem_id
    )

    solution = None

    for item in solutions_list:

        if item["id"] == solution_id:

            solution = item
            break

    if not solution:
        return

    update_solution_status(
        solution_id,
        "accepted"
    )

    await notify_user(
        solution["telegram_id"],

        f"🎉 راه‌حل شما برای مسئله "
        f"#{problem_id} پذیرفته شد!"
    )

    await send_screen(
        callback,

        "✅ <b>راه‌حل پذیرفته شد.</b>\n\n"
        f"راه‌حل #{solution_id}",

        back_menu()
    )


# =========================================================
# REJECT SOLUTION
# =========================================================

@dp.callback_query(F.data.startswith("rejectsolution_"))
async def reject_solution(callback: CallbackQuery):

    await safe_callback_answer(callback)

    parts = callback.data.split("_")

    if len(parts) != 3:
        return

    try:

        solution_id = int(parts[1])
        problem_id = int(parts[2])

    except ValueError:

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:
        return

    solutions_list = get_problem_solutions(
        problem_id
    )

    solution = None

    for item in solutions_list:

        if item["id"] == solution_id:

            solution = item
            break

    if not solution:
        return

    update_solution_status(
        solution_id,
        "rejected"
    )

    await notify_user(
        solution["telegram_id"],

        f"ℹ️ راه‌حل شما برای مسئله "
        f"#{problem_id} پذیرفته نشد."
    )

    await send_screen(
        callback,

        "❌ <b>راه‌حل رد شد.</b>\n\n"
        f"راه‌حل #{solution_id}",

        back_menu()
    )


# =========================================================
# MATCHING
# =========================================================

@dp.callback_query(F.data.startswith("matching_"))
async def matching(callback: CallbackQuery):

    await safe_callback_answer(callback)

    try:

        problem_id = int(
            callback.data.split("_")[1]
        )

    except (ValueError, IndexError):

        return

    problem = get_problem(problem_id)

    if not problem:
        return

    if problem["telegram_id"] != callback.from_user.id:

        await send_screen(
            callback,

            "⛔ فقط صاحب مسئله می‌تواند "
            "افراد مناسب را پیدا کند.",

            main_menu()
        )

        return

    problem_skills = get_problem_skills(
        problem_id
    )

    users = find_matching_users(
        problem_id
    )

    if not users:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 بازگشت",
            callback_data=f"problem_{problem_id}"
        )

        await send_screen(
            callback,

            "🎯 <b>افراد مناسب</b>\n\n"

            "در حال حاضر فردی با مهارت‌های "
            "مرتبط پیدا نشد.\n\n"

            "با افزایش کاربران و مهارت‌ها، "
            "سیستم Matching دقیق‌تر خواهد شد.",

            builder.as_markup()
        )

        return

    text = (
        f"🎯 <b>افراد مناسب "
        f"برای مسئله #{problem_id}</b>\n\n"
    )

    if problem_skills:

        text += (
            "🧠 مهارت‌های مسئله:\n"

            + "\n".join(
                f"• {skill}"
                for skill in problem_skills
            )

            + "\n\n"
        )

    for index, user in enumerate(
        users[:10],
        1
    ):

        telegram_id = user["telegram_id"]

        full_name = (
            user["full_name"]
            or "بدون نام"
        )

        matched = user["matched_skills"]

        user_skills = get_user_skills(
            telegram_id
        )

        skills_text = ", ".join(
            user_skills
        )

        text += (
            f"🏆 <b>{index}. "
            f"{full_name}</b>\n"

            f"⭐ تطابق مهارت: {matched}\n"

            f"🧠 {skills_text}\n\n"
        )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 بازگشت به مسئله",
        callback_data=f"problem_{problem_id}"
    )

    builder.adjust(1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# PROFILE
# =========================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):

    await safe_callback_answer(callback)

    text, markup = build_profile_view(
        callback.from_user.id,
        callback.from_user.username,
        callback.from_user.full_name
    )

    await send_screen(
        callback,
        text,
        markup
    )


def build_profile_view(
    user_id: int,
    username: str | None,
    full_name: str | None
):
    """
    Builds the profile screen's (text, inline_markup) pair.
    Shared by the "profile" inline-button callback and the
    persistent-keyboard "👤 پروفایل من" reply button, which
    can't reuse a CallbackQuery-based handler directly since it
    arrives as a plain Message instead.
    """

    user = get_user(user_id)

    if not user:

        save_user(
            telegram_id=user_id,
            username=username,
            full_name=full_name
        )

        user = get_user(user_id)

    skills = get_user_skills(
        user_id
    )

    if skills:

        skills_text = "\n".join(
            f"• {skill}"
            for skill in skills
        )

    else:

        skills_text = (
            "هنوز مهارتی ثبت نشده است."
        )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="✍️ ویرایش بیو",
        callback_data="edit_bio"
    )

    builder.button(
        text="🧠 مدیریت مهارت‌ها",
        callback_data="add_skill"
    )

    builder.button(
        text="📊 فعالیت‌های من",
        callback_data="activity"
    )

    builder.button(
        text="🏠 منوی اصلی",
        callback_data="main_menu"
    )

    builder.adjust(1)

    user_username = user["username"]

    username_text = (
        f"@{user_username}"
        if user_username
        else "ندارد"
    )

    bio_text = (
        user["bio"]
        if user["bio"]
        else "هنوز چیزی ننوشتی."
    )

    text = (
        "👤 <b>پروفایل من</b>\n\n"

        f"نام: {user['full_name']}\n"
        f"Username: {username_text}\n\n"

        f"📝 <b>درباره من:</b>\n"
        f"{bio_text}\n\n"

        f"🧠 <b>مهارت‌ها:</b>\n"
        f"{skills_text}"
    )

    return text, builder.as_markup()


# =========================================================
# EDIT SKILLS (free text)
# =========================================================

MAX_SKILLS = 10
MAX_SKILL_LENGTH = 30


def parse_skills_input(text: str):
    """
    Splits on Persian و Latin commas, trims whitespace, drops
    empties/dupes, and caps length so one giant paste can't
    blow up the skills list or the DB row size.
    """

    raw_parts = (
        text
        .replace("،", ",")
        .split(",")
    )

    skills = []

    for part in raw_parts:

        skill = part.strip()

        if not skill:
            continue

        skill = skill[:MAX_SKILL_LENGTH]

        if skill not in skills:
            skills.append(skill)

        if len(skills) >= MAX_SKILLS:
            break

    return skills


@dp.callback_query(F.data == "add_skill")
async def edit_skills_prompt(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    current_skills = get_user_skills(
        user_id
    )

    current_text = (
        "، ".join(current_skills)
        if current_skills
        else "هنوز مهارتی ثبت نکرده‌ای."
    )

    set_state(
        user_id,
        step="edit_skills"
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 پروفایل",
        callback_data="profile"
    )

    await send_screen(
        callback,

        "🧠 <b>مهارت‌های من</b>\n\n"

        f"<b>فعلاً:</b> {current_text}\n\n"

        "مهارت‌هات رو بنویس، با ویرگول (،) "
        "جدا کن. مثال:\n"
        "«برنامه‌نویسی پایتون، طراحی UI، "
        "بازاریابی»\n\n"

        "⚠️ این لیست جدید کاملاً جای لیست "
        f"قبلی رو می‌گیره (حداکثر {MAX_SKILLS} مورد).",

        builder.as_markup()
    )


# =========================================================
# EDIT BIO
# =========================================================

MAX_BIO_LENGTH = 200


@dp.callback_query(F.data == "edit_bio")
async def edit_bio_prompt(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    user = get_user(user_id)

    current_bio = (
        user["bio"]
        if user and user["bio"]
        else "هنوز چیزی ننوشتی."
    )

    set_state(
        user_id,
        step="edit_bio"
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 پروفایل",
        callback_data="profile"
    )

    await send_screen(
        callback,

        "✍️ <b>ویرایش بیو</b>\n\n"

        f"<b>فعلاً:</b> {current_bio}\n\n"

        "یک جمله کوتاه درباره‌ی خودت بنویس "
        f"(حداکثر {MAX_BIO_LENGTH} کاراکتر) "
        "و بفرست.",

        builder.as_markup()
    )


# =========================================================
# ACTIVITY
# =========================================================

@dp.callback_query(F.data == "activity")
async def activity(callback: CallbackQuery):

    await safe_callback_answer(callback)

    builder = InlineKeyboardBuilder()

    builder.button(
        text="💡 مسائل من",
        callback_data="my_problems"
    )

    builder.button(
        text="🧠 راه‌حل‌های من",
        callback_data="my_solutions"
    )

    builder.button(
        text="🤝 درخواست‌های همکاری من",
        callback_data="my_collaborations"
    )

    builder.button(
        text="🔙 منوی اصلی",
        callback_data="main_menu"
    )

    builder.adjust(1)

    await send_screen(
        callback,

        "📊 <b>فعالیت‌های من</b>\n\n"
        "یکی از بخش‌های زیر را انتخاب کن:",

        builder.as_markup()
    )


# =========================================================
# MY PROBLEMS
# =========================================================

@dp.callback_query(F.data == "my_problems")
async def my_problems(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    problems = get_user_problems(
        user_id
    )

    if not problems:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="💡 ثبت مسئله",
            callback_data="create_problem"
        )

        builder.button(
            text="🔙 فعالیت‌های من",
            callback_data="activity"
        )

        builder.adjust(1)

        await send_screen(
            callback,

            "💡 <b>مسائل من</b>\n\n"
            "هنوز مسئله‌ای ثبت نکرده‌ای.",

            builder.as_markup()
        )

        return

    text = "💡 <b>مسائل من</b>\n\n"

    builder = InlineKeyboardBuilder()

    for problem in problems:

        problem_id = problem["id"]

        title = problem["title"]

        text += (
            f"#{problem_id} — {title}\n"
            f"🏷 {problem['category']}\n"
            f"📊 {problem['status']}\n\n"
        )

        builder.button(
            text=f"#{problem_id} {short_text(title)}",
            callback_data=f"problem_{problem_id}"
        )

    builder.button(
        text="🔙 فعالیت‌های من",
        callback_data="activity"
    )

    builder.adjust(1)

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# MY SOLUTIONS
# =========================================================

def describe_solution_status(solution) -> str:
    """
    Human-readable status for a solution shown to its own author.

    A solution goes through two independent gates:
    1. admin_status — content moderation by an admin.
    2. status       — the problem owner's pick, only relevant
                       once admin_status is 'approved'.
    """

    admin_status = solution["admin_status"]

    if admin_status == "pending":
        return "⏳ در انتظار بررسی ادمین"

    if admin_status == "rejected":
        return "🚫 توسط ادمین تأیید نشد"

    # admin_status == "approved" from here on
    status = solution["status"]

    if status == "accepted":
        return "✅ پذیرفته شد"

    if status == "rejected":
        return "❌ صاحب مسئله انتخاب نکرد"

    return "👀 در انتظار بررسی صاحب مسئله"


@dp.callback_query(F.data == "my_solutions")
async def my_solutions(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    solutions_list = get_user_solutions(
        user_id
    )

    if not solutions_list:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 فعالیت‌های من",
            callback_data="activity"
        )

        await send_screen(
            callback,

            "🧠 <b>راه‌حل‌های من</b>\n\n"
            "هنوز راه‌حلی ثبت نکرده‌ای.",

            builder.as_markup()
        )

        return

    text = "🧠 <b>راه‌حل‌های من</b>\n\n"

    for solution in solutions_list:

        text += (
            f"{describe_solution_status(solution)}\n"
            f"مسئله #{solution['problem_id']}\n"

            f"📌 {solution['title']}\n"

            f"{short_text(solution['solution_text'], 100)}\n\n"
        )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 فعالیت‌های من",
        callback_data="activity"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# MY COLLABORATIONS
# =========================================================

@dp.callback_query(F.data == "my_collaborations")
async def my_collaborations(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    collaborations = get_user_collaborations(
        user_id
    )

    if not collaborations:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 فعالیت‌های من",
            callback_data="activity"
        )

        await send_screen(
            callback,

            "🤝 <b>درخواست‌های همکاری من</b>\n\n"
            "هنوز برای مسئله‌ای درخواست همکاری "
            "ارسال نکرده‌ای.",

            builder.as_markup()
        )

        return

    text = (
        "🤝 <b>درخواست‌های همکاری من</b>\n\n"
    )

    for item in collaborations:

        status_icon = {
            "pending": "⏳",
            "accepted": "✅",
            "rejected": "❌",
        }.get(
            item["status"],
            "ℹ️"
        )

        text += (
            f"{status_icon} "
            f"مسئله #{item['problem_id']}\n"

            f"📌 {item['title']}\n"

            f"📊 {item['status']}\n\n"
        )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🔙 فعالیت‌های من",
        callback_data="activity"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# NOTIFICATIONS
# =========================================================

@dp.callback_query(F.data == "notifications")
async def notifications(callback: CallbackQuery):

    await safe_callback_answer(callback)

    user_id = callback.from_user.id

    notifications_list = get_unread_notifications(
        user_id
    )

    if not notifications_list:

        builder = InlineKeyboardBuilder()

        builder.button(
            text="🔙 منوی اصلی",
            callback_data="main_menu"
        )

        await send_screen(
            callback,

            "🔔 <b>اعلان‌ها</b>\n\n"
            "اعلان جدیدی نداری.",

            builder.as_markup()
        )

        return

    text = "🔔 <b>اعلان‌های جدید</b>\n\n"

    for notification in notifications_list:

        text += (
            f"• {notification['message']}\n\n"
        )

    mark_notifications_read(
        user_id
    )

    builder = InlineKeyboardBuilder()

    builder.button(
        text="🏠 منوی اصلی",
        callback_data="main_menu"
    )

    await send_screen(
        callback,
        text,
        builder.as_markup()
    )


# =========================================================
# ERROR HANDLER
# =========================================================

@dp.errors()
async def error_handler(event):

    logging.exception(
        "Unhandled exception: %s",
        event.exception
    )

    return True


# =========================================================
# MAIN
# =========================================================

async def main():

    logger.info("=" * 50)
    logger.info("Monopi V2 — starting up")
    logger.info("=" * 50)

    init_db()
    logger.info("Database ready at: %s", DB_PATH)

    background_tasks = [
        asyncio.create_task(backup_loop()),
        asyncio.create_task(notify_admins_startup()),
    ]

    # Let Railway's SIGTERM (sent on redeploy / manual restart /
    # scale-down) stop polling cleanly instead of killing the
    # process mid-request after the grace period expires.
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGTERM, signal.SIGINT):

        try:

            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(dp.stop_polling())
            )

        except NotImplementedError:

            # add_signal_handler isn't available on Windows;
            # harmless to skip there, Ctrl+C still works via
            # KeyboardInterrupt.
            pass

    logger.info(
        "Bot starting in polling mode (drop_pending_updates=%s)",
        DROP_PENDING_UPDATES
    )

    # Retry/backoff loop: keeps this *process* alive through
    # transient failures (Telegram API timeouts, DNS hiccups,
    # brief network drops) instead of exiting and letting
    # Railway's restart policy churn the container. A genuinely
    # unrecoverable error (bad token) fails fast instead of
    # retrying forever.
    backoff = 5
    max_backoff = 300

    try:

        while True:

            try:

                await dp.start_polling(
                    bot,
                    drop_pending_updates=DROP_PENDING_UPDATES,
                )

                # start_polling only returns on a clean stop
                # (dp.stop_polling() was called) — not an error.
                logger.info("Polling stopped cleanly.")
                break

            except TelegramUnauthorizedError:

                logger.error(
                    "BOT_TOKEN is invalid or was revoked — "
                    "check Railway → Variables. Not retrying."
                )

                raise

            except asyncio.CancelledError:

                raise

            except Exception:

                logger.exception(
                    "Polling crashed — retrying in %s seconds",
                    backoff
                )

                await asyncio.sleep(backoff)

                backoff = min(backoff * 2, max_backoff)

    finally:

        for task in background_tasks:
            task.cancel()

        logger.info("Closing bot session...")

        await bot.session.close()

        logger.info("Monopi V2 stopped.")


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Interrupted by user (Ctrl+C).")
