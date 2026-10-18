import asyncio
import os
import sqlite3
import time
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
PORT = int(os.getenv("PORT", "10000"))
REFERRAL_REWARD = 2
DAILY_BONUS = 2
MIN_WITHDRAWAL = 100
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. In Render > Environment add key BOT_TOKEN and set its value to your BotFather token.")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS users (
 user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0,
 referred_by INTEGER, last_daily INTEGER NOT NULL DEFAULT 0,
 banned INTEGER NOT NULL DEFAULT 0)""")
db.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
 amount INTEGER NOT NULL, upi TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL)""")
db.commit()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def get_user(uid):
    return db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def ensure_user(uid, referrer_id=None):
    if get_user(uid):
        return False
    if referrer_id == uid:
        referrer_id = None
    valid = get_user(referrer_id) if referrer_id else None
    db.execute("INSERT INTO users(user_id, referred_by) VALUES(?,?)",
               (uid, referrer_id if valid else None))
    if valid:
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                   (REFERRAL_REWARD, referrer_id))
    db.commit()
    return True

def banned(uid):
    row = get_user(uid)
    return bool(row and row["banned"])

@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    ref = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    ensure_user(uid, ref)
    if banned(uid):
        return await message.answer("Your account is restricted. Contact support.")
    me = await bot.get_me()
    await message.answer(
        "Welcome!\n\n"
        "/balance - check balance\n"
        "/refer - referral link\n"
        "/daily - daily bonus\n"
        f"/withdraw <amount> <UPI_ID> - request withdrawal (minimum ₹{MIN_WITHDRAWAL})\n"
        f"Referral reward: ₹{REFERRAL_REWARD}\nDaily bonus: ₹{DAILY_BONUS}"
    )

@dp.message(Command("balance"))
async def balance(message: Message):
    ensure_user(message.from_user.id)
    if banned(message.from_user.id):
        return await message.answer("Your account is restricted.")
    row = get_user(message.from_user.id)
    await message.answer(f"Your balance: ₹{row['balance']}")

@dp.message(Command("refer"))
async def refer(message: Message):
    ensure_user(message.from_user.id)
    me = await bot.get_me()
    await message.answer(f"Your referral link:\nhttps://t.me/{me.username}?start={message.from_user.id}\nReward: ₹{REFERRAL_REWARD} per new user")

@dp.message(Command("daily"))
async def daily(message: Message):
    ensure_user(message.from_user.id)
    uid = message.from_user.id
    if banned(uid):
        return await message.answer("Your account is restricted.")
    row = get_user(uid)
    now = int(time.time())
    if now - row["last_daily"] < 86400:
        left = 86400 - (now - row["last_daily"])
        return await message.answer(f"Already claimed. Try again in {left//3600}h {(left%3600)//60}m.")
    db.execute("UPDATE users SET balance=balance+?, last_daily=? WHERE user_id=?",
               (DAILY_BONUS, now, uid))
    db.commit()
    await message.answer(f"Daily bonus added: ₹{DAILY_BONUS}")

@dp.message(Command("withdraw"))
async def withdraw(message: Message):
    ensure_user(message.from_user.id)
    uid = message.from_user.id
    if banned(uid):
        return await message.answer("Your account is restricted.")
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        return await message.answer(f"Usage: /withdraw <amount> <UPI_ID>\nMinimum: ₹{MIN_WITHDRAWAL}")
    amount, upi = int(parts[1]), parts[2].strip()
    row = get_user(uid)
    if amount < MIN_WITHDRAWAL:
        return await message.answer(f"Minimum withdrawal is ₹{MIN_WITHDRAWAL}.")
    if amount > row["balance"]:
        return await message.answer("Insufficient balance.")
    if not upi or " " in upi or len(upi) > 100:
        return await message.answer("Enter a valid UPI ID without spaces.")
    db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, uid))
    cur = db.execute("INSERT INTO withdrawals(user_id,amount,upi,created_at) VALUES(?,?,?,?)",
                     (uid, amount, upi, int(time.time())))
    db.commit()
    wid = cur.lastrowid
    await message.answer(f"Withdrawal request #{wid} submitted for ₹{amount}. Admin will review it.")
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"Withdrawal #{wid}\nUser: {uid}\nAmount: ₹{amount}\nUPI: {upi}\n/approve {wid}\n/reject {wid}")
        except Exception:
            pass

@dp.message(Command("approve"))
async def approve(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Usage: /approve <request_id>")
    cur = db.execute("UPDATE withdrawals SET status='approved' WHERE id=? AND status='pending'", (int(parts[1]),))
    db.commit()
    await message.answer("Marked approved." if cur.rowcount else "Request not found or already processed.")

@dp.message(Command("reject"))
async def reject(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Usage: /reject <request_id>")
    wid = int(parts[1])
    row = db.execute("SELECT user_id,amount FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
    if not row:
        return await message.answer("Request not found or already processed.")
    db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (row["amount"], row["user_id"]))
    db.commit()
    await message.answer("Rejected and balance refunded.")
    try:
        await bot.send_message(row["user_id"], f"Withdrawal #{wid} rejected; ₹{row['amount']} refunded.")
    except Exception:
        pass

@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("Admin only.")
    users = db.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    pending = db.execute("SELECT COUNT(*) n FROM withdrawals WHERE status='pending'").fetchone()["n"]
    await message.answer(f"Admin summary\nUsers: {users}\nPending withdrawals: {pending}")

async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    return runner

async def main():
    runner = await run_web()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
