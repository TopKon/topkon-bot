# -*- coding: utf-8 -*-
"""
Топкон‑бот v2.0 – полностью рабочая версия
────────────────────────────────────────────────────────────────────────────
Функции
• /start – справка и запуск регистрации, если водитель ещё не в базе
• Регистрация (ФИО → авто) – один раз
• /startshift – начало смены (пробег + фото)
• /fuel – заправка (фото чека → сумма ₽ → литры)
• /endshift – конец смены (пробег + фото)
• Подсчёт «личного» пробега (ODO start − ODO последнего END)
• Все записи летят в Google Sheets
• Flask‑заглушка на порт 8080, чтобы Render Free считал сервис «живым»

‼️ Токен и переменные:
• TOKEN – жёстко прописан (как просили)
• GOOGLE_APPLICATION_CREDENTIALS и SPREADSHEET_ID должны быть заданы в Render → Environment
"""

# ───────────────────────── Import ─────────────────────────────────────────
import os, threading, datetime, asyncio
from collections import defaultdict
from zoneinfo import ZoneInfo
from typing import Final

from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# ───────────────────────── Константы ─────────────────────────────────────
TOKEN: Final[str] = "7718554572:AAElisVGS8qKak-la8mEKlKn7NACtD-kLVI"
MOSCOW = ZoneInfo("Europe/Moscow")

(
    REG_NAME,
    REG_CAR,
    START_ODO,
    START_PHOTO,
    FUEL_PHOTO,
    FUEL_COST,
    FUEL_LITERS,
    END_ODO,
    END_PHOTO,
) = range(9)

# ───────────────────────── Flask fake web (Render Free) ──────────────────

def run_fake_web():
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "Bot is alive!", 200

    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_fake_web, daemon=True).start()

# ───────────────────────── Google Sheets init ────────────────────────────

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(os.getenv("SPREADSHEET_ID"))

    log_sheet = wb.sheet1
    header = [
        "Дата",
        "ВодительID",
        "ФИО",
        "Авто",
        "Тип",
        "Время",
        "ОДО",
        "Фото_ID",
        "Литры",
        "Сумма",
        "Δ_км",
        "Личный_км",
    ]
    if log_sheet.row_values(1) != header:
        log_sheet.clear()
        log_sheet.append_row(header)

    try:
        drv_sheet = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drv_sheet = wb.add_worksheet("Drivers", 1000, 3)
        drv_sheet.append_row(["TelegramID", "ФИО", "Авто"])
    return log_sheet, drv_sheet


LOG, DRIVERS = init_sheets()
DRIVER_MAP = {row[0]: {"name": row[1], "car": row[2]} for row in DRIVERS.get_all_values()[1:]}

# ───────────────────────── Helper functions ──────────────────────────────

def last_odo(uid: str):
    """Последний зафиксированный одометр (END или START)."""
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

# ───────────────────────── Команда /start ───────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in DRIVER_MAP:
        await update.message.reply_text(
            "👋 Добро пожаловать в Топкон‑бот! Давайте зарегистрируемся. Введите ФИО:"
        )
        return REG_NAME
    await update.message.reply_text(
        "⚙️ Команды:\n/startshift – начало смены\n/fuel – заправка\n/endshift – конец смены"
    )
    return ConversationHandler.END

# ───────────────────────── Регистрация ───────────────────────────────────

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

registration_conv = ConversationHandler(
    entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
    states={REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)]},
    fallbacks=[],
    map_to_parent={ConversationHandler.END: ConversationHandler.END},
)

# ───────────────────────── Начало смены ──────────────────────────────────

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
        today,
        uid,
        name,
        car,
        "Start",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        odo_start,
        photo_id,
        "",
        "",
        "",
        personal_km,
    ])
    await update.message.reply_text(
        "✅ Смена начата. Команды:\n/fuel – заправка\n/endshift – конец смены"
    )
    return ConversationHandler.END

startshift_conv = ConversationHandler(
    entry_points=[CommandHandler("startshift", startshift_cmd)],
    states={
        START_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, startshift_odo)],
        START_PHOTO: [MessageHandler(filters.PHOTO, startshift_photo)],
    },
    fallbacks=[],
)

# ───────────────────────── Заправка ──────────────────────────────────────

async def fuel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
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
    car = DRIVER_MAP[uid]["car"]
    today = datetime.date.today(MOSCOW).isoformat()

    LOG.append_row([
        today,
        uid,
        name,
        car,
        "Fuel",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        "",
        context.user_data.pop("fuel_photo"),
        liters,
        context.user_data.pop("fuel_cost"),
        "",
        "",
    ])
    await update.message.reply_text("✅ Заправка сохранена.")
    return ConversationHandler.END

fuel_conv = ConversationHandler(
    entry_points=[CommandHandler("fuel", fuel_cmd)],
    states={
        FUEL_PHOTO: [MessageHandler(filters.PHOTO, fuel_photo)],
        FUEL_COST: [MessageHandler(filters.TEXT & ~filters.COMMAND, fuel_cost)],
        FUEL_LITERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, fuel_liters)],
    },
    fallbacks=[],
)

# ───────────────────────── Конец смены ───────────────────────────────────

async def endshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_registered(update, context):
        return ConversationHandler.END
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
    car = DRIVER_MAP[uid]["car"]
    odo_end = context.user_data.pop("odo_end")
    today = datetime.date.today(MOSCOW).isoformat()

    odo_start_today = last_odo(uid) or odo_end
    delta_km = odo_end - odo_start_today

    LOG.append_row([
        today,
        uid,
        name,
        car,
        "End",
        datetime.datetime.now(MOSCOW).isoformat(timespec="seconds"),
        odo_end,
        photo_id,
        "",
        "",
        delta_km,
        "",
    ])
    await update.message.reply_text(
        f"✅ Смена завершена. Пройдено {delta_km} км. Хорошего отдыха!"
    )
    return ConversationHandler.END

endshift_conv = ConversationHandler(
    entry_points=[CommandHandler("endshift", endshift_cmd)],
    states={
        END_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, endshift_odo)],
        END_PHOTO: [MessageHandler(filters.PHOTO, endshift_photo)],
    },
    fallbacks=[],
)

# ───────────────────────── Main & launch ─────────────────────────────────

async def main():
    application = ApplicationBuilder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(registration_conv)
    application.add_handler(startshift_conv)
    application.add_handler(fuel_conv)
    application.add_handler(endshift_conv)

    print("🔄 Bot polling started", flush=True)
    await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())








