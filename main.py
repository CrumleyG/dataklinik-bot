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

CANCEL_KEYWORDS = ["отменить запись", "поменять время", "отмени", "удалить запись", "отмена"]
BOOKING_KEYWORDS = [
    "запис", "на приём", "appointment", "запишите", "хочу записаться", "поставь на", "можно записаться"
]
CONSULT_WORDS = ["стоимость", "цена", "прайс", "какие есть", "сколько стоит", "услуги"]

# --- Распознавание намерений ---
def is_cancel_intent(text):
    q = text.lower()
    return any(kw in q for kw in CANCEL_KEYWORDS)

def is_booking_intent(text):
    q = text.lower()
    return any(kw in q for kw in BOOKING_KEYWORDS)

def is_consult_intent(text):
    q = text.lower()
    return any(w in q for w in CONSULT_WORDS)

# --- Улучшенное сопоставление услуги ---
def match_service(text):
    q = text.lower()
    # Прямое число: "2" → 2-я услуга
    m = re.match(r"^\s*(\d+)\s*$", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]["название"]
    # По названию или ключам (частично и нестрого)
    for key, s in SERVICES_DICT.items():
        title = s["название"].lower()
        if title in q:
            return s["название"]
        for kw in s.get("ключи", []):
            if kw.lower() in q:
                return s["название"]
        # Часть слова: "рент" → "рентген", "чист" → "чистка"
        for word in [title] + s.get("ключи", []):
            if any(w in q for w in word.lower().split()):
                return s["название"]
    # Пробуем fuzzy match
    for key, s in SERVICES_DICT.items():
        for kw in s.get("ключи", []):
            if kw.lower()[:5] in q or q in kw.lower():
                return s["название"]
    return None

def build_services_list():
    lines = ["📋 *Список услуг:*"]
    for i, s in enumerate(SERVICES, 1):
        lines.append(f"{i}. *{s['название']}* — {s['цена']}")
    return "\n".join(lines)

