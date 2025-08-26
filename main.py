import json
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- Configuration ---
API_TOKEN = "8282816055:AAHib5kGD1cy7fEzKjLZUMkHEh5WBBX8kA0"   # Replace with your bot token
LEAKOSINT_API_TOKEN = "7900116525:hn41NJ2G"  # Replace with your API token
LEAKOSINT_API_URL = "https://leakosintapi.com/"

# --- API Function ---
def query_leakosint(query: str):
    payload = {
        "token": LEAKOSINT_API_TOKEN,
        "request": query,
        "limit": 100,
        "lang": "en",
        "type": "json"
    }
    try:
        response = requests.post(LEAKOSINT_API_URL, json=payload, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"error": str(e)}

# --- Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to LeakOSINT Scanner Bot!\nSend me a query to search leaks.")

async def handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text.strip()
    await update.message.reply_text(f"Scanning for: {user_query} ...")
    
    result = query_leakosint(user_query)
    formatted = json.dumps(result, indent=2)
    
    # Telegram messages have a size limit, so truncate if too large
    if len(formatted) > 4000:
        formatted = formatted[:4000] + "\n\n[Output truncated]"
    
    await update.message.reply_text(f"Results:\n{formatted}")

# --- Main ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(API_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_query))

    print("Bot running...")
    app.run_polling()
