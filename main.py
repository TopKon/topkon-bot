# -*- coding: utf-8 -*-
"""
Топкон‑бот v2.4 — фиксы логики и записи в таблицу
────────────────────────────────────────────────────────────────────────────
• Исправлено положение столбцов: пробег, фото, литры и т.д. вставляются по
  своим колонкам, а не в «ФИО»
• /startshift  → одометр → фото — теперь всегда спрашивает в правильном
  порядке и записывает «личные км» (разница с последним окончанием смены)
• /fuel        → фото чека → сумма → литры — последовательность жёстко
  контролируется, пропуск невозможен
• /endshift    → одометр → фото — рассчитывается «Δ км» и закрывается смена
• Добавлена /help (дублирует список команд)
• Применён единый helper _log_row(...) для построения строк по заголовку
• Переменные окружения: TOKEN, SPREADSHEET_ID, GOOGLE_APPLICATION_CREDENTIALS

ВАЖНО: Перед деплоем убедитесь, что в Render в разделе *Environment* заданы
  TOKEN, SPREADSHEET_ID и GOOGLE_APPLICATION_CREDENTIALS.
"""

from __future__ import annotations
import datetime, os, threading
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
TOKEN: Final[str] = os.getenv("TOKEN", "")  # задаётся в Render ENV
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

_HEADER: Final[list[str]] = [
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
_COL_INDEX = {c: i for i, c in enumerate(_HEADER)}

# ───────────────────────── Flask‑заглушка (Render Free) ──────────────────

def _fake_web() -> None:
    app = Flask(__name__)

    @app.get("/")
    def ping():  # noqa: D401
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

    log_ws = wb.sheet1  # «Лог» – первая страница
    if log_ws.row_values(1) != _HEADER:
        log_ws.clear()
        log_ws.append_row(_HEADER)

    try:
        drv_ws = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drv_ws = wb.add_worksheet("Drivers", 1000, 3)
        drv_ws.append_row(["UID", "ФИО", "Авто"])
    return log_ws, drv_ws

LOG_WS, DRV_WS = _init_sheets()
DRIVERS: dict[str, dict[str, str]] = {
    r[0]: {"name": r[1], "car": r[2]} for r in DRV_WS.get_all_values()[1:]
}

# ───────────────────────── Helper‑функции ───────────────────────────────

def _now() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def _append_log(type_: str, uid: str, **fields) -> None:
    """Добавить строку в лог с произвольными полями."""
    row = ["" for _ in _HEADER]
    row[_COL_INDEX["Дата"]] = datetime.date.today(TZ).isoformat()
    row[_COL_INDEX["UID"]] = uid
    row[_COL_INDEX["ФИО"]] = DRIVERS[uid]["name"]
    row[_COL_INDEX["Авто"]] = DRIVERS[uid]["car"]
    row[_COL_INDEX["Тип"]] = type_
    row[_COL_INDEX["Время"]] = _now()
    # заполняем переданные поля
    for k, v in fields.items():
        if k not in _COL_INDEX:
            continue
        row[_COL_INDEX[k]] = v
    LOG_WS.append_row(row)


def _last_odo(uid: str, only_type: Optional[str] = None) -> Optional[int]:
    """Возвращает последний числовой одометр пользователя."""
    for record in reversed(LOG_WS.get_all_records())[::-1]:
        if record["UID"] != uid:
            continue
        if only_type and record["Тип"] != only_type:
            continue
        try:
            return int(record["ОДО"])
        except (ValueError, TypeError):
            continue
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

    DRV_WS.append_row([uid, name, car])
    DRIVERS[uid] = {"name": name, "car": car}

    await update.message.reply_text(
        "✅ Регистрация завершена! Используйте /startshift для начала смены"
    )
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
    odo_start = context.user_data.pop("start_odo")

    personal_km = odo_start - (_last_odo(uid, only_type="End") or odo_start)

    _append_log(
        "Start",
        uid,
        ОДО=odo_start,
        Фото=photo_id,
        Личный_км=personal_km,
    )
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
    if not update.message.photo:
        await update.message.reply_text("Отправьте именно фото (не файл).")
        return FUEL_PHOTO
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
    _append_log(
        "Fuel",
        uid,
        Фото=context.user_data.pop("fuel_photo"),
        Литры=liters,
        Сумма=context.user_data.pop("fuel_cost"),
    )
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
    if not update.message.photo:
        await update.message.reply_text("Отправьте именно фото (не файл).")
        return END_PHOTO
    photo_id = update.message.photo[-1].file_id
    uid = str(update.effective_user.id)
    odo_end = context.user_data.pop("end_odo")

    last_start = _last_odo(uid, only_type="Start") or odo_end
    delta_km = odo_end - last_start

    _append_log(
        "End",
        uid,
        ОДО=odo_end,
        Фото=photo_id,
        Δ_км=delta_km,
    )
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

# ───────────────────────── Help ─────────────────────────────────────────

async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ Доступные команды:\n/startshift – начало смены\n/fuel – заправка\n/endshift – конец смены"
    )

# ───────────────────────── Main ─────────────────────────────────────────

def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set")

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(registration_conv)
    application.add_handler(startshift_conv)
    application.add_handler(fuel_conv)
    application.add_handler(endshift_conv)

    print("🔄 Bot polling started", flush=True)
    application.run_polling()


if __name__ == "__main__":
    main()












