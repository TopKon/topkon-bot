import os
import datetime
import logging
from collections import defaultdict
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

BOT_NAME = "Топкон"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# ---------------------- Конфигурация таблиц ---------------------------------------
HEADERS = [
    "Дата",
    "Водитель",
    "Тип",
    "Время_Начала",
    "ОДО_Начала",
    "Время_Конца",
    "ОДО_Конец",
    "Пробег_км",
    "Топливо_л",
    "Расход_руб",
    "Фото_ID",
]
ANALYTICS_HEADERS = ["Дата", "Водитель", "Итого_руб"]

# ---------------------- Google Sheets --------------------------------------------

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(os.getenv("SPREADSHEET_ID"))

    # Основной журнал
    log_sheet = wb.sheet1
    if log_sheet.row_values(1) != HEADERS:
        log_sheet.clear()
        log_sheet.append_row(HEADERS)

    # Вкладка «Водители»
    try:
        drivers_sheet = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drivers_sheet = wb.add_worksheet("Drivers", rows=1000, cols=3)
        drivers_sheet.update("A1:C1", [["TelegramID", "ФИО", "Авто"]])

    # Вкладка «Аналитика»
    try:
        analytics_sheet = wb.worksheet("Analytics")
    except WorksheetNotFound:
        analytics_sheet = wb.add_worksheet("Analytics", rows=1000, cols=3)
        analytics_sheet.update("A1:C1", [ANALYTICS_HEADERS])

    return log_sheet, analytics_sheet, drivers_sheet


LOG_SHEET, ANALYTICS_SHEET, DRIVERS_SHEET = init_sheets()

# Загружаем водителей в память при старте
DRIVER_MAP = {
    row[0]: {"FullName": row[1], "CarNumber": row[2] if len(row) > 2 else ""}
    for row in DRIVERS_SHEET.get_all_values()[1:]
}

# ---------------------- Константы состояний --------------------------------------
(
    START_ODOMETER,
    END_ODOMETER,
    FUEL_LITERS,
    FUEL_COST,
    REG_NAME,
    REG_CAR,
) = range(6)

sessions: dict[int, dict] = defaultdict(dict)

# ---------------------- Вспомогательные функции -----------------------------------

def update_daily_cost(date_str: str, driver_id: str) -> float:
    """Пересчитать сумму расходов водителя за день и обновить вкладку «Аналитика»."""
    records = LOG_SHEET.get_all_records()
    total = sum(
        float(r.get("Расход_руб", 0) or 0)
        for r in records
        if r.get("Дата") == date_str and r.get("Тип") == "Fuel" and r.get("Водитель") == driver_id
    )

    rows = ANALYTICS_SHEET.get_all_values()
    for idx, row in enumerate(rows[1:], start=2):
        if row[0] == date_str and row[1] == driver_id:
            ANALYTICS_SHEET.update_cell(idx, 3, total)
            break
    else:
        ANALYTICS_SHEET.append_row([date_str, driver_id, total])

    return total

async def ensure_registered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    if chat_id in DRIVER_MAP:
        return True
    context.user_data.clear()
    await update.message.reply_text("🚗 Вы не зарегистрированы. Введите ФИО полностью:")
    return False

# ---------------------- Регистрация водителя --------------------------------------

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["FullName"] = update.message.text.strip()
    await update.message.reply_text("Введите номер автомобиля:")
    return REG_CAR

async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_user.id)
    full_name = context.user_data.get("FullName")
    car_number = update.message.text.strip()

    DRIVERS_SHEET.append_row([chat_id, full_name, car_number])
    DRIVER_MAP[chat_id] = {"FullName": full_name, "CarNumber": car_number}

    await update.message.reply_text(
        f"✅ Регистрация завершена, {full_name}. Начните смену командой /начало")
    return ConversationHandler.END

# ---------------------- Команды бота ---------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_user.id)
    if chat_id in DRIVER_MAP:
        name = DRIVER_MAP[chat_id]["FullName"]
        await update.message.reply_text(
            f"Привет, {name}! Я {BOT_NAME} 🤖 — помогу вести учёт топлива.")
        await update.message.reply_text(
            "Команды:\n"
            "/начало — начало смены 🚗\n"
            "/топливо — заправка ⛽\n"
            "/конец — завершить смену 🔚")
    else:
        await ensure_registered(update, context)
        return REG_NAME

async def cmd_startshift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await ensure_registered(update, context):
        return REG_NAME
    await update.message.reply_text("🚗 Фото одометра в начале смены и значение пробега:")
    return START_ODOMETER

async def save_start_odo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id_int = update.effective_user.id
    msg = update.message
    try:
        odo = int(msg.text.strip())
    except (ValueError, AttributeError):
        await msg.reply_text("Нужно число. Попробуйте ещё раз:")
        return START_ODOMETER

    sessions[chat_id_int] = {
        "Дата": datetime.date.today(tz=MOSCOW_TZ).isoformat(),
        "Водитель": str(chat_id_int),
        "Время_Начала": datetime.datetime.now(tz=MOSCOW_TZ).isoformat(timespec="seconds"),
        "ОДО_Начала": odo,
        "Фото_ID": msg.photo[-1].file_id if msg.photo else "",
    }

    await msg.reply_text("✅ Смена начата. Не забудьте завершить её командой /конец")
    return ConversationHandler.END

async def cmd_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await ensure_registered(update, context):
        return REG_NAME
    await update.message.reply_text("⛽ Введите количество литров:")
    return FUEL_LITERS

async def fuel_liters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        liters = float(update.message.text.replace(",", "."))
        context.user_data["liters"] = liters
    except ValueError:
        await update.message.reply_text("Нужно число литров. Попробуйте ещё раз:")
        return FUEL_LITERS
    await update.message.reply_text("Введите стоимость в рублях:")
    return FUEL_COST

async def fuel_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        cost = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Попробуйте ещё раз:")
        return FUEL_COST

    chat_id_int = update.effective_user.id
    chat_id = str(chat_id_int)
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""
    today = datetime.date.today(tz=MOSCOW_TZ).isoformat()

    LOG_SHEET.append_row([
        today,
        chat_id,
        "Fuel",
        "",
        "",
        "",
        "",
        "",
        context.user_data["liters"],
        cost,
        photo_id,
    ])

    total_today = update_daily_cost(today, chat_id)
    await update.message.reply_text(
        f"✅ Заправка сохранена. Траты за {today}: {total_today:.2f} ₽")
    return ConversationHandler.END

async def cmd_endshift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Закрыть смену (заглушка, чтобы бот не падал). Добавьте логику позже."""
    await update.message.reply_text(
        "🔚 Завершение смены пока не реализовано. Используйте /начало и /топливо — эти команды работают.")
    return ConversationHandler.END
