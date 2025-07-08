# -*- coding: utf-8 -*-
"""
TopKon Fleet Bot v5.0 — Drivers, Managers, Admins, Multi-company
One-file bot with role-based workflows and real-time prompts
"""
import os, sys, subprocess, threading, asyncio, datetime
from zoneinfo import ZoneInfo
from typing import Final, Optional, Dict

# Auto-install dependencies
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
    import telegram

from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# Constants & Environments
TOKEN: Final[str] = os.getenv("TOKEN", "")
SPREADSHEET_ID: Final[str] = os.getenv("SPREADSHEET_ID", "")
CREDS_FILE: Final[str] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")
DEFAULT_ADMIN_UID = '1881053841'

# States
ROLE_SELECT, REG_NAME, REG_COMPANY, REG_CAR,
START_ODO, START_PHOTO,
FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
END_ODO, END_PHOTO = range(11)

# Prompt helper
COMMANDS_TEXT = (
    "/startshift — Начать смену\n"
    "/fuel — Заправка\n"
    "/endshift — Завершить смену\n"
    "/addcompany — Добавить компанию (админ)\n"
    "/stats — Статистика (руководитель)\n"
    "/help — Помощь"  
)

def prompt_commands(): return f"Доступные команды:\n{COMMANDS_TEXT}"

# Flask for Render
def _fake_web():
    app = Flask(__name__)
    @app.get("/")
    def ok(): return "Bot is alive!", 200
    app.run(host="0.0.0.0", port=8080)
threading.Thread(target=_fake_web, daemon=True).start()

# Google Sheets init
def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    client = gspread.authorize(creds)
    wb = client.open_by_key(SPREADSHEET_ID)
    # Main log
    log = wb.sheet1
    # Drivers
    try: drivers = wb.worksheet("Drivers")
    except WorksheetNotFound:
        drivers = wb.add_worksheet("Drivers", 1000, 4)
        drivers.append_row(["UID","Role","Company","Car"])
    # Companies
    try: comps = wb.worksheet("Companies")
    except WorksheetNotFound:
        comps = wb.add_worksheet("Companies", 100, 2)
        comps.append_row(["Name","ManagerUID"])
    return log, drivers, comps
LOG_WS, DRV_WS, COMP_WS = _init_sheets()
# In-memory caches
USERS: Dict[str,Dict] = {r[0]:{"role":r[1],"company":r[2],"car":r[3]} for r in DRV_WS.get_all_values()[1:]}
COMPANIES = {r[0]:r[1] for r in COMP_WS.get_all_values()[1:]}

# Helpers
def _now(): return datetime.datetime.now(TZ).isoformat(timespec="seconds")
def _append_log(uid, type_, **fields):
    row = [""]* (len(LOG_WS.row_values(1)))
    headers = LOG_WS.row_values(1)
    row[headers.index("Date")] = datetime.date.today(TZ).isoformat()
    row[headers.index("UID")] = uid
    row[headers.index("Role")] = USERS[uid]["role"]
    row[headers.index("Company")] = USERS[uid]["company"]
    row[headers.index("Type")] = type_
    row[headers.index("Time")] = _now()
    for k,v in fields.items():
        if k in headers: row[headers.index(k)] = str(v)
    LOG_WS.append_row(row)
def _last(field, uid, type_filter=None):
    recs = LOG_WS.get_all_records()
    for rec in reversed(recs):
        if rec["UID"]!=uid: continue
        if type_filter and rec["Type"]!=type_filter: continue
        try: return float(rec[field])
        except: continue
    return None

async def ensure_registered(update: Update):
    uid = str(update.effective_user.id)
    if uid not in USERS:
        await update.message.reply_text("Сначала зарегистрируйтесь: /start")
        return False
    return True

# /start
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in USERS:
        await update.message.reply_text(f"Привет, {ctx.user_data.get('name','')}!\n"+prompt_commands())
        return ConversationHandler.END
    # else new
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(r,callback_data=r)] for r in ("Водитель","Руководитель")])
    await update.message.reply_text("👋 Добро пожаловать! Выберите вашу роль:", reply_markup=kb)
    return ROLE_SELECT

