# -*- coding: utf-8 -*-
"""
TopKon Fleet Bot v3.0 — Roles & Multi‑Company Support
One‑file bot for Drivers, Managers and Admins
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

# Constants & Env
TOKEN: Final[str] = os.getenv("TOKEN", "")
SPREADSHEET_ID: Final[str] = os.getenv("SPREADSHEET_ID", "")
CREDS_FILE: Final[str] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")
DEFAULT_ADMIN_UID = '1881053841'

# Conversation states
(
    REG_NAME, REG_CAR, REG_COMPANY,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO,
) = range(10)

# Command list text
def commands_text(role: str) -> str:
    base = []
    if role == 'Driver':
        base = ['/startshift', '/fuel', '/endshift']
    elif role == 'Manager':
        base = ['/onshift', '/yesterday']
    elif role == 'Admin':
        base = ['/addcompany', '/assignmanager', '/setrole', '/onshift', '/yesterday']
    return "Доступные команды: " + " | ".join(base)

# Flask stub for Render
def _fake_web():
    app = Flask(__name__)
    @app.get('/')
    def ping(): return 'alive', 200
    app.run(host='0.0.0.0', port=8080)
threading.Thread(target=_fake_web, daemon=True).start()

# Google Sheets init
def _init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS_FILE, scope)
    client = gspread.authorize(creds)
    wb = client.open_by_key(SPREADSHEET_ID)
    # Companies
    try: comp_ws = wb.worksheet('Companies')
    except WorksheetNotFound:
        comp_ws = wb.add_worksheet('Companies',100,3)
        comp_ws.append_row(['CompanyID','CompanyName','AdminUID'])
    # Users
    try: users_ws = wb.worksheet('Users')
    except WorksheetNotFound:
        users_ws = wb.add_worksheet('Users',1000,6)
        users_ws.append_row(['UID','Name','Car','Role','CompanyID','Status'])
    # Log
    try: log_ws = wb.worksheet('Log')
    except WorksheetNotFound:
        log_ws = wb.sheet1
        log_ws.clear()
        log_ws.append_row(['Date','UID','Role','CompanyID','Type','Time','ODO','Photo','Liters','Cost','Delta_km','Personal_km'])
    return comp_ws, users_ws, log_ws
COMP_WS, USERS_WS, LOG_WS = _init_sheets()

# In-memory caches
COMPANIES = {r[0]:(r[1],r[2]) for r in COMP_WS.get_all_values()[1:]}
USERS: Dict[str, Dict[str,str]] = {r[0]: dict(zip(['Name','Car','Role','CompanyID','Status'],r[1:])) for r in USERS_WS.get_all_values()[1:]}

# Helpers
def _now(): return datetime.datetime.now(TZ).isoformat(timespec='seconds')

def _append_log(uid: str, type_: str, **flds):
    user = USERS.get(uid)
    if not user or user['Status']!='Approved': return
    row = [datetime.date.today(TZ).isoformat(), uid, user['Role'], user['CompanyID'], type_, _now()] + ['']*7
    headers = ['Date','UID','Role','CompanyID','Type','Time','ODO','Photo','Liters','Cost','Delta_km','Personal_km']
    for k,v in flds.items():
        if k in headers:
            i = headers.index(k)
            row[i] = v
    LOG_WS.append_row(row)

async def get_user_role(uid: str) -> str:
    if uid == DEFAULT_ADMIN_UID: return 'Admin'
    u = USERS.get(uid)
    if u and u['Status']=='Approved': return u['Role']
    return ''

async def send_commands(update: Update, role: str):
    await update.message.reply_text(commands_text(role))

# Registration handlers
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    role = await get_user_role(uid)
    if role:
        await update.message.reply_text(f"Вы зарегистрированы как {role}.")
        await send_commands(update, role)
        return ConversationHandler.END
    await update.message.reply_text("👤 Введите ваше ФИО:")
    return REG_NAME

async def reg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("🚗 Введите номер авто:")
    return REG_CAR

async def reg_car(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['car'] = update.message.text.strip()
    # choose company
    kb = [[InlineKeyboardButton(name, callback_data=f"company_{cid}")] for cid,(name,_) in COMPANIES.items()]
    await update.message.reply_text("🏢 Выберите компанию:", reply_markup=InlineKeyboardMarkup(kb))
    return REG_COMPANY

async def reg_company_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    uid = str(update.effective_user.id)
    cid = update.callback_query.data.split('_',1)[1]
    name = ctx.user_data.pop('name')
    car = ctx.user_data.pop('car')
    USERS_WS.append_row([uid,name,car,'Driver',cid,'Pending'])
    USERS[uid] = {'Name':name,'Car':car,'Role':'Driver','CompanyID':cid,'Status':'Pending'}
    await update.callback_query.edit_message_text("✅ Заявка отправлена. Ждите одобрения менеджера.")
    # notify managers
    for u,d in USERS.items():
        if d['Role']=='Manager' and d['CompanyID']==cid and d['Status']=='Approved':
            await ctx.bot.send_message(chat_id=u, text=f"Новая заявка: {name}, авто {car}. /approve_{uid} или /reject_{uid}")
    return ConversationHandler.END

# Approve/Reject by manager
async def approve_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid) != 'Manager': return
    target = ctx.args[0] if ctx.args else ''
    if target in USERS and USERS[target]['Status']=='Pending':
        row = list(USERS_WS.get_all_values()).index([target,*['']*5])+1
        USERS_WS.update_cell(row,6,'Approved')  # Status col
        USERS[target]['Status']='Approved'
        await update.message.reply_text(f"Пользователь {USERS[target]['Name']} одобрен.")

async def reject_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid) != 'Manager': return
    target = ctx.args[0] if ctx.args else ''
    if target in USERS and USERS[target]['Status']=='Pending':
        row = list(USERS_WS.get_all_values()).index([target,*['']*5])+1
        USERS_WS.update_cell(row,6,'Rejected')
        USERS[target]['Status']='Rejected'
        await update.message.reply_text(f"Пользователь {USERS[target]['Name']} отклонен.")

# Driver flows
async def startshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid) != 'Driver': return
    await update.message.reply_text("Укажите пробег на начало смены (км):")
    return START_ODO

async def start_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        odo_start = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Нужно ввести число. Повторите:")
        return START_ODO
    ctx.user_data['odo_start'] = odo_start
    # compute personal km since last end
    last_end = None
    for rec in reversed(LOG_WS.get_all_records()):
        if rec['UID']==ctx.user_data.get('uid') and rec['Type']=='End': last_end=int(rec['ODO']); break
    personal = odo_start - (last_end or odo_start)
    ctx.user_data['personal'] = personal
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def start_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Это не фото. Пришлите фото:")
        return START_PHOTO
    uid = str(update.effective_user.id)
    odo = ctx.user_data.pop('odo_start')
    personal = ctx.user_data.pop('personal')
    _append_log(uid,'Start',ODO=odo,Photo=update.message.photo[-1].file_id,Personal_km=personal)
    await update.message.reply_text(f"✅ Смена начата. {commands_text('Driver')}")
    return ConversationHandler.END

async def fuel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if await get_user_role(uid) != 'Driver': return
    await update.message.reply_text("Пришлите фото чека:")
    return FUEL_PHOTO

async def fuel_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото чека. Присылайте:")
        return FUEL_PHOTO
    ctx.user_data['fuel_photo'] = update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: cost=float(update.message.text)
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите сумму:")
        return FUEL_COST
    ctx.user_data['fuel_cost']=cost
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: liters=float(update.message.text)
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите литры:")
        return FUEL_LITERS
    uid=str(update.effective_user.id)
    _append_log(uid,'Fuel',Photo=ctx.user_data.pop('fuel_photo'),Cost=ctx.user_data.pop('fuel_cost'),Liters=liters)
    await update.message.reply_text(f"✅ Заправка сохранена. {commands_text('Driver')}")
    return ConversationHandler.END

async def endshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Driver': return
    await update.message.reply_text("Укажите пробег на конец смены (км):")
    return END_ODO

async def end_odo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: odo_end=int(update.message.text)
    except ValueError:
        await update.message.reply_text("Нужно число. Повторите:")
        return END_ODO
    ctx.user_data['odo_end']=odo_end
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO

async def end_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Пришлите:")
        return END_PHOTO
    uid=str(update.effective_user.id)
    odo_end=ctx.user_data.pop('odo_end')
    # find last start
    last_start=None
    for rec in reversed(LOG_WS.get_all_records()):
        if rec['UID']==uid and rec['Type']=='Start': last_start=int(rec['ODO']); break
    delta=odo_end - (last_start or odo_end)
    _append_log(uid,'End',ODO=odo_end,Photo=update.message.photo[-1].file_id,Delta_km=delta)
    # send analytics
    name=USERS[uid]['Name']
    start_odo=last_start or odo_end
    await update.message.reply_text(f"{name}, вы проехали {delta} км. Хорошего отдыха! {commands_text('Driver')}")
    return ConversationHandler.END

# Manager flows
async def onshift_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Manager': return
    # list drivers with last type Start without End
    on=[]
    for r in LOG_WS.get_all_records():
        if r['Type']=='Start': on.append(r['UID'])
        if r['Type']=='End' and r['UID'] in on: on.remove(r['UID'])
    names=[USERS[u]['Name'] for u in set(on) if USERS[u]['CompanyID']==USERS[uid]['CompanyID']]
    await update.message.reply_text("На линии: " + ", ".join(names) + f". {commands_text('Manager')}")

async def yesterday_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Manager': return
    yesterday = datetime.date.today(TZ)-datetime.timedelta(days=1)
    kms=cost=0
    for r in LOG_WS.get_all_records():
        if r['Date']==yesterday.isoformat() and r['CompanyID']==USERS[uid]['CompanyID']:
            if r['Type']=='End': kms+=int(r['Delta_km'] or 0)
            if r['Type']=='Fuel': cost+=float(r['Cost'] or 0)
    await update.message.reply_text(f"Вчера: {kms} км, потрачено {cost}₽. {commands_text('Manager')}")

# Admin flows
async def addcompany_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Admin': return
    cid,name=ctx.args[0]," ".join(ctx.args[1:])
    COMP_WS.append_row([cid,name,DEFAULT_ADMIN_UID])
    COMPANIES[cid]=(name,DEFAULT_ADMIN_UID)
    await update.message.reply_text(f"Компания {name} добавлена. {commands_text('Admin')}")

async def assignmanager_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Admin': return
    cid,mid=ctx.args[0],ctx.args[1]
    # update users_ws
    row=[r[0] for r in USERS_WS.get_all_values()].index(mid)+1
    USERS_WS.update_cell(row,4,'Manager')
    USERS[mid]['Role']='Manager'
    await update.message.reply_text(f"Manager {mid} assigned. {commands_text('Admin')}")

async def setrole_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid=str(update.effective_user.id)
    if await get_user_role(uid)!='Admin': return
    target,role=ctx.args[0],ctx.args[1]
    row=[r[0] for r in USERS_WS.get_all_values()].index(target)+1
    USERS_WS.update_cell(row,4,role)
    USERS[target]['Role']=role
    await update.message.reply_text(f"Role set. {commands_text('Admin')}")

# Main
async def main():
    if not TOKEN or not SPREADSHEET_ID or not CREDS_FILE:
        raise RuntimeError("Environment not properly set")
    app = ApplicationBuilder().token(TOKEN).build()
    # registration
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            REG_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_CAR:[MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
            REG_COMPANY:[CallbackQueryHandler(reg_company_cb, pattern='^company_')]
        },
        fallbacks=[]
    )
    app.add_handler(reg_conv)
    # approve/reject
    app.add_handler(CommandHandler('approve', approve_cmd))
    app.add_handler(CommandHandler('reject', reject_cmd))
    # driver convs
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('startshift', startshift_cmd)],
        states={START_ODO:[MessageHandler(filters.TEXT, start_odo)], START_PHOTO:[MessageHandler(filters.PHOTO, start_photo)]},
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('fuel', fuel_cmd)],
        states={FUEL_PHOTO:[MessageHandler(filters.PHOTO, fuel_photo)], FUEL_COST:[MessageHandler(filters.TEXT, fuel_cost)], FUEL_LITERS:[MessageHandler(filters.TEXT, fuel_liters)]},
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('endshift', endshift_cmd)],
        states={END_ODO:[MessageHandler(filters.TEXT, end_odo)], END_PHOTO:[MessageHandler(filters.PHOTO, end_photo)]},
        fallbacks=[]
    ))
    # manager
    app.add_handler(CommandHandler('onshift', onshift_cmd))
    app.add_handler(CommandHandler('yesterday', yesterday_cmd))
    # admin
    app.add_handler(CommandHandler('addcompany', addcompany_cmd))
    app.add_handler(CommandHandler('assignmanager', assignmanager_cmd))
    app.add_handler(CommandHandler('setrole', setrole_cmd))

    print("🔄 Bot polling started")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__=='__main__':
    asyncio.run(main())















