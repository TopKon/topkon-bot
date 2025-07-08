# -*- coding: utf-8 -*-
"""
Топкон‑бот v2.3 — исправлена регистрация и повторное срабатывание
────────────────────────────────────────────────────────────────────────────
• /start теперь корректно работает как точка входа ConversationHandler
  — первый запуск запускает регистрацию (ФИО → Авто)
  — повторный запуск показывает доступные команды и завершается
• ConversationHandler для регистрации имеет entry_points = /start
  → текстовые сообщения вне состояния НЕ перехватываются
• Удалён лишний глобальный MessageHandler, из‑за которого бот "съедал"
  одометр вместо ФИО и застревал в регистрации
• Остальной функционал (/startshift, /fuel, /endshift) без изменений

Токен, ID таблицы и путь к JSON‑ключу — через переменные окружения:
  TOKEN, SPREADSHEET_ID, GOOGLE_APPLICATION_CREDENTIALS
"""

import os, threading, datetime
from zoneinfo import ZoneInfo
from typing import Final, Optional

from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# ───────────────────────── Константы ─────────────────────────────────────
TOKEN: Final[str] = os.getenv("TOKEN", "")  # <-- ОБЯЗАТЕЛЬНО в Render ENV
TZ = ZoneInfo("Europe/Moscow")

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

# ───────────────────────── Flask‑заглушка (Render Free) ──────────────────

def _fake_web():
    app = Flask(__name__)

    @app.get("/")
    def ping():
        return "Bot is alive!", 200

    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=_fake_web, daemon=True).start()

# ───────────────────────── Google Sheets ─────────────────────────────────

def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    gs = gspread.authorize(creds)
    wb = gs.open_by_key(os.getenv("SPREADSHEET_ID"))

    log = wb.sheet1
    header = [
        "Дата",
        "UID",
        "ФИО",
        "Авто",
        "Тип",
        "Время",
        "ОДО",
        "Фото",
        "Литры",
        "Сумма",
        "Δ_км",
        "Личный_км",
    ]
    if log.row_values(1) != header:
        log.clear()
        log.append_row(header)

    try:
        drv = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drv = wb.add_worksheet("Drivers", 1000, 3)
        drv.append_row(["UID", "ФИО", "Авто"])
    return log, drv

LOG_SHEET, DRV_SHEET = _init_sheets()
DRIVERS = {r[0]: {"name": r[1], "car": r[2]} for r in DRV_SHEET.get_all_values()[1:]}

# ───────────────────────── Helper ───────────────────────────────────────

def _now():
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def _last_odo(uid: str, entry_type: Optional[str] = None) -> Optional[int]:
    """Последний ОДО по пользователю."""
    for row in reversed(LOG_SHEET.get_all_records()):
        if row["UID"] != uid:
            continue
        if entry_type and row["Тип"] != entry_type:
            continue
        if row["ОДО"]:
            try:
                return int(row["ОДО"])
            except ValueError:
                return None
    return None

async def _ensure_reg(update: Update) -> bool:
    uid = str(update.effective_user.id)
    if uid in DRIVERS:
        return True
    await update.message.reply_text("🚗 Вы не зарегистрированы. Введите /start для регистрации")
    return False

# ───────────────────────── /start & Registration ────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in DRIVERS:
        await update.message.reply_text(
            "⚙️ Доступные команды:\n/startshift – начало смены\n/fuel – заправка\n/endshift – конец смены"
        )
        return ConversationHandler.END
    await update.message.reply_text("👋 Добро пожаловать! Введите ФИО:")
    return REG_NAME

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name_tmp"] = update.message.text.strip()
    await update.message.reply_text("Введите номер авто:")
    return REG_CAR

async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    name = context.user_data.pop("name_tmp")
    car = update.message.text.strip()

    DRV_SHEET.append_row([uid, name, car])
    DRIVERS[uid] = {"name": name, "car": car}

    await update.message.reply_text("✅ Регистрация завершена. Используйте /startshift")
    return ConversationHandler.END

