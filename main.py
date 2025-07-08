# -*- coding: utf-8 -*-
"""
main.py — Topkon Bot «из коробки»

Роли: Admin, Руководитель, Водитель
Регистрация + выбор роли и компании
Начало смены (пробег → фото), Заправка (фото → сумма → литры), Конец смены (пробег → фото)
Admin: /addcompany — добавить компанию
Меню после каждого шага, всегда отвечает на всё
Flask-заглушка для Render
"""
import os, sys, subprocess, threading, datetime, asyncio
from zoneinfo import ZoneInfo
from typing import Dict, Optional

# Авто‑pip при необходимости
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
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound

# Конфиг из окружения
TOKEN = os.getenv("TOKEN", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
TZ = ZoneInfo("Europe/Moscow")
ADMIN_UID = '1881053841'

# Состояния
(
    ROLE_SELECT, REG_COMPANY, REG_NAME, REG_CAR,
    ADDCOMPANY,
    START_ODO, START_PHOTO,
    FUEL_PHOTO, FUEL_COST, FUEL_LITERS,
    END_ODO, END_PHOTO
) = range(12)

# Шапка лога
HEADER = [
    "Дата","UID","Роль","Компания","ФИО","Авто",
    "Тип","Время","ОДО","Фото","Сумма","Литры","Δ_км","Личный_км"
]
IDX = {h:i for i,h in enumerate(HEADER)}

# Flask-заглушка

def _fake_web():
    app = Flask(__name__)
    @app.get('/')
    def ping(): return "OK",200
    app.run(host='0.0.0.0', port=8080)
threading.Thread(target=_fake_web, daemon=True).start()

# Инициализация Google Sheets

def init_sheets():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS, scope)
    gc = gspread.authorize(creds)
    wb = gc.open_by_key(SPREADSHEET_ID)
    # Лог
    log_ws = wb.sheet1
    if log_ws.row_values(1) != HEADER:
        log_ws.clear(); log_ws.append_row(HEADER)
    # Users
    try: usr_ws = wb.worksheet('Users')
    except WorksheetNotFound:
        usr_ws = wb.add_worksheet('Users',1000,5)
        usr_ws.append_row(["UID","Роль","Компания","Авто","ФИО"])
    # Companies
    try: comp_ws = wb.worksheet('Companies')
    except WorksheetNotFound:
        comp_ws = wb.add_worksheet('Companies',1000,2)
        comp_ws.append_row(["Компания","ManagerUID"])
    return log_ws, usr_ws, comp_ws

LOG_WS, USR_WS, COMP_WS = init_sheets()

# Словари в памяти
USERS: Dict[str,Dict] = {}
for r in USR_WS.get_all_values()[1:]:
    if len(r)>=5 and r[0].isdigit():
        uid,role,company,car,name = r[:5]
        USERS[uid] = {'role':role,'company':company,'car':car,'name':name}

COMPANIES: Dict[str,str] = {}
for r in COMP_WS.get_all_values()[1:]:
    if len(r)>=1:
        comp = r[0]; mgr = r[1] if len(r)>1 else ''
        COMPANIES[comp] = mgr

# Вспомогалки

def now_iso(): return datetime.datetime.now(TZ).isoformat(timespec='seconds')

def append_log(uid:str, **f):
    row = [""]*len(HEADER)
    row[IDX['Дата']] = datetime.date.today(TZ).isoformat()
    row[IDX['UID']] = uid
    info = USERS.get(uid,{})
    row[IDX['Роль']] = info.get('role','')
    row[IDX['Компания']] = info.get('company','')
    row[IDX['ФИО']] = info.get('name','')
    row[IDX['Авто']] = info.get('car','')
    row[IDX['Тип']] = f.get('Тип','')
    row[IDX['Время']] = now_iso()
    for k,v in f.items():
        if k in IDX:
            row[IDX[k]] = str(v)
    LOG_WS.append_row(row)


def last_odo(uid:str, only:Optional[str]=None)->int:
    for rec in reversed(LOG_WS.get_all_records()):
        if str(rec['UID'])==uid and (only is None or rec['Тип']==only):
            try: return int(rec['ОДО'])
            except: pass
    return 0


def menu_kb(uid:str):
    kb = []
    role = USERS.get(uid,{}).get('role')
    cmds = ['/start','/help']
    if role=='Admin': cmds+=['/addcompany']
    if role in ('Водитель','Руководитель'): cmds+=['/startshift','/fuel','/endshift']
    return ReplyKeyboardMarkup([cmds],resize_keyboard=True)

async def ensure_reg(update:Update)->bool:
    uid=str(update.effective_user.id)
    if uid in USERS: return True
    await update.message.reply_text("Пожалуйста, зарегистрируйтесь: /start")
    return False

# Handlers

