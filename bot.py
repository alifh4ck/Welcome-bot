import os
import re
import time
import html
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

from telegram import Update
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN =8671594553:AAFWS0yaL25U9zQClTW_wEasiqasYJ0-nIU
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
        schedule_delete(
            application, item["chat_id"], item["message_id"], item["delete_at"]
        )
    logger.info("Restored pending deletion jobs")


async def welcome_new_members(update, context):
    message, chat = update.effective_message, update.effective_chat
    if not message or not chat or not group_only(update):
        return

    settings = get_settings(chat.id)
    if not settings["welcome_enabled"]:
        return

    for user in message.new_chat_members or []:
        if IGNORE_BOTS and user.is_bot:
            continue
        try:
            text = format_welcome(settings["welcome_template"], user, settings)
            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            delete_at = time.time() + settings["delete_after"]
            save_pending_delete(chat.id, sent.message_id, delete_at)
            schedule_delete(context.application, chat.id, sent.message_id, delete_at)
            logger.info("Welcome sent to user %s in chat %s", user.id, chat.id)
        except TelegramError:
            logger.exception("Could not send welcome message")


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

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    commands = {
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
    }
    for name, callback in commands.items():
        app.add_handler(CommandHandler(name, callback))

    logger.info("Premium Telegram Welcome Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
