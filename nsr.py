
import asyncio
import html
import os
import random
import re
import sqlite3
import time
 
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
 
# ---------------------------------------------------------------- إعدادات
API_ID = 38874426
API_HASH = "74e2a6fe88d1580edb2bdcffbf454b16"
BOT_TOKEN = "9182978991:AAHTaUEXzaYD7uQhYKUw-dBpcnmxr6kUEgE"
FERNET = Fernet("eQQqLeBIJk3MvObR3nYVNRNwVT_gJ0w7rNFuPM1gS2E=".encode())
ADMIN_IDS = {8462628086}
 
MIN_INTERVAL = 1  # اقل شي دقيقة واحدة
PAGE_SIZE = 8
 
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
 
# ---------------------------------------------------------------- قاعدة البيانات
db = sqlite3.connect("data.db")
db.executescript(
    """
create table if not exists users(
    uid integer primary key,
    username text, first_name text,
    session text, message text,
    interval integer default 60,
    active integer default 0,
    banned integer default 0,
    next_run real default 0,
    sent_total integer default 0
);
create table if not exists targets(
    uid integer, chat_id integer, title text,
    primary key(uid, chat_id)
);
create table if not exists fsub(
    chat_id text primary key, title text, url text
);
create table if not exists settings(
    key text primary key, value text
);
create table if not exists plans(
    id text primary key, label text, days integer, stars integer
);
create table if not exists payments(
    uid integer, plan_id text, stars integer, ts real
);
"""
)
try:
    db.execute("alter table users add column sub_until real default 0")
except sqlite3.OperationalError:
    pass
db.executemany(
    "insert or ignore into plans(id, label, days, stars) values(?,?,?,?)",
    [
        ("day", "يوم", 1, None),
        ("week", "أسبوع", 7, None),
        ("month", "شهر", 30, None),
        ("year", "سنة", 365, None),
    ],
)
db.commit()
 
running = set()
cache = {}  # uid -> [(chat_id, title), ...] كروبات الحساب
 
 
def get_setting(key, default=None):
    row = db.execute("select value from settings where key=?", (key,)).fetchone()
    return row[0] if row else default
 
 
def set_setting(key, value):
    db.execute(
        "insert into settings(key, value) values(?,?) "
        "on conflict(key) do update set value=excluded.value",
        (key, str(value)),
    )
    db.commit()
 
 
def is_paid_mode():
    return get_setting("paid_mode", "0") == "1"
 
 
def get_sub_until(uid):
    row = db.execute("select sub_until from users where uid=?", (uid,)).fetchone()
    return row[0] if row else 0
 
 
def subscription_active(uid):
    if not is_paid_mode():
        return True
    return get_sub_until(uid) > time.time()
 
 
def fmt_date(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts and ts > 0 else "-"
 
 
def get_user(uid, username="", first_name=""):
    db.execute(
        "insert or ignore into users(uid, username, first_name) values(?,?,?)",
        (uid, username, first_name),
    )
    db.execute(
        "update users set username=?, first_name=? where uid=?", (username, first_name, uid)
    )
    db.commit()
    row = db.execute(
        "select session, message, interval, active, banned from users where uid=?", (uid,)
    ).fetchone()
    return row  # session, message, interval, active, banned
 
 
def user_client(enc_session):
    s = FERNET.decrypt(enc_session.encode()).decode()
    return TelegramClient(StringSession(s), API_ID, API_HASH)
 
 
class Form(StatesGroup):
    phone = State()
    code = State()
    password = State()
    message = State()
    interval = State()
    admin_broadcast = State()
    admin_channel = State()
    admin_planprice = State()
    admin_extend_sub = State()
 
 
login_tmp = {}  # uid -> {"client":..., "phone":..., "hash":...}
 
 
# ---------------------------------------------------------------- الأزرار (ملونة وبدون ايموجي)
# اللون يتحدد من اول ايموجي بالنص ثم الايموجيات تنشال من النص:
# 🔵 ازرق | 🟢 اخضر | 🔴 احمر | ✅ اخضر | بدون ايموجي = لون عادي
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\u25A0-\u25FF\uFE0F\u200d]+"
)
STYLE_MAP = {"🔵": "primary", "🟢": "success", "🔴": "danger", "✅": "success"}
 
 
def kb(rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*row)
    return b.as_markup()
 
 
def btn(text, data=None, url=None):
    style = None
    for ch, s in STYLE_MAP.items():
        if text.startswith(ch):
            style = s
            break
    clean = EMOJI_RE.sub("", text).strip()
    return InlineKeyboardButton(text=clean, callback_data=data, url=url, style=style)
 
 