# --- Извлечение полей из текста ---
def extract_fields(text):
    data = {}
    # Имя: ищем любые варианты, включая "Меня зовут", "Имя:", "я ..."
    m = re.search(r"(?:зовут|имя|я)[\s:]*([А-ЯA-Z][а-яa-zё]+)", text, re.I)
    if m:
        data["Имя"] = m.group(1)
    # Телефон
    m = re.search(r"(\+?\d[\d\s\-\(\)]{7,})", text)
    if m:
        phone = re.sub(r"[^\d+]", "", m.group(1))
        data["Телефон"] = phone
    # Услуга
    svc = match_service(text)
    if svc:
        data["Услуга"] = svc
    # Дата
    m = re.search(r"(сегодня|завтра|послезавтра|\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)", text.lower())
    if m:
        d = m.group(1)
        if "сегодня" in d:
            data["Дата"] = datetime.now().strftime("%d.%m.%Y")
        elif "завтра" in d and "послезавтра" not in d:
            data["Дата"] = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        elif "послезавтра" in d:
            data["Дата"] = (datetime.now() + timedelta(days=2)).strftime("%d.%m.%Y")
        else:
            try:
                # Поддержка разных форматов
                date_obj = None
                for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m", "%d/%m", "%d-%m"):
                    try:
                        date_obj = datetime.strptime(d, fmt)
                        break
                    except Exception:
                        continue
                if date_obj:
                    # Если год не указан — добавь текущий
                    if date_obj.year < 2000:
                        date_obj = date_obj.replace(year=datetime.now().year)
                    data["Дата"] = date_obj.strftime("%d.%m.%Y")
                else:
                    data["Дата"] = d
            except Exception:
                data["Дата"] = d
    # Время
    m = re.search(r"(\d{1,2})[.:](\d{2})", text)
    if m:
        data["Время"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    return data

def is_form_complete(form):
    return all(form.get(k) for k in ("Имя", "Телефон", "Услуга", "Дата", "Время"))

# --- Проверка занятых слотов ---
def get_taken_slots(услуга, дата):
    records = sheet.get_all_records()
    taken = []
    for rec in records:
        if rec.get("Услуга", "").strip().lower() == услуга.strip().lower() and \
           rec.get("Дата", "").strip() == дата.strip():
            taken.append(rec.get("Время", "").strip())
    return taken

# --- Поиск последней записи пользователя ---
def find_last_booking(chat_id):
    records = sheet.get_all_records()
    last = None
    for idx, rec in enumerate(records, start=2):
        if str(rec.get("ChatID", "")) == str(chat_id):
            last = (idx, rec)
    return last if last else (None, None)

# --- Регистрация и уведомление ---
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

# --- Отмена или изменение записи ---
async def handle_cancel_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.effective_chat.id
    row_idx, rec = find_last_booking(chat_id)
    if not rec:
        await update.message.reply_text("❗ У вас нет активных записей.")
        return
    # отмена
    if "отменить" in text or "удалить" in text:
        sheet.delete_row(row_idx)
        msg = (
            f"❌ Пациент отменил запись:\n"
            f"{rec['Имя']}, {rec['Услуга']} на {rec['Дата']} {rec['Время']}"
        )
        await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Ваша запись отменена.")
        return
    # смена времени
    svc = rec["Услуга"]
    date = rec["Дата"]
    # Слоты услуги теперь берём из SERVICES_DICT (по названию или ключу)
    slot_key = None
    for key, val in SERVICES_DICT.items():
        if val["название"].strip().lower() == svc.strip().lower():
            slot_key = key
            break
    slots = []
    if slot_key:
        slots = SERVICES_DICT[slot_key].get("слоты", [])
    # Если не нашли по названию — ищем по ключам
    if not slots:
        for key, s in SERVICES_DICT.items():
            if s["название"].strip().lower() == svc.strip().lower():
                slots = s.get("слоты", [])
                break
    if not slots:
        await update.message.reply_text("К сожалению, для этой услуги нет информации о слотах.")
        return
    taken_slots = get_taken_slots(svc, date)
    free_slots = [t for t in slots if t not in taken_slots or t == rec.get("Время")]
    if not free_slots:
        await update.message.reply_text("К сожалению, все слоты на этот день уже заняты.")
        return
    text_slots = ["Выберите новый слот:"]
    for i, t in enumerate(free_slots, 1):
        text_slots.append(f"{i}. {t}")
    await update.message.reply_text("\n".join(text_slots))
    context.user_data["awaiting_slot"] = {"row": row_idx, "slots": free_slots, "record": rec}

# --- Выбор слота при изменении времени ---
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

# --- Напоминания ---
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

# --- Главное: обработка сообщения ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # 1. Отмена или редактирование
    if is_cancel_intent(text):
        return await handle_cancel_or_edit(update, context)

    # 2. Ожидание выбора нового слота после "сменить время"
    if context.user_data.get("awaiting_slot"):
        handled = await handle_slot_selection(update, context)
        if handled:
            return

    # 3. Справочная информация
    if is_consult_intent(text):
        return await update.message.reply_text(
            build_services_list(), parse_mode="Markdown"
        )

    # 4. Обработка заявки на запись (заполнение полей)
    form = context.user_data.get("form", {})
    extracted = extract_fields(text)
    form.update(extracted)
    context.user_data["form"] = form

    # 4.1. Если не хватает поля "Услуга" — спросить услугу
    if not form.get("Услуга"):
        await update.message.reply_text(
            "Пожалуйста, выберите услугу из списка или опишите, на что хотите записаться:\n\n" +
            build_services_list(),
            parse_mode="Markdown"
        )
        return

    # 4.2. Если не хватает даты — спросить дату
    if not form.get("Дата"):
        await update.message.reply_text("На какую дату вы хотите записаться? (например: завтра, 24.05, послезавтра)")
        return

    # 4.3. Если не хватает времени — предложить свободные слоты
    if not form.get("Время"):
        svc = form["Услуга"]
        # Получаем ключ услуги
        slot_key = None
        for key, val in SERVICES_DICT.items():
            if val["название"].strip().lower() == svc.strip().lower():
                slot_key = key
                break
        slots = []
        if slot_key:
            slots = SERVICES_DICT[slot_key].get("слоты", [])
        if not slots:
            for key, s in SERVICES_DICT.items():
                if s["название"].strip().lower() == svc.strip().lower():
                    slots = s.get("слоты", [])
                    break
        if not slots:
            await update.message.reply_text("Извините, для этой услуги нет информации о свободных слотах.")
            return
        taken_slots = get_taken_slots(svc, form["Дата"])
        free_slots = [t for t in slots if t not in taken_slots]
        if not free_slots:
            await update.message.reply_text("Все слоты на выбранную дату уже заняты. Укажите другую дату.")
            return
        text_slots = ["Свободные слоты:"]
        for i, t in enumerate(free_slots, 1):
            text_slots.append(f"{i}. {t}")
        await update.message.reply_text("\n".join(text_slots) + "\n\nНапишите номер или время слота (например: 3 или 12:30)")
        # Ожидание выбора времени
        context.user_data["awaiting_time"] = {"slots": free_slots}
        return

    # 4.4. Если ожидался выбор времени
    if context.user_data.get("awaiting_time"):
        slots = context.user_data["awaiting_time"]["slots"]
        value = text.strip()
        slot_num = None
        if re.fullmatch(r"\d+", value):
            slot_num = int(value) - 1
            if 0 <= slot_num < len(slots):
                form["Время"] = slots[slot_num]
        else:
            # Пробуем распознать время напрямую
            for t in slots:
                if value in t:
                    form["Время"] = t
        context.user_data["form"] = form
        if form.get("Время"):
            del context.user_data["awaiting_time"]
        else:
            await update.message.reply_text("Пожалуйста, выберите время из списка выше (номер или время).")
            return

    # 4.5. Если не хватает телефона — запросить
    if not form.get("Телефон"):
        await update.message.reply_text("Укажите, пожалуйста, ваш номер телефона.")
        return

    # 4.6. Если не хватает имени — запросить
    if not form.get("Имя"):
        await update.message.reply_text("Пожалуйста, напишите ваше имя.")
        return

    # 5. Если форма заполнена — проверяем свободен ли слот
    if is_form_complete(form):
        taken_slots = get_taken_slots(form["Услуга"], form["Дата"])
        if form["Время"] in taken_slots:
            await update.message.reply_text("Этот слот уже занят. Выберите другое время.")
            # Очистить поле времени и начать заново выбор времени
            form["Время"] = ""
            context.user_data["form"] = form
            # Снова вызвать выбор времени
            svc = form["Услуга"]
            slot_key = None
            for key, val in SERVICES_DICT.items():
                if val["название"].strip().lower() == svc.strip().lower():
                    slot_key = key
                    break
            slots = []
            if slot_key:
                slots = SERVICES_DICT[slot_key].get("слоты", [])
            taken_slots = get_taken_slots(svc, form["Дата"])
            free_slots = [t for t in slots if t not in taken_slots]
            text_slots = ["Свободные слоты:"]
            for i, t in enumerate(free_slots, 1):
                text_slots.append(f"{i}. {t}")
            await update.message.reply_text("\n".join(text_slots) + "\n\nНапишите номер или время слота (например: 3 или 12:30)")
            context.user_data["awaiting_time"] = {"slots": free_slots}
            return
        # Всё ок — записываем
        await register_and_notify(form, update, context)
        context.user_data["form"] = {}
        return

    # Если всё равно что-то не хватает — fallback: подключаем OpenAI как ассистента
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": text})
    context.user_data["history"] = history[-20:]
    ai_system = (
        "Ты — вежливый бот стоматологии. Помогаешь собрать Имя, Телефон, Услугу, Дату, Время. " +
        "Если чего-то нет — уточняй. Для списка услуг используй только этот список:\n" +
        build_services_list()
    )
    msgs = [{"role": "system", "content": ai_system}] + history[-10:]
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
