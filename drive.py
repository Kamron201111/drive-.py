from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
import asyncio
import re
import time
import json
import os
import base64
from datetime import date

# =========================
# CONFIG
# =========================
API_ID   = int(os.environ.get("API_ID", "23651528"))
API_HASH = os.environ.get("API_HASH", "ca42cf77a78ee409550aac24e179c87e")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8145417631"))

BOT        = "@IPremium8_Renewbot"
STATS_FILE = "stats.json"
TIMEOUT    = 35 * 60  # 35 daqiqa

# Session: environment variable dan oladi (Koyeb), yo'q bo'lsa lokal fayl
_session_b64 = os.environ.get("SESSION_STRING", "")
if _session_b64:
    _session_bytes = base64.b64decode(_session_b64)
    with open("session.session", "wb") as _f:
        _f.write(_session_bytes)

client = TelegramClient("session", API_ID, API_HASH)

# =========================
# GLOBAL STATE
# =========================
all_sessions      = {}   # {sid: session}
pending_queue     = []   # [sid, ...]
code_requests     = {}   # {last4: {user_id, number, sent_msg_id}}
assigned_numbers  = set()
# premium_pending: {bot_msg_id: {user_id, number, sent_msg_id}}
# sent_msg_id — foydalanuvchiga yuborilgan raqam xabarining IDsi (reply uchun)
premium_pending   = {}
broadcast_mode    = {}   # {admin_id: "all"|"select"}
broadcast_targets = {}   # {admin_id: [user_ids]}
bot_active        = True  # /stop → False, /start → True

# =========================
# STATS
# =========================
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"users": {}, "all_users": {}, "reset_counts": {},
            "blocked": [], "number_offsets": {}}

def save_stats(s):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

stats = load_stats()

# Eski fayllarda bo'lmagan kalitlarni qo'shish
for _k, _v in [("blocked", []), ("number_offsets", {}),
               ("users", {}), ("all_users", {}), ("reset_counts", {})]:
    if _k not in stats:
        stats[_k] = _v

def ensure_user(user_id, name, username=""):
    uid = str(user_id)
    if uid not in stats["users"]:
        stats["users"][uid] = {
            "name": name, "total": 0,
            "session_count": 0, "daily": {}
        }
    if uid not in stats["all_users"]:
        stats["all_users"][uid] = {"name": name, "username": username or ""}
    stats["all_users"][uid]["name"] = name
    if username:
        stats["all_users"][uid]["username"] = username

def add_premium(user_id):
    uid   = str(user_id)
    today = str(date.today())
    stats["users"][uid]["session_count"] += 1
    stats["users"][uid]["total"]         += 1
    daily = stats["users"][uid]["daily"]
    daily[today] = daily.get(today, 0) + 1
    base   = stats["reset_counts"].get(uid, 0)
    offset = stats["number_offsets"].get(uid, 0)
    save_stats(stats)
    return base + stats["users"][uid]["session_count"] + offset

def reset_user_count(user_id):
    uid = str(user_id)
    if uid in stats["users"]:
        stats["reset_counts"][uid] = stats["reset_counts"].get(uid, 0)
        stats["users"][uid]["session_count"] = 0
        stats["number_offsets"].pop(uid, None)
        save_stats(stats)

def reset_all_counts():
    for uid in stats["users"]:
        stats["reset_counts"][uid] = stats["reset_counts"].get(uid, 0)
        stats["users"][uid]["session_count"] = 0
    stats["number_offsets"] = {}
    save_stats(stats)

# =========================
# BLOCK / UNBLOCK
# =========================
def is_blocked(user_id):
    return str(user_id) in [str(b) for b in stats.get("blocked", [])]

def block_user(user_id):
    uid     = str(user_id)
    blocked = [str(b) for b in stats.get("blocked", [])]
    if uid not in blocked:
        stats["blocked"].append(uid)
        save_stats(stats)

def unblock_user(user_id):
    stats["blocked"] = [
        str(b) for b in stats.get("blocked", []) if str(b) != str(user_id)
    ]
    save_stats(stats)

# =========================
# HELPERS
# =========================
def extract_number(text):
    """Matndagi eng uzun 6+ raqamli sonni qaytaradi"""
    nums = re.findall(r'\d{6,}', text or "")
    return max(nums, key=len) if nums else None

async def get_user_info(user_id):
    try:
        e    = await client.get_entity(user_id)
        name = ((getattr(e, 'first_name', '') or '') + ' ' +
                (getattr(e, 'last_name',  '') or '')).strip() or str(user_id)
        return name, (getattr(e, 'username', '') or '')
    except Exception:
        return str(user_id), ""