# ---------------------------------------------------------------- اشتراك إجباري
async def missing_channels(uid):
    chans = db.execute("select chat_id, title, url from fsub").fetchall()
    missing = []
    for chat_id, title, url in chans:
        try:
            cid = int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id
            member = await bot.get_chat_member(cid, uid)
            if member.status in ("left", "kicked"):
                missing.append((chat_id, title, url))
        except Exception:
            # البوت ماكو أدمن بالقناة أو خطأ -> نتجاوزها بدل ما نوقف الكل
            continue
    return missing
 
 
async def require_fsub(uid):
    missing = await missing_channels(uid)
    if not missing:
        return True
    b2 = InlineKeyboardBuilder()
    for chat_id, title, url in missing:
        b2.row(btn(title, url=url or "https://t.me"))
    b2.row(btn("🟢 تحققت، كملني", "fsub:check"))
    await bot.send_message(
        uid, "⚠️ لازم تشترك بالقنوات/الكروبات هذي قبل تسوي أي شي بالبوت:", reply_markup=b2.as_markup()
    )
    return False
 
 
# ---------------------------------------------------------------- القوائم
def main_menu(uid, active):
    rows = []
    if is_paid_mode():
        ok = subscription_active(uid)
        if ok:
            rows.append([btn("🟢 الاشتراك: نشط", "sub:menu")])
        else:
            rows.append([btn("🔴 الاشتراك: منتهي/غير مفعّل", "sub:menu")])
    rows += [
        [btn("🔵 تسجيل حساب", "login")],
        [btn("🔵 اختيار الكروبات", "groups:0:1")],
        [btn("🔵 تحديد الرسالة", "setmsg"), btn("🔵 تحديد الوقت", "setint")],
        [btn("🔴 إيقاف" if active else "🟢 تشغيل", "toggle")],
        [btn("🔴 تسجيل خروج وحذف الجلسة", "logout")],
    ]
    if uid in ADMIN_IDS:
        rows.append([btn("🔴 لوحة الأدمن", "admin:open")])
    return kb(rows)
 
 
async def send_menu(uid):
    session, message, interval, active, banned = get_user(uid)
    if banned:
        await bot.send_message(uid, "🚫 تم حظرك من استخدام هذا البوت.")
        return
    n = db.execute("select count(*) from targets where uid=?", (uid,)).fetchone()[0]
    text = (
        "<b>لوحة النشر التلقائي</b>\n\n"
        f"الحساب: {'✅ مسجّل' if session else '❌ غير مسجّل'}\n"
        f"الكروبات المختارة: {n}\n"
        f"الرسالة: {'✅ محددة' if message else '❌ غير محددة'}\n"
        f"كل: {interval} دقيقة\n"
        f"الحالة: {'🟢 شغّال' if active else '🔴 متوقف'}"
    )
    if is_paid_mode():
        text += f"\nالاشتراك ينتهي: {fmt_date(get_sub_until(uid))}"
    await bot.send_message(uid, text, reply_markup=main_menu(uid, active))
 
 
# ---------------------------------------------------------------- /start
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    get_user(uid, m.from_user.username or "", m.from_user.first_name or "")
    session, message, interval, active, banned = get_user(uid)
    if banned:
        await m.answer("🚫 تم حظرك من استخدام هذا البوت.")
        return
    if not await require_fsub(uid):
        return
    await send_menu(uid)
 
 
@router.callback_query(F.data == "fsub:check")
async def fsub_check(c: CallbackQuery):
    uid = c.from_user.id
    missing = await missing_channels(uid)
    if missing:
        await c.answer("لسه ما اشتركت بالكل ❌", show_alert=True)
        return
    await c.message.delete()
    await send_menu(uid)
 
 
@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()
 
 
# ---------------------------------------------------------------- القائمة الرئيسية
@router.callback_query(F.data == "login")
async def cb_login(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.phone)
    await c.message.answer(
        "ارسل رقمك مع رمز الدولة، مثال: +964770xxxxxxx",
        reply_markup=kb([[btn("🔴 إلغاء", "cancel")]]),
    )
    await c.answer()
 
 
@router.callback_query(F.data == "setmsg")
async def cb_setmsg(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.message)
    await c.message.answer(
        "ارسل نص الرسالة اللي تريد تنشرها (بدون وسوم HTML، التنسيق راح يجي بأزرار بعدين).",
        reply_markup=kb([[btn("🔴 إلغاء", "cancel")]]),
    )
    await c.answer()
 
 