registration_conv = ConversationHandler(
    entry_points=[CommandHandler("start", cmd_start)],
    states={
        REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
        REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
    },
    fallbacks=[],
)

# ───────────────────────── Начало смены ────────────────────────────────

async def startshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_reg(update):
        return ConversationHandler.END
    await update.message.reply_text("Введите одометр (км) на начало смены:")
    return START_ODO

async def startshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo_val = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите одометр:")
        return START_ODO
    context.user_data["start_odo"] = odo_val
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def startshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""
    uid = str(update.effective_user.id)
    name, car = DRIVERS[uid]["name"], DRIVERS[uid]["car"]
    odo_start = context.user_data.pop("start_odo")

    personal = odo_start - (_last_odo(uid) or odo_start)

    LOG_SHEET.append_row([
        datetime.date.today(TZ).isoformat(),
        uid,
        name,
        car,
        "Start",
        _now(),
        odo_start,
        photo_id,
        "",
        "",
        "",
        personal,
    ])
    await update.message.reply_text("✅ Смена начата. /fuel или /endshift")
    return ConversationHandler.END

startshift_conv = ConversationHandler(
    entry_points=[CommandHandler("startshift", startshift_cmd)],
    states={
        START_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, startshift_odo)],
        START_PHOTO: [MessageHandler(filters.PHOTO, startshift_photo)],
    },
    fallbacks=[],
)

# ───────────────────────── Заправка ─────────────────────────────────────

async def fuel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_reg(update):
        return ConversationHandler.END
    await update.message.reply_text("Пришлите фото чека:")
    return FUEL_PHOTO

async def fuel_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fuel_photo"] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cost = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите сумму:")
        return FUEL_COST
    context.user_data["fuel_cost"] = cost
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        liters = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите литры:")
        return FUEL_LITERS

    uid = str(update.effective_user.id)
    name, car = DRIVERS[uid]["name"], DRIVERS[uid]["car"]
    LOG_SHEET.append_row([
        datetime.date.today(TZ).isoformat(),
        uid,
        name,
        car,
        "Fuel",
        _now(),
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

# ───────────────────────── Конец смены ─────────────────────────────────--

async def endshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_reg(update):
        return ConversationHandler.END
    await update.message.reply_text("Введите одометр на конец смены:")
    return END_ODO

async def endshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo_val = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите одометр:")
        return END_ODO
    context.user_data["end_odo"] = odo_val
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO

async def endshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""
    uid = str(update.effective_user.id)
    name, car = DRIVERS[uid]["name"], DRIVERS[uid]["car"]
    odo_end = context.user_data.pop("end_odo")

    last_start = _last_odo(uid, entry_type="Start") or odo_end
    delta = odo_end - last_start

    LOG_SHEET.append_row([
        datetime.date.today(TZ).isoformat(),
        uid,
        name,
        car,
        "End",
        _now(),
        odo_end,
        photo_id,
        "",
        "",
        delta,
        "",
    ])
    await update.message.reply_text("✅ Смена завершена. Хорошего отдыха! /startshift — новая смена")
    return ConversationHandler.END

endshift_conv = ConversationHandler(
    entry_points=[CommandHandler("endshift", endshift_cmd)],
    states={
        END_ODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, endshift_odo)],
        END_PHOTO: [MessageHandler(filters.PHOTO, endshift_photo)],
    },
    fallbacks=[],
)

# ───────────────────────── Main ─────────────────────────────────────────

def main():
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set")

    app = ApplicationBuilder().token(TOKEN).build()

    # Conversation handlers
    app.add_handler(registration_conv)
    app.add_handler(startshift_conv)
    app.add_handler(fuel_conv)
    app.add_handler(endshift_conv)

    print("🔄 Bot polling started", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()











