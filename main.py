import os
import json
import sqlite3
import logging
from datetime import datetime
from flask import Flask, request
import requests
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Config + logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LEAKOSINT_API_TOKEN = os.getenv("LEAKOSINT_API_TOKEN")
LEAKOSINT_API_URL = os.getenv("LEAKOSINT_API_URL", "https://leakosintapi.com/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
PORT = int(os.getenv("PORT", 10000))
ADMIN_IDS = set(int(x.strip()) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip())
UPI_ID = os.getenv("UPI_ID", "your-upi@bank")  # show this in deposit instructions
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # set by Render automatically

DB_PATH = os.getenv("DB_PATH", "bot.db")
COIN_COST_PER_SEARCH = int(os.getenv("COIN_COST_PER_SEARCH", "1"))
NEW_USER_COINS = int(os.getenv("NEW_USER_COINS", "1"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "1"))

# --- Flask app for webhook ---
flask_app = Flask(__name__)

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        referral_code TEXT UNIQUE,
        referred_by INTEGER,
        coins INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER,
        kind TEXT, -- search, deposit, reward, admin_adjust
        amount INTEGER,
        note TEXT,
        created_at TEXT
    );
    """)
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def generate_referral_code(tg_id):
    return f"r{tg_id}"

def get_user_by_tg(tg_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id,username,referral_code,referred_by,coins,created_at FROM users WHERE telegram_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_refcode(code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id,username,referral_code,referred_by,coins,created_at FROM users WHERE referral_code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_identifier(identifier):  # accept numeric telegram_id or @username or username
    conn = get_conn()
    cur = conn.cursor()
    if identifier.isdigit():
        cur.execute("SELECT telegram_id,username,referral_code,referred_by,coins,created_at FROM users WHERE telegram_id=?", (int(identifier),))
    else:
        uname = identifier.lstrip("@")
        cur.execute("SELECT telegram_id,username,referral_code,referred_by,coins,created_at FROM users WHERE username=?", (uname,))
    row = cur.fetchone()
    conn.close()
    return row

def ensure_user(tg_user):
    # tg_user: telegram.User object
    tg_id = tg_user.id
    username = tg_user.username or f"user{tg_id}"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM users WHERE telegram_id=?", (tg_id,))
    if cur.fetchone():
        conn.close()
        return False  # existed
    refcode = generate_referral_code(tg_id)
    created_at = datetime.utcnow().isoformat()
    cur.execute("INSERT INTO users (telegram_id, username, referral_code, coins, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, username, refcode, NEW_USER_COINS, created_at))
    cur.execute("INSERT INTO transactions (telegram_id, kind, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, 'reward', NEW_USER_COINS, 'new_user_bonus', created_at))
    conn.commit()
    conn.close()
    return True  # new user created

def award_coins(tg_id, amount, kind="admin_adjust", note=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = coins + ? WHERE telegram_id=?", (amount, tg_id))
    cur.execute("INSERT INTO transactions (telegram_id, kind, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, kind, amount, note, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def set_coins(tg_id, amount):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = ? WHERE telegram_id=?", (amount, tg_id))
    cur.execute("INSERT INTO transactions (telegram_id, kind, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, 'admin_set', amount, 'admin_set_coins', datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def deduct_coins(tg_id, amount):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT coins FROM users WHERE telegram_id=?", (tg_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "user_not_found"
    coins = row[0]
    if coins < amount:
        conn.close()
        return False, "insufficient"
    cur.execute("UPDATE users SET coins = coins - ? WHERE telegram_id=?", (amount, tg_id))
    cur.execute("INSERT INTO transactions (telegram_id, kind, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, 'search', -amount, 'osint_search', datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return True, None

def get_balance(tg_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT coins FROM users WHERE telegram_id=?", (tg_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def list_users(limit=100):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id,username,referral_code,referred_by,coins,created_at FROM users ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

# --- LeakOSINT query helper ---
def query_leakosint(query: str):
    payload = {
        "token": LEAKOSINT_API_TOKEN,
        "request": query,
        "limit": 100,
        "lang": "en",
        "type": "json"
    }
    try:
        r = requests.post(LEAKOSINT_API_URL, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("LeakOSINT query failed")
        return {"error": str(e)}

# --- Bot handlers ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    args = context.args or []
    new = ensure_user(tg_user)
    # Handle referral if provided
    if args:
        ref = args[0].strip()
        refrow = get_user_by_refcode(ref)
        if refrow:
            ref_tg_id = refrow[0]
            # don't reward if referrer is same as referred
            if ref_tg_id != tg_user.id:
                # mark referred_by if not set and reward both
                conn = get_conn()
                cur = conn.cursor()
                cur.execute("SELECT referred_by FROM users WHERE telegram_id=?", (tg_user.id,))
                r = cur.fetchone()
                if r and not r[0]:
                    cur.execute("UPDATE users SET referred_by=? WHERE telegram_id=?", (ref_tg_id, tg_user.id))
                    # reward both
                    award_coins(ref_tg_id, REFERRAL_REWARD, kind="referral", note=f"referred {tg_user.id}")
                    award_coins(tg_user.id, REFERRAL_REWARD, kind="referral", note=f"referred_by {ref_tg_id}")
                conn.commit()
                conn.close()
    txt = "🔎 LeakOSINT Scanner Bot\nSend /search <query> to scan. Each search costs 1 coin.\n"
    txt += f"You got {NEW_USER_COINS} coin(s) for joining.\nUse /referral to get your referral link."
    await update.message.reply_text(txt)

async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    row = get_user_by_tg(tg.id)
    if not row:
        await update.message.reply_text("User not found. Send /start first.")
        return
    refcode = row[2]
    # Build a start link that includes refcode
    bot_username = (await context.bot.get_me()).username
    start_link = f"https://t.me/{bot_username}?start={refcode}"
    await update.message.reply_text(f"Your referral code: `{refcode}`\nInvite link: {start_link}\nBoth you and the new user get {REFERRAL_REWARD} coin(s) when they use this link.", parse_mode=constants.ParseMode.MARKDOWN)

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    bal = get_balance(tg.id)
    await update.message.reply_text(f"Your balance: {bal} coin(s).")

async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    args = context.args or []
    amount = args[0] if args else "1"
    # Give instructions to transfer and include username for tracking
    username = tg.username or f"user{tg.id}"
    text = (
        f"To deposit {amount} coin(s):\n\n"
        f"1) Send the transfer to UPI ID: `{UPI_ID}`\n"
        f"2) In the UPI transaction note/message include your Telegram username: `{username}`\n"
        f"3) After you make the transfer, notify an admin or wait for manual approval.\n\n"
        "Admin will credit coins after verifying transaction.\n"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    query = " ".join(context.args or [])
    if not query:
        await update.message.reply_text("Usage: /search <query>")
        return
    # check admin exemption
    if tg.id not in ADMIN_IDS:
        ok, reason = deduct_coins(tg.id, COIN_COST_PER_SEARCH)
        if not ok:
            await update.message.reply_text("Insufficient coins. Use /deposit to top up.")
            return
    await update.message.reply_text(f"Scanning for: `{query}` …", parse_mode=constants.ParseMode.MARKDOWN)
    result = query_leakosint(query)
    out = json.dumps(result, indent=2)
    if len(out) > 3800:
        out = out[:3800] + "\n\n[truncated]"
    await update.message.reply_text(f"```\n{out}\n```", parse_mode=constants.ParseMode.MARKDOWN)

async def search_number_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Just a wrapper that tags the query as number search (no API difference)
    q = " ".join(context.args or [])
    if not q:
        await update.message.reply_text("Usage: /search_number <number>")
        return
    await search_cmd(update, context)

# --- Admin commands ---
def is_admin(tg_id):
    return tg_id in ADMIN_IDS

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Unauthorized.")
    rows = list_users(limit=200)
    lines = [f"{r[0]} | @{r[1]} | coins={r[4]} | ref={r[2]}" for r in rows]
    text = "Users:\n" + "\n".join(lines[:1000])
    if len(text) > 3800:
        text = text[:3800] + "\n\n[truncated]"
    await update.message.reply_text(text)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Unauthorized.")
    msg = " ".join(context.args or [])
    if not msg:
        return await update.message.reply_text("Usage: /broadcast <message>")
    rows = list_users(limit=10000)
    sent = 0
    for r in rows:
        tg_id = r[0]
        try:
            await context.bot.send_message(chat_id=tg_id, text=msg)
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(f"Broadcast sent to {sent} users.")

async def addcoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Unauthorized.")
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /addcoin <tg_id_or_username> <amount>")
    ident = args[0]
    amount = int(args[1])
    row = get_user_by_identifier(ident)
    if not row:
        return await update.message.reply_text("User not found.")
    tg_id = row[0]
    award_coins(tg_id, amount, kind="admin_adjust", note=f"added_by {update.effective_user.id}")
    await update.message.reply_text(f"Added {amount} coins to {tg_id}.")

async def setcoins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Unauthorized.")
    args = context.args or []
    if len(args) < 2:
        return await update.message.reply_text("Usage: /setcoins <tg_id_or_username> <amount>")
    ident = args[0]
    amount = int(args[1])
    row = get_user_by_identifier(ident)
    if not row:
        return await update.message.reply_text("User not found.")
    tg_id = row[0]
    set_coins(tg_id, amount)
    await update.message.reply_text(f"Set {tg_id} coins to {amount}.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Unauthorized.")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]
    cur.execute("SELECT SUM(coins) FROM users")
    total_coins = cur.fetchone()[0] or 0
    conn.close()
    await update.message.reply_text(f"Users: {users}\nTotal coins outstanding: {total_coins}")

# --- Flask webhook endpoint ---
application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("referral", referral_cmd))
application.add_handler(CommandHandler("balance", balance_cmd))
application.add_handler(CommandHandler("deposit", deposit_cmd))
application.add_handler(CommandHandler("search", search_cmd))
application.add_handler(CommandHandler("search_number", search_number_cmd))

# admin
application.add_handler(CommandHandler("users", users_cmd))
application.add_handler(CommandHandler("broadcast", broadcast_cmd))
application.add_handler(CommandHandler("addcoin", addcoin_cmd))
application.add_handler(CommandHandler("setcoins", setcoins_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))

@flask_app.route(WEBHOOK_PATH, methods=["POST"])
def webhook_handler():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "ok"

# --- start / set webhook on launch ---
def set_telegram_webhook():
    # Build webhook URL from Render-provided env var or user-provided RENDER_EXTERNAL_URL
    base = RENDER_EXTERNAL_URL or os.getenv("BASE_URL")
    if not base:
        logger.warning("RENDER_EXTERNAL_URL or BASE_URL not set; webhook will not be configured automatically.")
        return
    webhook_url = f"{base.rstrip('/')}{WEBHOOK_PATH}"
    logger.info(f"Setting webhook to {webhook_url}")
    application.bot.set_webhook(url=webhook_url)

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not LEAKOSINT_API_TOKEN:
        logger.error("Missing TELEGRAM_BOT_TOKEN or LEAKOSINT_API_TOKEN env vars.")
        raise SystemExit(1)
    init_db()
    set_telegram_webhook()
    # Run Flask app (PTB will process updates from update_queue)
    flask_app.run(host="0.0.0.0", port=PORT)
