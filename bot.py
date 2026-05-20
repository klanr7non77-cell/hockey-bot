import os
import sqlite3
import logging
import time
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web  # Нужно для обхода ограничений бесплатного тарифа Render

# Настройка логов
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация базы данных SQLite
conn = sqlite3.connect("hockey_bet_v2.db", check_same_thread=False)
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    balance INTEGER DEFAULT 1000,
    last_bonus INTEGER DEFAULT 0
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    team1 TEXT,
    team2 TEXT,
    coef_p1 REAL,
    coef_x REAL,
    coef_p2 REAL,
    status TEXT DEFAULT 'open'
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS bets (
    bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    match_id INTEGER,
    outcome TEXT,
    amount INTEGER,
    status TEXT DEFAULT 'pending'
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS promocodes (
    code TEXT PRIMARY KEY,
    reward INTEGER,
    uses_left INTEGER
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS claimed_promos (
    user_id INTEGER,
    code TEXT,
    PRIMARY KEY (user_id, code)
)
''')
conn.commit()

# --- СОСТОЯНИЯ ДЛЯ СТАВОК (FSM) ---
class BetStates(StatesGroup):
    choosing_outcome = State()
    entering_amount = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_user(user_id, username=""):
    cursor.execute("SELECT balance, last_bonus FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    if not res:
        cursor.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
        return 1000, 0
    
    if username:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
        
    return res[0], res[1]

def update_balance(user_id, amount):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ ---

@dp.message(Command("start"))
async def start_cmd(message: Message):
    balance, _ = get_user(message.from_user.id, message.from_user.username or message.from_user.first_name)
    await message.answer(
        f"🏒 Добро пожаловать на лед, {message.from_user.first_name}!\n\n"
        f"Твой баланс: {balance} монет 💰\n\n"
        f"📋 Меню игрока:\n"
        f"🔹 /profile — Твой профиль и статистика\n"
        f"🔹 /matches — Список доступных матчей\n"
        f"🔹 /mybets — Твои ставки\n"
        f"🔹 /top — Рейтинг игроков\n"
        f"🔹 /bonus — Ежедневный бонус\n"
        f"🔹 /promo [код] — Активировать промокод",
        parse_mode="Markdown"
    )

@dp.message(Command("profile"))
async def profile_cmd(message: Message):
    user_id = message.from_user.id
    balance, _ = get_user(user_id, message.from_user.username)
    
    cursor.execute("SELECT COUNT(*), SUM(amount) FROM bets WHERE user_id = ?", (user_id,))
    stats = cursor.fetchone()
    total_bets = stats[0] if stats[0] else 0
    total_spent = stats[1] if stats[1] else 0
    
    await message.answer(
        f"👤 Профиль игрока:\n\n"
        f"💳 Баланс: {balance} монет\n"
        f"🎟 Сделано ставок: {total_bets}\n"
        f"💸 Всего поставлено: {total_spent} монет",
        parse_mode="Markdown"
    )

@dp.message(Command("top"))
async def top_cmd(message: Message):
    cursor.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10")
    leaders = cursor.fetchall()
    
    text = "🏆 ТОП-10 ИГРОКОВ:\n\n"
    for i, (username, balance) in enumerate(leaders, 1):
        name = username if username else "Неизвестный"
        text += f"{i}. {name} — {balance} 💰\n"
        
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("bonus"))
async def bonus_cmd(message: Message):
    user_id = message.from_user.id
    _, last_bonus = get_user(user_id)
    current_time = int(time.time())
    
    if current_time - last_bonus >= 86400:
        bonus_amount = 670
        cursor.execute("UPDATE users SET last_bonus = ? WHERE user_id = ?", (current_time, user_id))
        update_balance(user_id, bonus_amount)
        await message.answer(f"🎁 Ты получил ежедневный бонус: +{bonus_amount} монет!\nПриходи завтра.")
    else:
        time_left = 86400 - (current_time - last_bonus)
        hours = time_left // 3600
        minutes = (time_left % 3600) // 60
        await message.answer(f"⏳ Следующий бонус будет доступен через {hours} ч. {minutes} мин.")

@dp.message(Command("promo"))
async def use_promo_cmd(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("❌ Введи промокод. Пример: /promo WIN")
    code = command.args.strip()
    user_id = message.from_user.id
    get_user(user_id, message.from_user.username)
    
    cursor.execute("SELECT reward, uses_left FROM promocodes WHERE code = ?", (code,))
    promo = cursor.fetchone()
    if not promo or promo[1] <= 0:
        return await message.answer("❌ Промокод не существует или уже закончился.")
        
    cursor.execute("SELECT 1 FROM claimed_promos WHERE user_id = ? AND code = ?", (user_id, code))
    if cursor.fetchone():
        return await message.answer("❌ Ты уже активировал этот код.")
        
    cursor.execute("INSERT INTO claimed_promos (user_id, code) VALUES (?, ?)", (user_id, code))
    cursor.execute("UPDATE promocodes SET uses_left = uses_left - 1 WHERE code = ?", (code,))
    update_balance(user_id, promo[0])
    await message.answer(f"✅ Успешно! Получено {promo[0]} монет.")

# --- СИСТЕМА СТАВОК ---

@dp.message(Command("matches"))
async def list_matches(message: Message):
    cursor.execute("SELECT match_id, team1, team2, coef_p1, coef_x, coef_p2 FROM matches WHERE status = 'open'")
    matches = cursor.fetchall()
    if not matches:
        return await message.answer("🏒 В данный момент нет открытых матчей для ставок.")
        
    text = "🏒 Линия матчей:\n\n"
    for m in matches:
        text += (f"🆔 Матч #{m[0]}\n"
                 f"⚔️ {m[1]} — {m[2]}\n"
                 f"📊 Кэфы: П1 [{m[3]}] | Х [{m[4]}] | П2 [{m[5]}]\n"
                 f"👉 Сделать ставку: /bet {m[0]}\n\n")
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("bet"))
async def start_bet(message: Message, command: CommandObject, state: FSMContext):
    if not command.args:
        return await message.answer("❌ Укажи ID матча. Например: /bet 1")
    try:
        match_id = int(command.args)
    except ValueError:
        return await message.answer("❌ ID матча должен быть цифрой.")
        
    cursor.execute("SELECT team1, team2, coef_p1, coef_x, coef_p2 FROM matches WHERE match_id = ? AND status = 'open'", (match_id,))
    match = cursor.fetchone()
    if not match:
        return await message.answer("❌ Матч не найден или закрыт.")
        
    await state.update_data(match_id=match_id, team1=match[0], team2=match[1], coef_p1=match[2], coef_x=match[3], coef_p2=match[4])
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"П1 ({match[0]})"), KeyboardButton(text="Ничья (Х)"), KeyboardButton(text=f"П2 ({match[1]})")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(f"Матч: {match[0]} — {match[1]}\nВыберите исход:", reply_markup=kb)
    await state.set_state(BetStates.choosing_outcome)

@dp.message(BetStates.choosing_outcome)
async def process_outcome(message: Message, state: FSMContext):
    text = message.text
    if "П1" in text: outcome = "P1"
    elif "Ничья" in text or "Х" in text: outcome = "X"
    elif "П2" in text: outcome = "P2"
    else:
        return await message.answer("❌ Выбери исход кнопкой на клавиатуре снизу.")
        
    await state.update_data(outcome=outcome)
    await message.answer("Введите сумму ставки (целым числом):", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BetStates.entering_amount)

@dp.message(BetStates.entering_amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
    except ValueError:
        await state.clear()
        return await message.answer("❌ Ставка отменена. Сумма должна быть числом.", reply_markup=ReplyKeyboardRemove())
        
    if amount <= 0:
        await state.clear()
        return await message.answer("❌ Ставка отменена. Сумма должна быть больше нуля.")
        
    user_id = message.from_user.id
    balance, _ = get_user(user_id)
    
    if amount > balance:
        await state.clear()
        return await message.answer("❌ Недостаточно средств на балансе. Ставка отменена.")
        
    data = await state.get_data()
    match_id = data['match_id']
    outcome = data['outcome']
    
    update_balance(user_id, -amount)
    cursor.execute("INSERT INTO bets (user_id, match_id, outcome, amount) VALUES (?, ?, ?, ?)", 
                   (user_id, match_id, outcome, amount))
    conn.commit()
    
    out_text = {"P1": data['team1'], "X": "Ничья", "P2": data['team2']}[outcome]
    await message.answer(f"✅ Ставка принята!\n\nМатч #{match_id}\nИсход: {out_text}\nСумма: {amount} монет.")
    await state.clear()

@dp.message(Command("mybets"))
async def my_bets_cmd(message: Message):
    cursor.execute('''
        SELECT b.match_id, m.team1, m.team2, b.outcome, b.amount, b.status 
        FROM bets b JOIN matches m ON b.match_id = m.match_id 
        WHERE b.user_id = ? ORDER BY b.bet_id DESC LIMIT 10
    ''', (message.from_user.id,))
    bets = cursor.fetchall()
    if not bets:
        return await message.answer("💬 У тебя пока нет истории ставок.")
        
    text = "📊 Твои последние 10 ставок:\n\n"
    for b in bets:
        status_ru = "⏳ В ожидании" if b[5] == "pending" else ("✅ Выигрыш" if b[5] == "won" else ("❌ Проигрыш" if b[5] == "lost" else "🔄 Возврат"))
        text += f"Матч #{b[0]} | {b[1]}-{b[2]} | Исход: {b[3]} | {b[4]} 💰 | {status_ru}\n"
    await message.answer(text, parse_mode="Markdown")

# --- АДМИН-ПАНЕЛЬ ---

@dp.message(Command("addmatch"))
async def add_match_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("📝 Формат: /addmatch Команда1 Команда2 КоэфП1 КоэфХ КоэфП2\nПример: /addmatch ЦСКА СКА 2.2 3.8 2.4")
    
    try:
        parts = command.args.split()
        t1, t2 = parts[0], parts[1]
        c_p1, c_x, c_p2 = float(parts[2]), float(parts[3]), float(parts[4])
        
        cursor.execute("INSERT INTO matches (team1, team2, coef_p1, coef_x, coef_p2) VALUES (?, ?, ?, ?, ?)", 
                       (t1, t2, c_p1, c_x, c_p2))
        conn.commit()
        match_id = cursor.lastrowid
        await message.answer(f"✅ Матч #{match_id} [{t1} — {t2}] успешно добавлен в линию!")
    except Exception:
        await message.answer("❌ Ошибка. Проверь пробелы и формат коэффициентов.")

@dp.message(Command("endmatch"))
async def end_match_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) != 2:
        return await message.answer("📝 Формат: /endmatch [ID] [P1, X или P2]\nПример: /endmatch 1 P1")
        
    match_id, result = command.args.split()
    if result not in ["P1", "X", "P2"]:
        return await message.answer("❌ Исход должен быть строго P1, X или P2")
        
    cursor.execute("UPDATE matches SET status = ? WHERE match_id = ?", (f"c_{result.lower()}", match_id))
    cursor.execute("SELECT bet_id, user_id, outcome, amount FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,))
    bets = cursor.fetchall()
    
    cursor.execute("SELECT coef_p1, coef_x, coef_p2 FROM matches WHERE match_id = ?", (match_id,))
    coefs = cursor.fetchone()
    if not coefs:
        return await message.answer("❌ Матч с таким ID не найден.")
        
    coef_dict = {"P1": coefs[0], "X": coefs[1], "P2": coefs[2]}
    
    for bet_id, u_id, outcome, amount in bets:
        if outcome == result:
            win_money = int(amount * coef_dict[result])
            cursor.execute("UPDATE bets SET status = 'won' WHERE bet_id = ?", (bet_id,))
            update_balance(u_id, win_money)
            try:
                await bot.send_message(u_id, f"🎉 Твоя ставка на матч #{match_id} сыграла! Выигрыш: {win_money} 💰")
            except Exception: pass
        else:
            cursor.execute("UPDATE bets SET status = 'lost' WHERE bet_id = ?", (bet_id,))
            try:
                await bot.send_message(u_id, f"❌ Ставка на матч #{match_id} проиграла.")
            except Exception: pass
            
    conn.commit()
    await message.answer(f"🏁 Матч #{match_id} закрыт (Победил {result}). Выплаты произведены.")

@dp.message(Command("cancelmatch"))
async def cancel_match_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("📝 Формат: /cancelmatch [ID_матча]")
        
    match_id = command.args.strip()
    cursor.execute("UPDATE matches SET status = 'cancelled' WHERE match_id = ?", (match_id,))
    cursor.execute("SELECT bet_id, user_id, amount FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,))
    bets = cursor.fetchall()
    
    for bet_id, u_id, amount in bets:
        cursor.execute("UPDATE bets SET status = 'refund' WHERE bet_id = ?", (bet_id,))
        update_balance(u_id, amount)
        try:
            await bot.send_message(u_id, f"🔄 Матч #{match_id} отменен. Твоя ставка {amount} 💰 возвращена.")
        except Exception: pass
        
    conn.commit()
    await message.answer(f"✅ Матч #{match_id} отменен. Всем игрокам сделан возврат средств.")

@dp.message(Command("give"))
async def give_money_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) != 2:
        return await message.answer("📝 Формат: /give [ID_игрока] [Сумма]")
    try:
        to_user_id, amount = map(int, command.args.split())
        get_user(to_user_id)
        update_balance(to_user_id, amount)
        await message.answer(f"✅ Успешно начислено {amount} монет игроку {to_user_id}.")
        await bot.send_message(to_user_id, f"💰 Администратор выдал тебе {amount} монет!")
    except Exception:
        await message.answer("❌ ID и сумма должны быть числами.")

@dp.message(Command("addpromo"))
async def add_promo_cmd(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) != 3:
        return await message.answer("📝 Формат: /addpromo [КОД] [СУММА] [АКТИВАЦИИ]")
    try:
        code, reward, uses = command.args.split()
        cursor.execute("INSERT INTO promocodes (code, reward, uses_left) VALUES (?, ?, ?)", (code, int(reward), int(uses)))
        conn.commit()
        await message.answer(f"🎫 Промокод {code} успешно создан!")
    except sqlite3.IntegrityError:
        await message.answer("❌ Такой промокод уже существует.")
    except Exception:
        await message.answer("❌ Ошибка формата.")

# --- ЗАПУСК ВЕБ-СЕРВЕРА ДЛЯ ОБХОДА ОГРАНИЧЕНИЙ RENDER ---
async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Бот успешно работает!"))
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