def new_sid(user_id):
    return f"{user_id}_{int(time.time()*1000)}"

def close_session(sid):
    sess = all_sessions.pop(sid, None)
    if sess and sess.get("timeout_task"):
        sess["timeout_task"].cancel()
    i = 0
    while i < len(pending_queue):
        if pending_queue[i] == sid:
            pending_queue.pop(i)
        else:
            i += 1

def is_admin(user_id):
    return user_id == ADMIN_ID

def get_current_n(uid_str):
    """Foydalanuvchining joriy #N sini qaytaradi"""
    if uid_str not in stats["users"]:
        return 0
    base   = stats["reset_counts"].get(uid_str, 0)
    sess   = stats["users"][uid_str]["session_count"]
    offset = stats["number_offsets"].get(uid_str, 0)
    return base + sess + offset

# =========================
# SAFE SEND / REPLY  (FloodWait + qayta urinish)
# =========================
async def safe_send(target, text, reply_to=None):
    for attempt in range(5):
        try:
            return await client.send_message(target, text, reply_to=reply_to)
        except FloodWaitError as e:
            wait = e.seconds + 3
            print(f"⏳ FloodWait safe_send: {e.seconds}s (urinish {attempt+1})")
            await asyncio.sleep(wait)
        except Exception as ex:
            print(f"❗ safe_send xato: {ex}")
            return None
    print("❌ safe_send: 5 urinishdan keyin ham muvaffaqiyatsiz")
    return None

async def safe_reply(event, text):
    for attempt in range(5):
        try:
            return await event.reply(text)
        except FloodWaitError as e:
            wait = e.seconds + 3
            print(f"⏳ FloodWait safe_reply: {e.seconds}s (urinish {attempt+1})")
            await asyncio.sleep(wait)
        except Exception as ex:
            print(f"❗ safe_reply xato: {ex}")
            return None
    return None

async def safe_click(msg, btn_text_lower):
    """Tugmani bosish — xatolarni yutib oladi, True/False qaytaradi"""
    try:
        await msg.click(text=next(
            btn.text
            for row in msg.buttons
            for btn in row
            if btn_text_lower in btn.text.lower()
        ))
        return True
    except StopIteration:
        return False
    except Exception as ex:
        print(f"❗ safe_click xato: {ex}")
        return False

async def find_bot_msg_by_last4(last4, limit=200):
    """
    BOT chat-idan last4 raqamini o'z ichiga olgan va
    tugmasi bor xabarni qaytaradi.
    """
    async for msg in client.iter_messages(BOT, limit=limit):
        if msg.text and last4 in msg.text and msg.buttons:
            return msg
    return None

