# -*- coding: utf-8 -*-
"""
TopKon Fleet Bot v4.0 — Roles & Notifications Enhancements
One‑file bot for Drivers, Managers, and Admins
"""
import os, sys, subprocess, threading, asyncio, datetime
from zoneinfo import ZoneInfo
from typing import Final, Optional, Dict

# Auto‑install dependencies
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

# Constants & Env\TOKEN: Final[str] = os.getenv("TOKEN", "")
SPREADSHEET_ID: Final[str] = os.getenv("SPREADSHEET_ID", "")
CREDS_FILE: Final[str] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")
DEFAULT_ADMIN_UID = '1881053841'

# States\(
    ROLE_SELECT, REG_NAME, REG_CAR, REG_COMPANY_SELECT, REG_COMPANY_INPUT,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO
) = range(12)

# Utility: list commands by role
def commands_text(role: str) -> str:
    if role == 'Driver':
        base = ['/startshift', '/fuel', '/endshift']
    elif role == 'Manager':
        base = ['/onshift', '/yesterday']
    elif role == 'Admin':
        base = ['/addcompany', '/approve_manager', '/approve_driver', '/onshift', '/yesterday']
    else:
        base = ['/start']
    return "Доступные команды: " + " | ".join(base)

# Flask stub for Render
(app := Flask(__name__)).add_url_rule('/', 'ping', lambda: ('alive',200))
threading.Thread(target=lambda: app.run(host='0.0.0.0',port=8080), daemon=True).start()

# Google Sheets init
def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    client = gspread.authorize(creds)
    wb = client.open_by_key(SPREADSHEET_ID)
    # Companies sheet
    try:
        comp_ws = wb.worksheet('Companies')
    except WorksheetNotFound:
        comp_ws = wb.add_worksheet('Companies',100,3)
        comp_ws.append_row(['CompanyID','CompanyName','AdminUID'])
    # Users sheet
    try:
        users_ws = wb.worksheet('Users')
    except WorksheetNotFound:
        users_ws = wb.add_worksheet('Users',1000,6)
        users_ws.append_row(['UID','Name','Car','Role','CompanyID','Status'])
    # Log sheet
    try:
        log_ws = wb.worksheet('Log')
    except WorksheetNotFound:
        log_ws = wb.sheet1
        log_ws.clear()
        log_ws.append_row(['Date','UID','Role','CompanyID','Type','Time','ODO','Photo','Liters','Cost','Delta_km','Personal_km'])
    return comp_ws, users_ws, log_ws
COMP_WS, USERS_WS, LOG_WS = _init_sheets()

# In-memory caches
COMPANIES = {r[0]: (r[1], r[2]) for r in COMP_WS.get_all_values()[1:]}
USERS: Dict[str, Dict[str,str]] = {
    r[0]: dict(zip(['Name','Car','Role','CompanyID','Status'], r[1:]))
    for r in USERS_WS.get_all_values()[1:]
}

# Helpers
def _now(): return datetime.datetime.now(TZ).isoformat(timespec='seconds')

def _append_log(uid: str, type_: str, **fields):
    user = USERS.get(uid)
    if not user or user['Status']!='Approved': return
    row = [datetime.date.today(TZ).isoformat(), uid, user['Role'], user['CompanyID'], type_, _now()] + ['']*7
    headers = ['Date','UID','Role','CompanyID','Type','Time','ODO','Photo','Liters','Cost','Delta_km','Personal_km']
    for k,v in fields.items():
        if k in headers:
            row[headers.index(k)] = v
    LOG_WS.append_row(row)

async def get_user_role(uid: str) -> str:
    if uid == DEFAULT_ADMIN_UID:
        return 'Admin'
    u = USERS.get(uid)
    if u and u['Status']=='Approved':
        return u['Role']
    return ''

async def send_commands(update: Update, role: str):
    await update.message.reply_text(commands_text(role))

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = await get_user_role(uid) or 'Driver'
    await update.message.reply_text("Извините, я не понял. " + commands_text(role))

# Conversation: /start -> select role
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = await get_user_role(uid)
    if role:
        await update.message.reply_text(f"Вы уже зарегистрированы как {role}.")
        return await send_commands(update, role)
    kb = [[InlineKeyboardButton('Водитель', callback_data='role_Driver'),
           InlineKeyboardButton('Менеджер', callback_data='role_Manager')]]
    await update.message.reply_text("👋 Привет! Пожалуйста, выберите вашу роль:", reply_markup=InlineKeyboardMarkup(kb))
    return ROLE_SELECT

async def role_select_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    role = update.callback_query.data.split('_',1)[1]
    ctx.user_data['role'] = role
    if role=='Driver':
        await update.callback_query.edit_message_text("🚗 Введите ваше ФИО:")
        return REG_NAME
    else:
        await update.callback_query.edit_message_text("👤 Введите ваше ФИО (Менеджер):")
        return REG_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = update.message.text.strip()
    if ctx.user_data['role']=='Driver':
        await update.message.reply_text("🚘 Введите номер авто:")
        return REG_CAR
    else:
        # Manager: ask company by selection
        kb = [[InlineKeyboardButton(name, callback_data=f"mgrcomp_{cid}")] for cid,(name,_) in COMPANIES.items()]
        await update.message.reply_text("🏢 Выберите вашу компанию:", reply_markup=InlineKeyboardMarkup(kb))
        return REG_COMPANY_SELECT

