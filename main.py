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
DOCTORS_GROUP_ID = -1002529967465

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

# --- Услуги и слоты ---
with open("services.json", "r", encoding="utf-8") as f:
    SERVICES_DICT = json.load(f)
SERVICES = list(SERVICES_DICT.values())

CANCEL_KEYWORDS = ["отменить", "отмена", "удалить", "поменять время"]
BOOKING_KEYWORDS = [
    "запис", "приём", "appointment", "запишите", "хочу записаться", "можно записаться"
]
CONSULT_WORDS = ["стоимость", "цена", "прайс", "услуги", "какие есть", "сколько стоит"]

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
    # По номеру из списка
    m = re.match(r"\b(\d{1,2})\b", q)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]["название"]
    # По ключевым словам и синонимам
    for key, s in SERVICES_DICT.items():
        if s["название"].lower() in q:
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
    # --- Имя: не из "Меня интересует", "Меня беспокоит" и т.п. ---
    m = re.search(r"(?:меня зовут|имя)\s*[:,\-]?[\s]*([А-ЯЁA-Z][а-яёa-zA-Z]+)", text, re.I)
    if m:
        data["Имя"] = m.group(1).capitalize()
    else:
        m = re.match(r"^\s*я\s+([А-ЯЁA-Z][а-яёa-zA-Z]+)\b", text, re.I)
        if m and not re.search(r"(интересует|беспокоит|устраивает)", text, re.I):
            data["Имя"] = m.group(1).capitalize()
        elif re.match(r"^\s*([А-ЯЁA-Z][а-яёa-zA-Z]+)\s*$", text, re.I):
            m2 = re.match(r"^\s*([А-ЯЁA-Z][а-яёa-zA-Z]+)\s*$", text, re.I)
            data["Имя"] = m2.group(1).capitalize()

    # --- Телефон ---
    m = re.search(r"(\+7\d{10}|8\d{10}|7\d{10}|\d{10,11})", text.replace(" ", ""))
    if m:
        phone = m.group(1)
        if phone.startswith("8"):
            phone = "+7" + phone[1:]
        elif phone.startswith("7") and len(phone) == 11:
            phone = "+7" + phone[1:]
        data["Телефон"] = phone

    # --- Услуга ---
    svc = match_service(text)
    if svc:
        data["Услуга"] = svc

    # --- Дата ---
    date_keywords = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    m = re.search(r"(сегодня|завтра|послезавтра|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)", text.lower())
    if m:
        d = m.group(1)
        now = datetime.now()
        if d in date_keywords:
            data["Дата"] = (now + timedelta(days=date_keywords[d])).strftime("%d.%m.%Y")
        else:
            for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m", "%d/%m", "%d-%m"):
                try:
                    date_obj = datetime.strptime(d, fmt)
                    if date_obj.year < 2000:
                        date_obj = date_obj.replace(year=now.year)
                    data["Дата"] = date_obj.strftime("%d.%m.%Y")
                    break
                except:
                    continue
            else:
                data["Дата"] = d

    # --- Время ---
    m = re.search(r"(\d{1,2})[:.\-](\d{2})", text)
    if m:
        h, m_ = int(m.group(1)), m.group(2)
        if 0 <= h <= 23 and 0 <= int(m_) <= 59:
            data["Время"] = f"{h:02d}:{m_}"

    return data

def is_form_complete(form):
    return all(form.get(k) for k in ("Имя", "Телефон", "Услуга", "Дата", "Время"))

def get_service_slots(service_name):
    for s in SERVICES_DICT.values():
        if s["название"].strip().lower() == service_name.strip().lower():
            return s.get("слоты", [])
    return []

def get_taken_slots(услуга, дата):
    records = sheet.get_all_records()
    taken = []
    for rec in records:
        usluga_cell = str(rec.get("Услуга", "")).strip().lower()
        date_cell = str(rec.get("Дата", "")).strip()
        if usluga_cell == услуга.strip().lower() and date_cell == дата.strip():
            time_cell = str(rec.get("Время", "")).strip()
            taken.append(time_cell)
    return taken