# =========================
# NOMER SO'ROVI
# =========================
@client.on(events.NewMessage(pattern=r"(\d+)\s*ta\s*(nomer|raqam)"))
async def get_numbers(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return

    if not bot_active:
        await safe_reply(event, "🔴 Hozirda nomer berish ochiq emas")
        return

    m = re.search(r"(\d+)", event.text)
    if not m:
        return
    count = int(m.group(1))
    if count < 1 or count > 20:
        await safe_reply(event, "❗ 1 dan 20 tagacha so'rang")
        return

    name, username = await get_user_info(user_id)
    ensure_user(user_id, name, username)
    save_stats(stats)

    sid = new_sid(user_id)
    all_sessions[sid] = {
        "sid":          sid,
        "user_id":      user_id,
        "needed":       count,
        "got":          0,
        "numbers":      [],   # [{"number","msg_id","confirmed","sent_msg_id"}]
        "success":      0,
        "active":       True,
        "limit_hit":    False,
        "timeout_task": None,
    }

    await safe_reply(event, f"⏳ {count} ta raqam so'ralmoqda...")

    for _ in range(count):
        pending_queue.append(sid)
        await safe_send(BOT, "/getNumber")
        await asyncio.sleep(0.6)

    task = asyncio.create_task(auto_freeze_timeout(sid))
    if sid in all_sessions:
        all_sessions[sid]["timeout_task"] = task

# =========================
# BOT JAVOBLARI  (NewMessage from BOT)
# =========================
@client.on(events.NewMessage(chats=BOT))
async def handle_bot(event):
    text = event.text or ""

    # --- DISABLED ---
    if "disabled by admin" in text.lower() or "currently disabled" in text.lower():
        if pending_queue:
            sid  = pending_queue[0]
            sess = all_sessions.get(sid)
            if sess:
                uid = sess["user_id"]
                i = 0
                while i < len(pending_queue):
                    if pending_queue[i] == sid:
                        pending_queue.pop(i)
                    else:
                        i += 1
                if not is_blocked(uid):
                    await safe_send(
                        uid,
                        "😔 Hozir bizda raqamlar mavjud emas.\n"
                        "Tez orada raqamlar bo'shagach sizga xabar beramiz! 🔔"
                    )
                close_session(sid)
        return

    # --- LIMIT ---
    if "Limit Reached" in text:
        if not pending_queue:
            return
        sid  = pending_queue[0]
        sess = all_sessions.get(sid)
        if not sess or not sess["active"] or sess["limit_hit"]:
            while pending_queue and pending_queue[0] == sid:
                pending_queue.pop(0)
            return
        sess["limit_hit"] = True
        got = sess["got"]
        uid = sess["user_id"]
        i = 0
        while i < len(pending_queue):
            if pending_queue[i] == sid:
                pending_queue.pop(i)
            else:
                i += 1
        if not is_blocked(uid):
            msg = (
                "🚫 Raqamlar hozir mavjud emas yoki limitga yetildi.\n"
                "Biroz kuting yoki kamroq raqam so'rang."
            ) if got == 0 else (
                f"⚠️ Faqat {got} ta raqam olindi — limit to'ldi.\n"
                "Qolganlar uchun biroz kuting."
            )
            await safe_send(uid, msg)
        close_session(sid)
        return

    # --- RAQAM ---
    if "Your number" in text:
        number = extract_number(text)
        if not number or not pending_queue:
            return

        sid  = pending_queue.pop(0)
        sess = all_sessions.get(sid)
        if not sess or not sess["active"]:
            return

        # Dublikat — freeze va qayta so'rov
        if number in assigned_numbers:
            last4   = number[-4:]
            bot_msg = await find_bot_msg_by_last4(last4)
            if bot_msg:
                await safe_click(bot_msg, "freeze")
            pending_queue.insert(0, sid)
            await asyncio.sleep(1)
            await safe_send(BOT, "/getNumber")
            return

        assigned_numbers.add(number)
        sess["numbers"].append({
            "number":      number,
            "msg_id":      event.id,
            "confirmed":   False,
            "sent_msg_id": None,
        })
        sess["got"] += 1

        uid = sess["user_id"]
        if not is_blocked(uid):
            sent = await safe_send(uid, f"📞 Raqam {sess['got']}:\n`{number}`")
            if sent:
                sess["numbers"][-1]["sent_msg_id"] = sent.id
        return

    # --- PREMIUM natija yangi xabar sifatida ---
    # Ba'zan BOT premium/not premium ni yangi xabar sifatida yuboradi
    if "premium activated" in text.lower() or "not premium" in text.lower():
        # premium_pending ichida number bo'yicha moslik topamiz
        number = extract_number(text)
        matched_key = None
        if number:
            last4 = number[-4:]
            for pmid, pdata in list(premium_pending.items()):
                if pdata["number"][-4:] == last4:
                    matched_key = pmid
                    break
        # Agar raqam bilan topilmasa — birinchisini olamiz
        if matched_key is None and premium_pending:
            matched_key = next(iter(premium_pending))

        if matched_key is not None:
            pdata = premium_pending.pop(matched_key)
            activated = "premium activated" in text.lower()
            await _premium_result(
                pdata["user_id"],
                pdata["number"],
                pdata.get("sent_msg_id"),
                activated
            )
        return

# =========================
# PREMIUM NATIJA
# =========================
async def _premium_result(uid, number, sent_msg_id, activated: bool):
    uid_str = str(uid)
    if uid_str not in stats.get("users", {}):
        name, username = await get_user_info(uid)
        ensure_user(uid, name, username)
        save_stats(stats)

    if is_blocked(uid):
        return

    if activated:
        n = add_premium(uid)
        await safe_send(uid, f"✅ Tasdiqlandi #{n}", reply_to=sent_msg_id)
    else:
        await safe_send(uid, "❌ Tasdiqlanmadi", reply_to=sent_msg_id)

# =========================
# BOT XABAR TAHRIR  (kod natijasi + premium)
# =========================
@client.on(events.MessageEdited(chats=BOT))
async def edited_handler(event):
    text   = event.text or ""
    msg_id = event.id

    # Kod natijasi
    if "Code" in text and "Number" in text:
        number = extract_number(text)
        if number:
            last4 = number[-4:]
            req   = code_requests.get(last4)
            if req and not is_blocked(req["user_id"]):
                await safe_send(
                    req["user_id"],
                    f"📨 Kod:\n{text}",
                    reply_to=req.get("sent_msg_id")
                )
                code_requests.pop(last4, None)

    # Premium natijasi (tahrirlangan xabar orqali)
    if msg_id in premium_pending:
        pdata     = premium_pending.pop(msg_id)
        activated = "premium activated" in text.lower()
        await _premium_result(
            pdata["user_id"],
            pdata["number"],
            pdata.get("sent_msg_id"),
            activated
        )

# =========================
# OLINDI → Check premium
# Muammo: premium tugma topilmadi
# Fix:
#  1) sent_msg_id ni sessiyadan olamiz (botdan kelgan reply.id emas)
#  2) limit=200 gacha qidiradi
#  3) tugma topilmasa qayta urinadi (3x, 2s kutib)
#  4) Tugma bosilgandan keyin premium_pending ga yozadi,
#     edited va NewMessage ikkalasida ham ushlaydi
# =========================
@client.on(events.NewMessage(pattern=r"(?i)^olindi$"))
async def check_premium(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    if not event.is_reply:
        await safe_reply(event, "↩️ Raqam xabariga reply qilib yozing")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg:
        await safe_reply(event, "❌ Xabar topilmadi")
        return

    number = extract_number(reply_msg.text or "")
    if not number:
        await safe_reply(event, "❌ Xabarda raqam topilmadi")
        return

    last4 = number[-4:]

    # sent_msg_id — sessiyadan topamiz (bu foydalanuvchiga yuborilgan xabar)
    sent_msg_id = reply_msg.id  # fallback
    for sid, sess in all_sessions.items():
        if sess["user_id"] == user_id:
            for item in sess["numbers"]:
                if item["number"] == number:
                    item["confirmed"] = True
                    if item.get("sent_msg_id"):
                        sent_msg_id = item["sent_msg_id"]
                    break

    # BOT xabarini qidirish va tugmani bosish (3 urinish)
    clicked_msg_id = None
    for attempt in range(3):
        bot_msg = await find_bot_msg_by_last4(last4, limit=200)
        if bot_msg:
            # "premium" so'zi bo'lgan tugmani topamiz
            for row in bot_msg.buttons:
                for btn in row:
                    if "premium" in btn.text.lower():
                        try:
                            await bot_msg.click(text=btn.text)
                            clicked_msg_id = bot_msg.id
                        except Exception as ex:
                            print(f"❗ premium click xato: {ex}")
                        break
                if clicked_msg_id:
                    break

        if clicked_msg_id:
            break

        # Topilmadi — 2s kutib qayta urinamiz
        if attempt < 2:
            await asyncio.sleep(2)

    if not clicked_msg_id:
        await safe_reply(
            event,
            f"❌ Check premium tugmasi topilmadi\n"
            f"Raqam: `{number}`\n"
            "Iltimos, biroz kutib qayta urining yoki admin bilan bog'laning."
        )
        return

    # pending ga yozamiz — edited va NewMessage ikkalasida ham ushlaydi
    premium_pending[clicked_msg_id] = {
        "user_id":     user_id,
        "number":      number,
        "sent_msg_id": sent_msg_id,
    }
    # Foydalanuvchiga kutilmoqda deb xabar berish
    await safe_reply(event, "⏳ Premium tekshirilmoqda...")

# =========================
# KOD → Get Code
# =========================
@client.on(events.NewMessage(pattern=r"(?i)^kod$"))
async def request_code(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    if not event.is_reply:
        await safe_reply(event, "↩️ Raqam xabariga reply qilib yozing")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg:
        await safe_reply(event, "❌ Xabar topilmadi")
        return

    number = extract_number(reply_msg.text or "")
    if not number:
        await safe_reply(event, "❌ Raqam topilmadi")
        return

    last4 = number[-4:]
    code_requests[last4] = {
        "user_id":     user_id,
        "number":      number,
        "sent_msg_id": reply_msg.id,
    }

    bot_msg = await find_bot_msg_by_last4(last4, limit=200)
    if bot_msg:
        for row in bot_msg.buttons:
            for btn in row:
                if "code" in btn.text.lower():
                    try:
                        await bot_msg.click(text=btn.text)
                        return
                    except Exception as ex:
                        print(f"❗ code click xato: {ex}")

    await safe_reply(event, "❌ Kod tugmasi topilmadi")

# =========================
# LIMIT → Freeze
# =========================
@client.on(events.NewMessage(pattern=r"(?i)^limit$"))
async def freeze_number(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    if not event.is_reply:
        await safe_reply(event, "↩️ Raqam xabariga reply qilib yozing")
        return

    reply_msg = await event.get_reply_message()
    if not reply_msg:
        await safe_reply(event, "❌ Xabar topilmadi")
        return

    number = extract_number(reply_msg.text or "")
    if not number:
        await safe_reply(event, "❌ Raqam topilmadi")
        return

    last4 = number[-4:]

    # Sessiyada confirmed = True
    for sid, sess in all_sessions.items():
        if sess["user_id"] == user_id:
            for item in sess["numbers"]:
                if item["number"] == number:
                    item["confirmed"] = True
                    break

    bot_msg = await find_bot_msg_by_last4(last4, limit=200)
    if bot_msg:
        clicked = await safe_click(bot_msg, "freeze")
        if clicked:
            await safe_reply(event, f"🧊 {number} freeze qilindi")
            return

    await safe_reply(event, "❌ Freeze tugmasi topilmadi yoki xato yuz berdi")

# =========================
# TIMEOUT: 35 daqiqa
# =========================
async def auto_freeze_timeout(sid):
    await asyncio.sleep(TIMEOUT)

    sess = all_sessions.get(sid)
    if not sess or not sess["active"]:
        return

    uid = sess["user_id"]

    for item in sess["numbers"]:
        if item["confirmed"]:
            continue

        number  = item["number"]
        last4   = number[-4:]
        bot_msg = await find_bot_msg_by_last4(last4, limit=200)
        if bot_msg:
            await safe_click(bot_msg, "freeze")
            await asyncio.sleep(1)

        if not is_blocked(uid):
            await safe_send(
                uid,
                "⏰ 35 daqiqa o'tdi — raqam freeze qilindi ❄️",
                reply_to=item.get("sent_msg_id")
            )

    close_session(sid)

# =========================
# FOYDALANUVCHI: /mystats
# =========================
@client.on(events.NewMessage(pattern=r"^/mystats$"))
async def my_stats(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    uid   = str(user_id)
    today = str(date.today())
    if uid not in stats.get("users", {}):
        await safe_reply(event, "📊 Sizda hali statistika yo'q")
        return
    data      = stats["users"][uid]
    today_cnt = data["daily"].get(today, 0)
    current_n = get_current_n(uid)
    await safe_reply(
        event,
        f"📊 Sizning statistikangiz:\n\n"
        f"📅 Bugun: {today_cnt} ta\n"
        f"✅ Jami: {data['total']} ta\n"
        f"🔢 Joriy #N: {current_n}"
    )

# =========================
# FOYDALANUVCHI: /resetme
# =========================
@client.on(events.NewMessage(pattern=r"^/resetme$"))
async def reset_me(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    uid = str(user_id)
    if uid not in stats.get("users", {}):
        await safe_reply(event, "📊 Sizda hali statistika yo'q")
        return
    stats["users"][uid]["session_count"] = 0
    stats["reset_counts"][uid]           = stats["reset_counts"].get(uid, 0)
    stats["number_offsets"].pop(uid, None)
    save_stats(stats)
    await safe_reply(event, "♻️ Sizning #N hisobingiz 0 dan boshladi.\nJami statistika saqlanib qoldi.")

# =========================
# ADMIN: /block (lichkada)
# =========================
@client.on(events.NewMessage(pattern=r"^/block$"))
async def admin_block(event):
    if not is_admin(event.sender_id):
        return
    target_id = event.chat_id
    if target_id == ADMIN_ID:
        await safe_reply(event, "❌ O'zingizni bloklashingiz mumkin emas")
        return
    if is_blocked(target_id):
        await safe_reply(event, f"⚠️ Allaqachon bloklangan (ID: {target_id})")
        return
    block_user(target_id)
    name, _ = await get_user_info(target_id)
    await safe_reply(
        event,
        f"🚫 Bloklandi: {name} (ID: {target_id})\n"
        "Endi u hech narsadan foydalana olmaydi."
    )

# =========================
# ADMIN: /unblock (lichkada)
# =========================
@client.on(events.NewMessage(pattern=r"^/unblock$"))
async def admin_unblock(event):
    if not is_admin(event.sender_id):
        return
    target_id = event.chat_id
    if not is_blocked(target_id):
        await safe_reply(event, f"⚠️ Bloklangan emas (ID: {target_id})")
        return
    unblock_user(target_id)
    name, _ = await get_user_info(target_id)
    await safe_reply(
        event,
        f"✅ Blok ochildi: {name} (ID: {target_id})\n"
        "Endi ishlatishi mumkin."
    )

# =========================
# ADMIN: #N (lichkada) — keyingi raqam offseti
# =========================
@client.on(events.NewMessage(pattern=r"^#(\d+)$"))
async def set_number_offset(event):
    if not is_admin(event.sender_id):
        return
    target_id = event.chat_id
    if target_id == ADMIN_ID:
        await safe_reply(event, "❌ Bu buyruq foydalanuvchi lichkasida ishlatiladi")
        return

    offset_val = int(event.pattern_match.group(1))
    uid        = str(target_id)

    # Keyingi add_premium chaqiruvida qaytadigan qiymat = offset_val bo'lsin
    # add_premium: base + session_count+1 + new_offset = offset_val
    # => new_offset = offset_val - base - (session_count+1)
    users = stats.get("users", {})
    if uid in users:
        base       = stats["reset_counts"].get(uid, 0)
        sess_count = users[uid]["session_count"]
        new_offset = offset_val - base - sess_count - 1
    else:
        # Foydalanuvchi hali yo'q, birinchi premium = offset_val bo'ladi
        new_offset = offset_val - 1

    stats["number_offsets"][uid] = new_offset
    save_stats(stats)
    name, _ = await get_user_info(target_id)
    await safe_reply(event, f"✅ {name} uchun keyingi #N: {offset_val} dan boshlanadi")

# =========================
# ADMIN: /refreshstat
# =========================
@client.on(events.NewMessage(pattern=r"^/refreshstat$"))
async def refresh_stat(event):
    if not is_admin(event.sender_id):
        return
    users     = stats.get("users", {})
    all_users = stats.get("all_users", {})
    today     = str(date.today())
    if not users:
        await safe_reply(event, "📊 Hali hech kim olmagan")
        return

    sent = failed = 0
    for uid, data in users.items():
        try:
            user_id   = int(uid)
            today_cnt = data["daily"].get(today, 0)
            current_n = get_current_n(uid)
            name      = all_users.get(uid, {}).get("name", uid)
            uname     = all_users.get(uid, {}).get("username", "")
            uname_str = f"@{uname}" if uname else ""
            msg = (
                f"📊 Statistikangiz:\n\n"
                f"👤 {name} {uname_str}\n"
                f"📅 Bugun: {today_cnt} ta\n"
                f"✅ Jami: {data['total']} ta\n"
                f"🔢 Joriy #N: {current_n}"
            )
            await safe_send(user_id, msg)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as ex:
            print(f"refreshstat xato {uid}: {ex}")
            failed += 1

    await safe_reply(event, f"📤 Yuborildi: {sent}\n❌ Xato: {failed}")

# =========================
# ADMIN PANEL
# =========================
@client.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_panel(event):
    if not is_admin(event.sender_id):
        return
    today       = str(date.today())
    users       = stats.get("users", {})
    today_cnt   = sum(v["daily"].get(today, 0) for v in users.values())
    total_cnt   = sum(v["total"] for v in users.values())
    user_count  = len(stats.get("all_users", {}))
    blocked_cnt = len(stats.get("blocked", []))

    await safe_reply(event,
        f"🛠 Admin Panel\n\n"
        f"👥 Foydalanuvchilar: {user_count}\n"
        f"🚫 Bloklangan: {blocked_cnt}\n"
        f"📅 Bugun: {today_cnt} ta premium\n"
        f"✅ Jami: {total_cnt} ta premium\n\n"
        f"Buyruqlar:\n"
        f"/stat         — bugungi statistika\n"
        f"/allstat      — hammaning statistikasi\n"
        f"/refreshstat  — hammaga statistika yuborish\n"
        f"/resetall     — hammaning #N hisobini nollash\n"
        f"/broadcast    — xabar yuborish\n"
        f"/blocklist    — bloklangan ro'yxat\n"
        f"/stop         — botni to'xtatish\n\n"
        f"📌 Lichkada ishlatiladigan:\n"
        f"/block        — foydalanuvchini bloklash\n"
        f"/unblock      — blokdan chiqarish\n"
        f"#N            — keyingi #N ni N dan boshlash\n"
        f"/resetuser    — foydalanuvchi hisobini nollash"
    )

# =========================
# ADMIN: /blocklist
# =========================
@client.on(events.NewMessage(pattern=r"^/blocklist$"))
async def blocked_list(event):
    if not is_admin(event.sender_id):
        return
    blocked = stats.get("blocked", [])
    if not blocked:
        await safe_reply(event, "✅ Bloklangan foydalanuvchi yo'q")
        return
    lines = [f"🚫 Bloklangan ({len(blocked)} ta):\n"]
    for uid in blocked:
        name = stats.get("all_users", {}).get(str(uid), {}).get("name", str(uid))
        lines.append(f"• {name} (ID: {uid})")
    await safe_reply(event, "\n".join(lines))

# =========================
# ADMIN: /stat
# =========================
@client.on(events.NewMessage(pattern=r"^/stat$"))
async def today_stat(event):
    if not is_admin(event.sender_id):
        return
    today = str(date.today())
    users = stats.get("users", {})
    lines = [f"📅 Bugun ({today}):\n"]
    total = 0
    for uid, data in sorted(users.items(), key=lambda x: -x[1]["daily"].get(today, 0)):
        cnt = data["daily"].get(today, 0)
        if cnt > 0:
            lines.append(f"👤 {data['name']}: {cnt} ta")
            total += cnt
    lines.append("Hali hech kim olmagan" if total == 0 else f"\n✅ Jami: {total} ta")
    await safe_reply(event, "\n".join(lines))

# =========================
# ADMIN: /allstat
# =========================
@client.on(events.NewMessage(pattern=r"^/allstat$"))
async def all_stat(event):
    if not is_admin(event.sender_id):
        return
    users = stats.get("users", {})
    if not users:
        await safe_reply(event, "📊 Hali hech kim olmagan")
        return
    lines = ["📊 Umumiy statistika:\n"]
    total = 0
    for uid, data in sorted(users.items(), key=lambda x: -x[1]["total"]):
        if data["total"] > 0:
            lines.append(f"👤 {data['name']}: {data['total']} ta")
            total += data["total"]
    lines.append(f"\n✅ Jami: {total} ta premium")
    await safe_reply(event, "\n".join(lines))

# =========================
# ADMIN: /resetall
# =========================
@client.on(events.NewMessage(pattern=r"^/resetall$"))
async def reset_all(event):
    if not is_admin(event.sender_id):
        return
    reset_all_counts()
    await safe_reply(event, "♻️ Hammaning #N hisoblagichi nollandi.\nJami statistika saqlanib qoldi.")

# =========================
# ADMIN: /resetuser (lichkada)
# =========================
@client.on(events.NewMessage(pattern=r"^/resetuser$"))
async def reset_user_cmd(event):
    if not is_admin(event.sender_id):
        return
    target_id = event.chat_id
    if target_id == ADMIN_ID:
        await safe_reply(event, "❌ Bu buyruq foydalanuvchi lichkasida ishlatiladi")
        return
    uid = str(target_id)
    if uid not in stats.get("users", {}):
        await safe_reply(event, "❌ Bu foydalanuvchi topilmadi")
        return
    reset_user_count(target_id)
    name, _ = await get_user_info(target_id)
    await safe_reply(event, f"♻️ {name} hisoblagichi nollandi")

# =========================
# FOYDALANUVCHI: /all
# =========================
@client.on(events.NewMessage(pattern=r"^/all$"))
async def all_stats_cmd(event):
    user_id = event.sender_id
    if is_blocked(user_id):
        return
    users = stats.get("users", {})
    if not users:
        await safe_reply(event, "📊 Hali hech kim olmagan")
        return
    lines = ["📊 Statistika:\n"]
    total = 0
    for uid, data in sorted(users.items(), key=lambda x: -x[1]["total"]):
        if data["total"] > 0:
            lines.append(f"👤 {data['name']}: {data['total']} ta")
            total += data["total"]
    lines.append(f"\n✅ Jami: {total} ta premium")
    await safe_reply(event, "\n".join(lines))

# =========================
# BROADCAST
# =========================
@client.on(events.NewMessage(pattern=r"^/broadcast$"))
async def broadcast_cmd(event):
    if not is_admin(event.sender_id):
        return
    all_users = stats.get("all_users", {})
    if not all_users:
        await safe_reply(event, "❌ Foydalanuvchilar yo'q")
        return
    lines = [
        "👥 Kimga yuborish?\n",
        "/broadcast_all — hammaga",
        "Yoki: /broadcast_select 1,3\n",
        "Ro'yxat:"
    ]
    for i, (fuid, fdata) in enumerate(all_users.items(), 1):
        uname = f"@{fdata['username']}" if fdata.get("username") else ""
        lines.append(f"{i}. {fdata['name']} {uname}")
    await safe_reply(event, "\n".join(lines))

@client.on(events.NewMessage(pattern=r"^/broadcast_all$"))
async def broadcast_all(event):
    if not is_admin(event.sender_id):
        return
    broadcast_mode[event.sender_id] = "all"
    await safe_reply(event, "📢 Hammaga yuborish. Endi xabar/rasm/video yuboring:")

@client.on(events.NewMessage(pattern=r"^/broadcast_select (.+)$"))
async def broadcast_select(event):
    if not is_admin(event.sender_id):
        return
    uid       = event.sender_id
    all_users = stats.get("all_users", {})
    uids_list = list(all_users.keys())
    try:
        nums     = [int(x.strip()) - 1 for x in event.pattern_match.group(1).split(",")]
        selected = [int(uids_list[n]) for n in nums if 0 <= n < len(uids_list)]
    except Exception:
        await safe_reply(event, "❌ Format: /broadcast_select 1,3,5")
        return
    broadcast_targets[uid] = selected
    broadcast_mode[uid]    = "select"
    await safe_reply(event, f"✅ {len(selected)} ta tanlandi. Xabar yuboring:")

@client.on(events.NewMessage(pattern=r"^/stop$"))
async def stop_bot(event):
    global bot_active
    if not is_admin(event.sender_id):
        return
    bot_active = False
    await safe_reply(event, "🔴 Nomer berish to'xtatildi. Yoqish uchun /start yuboring.")

@client.on(events.NewMessage(pattern=r"^/start$"))
async def start_bot(event):
    global bot_active
    if not is_admin(event.sender_id):
        return
    bot_active = True
    await safe_reply(event, "🟢 Nomer berish yoqildi!")

# Broadcast xabari — MUHIM: pattern=r"." yoki pattern yo'q bo'lsa
# barcha xabarlarni tutadi. Shuning uchun broadcast_mode da bo'lganda
# buyruqlarni o'tkazib yuboramiz.
@client.on(events.NewMessage())
async def handle_broadcast(event):
    uid = event.sender_id
    if uid not in broadcast_mode:
        return
    # Har qanday / bilan boshlanadigan matnni o'tkazib yuboramiz
    if event.text and event.text.startswith("/"):
        return
    # BOT xabarlarini o'tkazib yuboramiz
    if event.chat_id and str(event.chat_id).lstrip("-") == BOT.lstrip("@"):
        return

    mode    = broadcast_mode.pop(uid)
    all_u   = stats.get("all_users", {})
    blocked = [str(b) for b in stats.get("blocked", [])]

    if mode == "all":
        targets = [int(x) for x in all_u.keys() if x not in blocked]
    else:
        targets = [t for t in broadcast_targets.pop(uid, []) if str(t) not in blocked]

    sent = failed = 0
    for tid in targets:
        if tid == uid:
            continue
        for attempt in range(3):
            try:
                await client.forward_messages(tid, event.message)
                sent += 1
                await asyncio.sleep(0.4)
                break
            except FloodWaitError as e:
                print(f"⏳ Broadcast FloodWait: {e.seconds}s")
                await asyncio.sleep(e.seconds + 3)
            except Exception as ex:
                print(f"❗ broadcast xato {tid}: {ex}")
                failed += 1
                break

    await safe_reply(event, f"📤 Yuborildi: {sent}\n❌ Xato: {failed}")

# =========================
# HELP
# =========================
@client.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(event):
    if is_blocked(event.sender_id):
        return
    await safe_reply(event,
        "📖 Buyruqlar:\n\n"
        "👤 Foydalanuvchilar:\n"
        "  5 ta nomer  — raqam so'rash\n"
        "  kod         — raqamga reply → kod olish\n"
        "  limit       — raqamga reply → freeze\n"
        "  olindi      — raqamga reply → premium tekshirish\n"
        "  /all        — umumiy statistika\n"
        "  /mystats    — mening statistikam\n"
        "  /resetme    — #N hisobimni nollash\n\n"
        "🛠 Admin:\n"
        "  /admin           — panel\n"
        "  /stat            — bugun\n"
        "  /allstat         — hammasi\n"
        "  /refreshstat     — hammaga statistika yuborish\n"
        "  /resetall        — hisobni nollash\n"
        "  /broadcast       — xabar yuborish\n"
        "  /blocklist       — bloklangan ro'yxat\n"
        "  /stop            — nomer berishni to'xtatish\n"
        "  /start           — nomer berishni yoqish\n\n"
        "📌 Admin lichkasida:\n"
        "  /block       — bloklash\n"
        "  /unblock     — blokdan chiqarish\n"
        "  /resetuser   — hisobini nollash\n"
        "  #4           — keyingi #N ni 4 dan boshlash"
    )

# =========================
# START
# =========================
print("🚀 Userbot ishga tushdi")
client.start()
client.run_until_disconnected()