@router.callback_query(F.data == "setint")
async def cb_setint(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.interval)
    await c.message.answer(
        f"ارسل الوقت بالدقائق بين كل دورة نشر (الحد الأدنى {MIN_INTERVAL} دقيقة).",
        reply_markup=kb([[btn("🔴 إلغاء", "cancel")]]),
    )
    await c.answer()
 
 
@router.callback_query(F.data == "toggle")
async def cb_toggle(c: CallbackQuery):
    uid = c.from_user.id
    session, message, interval, active, banned = get_user(uid)
    n = db.execute("select count(*) from targets where uid=?", (uid,)).fetchone()[0]
    if not active and not (session and message and n):
        await c.answer("لازم تسجل حساب وتحدد رسالة وكروب واحد على الأقل.", show_alert=True)
        return
    if not active and not subscription_active(uid):
        await c.answer("اشتراكك منتهي أو غير مفعّل. جدده أول من زر الاشتراك.", show_alert=True)
        return
    db.execute("update users set active=?, next_run=? where uid=?", (0 if active else 1, 0, uid))
    db.commit()
    await c.message.delete()
    await send_menu(uid)
 
 
@router.callback_query(F.data == "logout")
async def cb_logout(c: CallbackQuery):
    uid = c.from_user.id
    session = get_user(uid)[0]
    if session:
        try:
            async with user_client(session) as cl:
                await cl.log_out()
        except Exception:
            pass
    db.execute("update users set session=null, active=0 where uid=?", (uid,))
    db.execute("delete from targets where uid=?", (uid,))
    db.commit()
    cache.pop(uid, None)
    await c.message.delete()
    await send_menu(uid)
 
 
@router.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    login_tmp.pop(c.from_user.id, None)
    await c.message.delete()
    await send_menu(c.from_user.id)
 
 
# ---------------------------------------------------------------- الاشتراك (نجوم تلجرام)
@router.callback_query(F.data == "sub:menu")
async def sub_menu(c: CallbackQuery):
    uid = c.from_user.id
    if not is_paid_mode():
        await c.answer("البوت مجاني حالياً، ماكو حاجة لاشتراك.", show_alert=True)
        return
    plans = db.execute("select id, label, days, stars from plans").fetchall()
    rows = []
    for pid, label, days, stars in plans:
        if stars:
            rows.append([btn(f"🔵 {label} - {stars} نجمة", f"sub:buy:{pid}")])
    if not rows:
        await c.answer("الأسعار لسه ما انحددت من الأدمن.", show_alert=True)
        return
    rows.append([btn("رجوع", "done")])
    text = f"💳 <b>الاشتراك</b>\nينتهي حالياً: {fmt_date(get_sub_until(uid))}\n\nاختر المدة:"
    try:
        await c.message.edit_text(text, reply_markup=kb(rows))
    except Exception:
        await c.message.answer(text, reply_markup=kb(rows))
    await c.answer()
 
 
@router.callback_query(F.data.startswith("sub:buy:"))
async def sub_buy(c: CallbackQuery):
    pid = c.data.split(":")[2]
    row = db.execute("select label, days, stars from plans where id=?", (pid,)).fetchone()
    if not row or not row[2]:
        await c.answer("هذا الخيار غير متوفر حالياً.", show_alert=True)
        return
    label, days, stars = row
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=f"اشتراك {label}",
        description=f"تفعيل النشر التلقائي لمدة {label} ({days} يوم).",
        payload=f"sub:{pid}",
        currency="XTR",
        prices=[LabeledPrice(label=f"اشتراك {label}", amount=stars)],
    )
    await c.answer()
 
 
@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pcq.id, ok=True)
 
 
@router.message(F.successful_payment)
async def on_successful_payment(m: Message):
    payload = m.successful_payment.invoice_payload
    stars = m.successful_payment.total_amount
    if not payload.startswith("sub:"):
        return
    pid = payload.split(":", 1)[1]
    row = db.execute("select label, days from plans where id=?", (pid,)).fetchone()
    if not row:
        return
    label, days = row
    uid = m.from_user.id
    current = get_sub_until(uid)
    base = current if current > time.time() else time.time()
    new_until = base + days * 86400
    db.execute("update users set sub_until=? where uid=?", (new_until, uid))
    db.execute(
        "insert into payments(uid, plan_id, stars, ts) values(?,?,?,?)", (uid, pid, stars, time.time())
    )
    db.commit()
    await m.answer(f"✅ تم تفعيل اشتراك {label}. ينتهي: {fmt_date(new_until)}")
    await send_menu(uid)
 
 
