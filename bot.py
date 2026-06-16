import json, os, signal, sys, asyncio, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, PreCheckoutQueryHandler

TOKEN = "8666250439:AAFQCmNXjvm_sZinznHUyy_Kc2XkGz60FhY"
ADMIN_ID = 1782389554
GROUP_ID = -1003999869063
MONO_LINK = "https://send.monobank.ua/jar/9zscx8wwwb"
PID_FILE = "/tmp/strongsport.pid"
DATA_FILE = Path(__file__).parent / "data.json"

SUBS = {
    "pro": {"name": "Pro", "days": 30, "price": 4.99, "stars": 227},
    "mega": {"name": "Mega", "days": 60, "price": 8.49, "stars": 386},
    "ultra": {"name": "Ultra", "days": 99999, "price": 20.0, "stars": 910},
}

RANKS = {"Basic": 0, "Pro": 1, "Mega": 2, "Ultra": 3}
TYPES_BY_RANK = {0: "Basic", 1: "Pro", 2: "Mega", 3: "Ultra"}

last_click = {}
user_states = {}
user_last_msg = {}

def load():
    if DATA_FILE.exists():
        try: return json.loads(DATA_FILE.read_text())
        except: pass
    return {"basic_used": [], "claim_counter": 0, "subscriptions": {}, "claims": {}, "pending_links": {}}

def save(d):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

def now_athens():
    return datetime.now(tz=ZoneInfo("Europe/Athens"))

def has_forever_sub(uid):
    d = load()
    sub = d.get("subscriptions", {}).get(str(uid))
    if not sub: return False
    return sub.get("tier", 0) >= 3

def is_forever(sub):
    if not sub: return False
    try:
        end = datetime.fromisoformat(sub["end"])
        now = now_athens()
        diff = end - now
        return diff.days > 9000
    except: return False

def grant_subscription(uid, sub_type, days):
    d = load()
    now = now_athens()
    subs = d.setdefault("subscriptions", {})
    uid_str = str(uid)
    cur_end = None
    cur_tier = 0
    if uid_str in subs:
        try:
            cur = datetime.fromisoformat(subs[uid_str]["end"])
            if cur.tzinfo is None: cur = cur.replace(tzinfo=ZoneInfo("Europe/Athens"))
            if cur > now: cur_end, cur_tier = cur, subs[uid_str].get("tier", 0)
        except: pass
    new_end = (cur_end + timedelta(days=days)) if cur_end else (now + timedelta(days=days))
    new_tier = max(cur_tier, RANKS.get(sub_type, 0))
    new_type = TYPES_BY_RANK.get(new_tier, sub_type)
    subs[uid_str] = {"type": new_type, "end": new_end.isoformat(), "tier": new_tier}
    save(d)
    return new_end

async def revoke_link(user_id):
    d = load()
    link = d.get("pending_links", {}).pop(str(user_id), None)
    if link:
        url = f"https://api.telegram.org/bot{TOKEN}/revokeChatInviteLink"
        try:
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": GROUP_ID, "invite_link": link})
        except: pass
        save(d)

async def add_to_group(user_id):
    # 1. Пробуем прямое добавление
    url = f"https://api.telegram.org/bot{TOKEN}/addChatMember"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json={"chat_id": GROUP_ID, "user_id": user_id})
    data = r.json()
    if data.get("ok"):
        await revoke_link(user_id)
        return {"ok": True, "method": "direct"}
    
    # 2. Создаём ссылку БЕЗ срока истечения, только member_limit=1
    url2 = f"https://api.telegram.org/bot{TOKEN}/createChatInviteLink"
    async with httpx.AsyncClient() as client:
        r2 = await client.post(url2, json={"chat_id": GROUP_ID, "member_limit": 1})
    data2 = r2.json()
    if data2.get("ok"):
        link = data2["result"]["invite_link"]
        d = load()
        d.setdefault("pending_links", {})[str(user_id)] = link
        save(d)
        return {"ok": True, "method": "link", "link": link}
    return {"ok": False, "error": data2.get("description", data.get("description", "unknown"))}