async def cmd_start(update:Update,ctx):
    uid=str(update.effective_user.id)
    if uid==ADMIN_UID and uid not in USERS:
        USERS[uid]={'role':'Admin','company':'','car':'','name':'Admin'}
        USR_WS.append_row([uid,'Admin','','','Admin'])
    if uid in USERS:
        await update.message.reply_text(
            f"Привет, {USERS[uid]['name']}! Выберите команду:",
            reply_markup=menu_kb(uid)
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Добро пожаловать! Выберите роль:",
        reply_markup=ReplyKeyboardMarkup([['Водитель','Руководитель']],resize_keyboard=True)
    )
    return ROLE_SELECT

async def role_select(update:Update,ctx):
    role=update.message.text.strip()
    if role not in ('Водитель','Руководитель'):
        await update.message.reply_text("Пожалуйста выберите Водитель или Руководитель.")
        return ROLE_SELECT
    ctx.user_data['role']=role
    await update.message.reply_text(
        "Введите компанию (ООО/ИП/АО 'Название'):",reply_markup=ReplyKeyboardRemove()
    )
    return REG_COMPANY

async def reg_company(update:Update,ctx):
    comp=update.message.text.strip()
    role=ctx.user_data['role']; uid=str(update.effective_user.id)
    if role=='Водитель' and comp not in COMPANIES:
        await update.message.reply_text(
            "Компания не найдена. Обратитесь к руководителю или администратору."
        )
        return REG_COMPANY
    ctx.user_data['company']=comp
    await update.message.reply_text("Введите ваше ФИО:")
    return REG_NAME

async def reg_name(update:Update,ctx):
    name=update.message.text.strip()
    ctx.user_data['name']=name
    role=ctx.user_data['role']
    if role=='Водитель':
        await update.message.reply_text("Введите номер авто:")
        return REG_CAR
    # Руководитель
    uid=str(update.effective_user.id)
    USERS[uid]={'role':'Руководитель','company':ctx.user_data['company'],'car':'','name':name}
    USR_WS.append_row([uid,'Руководитель',ctx.user_data['company'],'',name])
    # связать менеджера с компанией
    COMP_WS.append_row([ctx.user_data['company'],uid])
    COMPANIES[ctx.user_data['company']]=uid
    await update.message.reply_text(
        "✅ Вы зарегистрированы как Руководитель.",reply_markup=menu_kb(uid)
    )
    return ConversationHandler.END

async def reg_car(update:Update,ctx):
    car=update.message.text.strip(); uid=str(update.effective_user.id)
    USERS[uid]={'role':'Водитель','company':ctx.user_data['company'],'car':car,'name':ctx.user_data['name']}
    USR_WS.append_row([uid,'Водитель',ctx.user_data['company'],car,ctx.user_data['name']])
    await update.message.reply_text(
        "✅ Регистрация завершена.",reply_markup=menu_kb(uid)
    )
    return ConversationHandler.END

# /addcompany
async def addcompany_cmd(update:Update,ctx):
    uid=str(update.effective_user.id)
    if USERS.get(uid,{}).get('role')!='Admin':
        await update.message.reply_text("Нет прав." ,reply_markup=menu_kb(uid)); return
    await update.message.reply_text("Введите название компании (ООО/...):",reply_markup=ReplyKeyboardRemove())
    return ADDCOMPANY

async def addcompany_input(update:Update,ctx):
    comp=update.message.text.strip(); uid=str(update.effective_user.id)
    COMP_WS.append_row([comp, ''])
    COMPANIES[comp]=''
    await update.message.reply_text(
        f"✅ Компания '{comp}' добавлена.",reply_markup=menu_kb(uid)
    )
    return ConversationHandler.END

# /startshift
async def startshift_cmd(update:Update,ctx):
    if not await ensure_reg(update): return
    uid=str(update.effective_user.id)
    if USERS[uid]['role']!='Водитель':
        await update.message.reply_text("Только водители.",reply_markup=menu_kb(uid)); return
    await update.message.reply_text("Укажите пробег на начало смены (км):",reply_markup=menu_kb(uid))
    return START_ODO

async def start_odo(update:Update,ctx):
    uid=str(update.effective_user.id)
    try: v=int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число. Повторите:"); return START_ODO
    ctx.user_data['start_odo']=v
    await update.message.reply_text("Пришлите фото одометра:")
    return START_PHOTO

async def start_photo(update:Update,ctx):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Пришлите одометр:"); return START_PHOTO
    uid=str(update.effective_user.id); fid=update.message.photo[-1].file_id
    odo=ctx.user_data.pop('start_odo'); prev=last_odo(uid,'End'); personal=odo-prev
    append_log(uid, Тип='Start', ОДО=odo, Фото=fid, Личный_км=personal)
    await update.message.reply_text(
        f"✅ Смена начата. Пробег вне смены: {personal} км.",reply_markup=menu_kb(uid)
    )
    return ConversationHandler.END

