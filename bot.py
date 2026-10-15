import os
import re
import time
import html
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

from telegram import Update, BotCommand, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType
from telegram.ext import ChatMemberHandler, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Load BOT_TOKEN from a local .env file if it exists.
# Never commit .env to a public repository.
ENV_FILE = Path(__file__).with_name(".env")
if ENV_FILE.exists():
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "BOT_TOKEN" and not os.getenv("BOT_TOKEN"):
            os.environ["BOT_TOKEN"] = value.strip().strip('"').strip("'")

BOT_TOKEN =
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).with_name("data") / "welcome.sqlite3")))

DEFAULT_DELETE_SECONDS = 30
MIN_DELETE_SECONDS = 5
MAX_DELETE_SECONDS = 600
IGNORE_BOTS = True

DEFAULT_TEMPLATE = (
    "✨ <b>𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎 𝐎𝐔𝐑 𝐆𝐑𝐎𝐔𝐏</b> ✨\n\n"
    "🎉 Hey, {mention}! ❤️\n"
    "নতুন একটা Family-তে তোমাকে স্বাগতম! 🫶🌸\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "💫 <b>WELCOME TO OUR COMMUNITY!</b>\n\n"
    "🤝 সবার সাথে Friendly থাকো\n"
    "💖 সবাইকে Respect করো\n"
    "🚀 Stay Active • Stay Connected\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "👤 <b>Member:</b> {full_name}\n"
    "🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
    "🛡️ <b>GROUP RULE:</b> Respect Everyone ❤️\n\n"
    "💌 আমাদের Community-তে তোমাকে স্বাগতম!\n"
    "💖 <b>ENJOY YOUR STAY!</b> 💖\n\n"
    "⏳ <i>Auto-delete in {delete_after} seconds.</i>"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("premium_welcome")


@contextmanager
def db_connect():
    """Yield a SQLite connection and always close it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db_connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                welcome_enabled INTEGER NOT NULL DEFAULT 1,
                delete_after INTEGER NOT NULL DEFAULT 30,
                welcome_template TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_deletions (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
        """)


