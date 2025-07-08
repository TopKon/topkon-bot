# -*- coding: utf-8 -*-
"""
Топкон‑бот — однокомпонентная версия
===============================
Один файл (`topkon_bot.py`) содержит всю логику бота, auto‑install зависимостей и мини‑Flask-заглушку для Render Free.

Переменные окружения:
  TOKEN — токен бота
  SPREADSHEET_ID — ID Google‑таблицы
  GOOGLE_APPLICATION_CREDENTIALS — имя json‑ключа GCP

Команды:
  /start — регистрация и справка
  /startshift — начало смены
  /fuel — заправка
  /endshift — конец смены
  /changecar — сменить номер авто
  /help — список команд
"""
from __future__ import annotations
import os, sys, subprocess, datetime, threading
from zoneinfo import ZoneInfo
from typing import Optional, Final

# автоматически ставим зависимости
REQUIRE = [
    "python-telegram-bot==20.8",
    "gspread==6.0.2",
    "oauth2client==4.1.3",
    "Flask==2.3.3",
]
try:
    import telegram
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", *REQUIRE])
# безопасно импортируем
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# константы
TOKEN: Final[str] = os.getenv("TOKEN", "")
TZ = ZoneInfo("Europe/Moscow")
(
    REG_NAME, REG_CAR,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO,
    CHANGE_CAR,
) = range(10)

_HEADER = [
    "Дата","UID","ФИО","Авто","Тип","Время","ОДО","Фото","Литры","Сумма","Δ_км","Личный_км"
]
_COL_ID = {h: i for i, h in enumerate(_HEADER)}

# Flask-заглушка
def _fake_web():
    app = Flask(__name__)
    @app.get("/")
    def ok(): return "Bot is alive!", 200
    app.run(host="0.0.0.0", port=8080)
threading.Thread(target=_fake_web, daemon=True).start()

# Google Sheets
def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"), scope
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(os.getenv("SPREADSHEET_ID"))
    log_ws = wb.sheet1
    if log_ws.row_values(1) != _HEADER:
        log_ws.clear()
        log_ws.append_row(_HEADER)
    try:
        drv = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drv = wb.add_worksheet("Drivers", 1000, 3)
        drv.append_row(["UID","ФИО","Авто"])
    return log_ws, drv

LOG_WS, DRV_WS = _init_sheets()
DRIVERS = {r[0]: {"name": r[1], "car": r[2]} for r in DRV_WS.get_all_values()[1:]}

# вспомогательные

def _now(): return datetime.datetime.now(TZ)

def _append(uid: str, type_: str, **fields):
    if uid not in DRIVERS: return
    row = [""] * len(_HEADER)
    row[_COL_ID["Дата"]] = _now().date().isoformat()
    row[_COL_ID["UID"]] = uid
    row[_COL_ID["ФИО"]] = DRIVERS[uid]["name"]
    row[_COL_ID["Авто"]] = DRIVERS[uid]["car"]
    row[_COL_ID["Тип"]] = type_
    row[_COL_ID["Время"]] = _now().isoformat(timespec="seconds")
    for k,v in fields.items():
        if k in _COL_ID: row[_COL_ID[k]] = v
    LOG_WS.append_row(row)

async def _need_reg(update: Update):
    uid = str(update.effective_user.id)
    if uid not in DRIVERS:
        await update.message.reply_text("🚗 Сначала /start для регистрации.")
        return True
    return False

# ========== регистрация ==========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in DRIVERS:
        await update.message.reply_text(
            "⚙️ Доступные: /startshift /fuel /endshift /changecar /help"
        )
        return ConversationHandler.END
    await update.message.reply_text("👤 Введите ФИО:")
    return REG_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name']=update.message.text.strip()
    await update.message.reply_text("🚘 Введите номер авто:")
    return REG_CAR

async def reg_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    name=ctx.user_data.pop('name')
    car=update.message.text.strip()
    DRV_WS.append_row([uid,name,car])
    DRIVERS[uid]={'name':name,'car':car}
    await update.message.reply_text(f"✅ {name}, регистрация завершена!")
    return ConversationHandler.END

reg_conv=ConversationHandler(
    entry_points=[CommandHandler('start',cmd_start)],
    states={
        REG_NAME:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_name)],
        REG_CAR: [MessageHandler(filters.TEXT&~filters.COMMAND,reg_car)],
    },
    fallbacks=[]
)

# ========== смена авто ==========
async def cmd_changecar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _need_reg(update): return ConversationHandler.END
    uid=str(update.effective_user.id)
    await update.message.reply_text(f"{DRIVERS[uid]['name']}, введите новый номер авто:")
    return CHANGE_CAR

async def change_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    new=update.message.text.strip()
    # обновляем в листе
    col=DRV_WS.col_values(1)
    row=col.index(uid)+1
    DRV_WS.update_cell(row,3,new)
    DRIVERS[uid]['car']=new
    await update.message.reply_text(f"✅ Номер авто обновлен: {new}")
    return ConversationHandler.END