# /fuel
async def fuel_cmd(update:Update,ctx):
    if not await ensure_reg(update): return
    uid=str(update.effective_user.id)
    if USERS[uid]['role']!='Водитель':
        await update.message.reply_text("Только водители.",reply_markup=menu_kb(uid)); return
    await update.message.reply_text("Пришлите фото чека:",reply_markup=menu_kb(uid))
    return FUEL_PHOTO

async def fuel_photo(update:Update,ctx):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото чека."); return FUEL_PHOTO
    ctx.user_data['f_photo']=update.message.photo[-1].file_id
    await update.message.reply_text("Введите сумму (₽):")
    return FUEL_COST

async def fuel_cost(update:Update,ctx):
    try: c=float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число."); return FUEL_COST
    ctx.user_data['f_cost']=c
    await update.message.reply_text("Введите литры:")
    return FUEL_LITERS

async def fuel_liters(update:Update,ctx):
    uid=str(update.effective_user.id)
    try: l=float(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число."); return FUEL_LITERS
    append_log(uid, Тип='Fuel', Фото=ctx.user_data.pop('f_photo'), Сумма=ctx.user_data.pop('f_cost'), Литры=l)
    await update.message.reply_text("✅ Заправка сохранена.",reply_markup=menu_kb(uid))
    return ConversationHandler.END

# /endshift
async def endshift_cmd(update:Update,ctx):
    if not await ensure_reg(update): return
    uid=str(update.effective_user.id)
    if USERS[uid]['role']!='Водитель':
        await update.message.reply_text("Только водители.",reply_markup=menu_kb(uid)); return
    await update.message.reply_text("Укажите пробег на конец смены (км):",reply_markup=menu_kb(uid))
    return END_ODO

async def end_odo(update:Update,ctx):
    uid=str(update.effective_user.id)
    try: v=int(update.message.text.replace(',','.'))
    except:
        await update.message.reply_text("Нужно число."); return END_ODO
    ctx.user_data['end_odo']=v
    await update.message.reply_text("Пришлите фото одометра:")
    return END_PHOTO

async def end_photo(update:Update,ctx):
    if not update.message.photo:
        await update.message.reply_text("Нужно фото."); return END_PHOTO
    uid=str(update.effective_user.id); fid=update.message.photo[-1].file_id
    odo=ctx.user_data.pop('end_odo'); prev=last_odo(uid,'Start'); delta=odo-prev
    append_log(uid, Тип='End', ОДО=odo, Фото=fid, Δ_км=delta)
    await update.message.reply_text(
        f"✅ Смена завершена. Вы проехали {delta} км. Хорошего отдыха!",reply_markup=menu_kb(uid)
    )
    return ConversationHandler.END

# /help
async def help_cmd(update:Update,ctx):
    uid=str(update.effective_user.id)
    await update.message.reply_text("Выберите команду:",reply_markup=menu_kb(uid))

# fallback
async def unknown(update:Update,ctx):
    uid=str(update.effective_user.id)
    await update.message.reply_text(
        "Извините, я не понял. Пожалуйста, выберите команду:",
        reply_markup=menu_kb(uid)
    )

# Main

def main():
    if not TOKEN: raise RuntimeError("TOKEN env var not set")
    app = ApplicationBuilder().token(TOKEN).build()
    # регистрация
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler('start',cmd_start)],
            states={
                ROLE_SELECT:[MessageHandler(filters.TEXT&~filters.COMMAND,role_select)],
                REG_COMPANY:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_company)],
                REG_NAME:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_name)],
                REG_CAR:[MessageHandler(filters.TEXT&~filters.COMMAND,reg_car)],
                ADDCOMPANY:[MessageHandler(filters.TEXT&~filters.COMMAND,addcompany_input)],
            },
            fallbacks=[CommandHandler('start',cmd_start)]
        )
    )
    # прочие
    app.add_handler(CommandHandler('addcompany',addcompany_cmd))
    app.add_handler(CommandHandler('startshift',startshift_cmd))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('startshift',startshift_cmd)],
        states={START_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,start_odo)],
                START_PHOTO:[MessageHandler(filters.PHOTO,start_photo)]},
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('fuel',fuel_cmd)],
        states={FUEL_PHOTO:[MessageHandler(filters.PHOTO,fuel_photo)],
                FUEL_COST:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_cost)],
                FUEL_LITERS:[MessageHandler(filters.TEXT&~filters.COMMAND,fuel_liters)]},
        fallbacks=[]
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('endshift',endshift_cmd)],
        states={END_ODO:[MessageHandler(filters.TEXT&~filters.COMMAND,end_odo)],
                END_PHOTO:[MessageHandler(filters.PHOTO,end_photo)]},
        fallbacks=[]
    ))
    app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(MessageHandler(filters.ALL,unknown))
    print("🔄 Bot polling started",flush=True)
    asyncio.run(app.initialize())
    app.run_polling()

if __name__=='__main__':
    main()





