def get_settings(chat_id):
    with db_connect() as con:
        row = con.execute(
            "SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            return dict(row)

        con.execute(
            """INSERT OR IGNORE INTO group_settings
               (chat_id, welcome_enabled, delete_after, welcome_template)
               VALUES (?, 1, ?, ?)""",
            (chat_id, DEFAULT_DELETE_SECONDS, DEFAULT_TEMPLATE),
        )
        row = con.execute(
            "SELECT * FROM group_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return dict(row)


def update_setting(chat_id, column, value):
    if column not in {"welcome_enabled", "delete_after", "welcome_template"}:
        raise ValueError("Invalid setting")
    get_settings(chat_id)
    with db_connect() as con:
        con.execute(
            f"UPDATE group_settings SET {column} = ? WHERE chat_id = ?",
            (value, chat_id),
        )


def save_pending_delete(chat_id, message_id, delete_at):
    with db_connect() as con:
        con.execute(
            """INSERT OR REPLACE INTO pending_deletions
               (chat_id, message_id, delete_at) VALUES (?, ?, ?)""",
            (chat_id, message_id, delete_at),
        )


def remove_pending_delete(chat_id, message_id):
    with db_connect() as con:
        con.execute(
            "DELETE FROM pending_deletions WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )


def get_pending_deletions():
    with db_connect() as con:
        rows = con.execute(
            "SELECT chat_id, message_id, delete_at FROM pending_deletions"
        ).fetchall()
        return [dict(row) for row in rows]


def group_only(update):
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


async def is_group_admin(update, context):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user or chat.type == ChatType.PRIVATE:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        logger.exception("Could not verify group admin")
        return False


async def require_admin(update, context):
    if not await is_group_admin(update, context):
        if update.effective_message:
            await update.effective_message.reply_text(
                "🛡️ এই কমান্ডটি শুধু Group Admin ব্যবহার করতে পারবেন।"
            )
        return False
    return True


def format_welcome(template, user, settings):
    full_name = html.escape(user.full_name or "Friend")
    username = f"@{user.username}" if user.username else "Not set"
    values = {
        "first_name": html.escape(user.first_name or "Friend"),
        "full_name": full_name,
        "username": html.escape(username),
        "user_id": str(user.id),
        "mention": f'<a href="tg://user?id={user.id}">{full_name}</a>',
        "delete_after": str(settings["delete_after"]),
    }
    pattern = re.compile(
        r"\{(first_name|full_name|username|user_id|mention|delete_after)\}"
    )
    return pattern.sub(lambda m: values[m.group(1)], template)


def validate_template(template):
    # Validate HTML without sending a test message.
    from html.parser import HTMLParser

    allowed_tags = {
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "code", "pre", "a", "tg-spoiler", "blockquote"
    }

    class TelegramHTMLValidator(HTMLParser):
        VOID = set()
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.error = None

        def handle_starttag(self, tag, attrs):
            if tag not in allowed_tags:
                self.error = f"Unsupported HTML tag: <{tag}>"
                return
            if tag == "a":
                attr_dict = dict(attrs)
                if not attr_dict.get("href"):
                    self.error = "The <a> tag needs an href attribute."
                    return
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag not in allowed_tags:
                self.error = f"Unsupported HTML tag: </{tag}>"
                return
            if not self.stack or self.stack[-1] != tag:
                self.error = f"Unmatched or misordered closing tag: </{tag}>"
                return
            self.stack.pop()

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()

    parser = TelegramHTMLValidator()
    try:
        parser.feed(template)
        parser.close()
    except Exception as exc:
        return str(exc)
    if parser.error:
        return parser.error
    if parser.stack:
        return f"Unclosed HTML tag: <{parser.stack[-1]}>"
    return None


async def delete_welcome(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id, message_id = data["chat_id"], data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info("Deleted welcome message %s in %s", message_id, chat_id)
    except (BadRequest, Forbidden):
        logger.warning(
            "Could not delete message %s in chat %s", message_id, chat_id,
            exc_info=True
        )
    except TelegramError:
        logger.exception("Telegram deletion error")
    finally:
        remove_pending_delete(chat_id, message_id)


def schedule_delete(application, chat_id, message_id, delete_at):
    delay = max(0, delete_at - time.time())
    application.job_queue.run_once(
        delete_welcome,
        when=delay,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"delete-{chat_id}-{message_id}",
    )


async def post_init(application: Application):
    for item in get_pending_deletions():
        schedule_delete(application, item["chat_id"], item["message_id"], item["delete_at"])

    # Make slash commands visible in Telegram's command menu.
    menu = [
        BotCommand("start", "Open bot menu"),
        BotCommand("help", "Show all commands"),
        BotCommand("welcome_on", "Enable welcome (admin)"),
        BotCommand("welcome_off", "Disable welcome (admin)"),
        BotCommand("welcome_status", "Show settings"),
        BotCommand("set_delete", "Set auto-delete seconds"),
        BotCommand("delete_time", "Show auto-delete delay"),
        BotCommand("set_welcome", "Set custom welcome"),
        BotCommand("reset_welcome", "Restore default welcome"),
        BotCommand("welcome_preview", "Preview welcome"),
        BotCommand("ban", "Ban replied-to member"),
        BotCommand("unban", "Unban by user ID"),
        BotCommand("kick", "Remove replied-to member"),
        BotCommand("mute", "Mute replied-to member"),
        BotCommand("unmute", "Unmute replied-to member"),
        BotCommand("admins", "List group admins"),
        BotCommand("id", "Show IDs"),
        BotCommand("ping", "Check bot status"),
    ]
    try:
        from telegram import BotCommandScopeAllGroupChats
        await application.bot.set_my_commands(menu)
        await application.bot.set_my_commands(menu, scope=BotCommandScopeAllGroupChats())
    except TelegramError:
        logger.exception("Could not register slash command menu")
    logger.info("Restored pending deletion jobs and registered commands")


async def send_temporary_notice(context, chat_id, text, delete_after=30):
    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        delete_at = time.time() + delete_after
        save_pending_delete(chat_id, sent.message_id, delete_at)
        schedule_delete(context.application, chat_id, sent.message_id, delete_at)
    except TelegramError:
        logger.exception("Could not send membership notice")


async def delete_join_service_message(update, context):
    """Delete Telegram's join/add service message, not the bot's welcome message."""
    message = update.effective_message
    # Telegram creates service messages for both joins and departures.
    # new_chat_members covers joins; left_chat_member covers a user leaving
    # or being removed. Delete either service message immediately.
    if not message or not (
        message.new_chat_members or message.left_chat_member
    ):
        return

    try:
        await message.delete()
        logger.info(
            "Deleted join/leave service message %s in chat %s",
            message.message_id,
            message.chat_id,
        )
    except (BadRequest, Forbidden):
        logger.warning(
            "Cannot delete join/leave service message %s in chat %s. "
            "Check that the bot is an admin with Delete Messages permission.",
            message.message_id,
            message.chat_id,
            exc_info=True,
        )
    except TelegramError:
        logger.exception("Telegram API error deleting join service message")


async def welcome_member_update(update, context):
    event = update.chat_member
    if not event:
        return
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    active = {"member", "administrator", "creator"}
    was_active, is_active = old_status in active, new_status in active
    user = event.new_chat_member.user
    if IGNORE_BOTS and user.is_bot:
        return

    settings = get_settings(chat.id)
    if not was_active and is_active:
        if not settings["welcome_enabled"]:
            return
        try:
            text = format_welcome(settings["welcome_template"], user, settings)
            sent = await context.bot.send_message(
                chat_id=chat.id, text=text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            delete_at = time.time() + settings["delete_after"]
            save_pending_delete(chat.id, sent.message_id, delete_at)
            schedule_delete(context.application, chat.id, sent.message_id, delete_at)
            logger.info("Welcome sent to user %s in chat %s", user.id, chat.id)
        except TelegramError:
            logger.exception("Could not send welcome message")
    elif was_active and not is_active:
        name = html.escape(user.full_name or "Member")
        await send_temporary_notice(
            context, chat.id, f"👋 <b>Goodbye!</b> {name} গ্রুপ থেকে চলে গেছেন।",
            settings["delete_after"],
        )


async def cmd_start(update, context):
    bot_user = await context.bot.get_me()
    group_url = f"https://t.me/{bot_user.username}?startgroup=true"
    channel_url = f"https://t.me/{bot_user.username}?startchannel&admin=post_messages+delete_messages"
    keyboard = [
        [
            InlineKeyboardButton("➕ Add to Group", url=group_url),
            InlineKeyboardButton("📢 Add to Channel", url=channel_url),
        ],
        [
            InlineKeyboardButton("🆔 Show IDs", callback_data="show_ids"),
            InlineKeyboardButton("📚 Commands", callback_data="show_help"),
        ],
    ]
    text = (
        "👑 <b>PREMIUM WELCOME BOT</b>\\n"
        "━━━━━━━━━━━━━━━━━━\\n"
        "👋 স্বাগতম! Welcome to your control panel.\\n\\n"
        "➕ Group-এ যোগ করতে Add to Group চাপুন।\\n"
        "📢 Channel-এ যোগ করতে Add to Channel চাপুন।\\n\\n"
        "🛡️ Join করার পর bot-কে প্রয়োজনীয় Admin permissions দিন।"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def start_menu_callback(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "show_help":
        await query.message.reply_text("📚 সব কমান্ড দেখতে /help পাঠান।")
    elif query.data == "show_ids":
        user = update.effective_user
        chat = update.effective_chat
        bot_user = await context.bot.get_me()
        await query.message.reply_text(
            f"👤 User ID: {user.id if user else 'N/A'}\\n"
            f"🤖 Bot ID: {bot_user.id}\\n"
            f"💬 Current Chat ID: {chat.id if chat else 'N/A'}\\n"
            "Group/Channel ID পেতে সেই chat-এ /id ব্যবহার করুন।"
        )


async def bot_membership_update(update, context):
    event = update.my_chat_member
    if not event:
        return
    chat = event.chat
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    active = {"member", "administrator", "creator"}
    if old_status in active or new_status not in active:
        return

    chat_kind = "Group" if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else "Channel"
    text = (
        f"✅ <b>Bot joined {chat_kind} successfully!</b>\\n\\n"
        f"📌 Name: {html.escape(chat.title or 'Unknown')}\\n"
        f"🆔 Chat ID: <code>{chat.id}</code>\\n"
        f"🤖 Bot ID: <code>{context.bot.id}</code>\\n\\n"
        "🎉 Bot added successfully. প্রয়োজনীয় permission যাচাই করুন।"
    )
    try:
        await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)
    except TelegramError:
        # In channels, a confirmation post requires permission to post messages.
        logger.info("Could not post bot-joined confirmation in chat %s", chat.id)


async def cmd_help(update, context):
    text = (
        "✨ <b>PREMIUM WELCOME BOT — HELP</b>\n\n"
        "👋 <b>Welcome Controls (Admin)</b>\n"
        "/welcome_on — Welcome চালু\n"
        "/welcome_off — Welcome বন্ধ\n"
        "/welcome_status — Settings দেখুন\n\n"
        "⏳ <b>Auto Delete (Admin)</b>\n"
        "/set_delete 30 — Delay পরিবর্তন (5–600 sec)\n"
        "/delete_time — বর্তমান Delay\n\n"
        "🎨 <b>Message Design (Admin)</b>\n"
        "/set_welcome আপনার টেক্সট — Custom template\n"
        "/reset_welcome — Default design ফিরিয়ে আনুন\n"
        "/welcome_preview — Preview দেখুন\n\n"
        "🧰 <b>Utilities</b>\n"
        "/id — User ও Group ID\n"
        "/ping — Bot status\n"
        "/help — এই Help\n\n"
        "Placeholders: {first_name}, {full_name}, {username}, "
        "{user_id}, {mention}, {delete_after}\n"
        "Custom template-এ Telegram HTML ব্যবহার করা যাবে।"
    )
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_welcome_on(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    update_setting(update.effective_chat.id, "welcome_enabled", 1)
    await update.effective_message.reply_text("✅ Auto Welcome চালু করা হয়েছে।")


async def cmd_welcome_off(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    update_setting(update.effective_chat.id, "welcome_enabled", 0)
    await update.effective_message.reply_text("⏸️ Auto Welcome বন্ধ করা হয়েছে।")


async def cmd_welcome_status(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    s = get_settings(update.effective_chat.id)
    status = "ON ✅" if s["welcome_enabled"] else "OFF ⏸️"
    await update.effective_message.reply_text(
        f"⚙️ WELCOME SETTINGS\n\nWelcome: {status}\n"
        f"Auto Delete: {s['delete_after']} seconds\nStorage: SQLite"
    )


async def cmd_set_delete(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    if len(context.args) != 1:
        return await update.effective_message.reply_text(
            "ব্যবহার: /set_delete 30\nসীমা: 5 থেকে 600 সেকেন্ড।"
        )
    try:
        seconds = int(context.args[0])
    except ValueError:
        return await update.effective_message.reply_text("❌ পূর্ণসংখ্যা দিন। উদাহরণ: /set_delete 30")
    if not MIN_DELETE_SECONDS <= seconds <= MAX_DELETE_SECONDS:
        return await update.effective_message.reply_text(
            f"❌ সময় {MIN_DELETE_SECONDS}–{MAX_DELETE_SECONDS} সেকেন্ডের মধ্যে দিন।"
        )
    update_setting(update.effective_chat.id, "delete_after", seconds)
    await update.effective_message.reply_text(
        f"✅ নতুন Welcome Message {seconds} সেকেন্ড পর ডিলিট হবে।"
    )


async def cmd_delete_time(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    s = get_settings(update.effective_chat.id)
    await update.effective_message.reply_text(
        f"⏳ বর্তমান Auto Delete: {s['delete_after']} seconds"
    )


async def cmd_set_welcome(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return

    raw = update.effective_message.text or ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return await update.effective_message.reply_text(
            "ব্যবহার:\n/set_welcome ✨ <b>Welcome {mention}!</b>\n\n"
            "HTML ও placeholders ব্যবহার করুন।\n"
            "Preview: /welcome_preview | Reset: /reset_welcome"
        )

    template = parts[1].strip()
    if len(template) > 3500:
        return await update.effective_message.reply_text("❌ Template সর্বোচ্চ 3500 characters হতে পারবে।")

    error = validate_template(template)
    if error:
        return await update.effective_message.reply_text(
            f"❌ Template HTML ভুল: {error}\n"
            "HTML tags ঠিক করে আবার চেষ্টা করুন।"
        )

    update_setting(update.effective_chat.id, "welcome_template", template)
    await update.effective_message.reply_text(
        "✅ Custom Welcome Template সংরক্ষণ করা হয়েছে। /welcome_preview দিয়ে পরীক্ষা করুন।"
    )


async def cmd_reset_welcome(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    update_setting(update.effective_chat.id, "welcome_template", DEFAULT_TEMPLATE)
    await update.effective_message.reply_text("✅ Default Premium Welcome Design ফিরিয়ে আনা হয়েছে।")


async def cmd_welcome_preview(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("এই কমান্ডটি গ্রুপে ব্যবহার করুন।")
    settings = get_settings(update.effective_chat.id)
    user = update.effective_user
    text = format_welcome(settings["welcome_template"], user, settings)
    try:
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except TelegramError:
        await update.effective_message.reply_text("❌ Preview পাঠানো যায়নি। Template HTML পরীক্ষা করুন।")


async def cmd_id(update, context):
    user, chat = update.effective_user, update.effective_chat
    await update.effective_message.reply_text(
        f"👤 User ID: {user.id if user else 'N/A'}\n"
        f"💬 Chat ID: {chat.id if chat else 'N/A'}"
    )


async def cmd_ping(update, context):
    await update.effective_message.reply_text(
        "🏓 Pong!\n✅ Bot is running\n💾 Storage: SQLite"
    )



async def replied_target(update, context):
    msg = update.effective_message
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("⚠️ সদস্যের মেসেজে Reply দিয়ে কমান্ডটি পাঠাও।")
        return None
    target = msg.reply_to_message.from_user
    if target.id == context.bot.id:
        await msg.reply_text("নিজেকে টার্গেট করা যাবে না।")
        return None
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, target.id)
        if member.status in ("administrator", "creator"):
            await msg.reply_text("🛡️ অন্য Admin-কে এই কমান্ড দিয়ে পরিবর্তন করা যাবে না।")
            return None
    except TelegramError:
        pass
    return target


async def cmd_ban(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    target = await replied_target(update, context)
    if not target:
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.effective_message.reply_text(f"⛔ {html.escape(target.full_name)}-কে ban করা হয়েছে।")
    except TelegramError:
        logger.exception("Ban failed")
        await update.effective_message.reply_text("❌ Ban করা যায়নি। Bot-এর Ban Users permission পরীক্ষা করুন।")


async def cmd_kick(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    target = await replied_target(update, context)
    if not target:
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        await update.effective_message.reply_text(f"👢 {html.escape(target.full_name)}-কে remove করা হয়েছে।")
    except TelegramError:
        logger.exception("Kick failed")
        await update.effective_message.reply_text("❌ Remove করা যায়নি। Bot-এর Ban Users permission পরীক্ষা করুন।")


async def cmd_unban(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.effective_message.reply_text("ব্যবহার: /unban USER_ID")
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, int(context.args[0]), only_if_banned=True)
        await update.effective_message.reply_text("✅ Unban request সম্পন্ন হয়েছে।")
    except TelegramError:
        logger.exception("Unban failed")
        await update.effective_message.reply_text("❌ Unban করা যায়নি।")


async def cmd_mute(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    target = await replied_target(update, context)
    if not target:
        return
    try:
        minutes = int(context.args[0]) if context.args else 10
    except ValueError:
        return await update.effective_message.reply_text("ব্যবহার: /mute 10")
    if not 1 <= minutes <= 10080:
        return await update.effective_message.reply_text("সময় 1–10080 মিনিটের মধ্যে দিন।")
    from datetime import datetime, timedelta, timezone
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        )
        await update.effective_message.reply_text(f"🔇 {html.escape(target.full_name)}-কে {minutes} মিনিট mute করা হয়েছে।")
    except TelegramError:
        logger.exception("Mute failed")
        await update.effective_message.reply_text("❌ Mute করা যায়নি। Bot-এর Restrict Members permission পরীক্ষা করুন।")


async def cmd_unmute(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    if not await require_admin(update, context):
        return
    target = await replied_target(update, context)
    if not target:
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True,
                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        await update.effective_message.reply_text(f"🔊 {html.escape(target.full_name)}-এর mute তুলে দেওয়া হয়েছে।")
    except TelegramError:
        logger.exception("Unmute failed")
        await update.effective_message.reply_text("❌ Unmute করা যায়নি।")


async def cmd_admins(update, context):
    if not group_only(update):
        return await update.effective_message.reply_text("গ্রুপে ব্যবহার করুন।")
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        lines = ["🛡️ <b>GROUP ADMINS</b>"]
        lines.extend(f"• {html.escape(a.user.full_name)} — <code>{a.user.id}</code>" for a in admins)
        await update.effective_message.reply_text("\\n".join(lines), parse_mode=ParseMode.HTML)
    except TelegramError:
        logger.exception("Could not list admins")
        await update.effective_message.reply_text("❌ Admin list পাওয়া যায়নি।")

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Set it first: export BOT_TOKEN='your_token'"
        )

    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Delete Telegram-generated "X joined the group" service messages.
    # Welcome messages are sent independently from chat_member updates.
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS
            | filters.StatusUpdate.LEFT_CHAT_MEMBER,
            delete_join_service_message,
        ),
        group=0,
    )
    app.add_handler(
        ChatMemberHandler(welcome_member_update, ChatMemberHandler.CHAT_MEMBER),
        group=0,
    )
    app.add_handler(
        ChatMemberHandler(bot_membership_update, ChatMemberHandler.MY_CHAT_MEMBER),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(start_menu_callback, pattern="^(show_ids|show_help)$"))
    commands = {
        "start": cmd_start,
        "help": cmd_help,
        "welcome_on": cmd_welcome_on,
        "welcome_off": cmd_welcome_off,
        "welcome_status": cmd_welcome_status,
        "set_delete": cmd_set_delete,
        "delete_time": cmd_delete_time,
        "set_welcome": cmd_set_welcome,
        "reset_welcome": cmd_reset_welcome,
        "welcome_preview": cmd_welcome_preview,
        "id": cmd_id,
        "ping": cmd_ping,
        "ban": cmd_ban,
        "kick": cmd_kick,
        "unban": cmd_unban,
        "mute": cmd_mute,
        "unmute": cmd_unmute,
        "admins": cmd_admins,
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))

    logger.info("Premium Telegram Welcome Bot starting...")
    app.run_polling(allowed_updates=["message", "chat_member", "my_chat_member", "callback_query"])


if __name__ == "__main__":
    main()