# ---------------------------------------------------------------- اختيار الكروبات
@router.callback_query(F.data.startswith("groups:"))
async def cb_groups(c: CallbackQuery):
    _, page, refresh = c.data.split(":")
    await show_groups(c, int(page), refresh == "1")
 
 
async def show_groups(c: CallbackQuery, page, refresh):
    uid = c.from_user.id
    session = get_user(uid)[0]
    if not session:
        await c.answer("سجّل حسابك أول.", show_alert=True)
        return
    if refresh or uid not in cache:
        await c.answer("جاري جلب الكروبات...")
        async with user_client(session) as cl:
            dialogs = await cl.get_dialogs()
            cache[uid] = [(d.id, d.title) for d in dialogs if d.is_group]
    groups = cache[uid]
    chosen = {r[0] for r in db.execute("select chat_id from targets where uid=?", (uid,))}
    chunk = groups[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    # المختار يطلع اخضر (✅)، غير المختار لون عادي
    rows = [[btn(("✅ " if i in chosen else "") + (t or "?")[:30], f"t:{i}:{page}")] for i, t in chunk]
    nav = []
    if page > 0:
        nav.append(btn("السابق", f"groups:{page-1}:0"))
    if (page + 1) * PAGE_SIZE < len(groups):
        nav.append(btn("التالي", f"groups:{page+1}:0"))
    if nav:
        rows.append(nav)
    rows.append([btn("🟢 تم", "done")])
    try:
        await c.message.edit_text(f"اختر الكروبات ({len(groups)} كروب):", reply_markup=kb(rows))
    except Exception:
        await c.message.answer(f"اختر الكروبات ({len(groups)} كروب):", reply_markup=kb(rows))
 
 
@router.callback_query(F.data.startswith("t:"))
async def cb_target(c: CallbackQuery):
    uid = c.from_user.id
    _, chat_id, page = c.data.split(":")
    chat_id = int(chat_id)
    exists = db.execute("select 1 from targets where uid=? and chat_id=?", (uid, chat_id)).fetchone()
    if exists:
        db.execute("delete from targets where uid=? and chat_id=?", (uid, chat_id))
    else:
        title = next((t for i, t in cache.get(uid, []) if i == chat_id), str(chat_id))
        db.execute("insert into targets values(?,?,?)", (uid, chat_id, title))
    db.commit()
    await show_groups(c, int(page), False)
 
 
@router.callback_query(F.data == "done")
async def cb_done(c: CallbackQuery):
    await c.message.delete()
    await send_menu(c.from_user.id)
 
 
# ---------------------------------------------------------------- تسجيل الدخول (Telethon)
@router.message(Form.phone)
async def on_phone(m: Message, state: FSMContext):
    uid = m.from_user.id
    phone = m.text.strip()
    cl = TelegramClient(StringSession(), API_ID, API_HASH)
    await cl.connect()
    try:
        sent = await cl.send_code_request(phone)
    except PhoneNumberInvalidError:
        await cl.disconnect()
        await m.answer("الرقم غير صحيح، جرّب مرة ثانية.")
        return
    login_tmp[uid] = {"client": cl, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Form.code)
    await m.answer(
        "وصلك كود من تلجرام. ارسله <b>مفصول بمسافات</b> مثل: <code>1 2 3 4 5</code>\n"
        "(تلجرام يلغي الكود اذا انرسل متصل)."
    )
 
 
@router.message(Form.code)
async def on_code(m: Message, state: FSMContext):
    uid = m.from_user.id
    code = "".join(ch for ch in m.text if ch.isdigit())
    d = login_tmp.get(uid)
    if not d:
        await state.clear()
        await m.answer("انتهت الجلسة، ابدأ تسجيل الدخول من جديد.")
        return
    try:
        await d["client"].sign_in(d["phone"], code, phone_code_hash=d["hash"])
    except SessionPasswordNeededError:
        await state.set_state(Form.password)
        await m.answer("حسابك عليه تحقق بخطوتين. ارسل كلمة المرور.")
        return
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await d["client"].disconnect()
        login_tmp.pop(uid, None)
        await state.clear()
        await m.answer("الكود غلط أو منتهي. اضغط تسجيل حساب وجرّب من جديد.")
        return
    await finish_login(m, uid, state)
 
 
@router.message(Form.password)
async def on_password(m: Message, state: FSMContext):
    uid = m.from_user.id
    d = login_tmp.get(uid)
    if not d:
        await state.clear()
        await m.answer("انتهت الجلسة، ابدأ تسجيل الدخول من جديد.")
        return
    try:
        await d["client"].sign_in(password=m.text)
    except PasswordHashInvalidError:
        await m.answer("كلمة المرور غلط، جرّب مرة ثانية.")
        return
    try:
        await m.delete()  # نحذف رسالة الباسورد من الشات
    except Exception:
        pass
    await finish_login(m, uid, state)
 
 
async def finish_login(m: Message, uid, state: FSMContext):
    d = login_tmp.pop(uid)
    s = d["client"].session.save()
    await d["client"].disconnect()
    enc = FERNET.encrypt(s.encode()).decode()
    db.execute("update users set session=? where uid=?", (enc, uid))
    db.commit()
    await state.clear()
    cache.pop(uid, None)
    await m.answer("✅ تم تسجيل الحساب.")
    await send_menu(uid)
 
 
# ---------------------------------------------------------------- الرسالة + تنسيق
@router.message(Form.message)
async def on_message_text(m: Message, state: FSMContext):
    await state.update_data(raw=m.text, formats=[])
    await show_format_preview(m.from_user.id, state, m.answer)
 
 
def render_formatted(raw, formats):
    s = html.escape(raw)
    for tag in formats:
        s = f"<{tag}>{s}</{tag}>"
    return s
 
 
async def show_format_preview(uid, state: FSMContext, sender):
    data = await state.get_data()
    raw, formats = data.get("raw", ""), data.get("formats", [])
    preview = render_formatted(raw, formats)
 
    def mark(tag, label):
        return (("✅ " if tag in formats else "") + label, f"fmt:{tag}")
 
    rows = []
    t1, d1 = mark("b", "عريض")
    t2, d2 = mark("i", "مائل")
    rows.append([btn(t1, d1), btn(t2, d2)])
    t3, d3 = mark("u", "تحته خط")
    t4, d4 = mark("s", "شطب")
    rows.append([btn(t3, d3), btn(t4, d4)])
    rows.append([btn("🔴 مسح التنسيق", "fmt:clear")])
    rows.append([btn("🟢 حفظ الرسالة", "fmt:save"), btn("🔴 إلغاء", "cancel")])
    await sender(f"معاينة الرسالة:\n\n{preview}", reply_markup=kb(rows))
 
 
@router.callback_query(F.data.startswith("fmt:"), Form.message)
async def cb_fmt(c: CallbackQuery, state: FSMContext):
    tag = c.data.split(":")[1]
    data = await state.get_data()
    formats = data.get("formats", [])
    if tag == "clear":
        formats = []
    elif tag == "save":
        raw = data.get("raw", "")
        final = render_formatted(raw, formats)
        db.execute("update users set message=? where uid=?", (final, c.from_user.id))
        db.commit()
        await state.clear()
        await c.message.delete()
        await send_menu(c.from_user.id)
        await c.answer()
        return
    else:
        formats = formats[:]
        if tag in formats:
            formats.remove(tag)
        else:
            formats.append(tag)
    await state.update_data(formats=formats)
    await c.message.delete()
    await show_format_preview(c.from_user.id, state, c.message.answer)
    await c.answer()
 
 
# ---------------------------------------------------------------- الفترة الزمنية
@router.message(Form.interval)
async def on_interval(m: Message, state: FSMContext):
    text = m.text.strip()
    if not text.isdigit() or int(text) < MIN_INTERVAL:
        await m.answer(f"ارسل رقم صحيح، {MIN_INTERVAL} أو أكثر.")
        return
    db.execute("update users set interval=? where uid=?", (int(text), m.from_user.id))
    db.commit()
    await state.clear()
    await send_menu(m.from_user.id)
 
 
# ---------------------------------------------------------------- لوحة الأدمن
def admin_only(uid):
    return uid in ADMIN_IDS
 
 
@router.callback_query(F.data == "admin:open")
async def admin_open(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    rows = [
        [btn("🔵 المستخدمين", "admin:users:0")],
        [btn("🔵 إحصائيات", "admin:stats")],
        [btn("🟢 إشعار عام", "admin:broadcast")],
        [btn("🔵 قنوات الاشتراك الإجباري", "admin:channels")],
        [btn("🔵 إعدادات الاشتراك المدفوع", "admin:sub")],
        [btn("رجوع", "done")],
    ]
    await c.message.edit_text("🛡 <b>لوحة الأدمن</b>", reply_markup=kb(rows))
    await c.answer()
 
 
@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    page = int(c.data.split(":")[2])
    users = db.execute(
        "select uid, username, first_name, active, banned from users order by uid"
    ).fetchall()
    chunk = users[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for uid, username, first_name, active, banned in chunk:
        # محظور = احمر، شغّال = اخضر، متوقف = لون عادي
        prefix = "🔴 " if banned else ("🟢 " if active else "")
        label = f"{prefix}{first_name or ''} @{username or '-'} ({uid})"
        rows.append([btn(label[:60], f"admin:user:{uid}")])
    nav = []
    if page > 0:
        nav.append(btn("السابق", f"admin:users:{page-1}"))
    if (page + 1) * PAGE_SIZE < len(users):
        nav.append(btn("التالي", f"admin:users:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("رجوع", "admin:open")])
    await c.message.edit_text(f"👥 المستخدمين ({len(users)}):", reply_markup=kb(rows))
    await c.answer()
 
 
@router.callback_query(F.data.startswith("admin:user:"))
async def admin_user_detail(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    uid = int(c.data.split(":")[2])
    row = db.execute(
        "select username, first_name, message, interval, active, banned, sent_total, sub_until "
        "from users where uid=?",
        (uid,),
    ).fetchone()
    if not row:
        await c.answer("غير موجود.", show_alert=True)
        return
    username, first_name, message, interval, active, banned, sent_total, sub_until = row
    targets = db.execute("select title from targets where uid=?", (uid,)).fetchall()
    titles = "، ".join(t[0] or "?" for t in targets[:10]) or "لا شي"
    msg_preview = (message or "لا توجد")[:300]
    text = (
        f"<b>مستخدم:</b> {first_name or ''} @{username or '-'} (<code>{uid}</code>)\n"
        f"<b>الحالة:</b> {'🚫 محظور' if banned else ('🟢 شغّال' if active else '⚪ متوقف')}\n"
        f"<b>كل:</b> {interval} دقيقة\n"
        f"<b>عدد مرات الإرسال:</b> {sent_total}\n"
        f"<b>الاشتراك ينتهي:</b> {fmt_date(sub_until)}\n"
        f"<b>الكروبات ({len(targets)}):</b> {titles}\n\n"
        f"<b>نص الرسالة الحالي:</b>\n{msg_preview}"
    )
    rows = [
        [btn("🟢 فك الحظر" if banned else "🔴 حظر", f"admin:{'unban' if banned else 'ban'}:{uid}")],
        [btn("🟢 تمديد الاشتراك يدوياً (أيام)", f"admin:extend:{uid}")],
        [btn("رجوع", "admin:users:0")],
    ]
    await c.message.edit_text(text, reply_markup=kb(rows))
    await c.answer()
 
 
@router.callback_query(F.data.startswith("admin:ban:") | F.data.startswith("admin:unban:"))
async def admin_ban_toggle(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    action, uid = c.data.split(":")[1], int(c.data.split(":")[2])
    banned = 1 if action == "ban" else 0
    db.execute(
        "update users set banned=?, active=? where uid=?",
        (banned, 0 if banned else 0, uid),
    )
    db.commit()
    await c.answer("✅ تم الحظر" if banned else "✅ تم فك الحظر", show_alert=True)
    await admin_user_detail(c)
 
 
@router.callback_query(F.data == "admin:stats")
async def admin_stats(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    total = db.execute("select count(*) from users").fetchone()[0]
    active = db.execute("select count(*) from users where active=1").fetchone()[0]
    banned = db.execute("select count(*) from users where banned=1").fetchone()[0]
    sessions = db.execute("select count(*) from users where session is not null").fetchone()[0]
    sent = db.execute("select coalesce(sum(sent_total),0) from users").fetchone()[0]
    text = (
        "📊 <b>إحصائيات</b>\n\n"
        f"إجمالي المستخدمين: {total}\n"
        f"حسابات مسجّلة: {sessions}\n"
        f"شغّالين الآن: {active}\n"
        f"محظورين: {banned}\n"
        f"إجمالي الرسائل المرسلة: {sent}"
    )
    await c.message.edit_text(text, reply_markup=kb([[btn("رجوع", "admin:open")]]))
    await c.answer()
 
 
@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(c: CallbackQuery, state: FSMContext):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    await state.set_state(Form.admin_broadcast)
    await c.message.answer("ارسل نص الإشعار اللي يوصل لكل المستخدمين (من البوت نفسه).")
    await c.answer()
 
 
@router.message(Form.admin_broadcast)
async def admin_broadcast_send(m: Message, state: FSMContext):
    if not admin_only(m.from_user.id):
        return
    await state.clear()
    uids = [r[0] for r in db.execute("select uid from users")]
    ok = 0
    for uid in uids:
        try:
            await bot.send_message(uid, m.html_text or m.text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ انرسل الإشعار لـ {ok}/{len(uids)} مستخدم.")
    await send_menu(m.from_user.id)
 
 
@router.callback_query(F.data == "admin:channels")
async def admin_channels(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    chans = db.execute("select chat_id, title, url from fsub").fetchall()
    rows = [[btn(f"🔴 حذف {t}", f"admin:delchan:{i}")] for i, t, _ in chans]
    rows.append([btn("🟢 إضافة قناة", "admin:addchan")])
    rows.append([btn("رجوع", "admin:open")])
    text = "📡 قنوات الاشتراك الإجباري:" if chans else "📡 ماكو قنوات حالياً."
    await c.message.edit_text(text, reply_markup=kb(rows))
    await c.answer()
 
 
@router.callback_query(F.data.startswith("admin:delchan:"))
async def admin_delchan(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    chat_id = c.data.split(":", 2)[2]
    db.execute("delete from fsub where chat_id=?", (chat_id,))
    db.commit()
    await admin_channels(c)
 
 
@router.callback_query(F.data == "admin:addchan")
async def admin_addchan_start(c: CallbackQuery, state: FSMContext):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    await state.set_state(Form.admin_channel)
    await c.message.answer(
        "ارسل يوزر القناة/الكروب فقط، مثال: <code>@mychannel</code>\n\n"
        "⚠️ لازم البوت يكون أدمن بالقناة حتى يكدر يتحقق من الاشتراك."
    )
    await c.answer()
 
 
@router.message(Form.admin_channel)
async def admin_addchan_save(m: Message, state: FSMContext):
    if not admin_only(m.from_user.id):
        return
    raw = (m.text or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.lstrip("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,}", raw):
        await m.answer("ارسل اليوزر فقط، مثال: @mychannel")
        return
    username = "@" + raw
    try:
        chat = await bot.get_chat(username)
        title = chat.title or username
    except Exception:
        title = username
    url = f"https://t.me/{raw}"
    db.execute(
        "insert or replace into fsub(chat_id, title, url) values(?,?,?)",
        (username, title, url),
    )
    db.commit()
    await state.clear()
    await m.answer(f"✅ تمت إضافة {title} ({username}).")
    await send_menu(m.from_user.id)
 
 
@router.callback_query(F.data.startswith("admin:extend:"))
async def admin_extend_start(c: CallbackQuery, state: FSMContext):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    target = int(c.data.split(":")[2])
    await state.set_state(Form.admin_extend_sub)
    await state.update_data(target=target)
    await c.message.answer(f"ارسل عدد الأيام اللي تريد تضيفها لاشتراك المستخدم <code>{target}</code>:")
    await c.answer()
 
 
@router.message(Form.admin_extend_sub)
async def admin_extend_save(m: Message, state: FSMContext):
    if not admin_only(m.from_user.id):
        return
    if not m.text.strip().lstrip("-").isdigit():
        await m.answer("ارسل رقم صحيح.")
        return
    days = int(m.text.strip())
    data = await state.get_data()
    target = data["target"]
    current = get_sub_until(target)
    base = current if current > time.time() else time.time()
    new_until = base + days * 86400
    db.execute("update users set sub_until=? where uid=?", (new_until, target))
    db.commit()
    await state.clear()
    await m.answer(f"✅ تم. اشتراك {target} ينتهي الآن: {fmt_date(new_until)}")
    try:
        await bot.send_message(
            target, f"🎁 تم تمديد اشتراكك من الأدمن. ينتهي: {fmt_date(new_until)}"
        )
    except Exception:
        pass
    await send_menu(m.from_user.id)
 
 
def render_admin_sub():
    paid = is_paid_mode()
    plans = db.execute("select id, label, days, stars from plans").fetchall()
    text = f"💳 <b>إعدادات الاشتراك</b>\nوضع البوت: {'🔴 مدفوع' if paid else '🟢 مجاني'}\n\n"
    rows = [[btn(f"🔵 تبديل إلى {'مجاني' if paid else 'مدفوع'}", "admin:submode")]]
    for pid, label, days, stars in plans:
        price = f"{stars} نجمة" if stars else "غير محدد"
        rows.append([btn(f"🔵 {label} ({days} يوم): {price}", f"admin:planprice:{pid}")])
    rows.append([btn("رجوع", "admin:open")])
    return text, kb(rows)
 
 
@router.callback_query(F.data == "admin:sub")
async def admin_sub(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    text, markup = render_admin_sub()
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer()
 
 
@router.callback_query(F.data == "admin:submode")
async def admin_submode(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    set_setting("paid_mode", "0" if is_paid_mode() else "1")
    text, markup = render_admin_sub()
    await c.message.edit_text(text, reply_markup=markup)
    await c.answer()
 
 
@router.callback_query(F.data.startswith("admin:planprice:"))
async def admin_planprice_start(c: CallbackQuery, state: FSMContext):
    if not admin_only(c.from_user.id):
        await c.answer("ماعندك صلاحية.", show_alert=True)
        return
    pid = c.data.split(":")[2]
    label = db.execute("select label from plans where id=?", (pid,)).fetchone()[0]
    await state.set_state(Form.admin_planprice)
    await state.update_data(pid=pid)
    await c.message.answer(f"ارسل سعر النجوم (رقم صحيح) لخطة «{label}»:")
    await c.answer()
 
 
@router.message(Form.admin_planprice)
async def admin_planprice_save(m: Message, state: FSMContext):
    if not admin_only(m.from_user.id):
        return
    if not m.text.strip().isdigit() or int(m.text.strip()) <= 0:
        await m.answer("ارسل رقم صحيح أكبر من صفر.")
        return
    stars = int(m.text.strip())
    data = await state.get_data()
    db.execute("update plans set stars=? where id=?", (stars, data["pid"]))
    db.commit()
    await state.clear()
    text, markup = render_admin_sub()
    await m.answer(text, reply_markup=markup)
 
 
# ---------------------------------------------------------------- النشر التلقائي
async def send_round(uid):
    if uid in running:
        return
    running.add(uid)
    ok, fail = 0, []
    try:
        session, message, _, _, banned = get_user(uid)
        if banned:
            return
        targets = db.execute("select chat_id, title from targets where uid=?", (uid,)).fetchall()
        async with user_client(session) as cl:
            await cl.get_dialogs()
            for chat_id, title in targets:
                try:
                    await cl.send_message(chat_id, message, parse_mode="html")
                    ok += 1
                except FloodWaitError as ex:
                    fail.append(f"{title}: انتظار {ex.seconds}ث")
                    if ex.seconds > 60:
                        break
                    await asyncio.sleep(ex.seconds + 1)
                except Exception as ex:
                    fail.append(f"{title}: {type(ex).__name__}")
                await asyncio.sleep(random.randint(5, 15))
        db.execute("update users set sent_total = sent_total + ? where uid=?", (ok, uid))
        db.commit()
        report = f"📤 انرسلت لـ {ok}/{len(targets)} كروب."
        if fail:
            report += "\n⚠️ " + "\n⚠️ ".join(fail[:10])
        await bot.send_message(uid, report)
    except Exception as ex:
        await bot.send_message(uid, f"❌ خطأ بالنشر: {type(ex).__name__}")
    finally:
        running.discard(uid)
 
 
async def scheduler():
    while True:
        try:
            now = time.time()
            paid = is_paid_mode()
            rows = db.execute(
                "select uid, interval, sub_until from users where active=1 and banned=0 "
                "and session is not null and message is not null and next_run<=?",
                (now,),
            ).fetchall()
            for uid, interval, sub_until in rows:
                if paid and sub_until <= now:
                    db.execute("update users set active=0 where uid=?", (uid,))
                    db.commit()
                    try:
                        await bot.send_message(
                            uid,
                            "⏳ انتهى اشتراكك، تم إيقاف النشر. جدده للمتابعة.",
                            reply_markup=kb([[btn("🟢 تجديد الاشتراك", "sub:menu")]]),
                        )
                    except Exception:
                        pass
                    continue
                db.execute("update users set next_run=? where uid=?", (now + interval * 60, uid))
                db.commit()
                asyncio.create_task(send_round(uid))
        except Exception as ex:
            print("scheduler:", ex)
        await asyncio.sleep(15)
 
 
async def main():
    asyncio.create_task(scheduler())
    print("Bot is running...")
    await dp.start_polling(bot)
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