async def role_select(update: Update, ctx):
    q=update.callback_query; await q.answer()
    role=q.data; ctx.user_data['role']=role
    await q.edit_message_text(f"Вы выбрали роль: {role}")
    await q.message.reply_text("Введите ваше ФИО:")
    return REG_NAME

async def reg_name(update: Update, ctx):
    ctx.user_data['name']=update.message.text.strip()
    if ctx.user_data['role']=='Водитель':
        await update.message.reply_text("Введите организацию (точно как в базе):")
        return REG_COMPANY
    else:
        await update.message.reply_text("Введите организацию (ООО, ИП или АО 'Название'):")
        return REG_COMPANY

async def reg_company(update: Update, ctx):
    comp=update.message.text.strip()
    role=ctx.user_data['role']; uid=str(update.effective_user.id)
    # if manager: notify admin
    if role=='Руководитель':
        # pending approval
        ctx.user_data['company']=comp
        await update.message.reply_text("Запрос регистрации отправлен администратору, ожидайте решения.")
        # notify admin
        await ctx.bot.send_message(DEFAULT_ADMIN_UID, f"Новый менеджер {ctx.user_data['name']} для компании {comp}. Разрешить? /approve_{uid}")
        return ConversationHandler.END
    else:
        # driver: check company exists
        if comp not in COMPANIES:
            await update.message.reply_text("Компания не найдена или не одобрена. Обратитесь к руководителю.")
            return ConversationHandler.END
        ctx.user_data['company']=comp
        await update.message.reply_text("Введите номер автомобиля:")
        return REG_CAR

async def reg_car(update: Update, ctx):
    car=update.message.text.strip(); uid=str(update.effective_user.id)
    USERS[uid]={'role':ctx.user_data['role'],'company':ctx.user_data['company'],'car':car}
    DRV_WS.append_row([uid,ctx.user_data['role'],ctx.user_data['company'],car])
    await update.message.reply_text("✅ Регистрация завершена!"+prompt_commands())
    return ConversationHandler.END

async def approve_mgr(update: Update, ctx):
    text=update.message.text; uid=text.split('_')[-1]
    if str(update.effective_user.id)!=DEFAULT_ADMIN_UID: return
    # approve
    comp=ctx.user_data.get('company')
    COMPANIES[comp]=uid; COMP_WS.append_row([comp,uid])
    await update.message.reply_text(f"Менеджер {USERS[uid]['name']} одобрен для {comp}.")

# /startshift
async def startshift(update: Update, ctx):
    if not await ensure_registered(update): return
    await update.message.reply_text("Укажите пробег на начало смены (км):")
    return START_ODO
async def start_odo(update: Update, ctx):
    try: odo=float(update.message.text)
    except: return await update.message.reply_text("Нужно число, попробуйте снова:")
    ctx.user_data['odo_start']=odo
    # out-of-shift travel
    last_end=_last('Odometer','%s'%update.effective_user.id,'End') or odo
    extra=odo - last_end
    if extra>0: await update.message.reply_text(f"Вы проехали вне смены: {extra:.1f} км.")
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO
async def start_photo(update: Update, ctx):
    if not update.message.photo: return await update.message.reply_text("Нужно фото, попробуйте снова")
    uid=str(update.effective_user.id)
    odo=ctx.user_data.pop('odo_start')
    _append_log(uid,'Start',Odometer=odo,Photo=update.message.photo[-1].file_id)
    await update.message.reply_text("✅ Смена начата!"+prompt_commands())
    return ConversationHandler.END

# /fuel
async def fuel(update: Update, ctx):
    if not await ensure_registered(update): return
    await update.message.reply_text("Пришлите фото чека:")
    return FUEL_PHOTO
