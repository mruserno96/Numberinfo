import os
import json
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LEAKOSINT_API_TOKEN = os.getenv("LEAKOSINT_API_TOKEN")
LEAKOSINT_API_URL = os.getenv("LEAKOSINT_API_URL", "https://leakosintapi.com/")

def query_leakosint(query: str):
    payload = {
        "token": LEAKOSINT_API_TOKEN,
        "request": query,
        "limit": 100,
        "lang": "en",
        "type": "json"
    }
    try:
        r = requests.post(LEAKOSINT_API_URL, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 LeakOSINT Scanner\nSend a query to search leaks.")

async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        return await update.message.reply_text("Send a non-empty query.")
    await update.message.reply_text(f"Scanning for: `{q}` …", parse_mode="Markdown")
    result = query_leakosint(q)
    text = json.dumps(result, indent=2)
    if len(text) > 3900:
        text = text[:3900] + "\n\n[truncated]"
    await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")

def main():
    if not TELEGRAM_BOT_TOKEN or not LEAKOSINT_API_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or LEAKOSINT_API_TOKEN env vars.")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