def find_last_booking(chat_id):
    records = sheet.get_all_records()
    last = None
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("Chat ID", "")) == str(chat_id):
            last = (idx, rec)
    return last if last else (None, None)

async def register_and_notify(form, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [
        form["Имя"],
        form["Телефон"],
        form["Услуга"],
        form["Дата"],
        form["Время"],
        now_ts,
        chat_id
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
    await update.message.reply_text("✅ Запись подтверждена! Спасибо, ждём вас!")

async def handle_cancel_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.effective_chat.id
    row_idx, rec = find_last_booking(chat_id)
    if not rec:
        await update.message.reply_text("❗ У вас нет активных записей.")
        return
    if "отменить" in text or "удалить" in text:
        sheet.delete_row(row_idx)
        msg = (
            f"❌ Пациент отменил запись:\n"
            f"{rec['Имя']}, {rec['Услуга']} на {rec['Дата']} {rec['Время']}"
        )
        await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Ваша запись отменена.")
        return
    svc = rec["Услуга"]
    date = rec["Дата"]
    slots = get_service_slots(svc)
    if not slots:
        await update.message.reply_text("Нет информации о слотах для этой услуги.")
        return
    taken_slots = get_taken_slots(svc, date)
    free_slots = [t for t in slots if t not in taken_slots or t == rec.get("Время")]
    if not free_slots:
        await update.message.reply_text("Все слоты на этот день заняты.")
        return
    text_slots = ["Выберите новый слот:"]
    for i, t in enumerate(free_slots, 1):
        text_slots.append(f"{i}. {t}")
    await update.message.reply_text("\n".join(text_slots))
    context.user_data["awaiting_slot"] = {"row": row_idx, "slots": free_slots, "record": rec}

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
    sheet.update_cell(row_idx, 5, new_time)
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

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%d.%m.%Y")
    records = sheet.get_all_records()
    for rec in records:
        if rec.get("Дата") == today:
            chat_id = rec.get("Chat ID")
            svc = rec.get("Услуга")
            time_ = rec.get("Время")
            await context.bot.send_message(
                chat_id,
                f"🔔 Напоминание: у вас сегодня запись на *{svc}* в *{time_}*.",
                parse_mode="Markdown"
            )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if is_cancel_intent(text):
        return await handle_cancel_or_edit(update, context)

    if context.user_data.get("awaiting_slot"):
        handled = await handle_slot_selection(update, context)
        if handled:
            return

    # Сохраняем историю для OpenAI
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": text})
    context.user_data["history"] = history[-20:]

    form = context.user_data.get("form", {})
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v and (not form.get(k) or form.get(k).lower() != v.lower()):
            form[k] = v
    context.user_data["form"] = form

    # Консультация (цены/услуги)
    if is_consult_intent(text):
        ai_instruction = (
            "Ты — внимательный и вежливый администратор клиники. "
            "Если клиент спрашивает только про цены, услуги или слоты — объясни это, не навязывай запись. "
            "Отправь только нужную информацию, если человек не хочет записываться."
        )
        ai_system = ai_instruction + "\nВот список услуг:\n" + build_services_list()
        msgs = [{"role": "system", "content": ai_system}] + history[-10:]
        try:
            resp = openai.chat.completions.create(model="gpt-4o", messages=msgs)
            reply = resp.choices[0].message.content
        except Exception:
            reply = "Ошибка AI 🤖"
        await update.message.reply_text(reply)
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-20:]
        return

    # 1. Если указана услуга и дата, но нет времени — предлагай только слоты этой услуги
    if form.get("Услуга") and form.get("Дата") and not form.get("Время"):
        svc = form["Услуга"]
        date = form["Дата"]
        slots = get_service_slots(svc)
        taken_slots = get_taken_slots(svc, date)
        free_slots = [t for t in slots if t not in taken_slots]
        if free_slots:
            slot_texts = [f"{i+1}. {t}" for i, t in enumerate(free_slots)]
            await update.message.reply_text(
                "Свободные слоты на выбранную дату:\n" +
                "\n".join(slot_texts) +
                "\nНапишите номер или время (например: 2 или 12:00)."
            )
            context.user_data["awaiting_time"] = {"slots": free_slots}
            return
        else:
            await update.message.reply_text("На эту дату нет свободных слотов. Попробуйте другую дату или услугу.")
            return

    # 2. Если ожидаем время — пишем в форму и записываем
    if context.user_data.get("awaiting_time"):
        slots = context.user_data["awaiting_time"]["slots"]
        value = text.strip()
        slot_num = None
        if re.fullmatch(r"\d+", value):
            slot_num = int(value) - 1
            if 0 <= slot_num < len(slots):
                form["Время"] = slots[slot_num]
        else:
            for t in slots:
                if value in t:
                    form["Время"] = t
        context.user_data["form"] = form
        if form.get("Время"):
            del context.user_data["awaiting_time"]
        else:
            await update.message.reply_text("Пожалуйста, выберите время из списка (номер или время).")
            return

    # 3. Проверяем полную форму — финальная запись только если все поля есть и время валидное
    if is_form_complete(form):
        svc = form["Услуга"]
        slots = get_service_slots(svc)
        if form["Время"] not in slots:
            await update.message.reply_text(
                f"Время {form['Время']} недоступно для услуги {svc}. Пожалуйста, выберите только из доступных слотов."
            )
            form["Время"] = ""
            context.user_data["form"] = form
            return
        taken_slots = get_taken_slots(form["Услуга"], form["Дата"])
        if form["Время"] in taken_slots:
            await update.message.reply_text("Этот слот уже занят. Выберите другое время.")
            form["Время"] = ""
            context.user_data["form"] = form
            return
        await register_and_notify(form, update, context)
        context.user_data["form"] = {}
        return

    # 4. Если что-то не хватает — AI сопровождает до сбора всех данных
    form_state = (
        f"Имя: {form.get('Имя', '—')}\n"
        f"Телефон: {form.get('Телефон', '—')}\n"
        f"Услуга: {form.get('Услуга', '—')}\n"
        f"Дата: {form.get('Дата', '—')}\n"
        f"Время: {form.get('Время', '—')}\n"
    )
    prompt = (
        "Ты — вежливый, живой администратор клиники. "
        "Если у клиента не хватает каких-то данных для записи (имя, телефон, услуга, дата, время) — уточни именно их, но дружелюбно, не строго по шагам. "
        "Разрешается реагировать на вопросы про услуги и цены, объяснять и предлагать. "
        "Если всё есть — подтверди запись. Не проси одно и то же по 10 раз.\n"
        "Текущие данные клиента:\n" + form_state +
        "Вот список услуг:\n" + build_services_list()
    )
    msgs = [{"role": "system", "content": prompt}] + history[-10:]
    try:
        resp = openai.chat.completions.create(model="gpt-4o", messages=msgs)
        reply = resp.choices[0].message.content
    except Exception:
        reply = "Ошибка AI 🤖"
    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-20:]

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def start_scheduler(_: ContextTypes.DEFAULT_TYPE):
        scheduler.add_job(send_reminders, "cron", hour=9, minute=0, args=[app.bot])
        scheduler.start()

    app.post_init = start_scheduler

    RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if RENDER_URL.startswith("https://"):
        RENDER_URL = RENDER_URL.replace("https://", "")
    if RENDER_URL.startswith("http://"):
        RENDER_URL = RENDER_URL.replace("http://", "")
    if RENDER_URL.endswith("/"):
        RENDER_URL = RENDER_URL.rstrip("/")
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