async def get_usd_uah_rate():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json")
            return float(r.json()[0]["rate"])
    except: return 41.0

def format_price_uah(usd_price, rate):
    return f"{int(usd_price * rate)}.99"

def get_main_kb(uid):
    d = load()
    if has_forever_sub(uid):
        buttons = [["💙 Поддержать донатом", "⏳ Срок"]]
    else:
        buttons = [["📋 Подписки", "⏳ Срок"]]
    if uid not in d.get("basic_used", []): buttons.append(["🎁 Бесплатный доступ"])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def sub_inline_kb():
    kb = []
    for k, v in SUBS.items():
        label = f"{v['name']} — ${v['price']:.0f} (навсегда)" if v["days"] >= 99999 else f"{v['name']} — ${v['price']:.2f} ({v['days']} дней)"
        kb.append([InlineKeyboardButton(label, callback_data=f"sub_{k}")])
    return InlineKeyboardMarkup(kb)

def donate_confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да", callback_data="donate_yes"),
         InlineKeyboardButton("❌ Нет", callback_data="donate_no")]])

def donate_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Монобанк", callback_data="donate_mono"),
         InlineKeyboardButton("⭐ Telegram Stars", callback_data="donate_star")],
        [InlineKeyboardButton("Назад", callback_data="back_donate")]])

def donate_mono_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back_donate_pay")]])

def pay_kb(sid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Монобанк", callback_data=f"pay_mono_{sid}"),
         InlineKeyboardButton("⭐ Telegram Stars", callback_data=f"pay_star_{sid}")],
        [InlineKeyboardButton("Назад", callback_data="back_sub")]])

def mono_kb(sid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data=f"bpay_{sid}")]])

def admin_kb(cid, uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Одобрить", callback_data=f"app_{cid}_{uid}"),
         InlineKeyboardButton("Отказать", callback_data=f"rej_{cid}_{uid}")]])

async def show_msg(bot, chat_id, uid, text, reply_markup=None):
    if uid in user_last_msg:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=user_last_msg[uid], text=text, reply_markup=reply_markup)
            return
        except: pass
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    user_last_msg[uid] = msg.message_id

async def check_spam(update: Update):
    uid = update.effective_user.id
    now = time.time()
    if uid in last_click and now - last_click[uid] < 2: return True
    last_click[uid] = now
    return True

async def start(update: Update, ctx):
    uid = update.effective_user.id
    chat = update.effective_chat.id
    msg = await ctx.bot.send_message(chat, "👊 Добро пожаловать в TrateLabs!", reply_markup=get_main_kb(uid))
    user_last_msg[uid] = msg.message_id
    asyncio.create_task(delayed_subs(chat, uid, ctx.bot))

async def delayed_subs(chat_id, uid, bot):
    await asyncio.sleep(1)
    if has_forever_sub(uid):
        await show_msg(bot, chat_id, uid, "У вас уже есть подписка Ultra навсегда!\n\nХотите поддержать проект донатом?", reply_markup=donate_confirm_kb())
    else:
        await show_msg(bot, chat_id, uid, "👇 Выбери подписку:", reply_markup=sub_inline_kb())