change_conv=ConversationHandler(
    entry_points=[CommandHandler('changecar',cmd_changecar)],
    states={ CHANGE_CAR:[MessageHandler(filters.TEXT&~filters.COMMAND,change_car)] },
    fallbacks=[]
)

# ========== начало смены ==========
async def cmd_startshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _need_reg(update): return ConversationHandler.END
    uid, name=str(update.effective_user.id),DRIVERS[str(update.effective_user.id)]['name']
    await update.message.reply_text(f"{name}, укажите пробег на начало смены:")
    return START_ODO

async def start_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data['odo_start']=int(update.message.text)
    except:
        await update.message.reply_text("Нужно число, попробуйте ещё:")
        return START_ODO
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def start_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото, пришлите снова:")
        return START_PHOTO
    uid=str(update.effective_user.id)
    odo=ctx.user_data.pop('odo_start')
    prev=_get_last('End',uid) or odo
    personal=odo-prev
    _append(uid,'Start',ОДО=odo,Фото=update.message.photo[-1].file_id,Личный_км=personal)
    await update.message.reply_text(f"✅ Смена начата.")
    return ConversationHandler.END

start_conv=ConversationHandler(
    entry_points=[CommandHandler('startshift',cmd_startshift)],
    states={
        START_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,start_odo)],
        START_PHOTO:[MessageHandler(filters.PHOTO,start_photo)]
    },
    fallbacks=[]
)

# вспомогательный для прошлого одометра

def _get_last(type_,uid):
    for rec in reversed(LOG_WS.get_all_records()):
        if rec['UID']==uid and rec['Тип']==type_:
            try: return int(rec['ОДО'])
            except: pass
    return None

# ========== заправка ==========
async def cmd_fuel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _need_reg(update): return ConversationHandler.END
    await update.message.reply_text(f"{DRIVERS[str(update.effective_user.id)]['name']}, пришлите фото чека:")
    return FUEL_PHOTO

async def fuel_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото чека, пришлите:")
        return FUEL_PHOTO
    ctx.user_data['fuel_photo']=update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data['fuel_cost']=float(update.message.text)
    except:
        await update.message.reply_text("Нужно число, повторите сумму:")
        return FUEL_COST
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: liters=float(update.message.text)
    except:
        await update.message.reply_text("Нужно число, повторите литры:")
        return FUEL_LITERS
    uid=str(update.effective_user.id)
    _append(uid,'Fuel',Фото=ctx.user_data.pop('fuel_photo'),Сумма=ctx.user_data.pop('fuel_cost'),Литры=liters)
    await update.message.reply_text("✅ Заправка сохранена.")
    return ConversationHandler.END

fuel_conv=ConversationHandler(
    entry_points=[CommandHandler('fuel',cmd_fuel)],
    states={
        FUEL_PHOTO:[MessageHandler(filters.PHOTO,fuel_photo)],
        FUEL_COST:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_cost)],
        FUEL_LITERS:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_liters)]
    },
    fallbacks=[]
)

# ========== конец смены ==========
async def cmd_endshift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await _need_reg(update): return ConversationHandler.END
    await update.message.reply_text(f"{DRIVERS[str(update.effective_user.id)]['name']}, укажите пробег на конец смены:")
    return END_ODO

async def end_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data['odo_end']=int(update.message.text)
    except:
        await update.message.reply_text("Нужно число, попробуйте ещё:")
        return END_ODO
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO

async def end_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото, пришлите:")
        return END_PHOTO
    uid=str(update.effective_user.id)
    odo_end=ctx.user_data.pop('odo_end')
    start_time, start_odo = _get_last_record(uid,'Start')
    delta=odo_end-start_odo
    _append(uid,'End',ОДО=odo_end,Фото=update.message.photo[-1].file_id,Δ_км=delta)
    # вычисляем время смены
    hours=((_now()-start_time).total_seconds()/3600)
    name=DRIVERS[uid]['name']
    await update.message.reply_text(
        f"✅ Смена завершена. {name}, вы проехали {delta} км и работали {hours:.1f} ч. Приятного отдыха!"
    )
    return ConversationHandler.END

end_conv=ConversationHandler(
    entry_points=[CommandHandler('endshift',cmd_endshift)],
    states={
        END_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,end_odo)],
        END_PHOTO:[MessageHandler(filters.PHOTO,end_photo)]
    },
    fallbacks=[]
)

# вспомогательный для времени/ODO

def _get_last_record(uid,type_):
    for rec in reversed(LOG_WS.get_all_records()):
        if rec['UID']==uid and rec['Тип']==type_:
            t=datetime.datetime.fromisoformat(rec['Время'])
            return t,int(rec['ОДО'])
    return _now(),0

# ========== help ==========
async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ /start /startshift /fuel /endshift /changecar /help")

# ========== main ==========
def main():
    if not TOKEN: raise RuntimeError("TOKEN env var not set")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(reg_conv)
    app.add_handler(change_conv)
    app.add_handler(start_conv)
    app.add_handler(fuel_conv)
    app.add_handler(end_conv)
    app.add_handler(CommandHandler('help', help_cmd))
    print("🔄 Bot started", flush=True)
    app.run_polling()

if __name__ == '__main__':
    main()