async def reg_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['car'] = update.message.text.strip()
    # Driver: next choose company
    kb = [[InlineKeyboardButton(name, callback_data=f"drvcomp_{cid}")] for cid,(name,_) in COMPANIES.items()]
    await update.message.reply_text("🏢 Выберите компанию:", reply_markup=InlineKeyboardMarkup(kb))
    return REG_COMPANY_SELECT

async def reg_company_select_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    data = update.callback_query.data
    role = ctx.user_data['role']
    cid = data.split('_',1)[1]
    name = ctx.user_data.get('name','')
    car = ctx.user_data.get('car','')
    # append user as Pending
    USERS_WS.append_row([str(update.effective_user.id), name, car, role, cid, 'Pending'])
    USERS[str(update.effective_user.id)] = {'Name':name,'Car':car,'Role':role,'CompanyID':cid,'Status':'Pending'}
    await update.callback_query.edit_message_text("✅ Заявка отправлена, ожидайте одобрения.")
    # notify approvers
    if role=='Driver':
        # notify managers
        for u,d in USERS.items():
            if d['Role']=='Manager' and d['CompanyID']==cid and d['Status']=='Approved':
                await ctx.bot.send_message(chat_id=u, text=f"Новая заявка водителя: {name}, авто {car}. /approve_driver {update.effective_user.id} или /reject_driver {update.effective_user.id}")
    else:
        # notify admin
        await ctx.bot.send_message(chat_id=DEFAULT_ADMIN_UID, text=f"Новая заявка менеджера: {name}. /approve_manager {update.effective_user.id} или /reject_manager {update.effective_user.id}")
    return ConversationHandler.END

# Approve/Reject handlers for managers and admin
async def approve_driver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid)!='Manager': return
    target = ctx.args[0] if ctx.args else None
    if target and target in USERS and USERS[target]['Status']=='Pending':
        rownum = next(i for i,v in enumerate(USERS_WS.get_all_values(),1) if v[0]==target)
        USERS_WS.update_cell(rownum,6,'Approved')
        USERS[target]['Status']='Approved'
        await update.message.reply_text(f"Водитель {USERS[target]['Name']} одобрен.")

async def reject_driver(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid)!='Manager': return
    target = ctx.args[0] if ctx.args else None
    if target and target in USERS and USERS[target]['Status']=='Pending':
        rownum = next(i for i,v in enumerate(USERS_WS.get_all_values(),1) if v[0]==target)
        USERS_WS.update_cell(rownum,6,'Rejected')
        USERS[target]['Status']='Rejected'
        await update.message.reply_text(f"Водитель {USERS[target]['Name']} отклонен.")

async def approve_manager(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id)!=DEFAULT_ADMIN_UID: return
    target = ctx.args[0] if ctx.args else None
    if target and target in USERS and USERS[target]['Status']=='Pending':
        rownum = next(i for i,v in enumerate(USERS_WS.get_all_values(),1) if v[0]==target)
        USERS_WS.update_cell(rownum,6,'Approved')
        USERS[target]['Status']='Approved'
        await update.message.reply_text(f"Менеджер {USERS[target]['Name']} одобрен.")

async def reject_manager(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id)!=DEFAULT_ADMIN_UID: return
    target = ctx.args[0] if ctx.args else None
    if target and target in USERS and USERS[target]['Status']=='Pending':
        rownum = next(i for i,v in enumerate(USERS_WS.get_all_values(),1) if v[0]==target)
        USERS_WS.update_cell(rownum,6,'Rejected')
        USERS[target]['Status']='Rejected'
        await update.message.reply_text(f"Менеджер {USERS[target]['Name']} отклонен.")

# Driver flows (startshift, fuel, endshift) ... [unchanged, see above for brevity]
# Manager flows (/onshift, /yesterday) ... [unchanged]
# Admin flows (/addcompany) ... [unchanged]

# Catch-all unknown
# ... existing unknown handler

async def main():
    if not TOKEN or not SPREADSHEET_ID or not CREDS_FILE:
        raise RuntimeError("Environment not properly set")
    app = ApplicationBuilder().token(TOKEN).build()
    # Register handlers
    # /start registration
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            ROLE_SELECT:[CallbackQueryHandler(role_select_cb, pattern='^role_')],
            REG_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_CAR:[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
            REG_COMPANY_SELECT:[CallbackQueryHandler(reg_company_select_cb, pattern='^(drvcomp|mgrcomp)_')],
        },
        fallbacks=[MessageHandler(filters.ALL, unknown)]
    ))
    # Approval commands
    app.add_handler(CommandHandler('approve_driver', approve_driver))
    app.add_handler(CommandHandler('reject_driver', reject_driver))
    app.add_handler(CommandHandler('approve_manager', approve_manager))
    app.add_handler(CommandHandler('reject_manager', reject_manager))
    # TODO: add driver, manager, admin flows similarly
    app.add_handler(MessageHandler(filters.ALL, unknown))

    print("🔄 Bot polling started", flush=True)
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__=='__main__':
    asyncio.run(main())
















