import os
import re
import json
import gspread
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Настройки окружения и ключи ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DOCTORS_GROUP_ID = -1002529967465  # ваш чат для врачей

# --- OpenAI ---
openai = OpenAI(api_key=OPENAI_API_KEY)

# --- Google Sheets ---
with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url(
    "https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit"
).sheet1

# --- Загрузка услуг и слотов ---
with open("services.json", "r", encoding="utf-8") as f:
    SERVICES_DICT = json.load(f)
SERVICES = list(SERVICES_DICT.values())

CANCEL_KEYWORDS = ["отменить запись", "поменять время"]
BOOKING_KEYWORDS = [
    "запис", "на приём", "appointment", "запишите", "хочу записаться"
]
CONSULT_WORDS = ["стоимость", "цена", "прайс", "какие есть"]

# Вспомогательные функции
def is_cancel_intent(text):
    q = text.lower()
    return any(kw in q for kw in CANCEL_KEYWORDS)

def is_booking_intent(text):
    q = text.lower()
    return any(kw in q for kw in BOOKING_KEYWORDS)

def is_consult_intent(text):
    q = text.lower()
    return any(w in q for w in CONSULT_WORDS)

def match_service(text):
    q = text.lower()
    m = re.match(r"^\s*(\d+)\s*$", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]["название"]
    for key, s in SERVICES_DICT.items():
        title = s["название"].lower()
        if title in q:
            return s["название"]
        for kw in s.get("ключи", []):
            if kw.lower() in q:
                return s["название"]
    return None

def build_services_list():
    lines = ["📋 *Список услуг:*"]
    for i, s in enumerate(SERVICES, 1):
        lines.append(f"{i}. *{s['название']}* — {s['цена']}")
    return "\n".join(lines)

def extract_fields(text):
    data = {}
    m = re.search(r"(?:зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)", text)
    if m:
        data["Имя"] = m.group(1)
    m = re.search(r"(\+?\d{7,15})", text)
    if m:
        data["Телефон"] = m.group(1)
    svc = match_service(text)
    if svc:
        data["Услуга"] = svc
    dm = re.search(r"(завтра|послезавтра|\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)", text)
    if dm:
        d = dm.group(1)
        if "завтра" in d:
            data["Дата"] = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        elif "послезавтра" in d:
            data["Дата"] = (datetime.now() + timedelta(days=2)).strftime("%d.%m.%Y")
        else:
            data["Дата"] = d
    tm = re.search(r"\b(\d{1,2}[:.]\d{2})\b", text)
    if tm:
        data["Время"] = tm.group(1).replace(".", ":")
    return data

def is_form_complete(form):
    return all(form.get(k) for k in ("Имя", "Телефон", "Услуга", "Дата", "Время"))

# --- Проверка занятых слотов ---
def get_taken_slots(услуга, дата):
    """
    Вернуть список занятых времен для данной услуги и даты.
    """
    records = sheet.get_all_records()
    taken = []
    for rec in records:
        if rec.get("Услуга", "").strip().lower() == услуга.strip().lower() and \
           rec.get("Дата", "").strip() == дата.strip():
            taken.append(rec.get("Время", "").strip())
    return taken

# --- Работа с записями в таблице ---
def find_last_booking(chat_id):
    records = sheet.get_all_records()
    last = None
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("ChatID", "")) == str(chat_id):
            last = (idx, rec)
    return last if last else (None, None)