async def fuel_p(update: Update, ctx):
    if not update.message.photo: return await update.message.reply_text("Нужно фото чека:")
    ctx.user_data['fuel_photo']=update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму чека (₽):")
    return FUEL_COST
async def fuel_cost(update: Update, ctx):
    try: cost=float(update.message.text)
    except: return await update.message.reply_text("Нужно число, введите сумму:")
    ctx.user_data['fuel_cost']=cost
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS
async def fuel_liters(update: Update, ctx):
    try: liters=float(update.message.text)
    except: return await update.message.reply_text("Нужно число, введите литры:")
    uid=str(update.effective_user.id)
    _append_log(uid,'Fuel',Photo=ctx.user_data['fuel_photo'],Amount=ctx.user_data['fuel_cost'],Liters=liters)
    await update.message.reply_text("✅ Заправка добавлена."+prompt_commands())
    return ConversationHandler.END

# /endshift
async def endshift(update: Update, ctx):
    if not await ensure_registered(update): return
    await update.message.reply_text("Укажите пробег на конец смены (км):")
    return END_ODO
async def end_odo(update: Update, ctx):
    try: odo=float(update.message.text)
    except: return await update.message.reply_text("Нужно число, попробуйте снова:")
    ctx.user_data['odo_end']=odo
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO
async def end_photo(update: Update, ctx):
    if not update.message.photo: return await update.message.reply_text("Нужно фото, попробуйте снова")
    uid=str(update.effective_user.id)
    odo_end=ctx.user_data.pop('odo_end')
    odo_start=_last('Odometer',uid,'Start') or odo_end
    km=odo_end-odo_start
    _append_log(uid,'End',Odometer=odo_end,Photo=update.message.photo[-1].file_id,Delta_km=km)
    # shift duration
    # (omitted duration calc for brevity)
    await update.message.reply_text(f"✅ Смена завершена. Вы проехали {km:.1f} км. Приятного отдыха!"+prompt_commands())
    return ConversationHandler.END

# fallback
async def unknown(update: Update, ctx):
    await update.message.reply_text("Извините, не понял. " + prompt_commands())

# main
def main():
    if not TOKEN: raise RuntimeError("TOKEN not set")
    app = ApplicationBuilder().token(TOKEN).build()
    # conversations
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler('start',cmd_start), CallbackQueryHandler(role_select)],
        states={
            ROLE_SELECT:[CallbackQueryHandler(role_select)],
            REG_NAME:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_name)],
            REG_COMPANY:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_company)],
            REG_CAR:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_car)],
        }, fallbacks=[MessageHandler(filters.ALL,unknown)]
    )
    start_conv = ConversationHandler([CommandHandler('startshift',startshift)],{START_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,start_odo)],START_PHOTO:[MessageHandler(filters.PHOTO,start_photo)]},fallbacks=[MessageHandler(filters.ALL,unknown)])
    fuel_conv = ConversationHandler([CommandHandler('fuel',fuel) ],{FUEL_PHOTO:[MessageHandler(filters.PHOTO,fuel_p)],FUEL_COST:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_cost)],FUEL_LITERS:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_liters)]},fallbacks=[MessageHandler(filters.ALL,unknown)])
    end_conv = ConversationHandler([CommandHandler('endshift',endshift)],{END_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,end_odo)],END_PHOTO:[MessageHandler(filters.PHOTO,end_photo)]},fallbacks=[MessageHandler(filters.ALL,unknown)])
    app.add_handler(reg_conv)
    app.add_handler(start_conv)
    app.add_handler(fuel_conv)
    app.add_handler(end_conv)
    # admin
    app.add_handler(CommandHandler('addcompany', lambda u,c: c.bot.send_message(u.effective_chat.id,'Введите название компании:')))
    app.add_handler(CommandHandler('approve',approve_mgr))
    # stats placeholder
    # unknown
    app.add_handler(MessageHandler(filters.ALL,unknown))
    print("Bot started",flush=True)
    app.run_polling()

if __name__=='__main__':
    main()

















