import os
import re
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI
from datetime import datetime

load_dotenv()

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
AIRTABLE_TOKEN      = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID    = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")  # должен быть tblKVUJdq68uq0cpN
RENDER_URL          = os.getenv("RENDER_EXTERNAL_URL")
PORT                = int(os.getenv("PORT", 10000))

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_URL]):
    raise RuntimeError("Нужно задать все ENV: TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, "
                       "AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_EXTERNAL_URL")

openai = OpenAI(api_key=OPENAI_API_KEY)

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}

def extract_fields(text: str):
    dt = re.search(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", text)
    tm = re.search(r"(\d{1,2}:\d{2})", text)
    svc = re.search(
        r"(?:записаться на|хочу на|на)\s+(.+?)(?=\s+(?:в|завтра|послезавтра|сегодня)|$)",
        text, re.IGNORECASE,
    )
    nm = re.search(r"(?:меня зовут\s*|я\s*)([А-ЯЁ][а-яё]+)", text)
    return (
        nm.group(1).strip() if nm else None,
        svc.group(1).strip() if svc else None,
        dt.group(1).strip() if dt else None,
        tm.group(1).strip() if tm else None,
    )

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_txt = update.message.text
    history = ctx.user_data.get("history", [])
    history.append({"role": "user", "content": user_txt})
    ctx.user_data["history"] = history[-10:]

    prompt = [
        {
            "role": "system",
            "content": (
                "Ты — виртуальная администратор стоматологической клиники. "
                "Вежливо веди диалог, извлекай из пользовательских фраз "
                "имя, услугу, дату и время. Подтверждай запись."
            ),
        }
    ] + history

    resp = openai.chat.completions.create(
        model="gpt-4o",
        messages=prompt,
    )
    reply = resp.choices[0].message.content
    history.append({"role": "assistant", "content": reply})
    ctx.user_data["history"] = history[-30:]

    await update.message.reply_text(reply)

    name, service, date_str, time_str = extract_fields(user_txt + " " + reply)
    print(f"🔍 Извлечено: name={name!r}, service={service!r}, date={date_str!r}, time={time_str!r}")

    if all([name, service, date_str, time_str]):
        dt_full = f"{date_str} {time_str}"
        payload = {
            "fields": {
                "Имя": name,
                "Фамилия": update.effective_user.last_name or "",
                "Username": update.effective_user.username or "",
                "Chat ID": str(update.effective_user.id),
                "Услуга": service,
                "Дата и время записи": dt_full,
                "Сообщение": user_txt,
                "Дата заявки": datetime.now().isoformat(),
            }
        }
        r = requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload)
        try:
            j = r.json()
        except:
            j = r.text
        print(f"📤 Airtable response: {r.status_code} {j}")

        if r.status_code in (200, 201):
            await update.message.reply_text(f"✅ Вы успешно записаны: {name}, «{service}» — {dt_full}.")
        else:
            await update.message.reply_text("⚠️ Ошибка при записи в таблицу. Проверьте логи.")
    else:
        print("⚠️ Недостаточно данных для записи — ждём уточнений.")

def main():
    print("🚀 Старт бота через Webhook…")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    ext = RENDER_URL if RENDER_URL.startswith("http") else "https://" + RENDER_URL
    webhook = f"{ext}/webhook"
    print("🔗 Устанавливаем webhook:", webhook)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