async def register_and_notify(form, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [
        chat_id,
        form["Имя"],
        form["Телефон"],
        form["Услуга"],
        form["Дата"],
        form["Время"],
        now_ts
    ]
    sheet.append_row(row)
    msg = (
        f"🦷 *Новая запись!*\n"
        f"Имя: {form['Имя']}\n"
        f"Телефон: {form['Телефон']}\n"
        f"Услуга: {form['Услуга']}\n"
        f"Дата: {form['Дата']}\n"
        f"Время: {form['Время']}"
    )
    await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
    await update.message.reply_text("✅ Запись подтверждена!")

# --- Обработка отмены / изменения ---
async def handle_cancel_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.effective_chat.id
    row_idx, rec = find_last_booking(chat_id)
    if not rec:
        await update.message.reply_text("❗ У вас нет активных записей.")
        return
    # отмена
    if "отменить запись" in text:
        sheet.delete_row(row_idx)
        msg = (
            f"❌ Пациент отменил запись:\n"
            f"{rec['Имя']}, {rec['Услуга']} на {rec['Дата']} {rec['Время']}"
        )
        await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Ваша запись отменена.")
        return
    # поменять время
    svc = rec["Услуга"]
    date = rec["Дата"]
    # Слоты услуги теперь берём из SERVICES_DICT
    slots = SERVICES_DICT.get(svc.lower(), {}).get("слоты")
    if not slots:
        # Если не нашлось по ключу, попробуем по названию (на случай несоответствия регистров)
        for key, s in SERVICES_DICT.items():
            if s["название"].strip().lower() == svc.strip().lower():
                slots = s.get("слоты", [])
                break
    if not slots:
        await update.message.reply_text("К сожалению, для этой услуги нет информации о слотах.")
        return
    # Получить занятые слоты
    taken_slots = get_taken_slots(svc, date)
    free_slots = [t for t in slots if t not in taken_slots or t == rec.get("Время")]
    if not free_slots:
        await update.message.reply_text("К сожалению, все слоты на этот день уже заняты.")
        return
    # список только свободных слотов
    text_slots = ["Выберите новый слот:"]
    for i, t in enumerate(free_slots, 1):
        text_slots.append(f"{i}. {t}")
    await update.message.reply_text("\n".join(text_slots))
    # сохранить состояние ожидания выбора слота
    context.user_data["awaiting_slot"] = {"row": row_idx, "slots": free_slots, "record": rec}

# --- Обработка выбора слота ---
async def handle_slot_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("awaiting_slot")
    if not state:
        return False
    text = update.message.text.strip()
    if not re.fullmatch(r"\d+", text):
        return False
    idx = int(text) - 1
    slots = state["slots"]
    if idx < 0 or idx >= len(slots):
        return False
    new_time = slots[idx]
    row_idx = state["row"]
    sheet.update_cell(row_idx, 6, new_time)
    rec = state["record"]
    await update.message.reply_text(f"✅ Время изменено на {new_time}.")
    msg = (
        f"✏️ Пациент поменял время:\n"
        f"{rec['Имя']}, услуга {rec['Услуга']}\n"
        f"Новая дата/время: {rec['Дата']} {new_time}"
    )
    await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
    del context.user_data["awaiting_slot"]
    return True

# --- Ежедневные напоминания ---
async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%d.%m.%Y")
    records = sheet.get_all_records()
    for rec in records:
        if rec.get("Дата") == today:
            chat_id = rec.get("ChatID")
            svc = rec.get("Услуга")
            time_ = rec.get("Время")
            await context.bot.send_message(
                chat_id,
                f"🔔 Напоминание: у вас сегодня запись на *{svc}* в *{time_}*.",
                parse_mode="Markdown"
            )

# --- Главное: хендлер и запуск ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if is_cancel_intent(text):
        return await handle_cancel_or_edit(update, context)
    if context.user_data.get("awaiting_slot"):
        handled = await handle_slot_selection(update, context)
        if handled:
            return
    if is_consult_intent(text):
        return await update.message.reply_text(
            build_services_list(), parse_mode="Markdown"
        )
    form = context.user_data.get("form", {})
    extracted = extract_fields(text)
    form.update(extracted)
    context.user_data["form"] = form
    if is_form_complete(form):
        await register_and_notify(form, update, context)
        context.user_data["form"] = {}
        return
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": text})
    context.user_data["history"] = history[-20:]
    ai_system = (
        "Ты — вежливый бот стоматологии. Помогаешь собрать Имя, Телефон, Услугу, Дату, Время.\n"
        "Если чего-то нет — спрашивай. Для списка услуг используй только этот список:\n"
        + build_services_list()
    )
    msgs = [{"role": "system", "content": ai_system}] + history[-10:]
    try:
        resp = openai.chat.completions.create(model="gpt-4o", messages=msgs)
        reply = resp.choices[0].message.content
    except Exception:
        return await update.message.reply_text("Ошибка AI 🤖")
    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-20:]
    form = context.user_data.get("form", {})
    if is_form_complete(form):
        await register_and_notify(form, update, context)
        context.user_data["form"] = {}

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    # 📌 Добавляем напоминания после запуска event loop
    async def start_scheduler(_: ContextTypes.DEFAULT_TYPE):
        scheduler.add_job(send_reminders, "cron", hour=9, minute=0, args=[app.bot])
        scheduler.start()

    app.post_init = start_scheduler

    RENDER_URL = os.getenv("RENDER_URL", "").strip()
    webhook = f"https://{RENDER_URL}/webhook"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
