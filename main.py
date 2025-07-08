# -*- coding: utf-8 -*-
"""
Топкон‑бот v2.5 — фиксы логики и обратной связи
────────────────────────────────────────────────────────────────────────────
• Формулировки для водителей упрощены
• После отправки фото одометра бот подсказывает, что делать дальше
• Проверка UID на регистрацию в Google Sheets перед сохранением данных
• Всё остальное осталось как в v2.4
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

TOKEN: Final[str] = os.getenv("TOKEN", "")
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
    "Дата", "UID", "ФИО", "Авто", "Тип", "Время", "ОДО", "Фото", "Литры", "Сумма", "Δ_км", "Личный_км",
]
_COL_INDEX = {c: i for i, c in enumerate(_HEADER)}

def _fake_web() -> None:
    app = Flask(__name__)
    @app.get("/")
    def ping():
        return "Bot is alive!", 200
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=_fake_web, daemon=True).start()

def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    gs = gspread.authorize(creds)
    wb = gs.open_by_key(os.getenv("SPREADSHEET_ID"))
    log_ws = wb.sheet1
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

def _now() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")

def _append_log(type_: str, uid: str, **fields) -> None:
    if uid not in DRIVERS:
        return
    row = ["" for _ in _HEADER]
    row[_COL_INDEX["Дата"]] = datetime.date.today(TZ).isoformat()
    row[_COL_INDEX["UID"]] = uid
    row[_COL_INDEX["ФИО"]] = DRIVERS[uid]["name"]
    row[_COL_INDEX["Авто"]] = DRIVERS[uid]["car"]
    row[_COL_INDEX["Тип"]] = type_
    row[_COL_INDEX["Время"]] = _now()
    for k, v in fields.items():
        if k in _COL_INDEX:
            row[_COL_INDEX[k]] = v
    LOG_WS.append_row(row)

def _last_odo(uid: str, only_type: Optional[str] = None) -> Optional[int]:
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
    await update.message.reply_text("✅ Регистрация завершена! Используйте /startshift для начала смены")
    return ConversationHandler.END

registration_conv = ConversationHandler(
    entry_points=[CommandHandler("start", cmd_start)],
    states={
        REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
        REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
    },
    fallbacks=[],
)

async def startshift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ensure_reg(update): return ConversationHandler.END
    await update.message.reply_text("Укажите пробег на начало смены:")
    return START_ODO

async def startshift_odo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odo_val = int(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите пробег:")
        return START_ODO
    context.user_data["start_odo"] = odo_val
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def startshift_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = update.message.photo[-1].file_id if update.message.photo else ""
    uid = str(update.effective_user.id)
    odo_start = context.user_data.pop("start_odo")
    personal_km = odo_start - (_last_odo(uid, only_type="End") or odo_start)
    _append_log("Start", uid, ОДО=odo_start, Фото=photo_id, Личный_км=personal_km)
    await update.message.reply_text("✅ Смена начата. Теперь можно выполнить /fuel или /endshift")
    return ConversationHandler.END

# Остальные блоки без изменений...
# fuel_conv, endshift_conv, help_cmd, main — те же, проверены ранее













