# -*- coding: utf-8 -*-
"""
Топкон‑бот v1.1 
────────────────────────────────────────────────────────────────────────────
• Однократная регистрация (ФИО + авто)
• Смена: старт → (опц.) заправка → конец
• Подсчёт «личного» пробега (ODO start − последний End)
• Хранение в Google Sheets
• Flask‑заглушка (порт 8080) — Render Free считает сервис «живым»
• Telegram polling (без Webhook) — простейший запуск
• Исправлены синтаксические ошибки и конфликт event‑loop
• Токен жёстко прописан по запросу клиента
"""

import os, threading, datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

# ───────────────────────── Flask (порт 8080) ───────────────────────────────
from flask import Flask

def run_fake_web():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "Bot is alive!", 200

    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_fake_web, daemon=True).start()

# ───────────────────────── Telegram ────────────────────────────────────────
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters,
    ConversationHandler,
)

# ───────────────────────── Google Sheets ───────────────────────────────────
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# ───────────────────────── Константы ───────────────────────────────────────
TOKEN = "7718554572:AAElisVGS8qKak-la8mEKlKn7NACtD-kLVI"  # актуальный токен
MOSCOW = ZoneInfo("Europe/Moscow")

# Conversation states
(
    REG_NAME, REG_CAR,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO,
) = range(9)

# ───────────────────────── Google Sheets init ──────────────────────────────

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(os.getenv("SPREADSHEET_ID"))

    log_sheet = wb.sheet1
    header = [
        "Дата", "ВодительID", "ФИО", "Авто", "Тип", "Время",
        "ОДО", "Фото_ID", "Литры", "Сумма", "Δ_км", "Личный_км",
    ]
    if log_sheet.row_values(1) != header:
        log_sheet.clear(); log_sheet.append_row(header)

    try:
        drv_sheet = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drv_sheet = wb.add_worksheet("Drivers", 1000, 3)
        drv_sheet.append_row(["TelegramID", "ФИО", "Авто"])
    return log_sheet, drv_sheet

LOG, DRIVERS = init_sheets()
DRIVER_MAP = {row[0]: {"name": row[1], "car": row[2]} for row in DRIVERS.get_all_values()[1:]}

# ───────────────────────── Вспомогательные ────────────────────────────────

def last_odo(uid: str):
    """Вернёт последний одометр из журнала."""
    for row in reversed(LOG.get_all_records()):
        if row["ВодительID"] == uid and row["ОДО"]:
            return int(row["ОДО"])
    return None

async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in DRIVER_MAP:
        return True
    await update.message.reply_text("🚗 Вы не зарегистрированы. Введите ФИО:")
    return False

# ───────────────────────── Регистрация ─────────────────────────────────────

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name_tmp"] = update.message.text.strip()
    await update.message.reply_text("Введите номер автомобиля:")
    return REG_CAR

async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    name = context.user_data.pop("name_tmp")
    car = update.message.text.strip()

    DRIVERS.append_row([uid, name, car])
    DRIVER_MAP[uid] = {"name": name, "car": car}

    await update.message.reply_text(
        f"✅ Регистрация завершена, {name}. Используйте /startshift"
    )
    return ConversationHandler.END

# ───────────────────────── Старт смены ─────────────────────────────────────

async def startshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
    await update.message.reply_text("Введите пробег на начало смены (число):")
    return START_ODO

async def startshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите:")
        return START_ODO
    context.user_data["start_odo"] = odo
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def startshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""

    uid = str(update.effective_user.id)
    name = DRIVER_MAP[uid]["name"]
    car = DRIVER_MAP[uid]["car"]
    odo_start = context.user_data.pop("start_odo")
    today = datetime.date.today(MOSCOW).isoformat()

    personal_km = odo_start - (last_odo(uid) or odo_start)

    LOG.append_row([
        today, uid, name, car, "Start",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        odo_start, photo_id, "", "", "", personal_km,
    ])
    await update.message.reply_text(
        "✅ Смена начата. Команды:\n/fuel – заправка\n/endshift – конец смены"
    )
    return ConversationHandler.END

# ───────────────────────── Заправка ────────────────────────────────────────
async def fuel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришлите фото чека:")
    return FUEL_PHOTO

async def fuel_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fuel_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму чека (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите сумму:")
        return FUEL_COST
    context.user_data["fuel_cost"] = cost
    await update.message.reply_text("Введите количество литров:")
    return FUEL_LITERS

async def fuel_liters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        liters = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите литры:")
        return FUEL_LITERS

    uid = str(update.effective_user.id)
    name = DRIVER_MAP[uid]["name"]
    car  = DRIVER_MAP[uid]["car"]
    today = datetime.date.today(MOSCOW).isoformat()

    LOG.append_row([
        today, uid, name, car, "Fuel",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        "", context.user_data.pop("fuel_photo"), liters, context.user_data.pop("fuel_cost"), "", "",
    ])
    await update.message.reply_text("✅ Заправка сохранена.")
    return ConversationHandler.END

# ───────────────────────── Конец смены ─────────────────────────────────────
async def endshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите пробег на конец смены (число):")
    return END_ODO

async def endshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo_end = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите:")
        return END_ODO
    context.user_data["odo_end"] = odo_end
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO

async def endshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""

    uid = str(update.effective_user.id)
    name = DRIVER_MAP[uid]["name"]
    car  = DRIVER_MAP[uid]["car"]
    odo_end = context.user_data.pop("odo_end")
    odo_start = last_odo(uid) or odo_end
    delta = odo_end - odo_start

    today = datetime.date.today(MOSCOW).isoformat()
    LOG.append_row([
        today, uid, name, car, "End",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        odo_end, photo_id, "", "", delta, "",
    ])
    await update.message.reply_text(
        f"✅ Смена закрыта. Пробег за смену: {delta} км.\n"
        "Чтобы начать новую смену — /startshift"
    )
    return ConversationHandler.END

# ───────────────────────── Команда /start ─────────────────────────────────
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in DRIVER_MAP:
        await update.message.reply_text(
            "Команды:\n"
            "/startshift — начать смену\n"
            "/fuel       — заправка\n"
            "/endshift   — конец смены"
        )
    else:
        await update.message.reply_text("🚗 Вы не зарегистрированы. Введите ФИО:")
        return REG_NAME

# ───────────────────────── Main ───────────────────────────────────────────

def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    # Регистрация Conversation
    reg_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
        states={REG_CAR: [