async def text_handler(update: Update, ctx):
    text = update.message.text
    uid = update.effective_user.id
    chat = update.effective_chat.id
    if text == "📋 Подписки":
        if has_forever_sub(uid):
            await show_msg(ctx.bot, chat, uid, "У вас уже есть подписка Ultra навсегда!\n\nХотите поддержать проект донатом?", reply_markup=donate_confirm_kb())
        else:
            await show_msg(ctx.bot, chat, uid, "👇 Выбери подписку:", reply_markup=sub_inline_kb())
    elif text == "💙 Поддержать донатом":
        await show_msg(ctx.bot, chat, uid, "У вас уже есть подписка Ultra навсегда!\n\nХотите поддержать проект донатом?", reply_markup=donate_confirm_kb())
    elif text == "🎁 Бесплатный доступ":
        d = load()
        if uid in d.get("basic_used", []):
            await show_msg(ctx.bot, chat, uid, "Ты уже использовал бесплатный доступ.", reply_markup=get_main_kb(uid))
            return
        d.setdefault("basic_used", []).append(uid)
        save(d)
        end = grant_subscription(uid, "Basic", 1)
        r = await add_to_group(uid)
        if r["ok"]:
            msg = f"🎉 Ты получил бесплатный доступ на 1 день!\nПодписка активна до {end.strftime('%d.%m.%Y %H:%M')}."
            if r["method"] == "link": msg += f"\n\n🔗 Перейди по ссылке:\n{r['link']}"
            await show_msg(ctx.bot, chat, uid, msg, reply_markup=get_main_kb(uid))
        else:
            await show_msg(ctx.bot, chat, uid, f"Ошибка: {r['error']}\nНапиши админу.", reply_markup=get_main_kb(uid))
    elif text == "⏳ Срок":
        d = load()
        sub = d.get("subscriptions", {}).get(str(uid))
        if not sub:
            await show_msg(ctx.bot, chat, uid, "У тебя нет активной подписки.\n\nВыбери подписку в меню 📋 Подписки", reply_markup=get_main_kb(uid))
            return
        if is_forever(sub):
            await show_msg(ctx.bot, chat, uid, "Ultra подписка активна навсегда!", reply_markup=get_main_kb(uid))
            return
        end = datetime.fromisoformat(sub["end"])
        now = now_athens()
        diff = end - now
        if diff.total_seconds() <= 0:
            await show_msg(ctx.bot, chat, uid, "Твоя подписка истекла.\n\nВыбери новую в меню 📋 Подписки", reply_markup=get_main_kb(uid))
            return
        days, hours, mins = diff.days, diff.seconds // 3600, (diff.seconds % 3600) // 60
        left = f"{days} дней" if days > 0 else (f"{hours} ч {mins} мин" if hours > 0 else f"{mins} мин")
        await show_msg(ctx.bot, chat, uid, f"Подписка {sub['type']} доступна до {end.strftime('%d.%m.%Y %H:%M')} ({left})", reply_markup=get_main_kb(uid))

