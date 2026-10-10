# Premium Telegram Auto Welcome + Auto Delete Bot

বাংলা + English Welcome Message, SQLite settings, admin commands এবং auto-delete timer।

## Features
- নতুন সদস্য join করলে welcome message
- Default auto-delete: 30 seconds
- Per-group SQLite settings
- Pending deletion job restart recovery
- Admin-only configuration commands
- Custom HTML welcome template
- Bot account filter
- Basic logging and error handling

## Requirements
- Python 3.10+
- Telegram bot token from @BotFather
- Bot added to the group
- Bot must be able to send messages and delete its own welcome messages.
  Making it an administrator with Delete Messages permission is recommended.

## Install (Ubuntu VPS or Termux)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure and run
Set the token as an environment variable (do not publish your token):
```bash
export BOT_TOKEN="YOUR_BOTFATHER_TOKEN"
python bot.py
```

Database is created at `data/welcome.sqlite3`.

## Commands
- `/help`
- `/welcome_on` (group admin)
- `/welcome_off` (group admin)
- `/welcome_status`
- `/set_delete 30` (group admin; allowed 5–600 seconds)
- `/delete_time`
- `/set_welcome <template>` (group admin)
- `/reset_welcome` (group admin)
- `/welcome_preview`
- `/id`
- `/ping`

## Custom template placeholders
`{first_name}`, `{full_name}`, `{username}`, `{user_id}`, `{mention}`, `{delete_after}`

Example:
```text
/set_welcome ✨ <b>Welcome {mention}!</b>

❤️ আমাদের গ্রুপে তোমাকে স্বাগতম!
🆔 ID: <code>{user_id}</code>
💖 Enjoy your stay!
```

Telegram HTML formatting is supported. Keep tags balanced and use only Telegram-supported tags.

## Important notes
- Each group's settings are stored separately.
- Pending deletions are stored in SQLite and re-scheduled after restart.
- If the server is offline at the scheduled time, a message may only be deleted when the bot starts again.
- The bot can only delete messages it has permission to delete; Telegram's deletion limits still apply.
- Keep the database file private and back it up before changing deployments.


## Token setup using .env
1. Open `.env`.
2. Replace `PASTE_NEW_BOTFATHER_TOKEN_HERE` with a **new token** from @BotFather.
3. Keep `.env` private; do not upload it publicly.
4. Run `python bot.py`. The app loads the token from `.env` automatically.