async def sub_cb(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    sid = q.data.split("_")[1]
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    if sid == "basic":
        d = load()
        if uid in d.get("basic_used", []):
            await q.edit_message_text("Ты уже использовал бесплатный доступ.")
            return
        d.setdefault("basic_used", []).append(uid)
        save(d)
        end = grant_subscription(uid, "Basic", 1)
        r = await add_to_group(uid)
        if r["ok"]:
            msg = f"🎉 Бесплатный доступ активирован!\nПодписка до {end.strftime('%d.%m.%Y %H:%M')}."
            if r["method"] == "link": msg += f"\n\n🔗 Перейди по ссылке:\n{r['link']}"
            await q.edit_message_text(msg)
        else:
            await q.edit_message_text(f"Ошибка: {r['error']}\nНапиши админу.")
        return
    if has_forever_sub(uid):
        await q.edit_message_text("У вас уже есть подписка Ultra навсегда!\n\nХотите поддержать проект донатом?", reply_markup=donate_confirm_kb())
        return
    user_states[uid] = {"sub": sid}
    s = SUBS[sid]
    period = "навсегда" if s["days"] >= 99999 else f"{s['days']} дней"
    await q.edit_message_text(f"Ты выбрал: {s['name']} — ${s['price']:.2f}\nДоступ: {period}\n\nВыбери способ оплаты:", reply_markup=pay_kb(sid))

async def donate_confirm_cb(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    if q.data == "donate_yes":
        await q.edit_message_text("Выберите способ поддержки:", reply_markup=donate_kb())
    elif q.data == "donate_no":
        await q.edit_message_text("Хорошо! Если передумаете — кнопка 💙 Поддержать донатом всегда доступна.")

async def donate_cb(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    method = q.data.split("_")[1]
    if method == "mono":
        await q.edit_message_text(f"💙 Спасибо за желание поддержать!\n\nСсылка для доната:\n{MONO_LINK}", reply_markup=donate_mono_kb())
    elif method == "star":
        prices = [LabeledPrice("Поддержка проекта 💙", 50)]
        await ctx.bot.send_invoice(chat_id=uid, title="Донат TrateLabs", description="Поддержка проекта — любая сумма приветствуется!", payload="donate_stars", provider_token="", currency="XTR", prices=prices)

async def back_donate(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    await q.edit_message_text("У вас уже есть подписка Ultra навсегда!\n\nХотите поддержать проект донатом?", reply_markup=donate_confirm_kb())

async def back_donate_pay(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    await q.edit_message_text("Выберите способ поддержки:", reply_markup=donate_kb())

async def pay_cb(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    method, sid = parts[1], parts[2]
    uid = q.from_user.id
    user_states.setdefault(uid, {})["pay"] = method
    user_last_msg[uid] = q.message.message_id
    if method == "mono":
        s = SUBS[sid]
        rate = await get_usd_uah_rate()
        price_uah = format_price_uah(s["price"], rate)
        period = "навсегда" if s["days"] >= 99999 else f"{s['days']} дней"
        await q.edit_message_text(f"💳 Оплата через Монобанк\n\nПодписка: {s['name']} — {period}\nСумма к оплате: {price_uah} грн\n\nПереведи точную сумму по ссылке:\n{MONO_LINK}\n\nПосле оплаты пришли скриншот сюда в чат.", reply_markup=mono_kb(sid))
    elif method == "star":
        s = SUBS[sid]
        prices = [LabeledPrice(f"{s['name']} — ${s['price']:.2f}", s["stars"])]
        await ctx.bot.send_invoice(chat_id=uid, title=s["name"], description=f"Подписка {s['name']} — {'навсегда' if s['days'] >= 99999 else str(s['days']) + ' дней'}", payload=f"{sid}_stars", provider_token="", currency="XTR", prices=prices)

async def photo_h(update: Update, ctx):
    u = update.effective_user
    chat = update.effective_chat.id
    st = user_states.get(u.id)
    if not st or not st.get("sub") or st.get("pay") != "mono":
        await show_msg(ctx.bot, chat, u.id, "Сначала выбери подписку и способ оплаты Монобанк в меню 📋 Подписки.")
        return
    sid = st["sub"]
    d = load()
    d["claim_counter"] += 1
    cid = d["claim_counter"]
    d.setdefault("claims", {})[str(cid)] = {"uid": u.id, "sub": sid, "status": "pending"}
    save(d)
    sname = SUBS.get(sid, {}).get("name", sid)
    cap = f"Заявка #{cid}\nПользователь: @{u.username or 'нет'}\nID: {u.id}\nПодписка: {sname}\nОплата: Монобанк"
    photo = update.message.photo[-1] if update.message.photo else None
    if photo:
        await ctx.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=cap, reply_markup=admin_kb(cid, u.id))
    else:
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=cap + "\n\n(без скриншота)", reply_markup=admin_kb(cid, u.id))
    await show_msg(ctx.bot, chat, u.id, "Скриншот отправлен! Ожидай подтверждения от модератора.")

async def admin_cb(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ADMIN_ID: return
    parts = q.data.split("_")
    action, cid, uid = parts[0], int(parts[1]), int(parts[2])
    d = load()
    claim = d.get("claims", {}).get(str(cid))
    if not claim:
        await q.edit_message_caption(caption=q.message.caption + "\n\nЗаявка не найдена")
        return
    sid = claim["sub"]
    s = SUBS.get(sid, {})
    sname, days = s.get("name", sid), s.get("days", 0)
    if action == "app":
        grant_subscription(uid, sname, days)
        r = await add_to_group(uid)
        if r["ok"]:
            msg = f"Заявка одобрена! Подписка {sname} активна. Ты добавлен в группу."
            if r["method"] == "link": msg += f"\n\n🔗 Перейди по ссылке:\n{r['link']}"
            await q.edit_message_caption(caption=q.message.caption + "\n\n✅ Пользователь добавлен в группу!")
            await ctx.bot.send_message(chat_id=uid, text=msg)
        else:
            await q.edit_message_caption(caption=q.message.caption + f"\n\n❌ Ошибка добавления: {r['error']}")
            await ctx.bot.send_message(chat_id=uid, text=f"Заявка одобрена, но ошибка добавления: {r['error']}. Напиши админу.")
    elif action == "rej":
        await q.edit_message_caption(caption=q.message.caption + "\n\n❌ Заявка отклонена")
        await ctx.bot.send_message(chat_id=uid, text="Заявка отклонена. Свяжитесь с поддержкой для уточнения.")

async def pre_cb(update: Update, ctx):
    await update.pre_checkout_query.answer(ok=True)

async def pay_ok(update: Update, ctx):
    u = update.effective_user
    payload = update.message.successful_payment.invoice_payload
    if payload == "donate_stars":
        await update.message.reply_text("💙 Спасибо за донат! Поддержка очень важна для нас.")
        return
    sid = payload.split("_")[0]
    s = SUBS.get(sid, {})
    sname, days = s.get("name", sid), s.get("days", 0)
    grant_subscription(u.id, sname, days)
    r = await add_to_group(u.id)
    if r["ok"]:
        msg = f"Оплата прошла! Подписка {sname} активна. Ты добавлен в группу!"
        if r["method"] == "link": msg += f"\n\n🔗 Перейди по ссылке:\n{r['link']}"
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(f"Оплата прошла! Но ошибка добавления: {r['error']}\nНапиши админу.")
    await ctx.bot.send_message(chat_id=ADMIN_ID, text=f"Оплата Stars\nПользователь: @{u.username or 'нет'} (ID: {u.id})\nПодписка: {sname}")

async def back_sub(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    await q.edit_message_text("👇 Выбери подписку:", reply_markup=sub_inline_kb())

async def back_pay(update: Update, ctx):
    if not await check_spam(update): return
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    user_last_msg[uid] = q.message.message_id
    sid = q.data.split("_")[1]
    s = SUBS[sid]
    period = "навсегда" if s["days"] >= 99999 else f"{s['days']} дней"
    await q.edit_message_text(f"Ты выбрал: {s['name']} — ${s['price']:.2f}\nДоступ: {period}\n\nВыбери способ оплаты:", reply_markup=pay_kb(sid))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        app = Application.builder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.add_handler(CallbackQueryHandler(sub_cb, pattern=r"^sub_"))
        app.add_handler(CallbackQueryHandler(donate_confirm_cb, pattern=r"^donate_(yes|no)$"))
        app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay_"))
        app.add_handler(CallbackQueryHandler(donate_cb, pattern=r"^donate_"))
        app.add_handler(CallbackQueryHandler(back_donate, pattern=r"^back_donate$"))
        app.add_handler(CallbackQueryHandler(back_donate_pay, pattern=r"^back_donate_pay$"))
        app.add_handler(CallbackQueryHandler(back_sub, pattern=r"^back_sub$"))
        app.add_handler(CallbackQueryHandler(back_pay, pattern=r"^bpay_"))
        app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^(app_|rej_)"))
        app.add_handler(PreCheckoutQueryHandler(pre_cb))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, pay_ok))
        app.add_handler(MessageHandler(filters.PHOTO, photo_h))
        with open(PID_FILE, "w") as f: f.write(str(os.getpid()))
        print("бот запущен")
        app.run_polling()
    elif len(sys.argv) > 1 and sys.argv[1] == "stop":
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f: pid = f.read().strip()
            try:
                os.kill(int(pid), signal.SIGTERM)
                os.remove(PID_FILE)
                print("бот остановлен")
            except:
                os.remove(PID_FILE)
                print("бот не был запущен")
        else: print("бот не был запущен")
    else: print("используй: python3 bot.py start | python3 bot.py stop")
