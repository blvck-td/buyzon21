import logging
import re
import random
import string
import sqlite3
import uuid
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Импорт токена из файла config.py (файл должен находиться в корневой папке проекта)
from config import botkey

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Состояния диалога ---
(CHOOSING_CATEGORY, GETTING_PRICE, AFTER_CALC, ORDER_NAME, ORDER_LINK,
 ORDER_SCREENSHOT, FINISH_ORDER, PROMO_INPUT, ORDER_RECEIPT) = range(9)

# Только для этих ID доступна админ-панель
ADMIN_IDS: Set[int] = {733949485, 619771192}
DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")

# Промокоды и реферальные данные (в памяти)
promo_codes: Dict[str, Dict[str, Any]] = {}
referral_usage: Dict[str, Any] = {}

# ================= Работа с БД =================

def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                user_id INTEGER,
                username TEXT,
                category TEXT,
                price_yuan REAL,
                commission REAL,
                final_price REAL,
                order_name TEXT,
                order_link TEXT,
                status TEXT,
                created_at TEXT,
                screenshot TEXT,
                receipt TEXT,
                discount INTEGER,
                promo_code_used TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                referral_code TEXT,
                bonus INTEGER DEFAULT 0
            )
        ''')
        conn.commit()

def db_insert_order(order: Dict[str, Any]) -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO orders 
            (order_id, user_id, username, category, price_yuan, commission, final_price, order_name, order_link, status, created_at, screenshot, receipt, discount, promo_code_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order["order_id"],
            order["user_id"],
            order["username"],
            order["category"],
            order["price_yuan"],
            order["commission"],
            order["final_price"],
            order["order_name"],
            order["order_link"],
            order["status"],
            order["created_at"],
            order.get("screenshot"),
            order.get("receipt"),
            order.get("discount"),
            order.get("promo_code_used"),
        ))
        conn.commit()

def db_update_order_status(order_id: str, new_status: str) -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status=? WHERE order_id=?", (new_status, order_id))
        conn.commit()

def db_get_orders() -> List[sqlite3.Row]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders")
        return cur.fetchall()

def db_get_user_orders(user_id: int) -> List[sqlite3.Row]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE user_id=?", (user_id,))
        return cur.fetchall()

def db_set_referral_code(user_id: int, code: str) -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, referral_code, bonus) VALUES (?, ?, COALESCE((SELECT bonus FROM users WHERE user_id=?), 0))",
            (user_id, code, user_id)
        )
        conn.commit()

def db_get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT referral_code, bonus FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def db_update_user_bonus(user_id: int, bonus_change: int) -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT bonus FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if row:
            new_bonus = row["bonus"] + bonus_change
            cur.execute("UPDATE users SET bonus=? WHERE user_id=?", (new_bonus, user_id))
        else:
            cur.execute("INSERT INTO users (user_id, referral_code, bonus) VALUES (?, ?, ?)", (user_id, "", bonus_change))
        conn.commit()

# ================= Вспомогательные функции =================

def generate_random_code(length: int = 6) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def generate_order_id() -> str:
    return str(uuid.uuid4())

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        ["💼 Личный кабинет", "🧮 Рассчитать"],
        ["💬 Поддержка"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_categories_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("👕 Одежда", callback_data="Одежда")],
        [InlineKeyboardButton("👟 Обувь", callback_data="Обувь")],
        [InlineKeyboardButton("👜 Аксессуары", callback_data="Аксессуары")],
        [InlineKeyboardButton("🎒 Сумки", callback_data="Сумки")],
        [InlineKeyboardButton("⌚ Часы", callback_data="Часы")],
        [InlineKeyboardButton("💐 Парфюм", callback_data="Парфюм")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ================= ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЬСКОГО ИНТЕРФЕЙСА =================

# /start – всегда очищает данные и возвращает начальный экран с категориями
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    args = update.message.text.split()
    if len(args) > 1:
        context.user_data["referral_received"] = args[1]
    try:
        with open("category.jpg", "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption="Добро пожаловать! Выберите категорию:",
                reply_markup=get_categories_inline_keyboard()
            )
    except Exception as e:
        logger.error("Ошибка при отправке category.jpg: %s", e)
        await update.message.reply_text("Добро пожаловать! Выберите категорию:",
                                        reply_markup=get_categories_inline_keyboard())
    return CHOOSING_CATEGORY

async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["category"] = query.data
    try:
        await query.edit_message_caption(caption=f"Вы выбрали: {query.data}")
    except Exception as e:
        logger.error("Ошибка редактирования подписи: %s", e)
    try:
        media = []
        for file in ["instructions1.jpg", "instructions2.jpg"]:
            if os.path.exists(file):
                media.append(InputMediaPhoto(media=open(file, "rb")))
        if media:
            await context.bot.send_media_group(chat_id=query.message.chat_id, media=media)
    except Exception as e:
        logger.error("Ошибка отправки медиа-группы: %s", e)
    await query.message.reply_text("Введите цену в юанях:")
    return GETTING_PRICE

async def calculate_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price_yuan = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Введите корректное число.")
        return GETTING_PRICE
    commission = 2500 if price_yuan > 3000 else 1500
    final_price = price_yuan * 13 + commission
    category = context.user_data.get("category", "не указана")
    context.user_data["order"] = {
        "user_id": update.effective_user.id,
        "username": update.effective_user.username or update.effective_user.first_name,
        "category": category,
        "price_yuan": price_yuan,
        "commission": commission,
        "final_price": final_price,
        "status": "создан",
        "created_at": datetime.now().isoformat(),
    }
    response_text = (
        f"**Рассчёт стоимости**\n"
        f"Категория: {category}\n"
        f"Цена в юанях: {price_yuan}\n"
        f"Курс: 13\n"
        f"Комиссия: {commission}\n"
        f"**Итоговая стоимость: {final_price}₽**"
    )
    await update.message.reply_text(response_text)
    keyboard = [
        [
            InlineKeyboardButton("🔄 Новый расчёт", callback_data="new_calc"),
            InlineKeyboardButton("🛒 Сделать заказ", callback_data="make_order"),
        ]
    ]
    await update.message.reply_text("Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AFTER_CALC

async def after_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "new_calc":
        return await start(update, context)
    elif query.data == "make_order":
        text_prompt = "Укажите название заказа.\n(Например: 👟 Кроссовки Nike Air Max 96, 44 размер, жёлто-белые)"
        try:
            await query.edit_message_caption(caption=text_prompt)
        except Exception:
            await query.edit_message_text(text_prompt)
        return ORDER_NAME

async def order_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_name = update.message.text.strip()
    context.user_data["order"]["order_name"] = order_name
    try:
        with open("link.jpg", "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption="Что покупаем?\nУкажите ссылку на товар с сайта Poizon 🔗"
            )
    except Exception as e:
        logger.error("Ошибка при отправке link.jpg: %s", e)
        await update.message.reply_text("Укажите ссылку на товар с сайта Poizon 🔗")
    return ORDER_LINK

async def order_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text_received = update.message.text.strip()
    match = re.search(r"(https?://\S+)", text_received)
    extracted_link = match.group(0) if match else text_received
    context.user_data["order"]["order_link"] = extracted_link
    try:
        with open("screenorder.jpg", "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption="Отправьте скриншот, на котором видно: Товар, размер, цвет"
            )
    except Exception as e:
        logger.error("Ошибка при отправке screenorder.jpg: %s", e)
        await update.message.reply_text("Отправьте скриншот, на котором видно: Товар, размер, цвет")
    return ORDER_SCREENSHOT

async def order_screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo_file_id = update.message.photo[-1].file_id
    context.user_data["order"]["screenshot"] = photo_file_id
    order = context.user_data.get("order")
    if order:
        basket: List[Dict[str, Any]] = context.user_data.get("basket", [])
        basket.append(order)
        context.user_data["basket"] = basket
        final_text = (
            f"Название: {order['order_name']}\n"
            f"Итоговая стоимость: {order['final_price']}₽\n"
            f"Ссылка: {order['order_link']}\n"
            f"Статус: {order['status']}"
        )
        keyboard = [
            [
                InlineKeyboardButton("➕ Добавить товар", callback_data="add_product"),
                InlineKeyboardButton("✅ Завершить заказ", callback_data="finish_order"),
            ]
        ]
        await update.message.reply_photo(
            photo=photo_file_id,
            caption=final_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text("Ошибка: данные заказа отсутствуют.")
    return FINISH_ORDER

# При завершении заказа: если запущен по реферальной ссылке – автоматически применяется скидка 300₽.
async def order_finalization_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "add_product":
        return await start(update, context)
    elif query.data == "finish_order":
        basket: List[Dict[str, Any]] = context.user_data.get("basket", [])
        if not basket:
            await query.edit_message_text("Корзина пуста.")
            return ConversationHandler.END
        for item in basket:
            item["order_id"] = generate_order_id()
            db_insert_order(item)
        total_cost = sum(item["final_price"] for item in basket)
        details = "Ваш заказ:\n"
        for item in basket:
            details += (
                f"ID: {item['order_id']}. {item['order_name']} – {item['final_price']}₽\n"
                f"Ссылка: {item['order_link']}\n"
            )
        details += f"\nОбщая стоимость: {total_cost}₽"
        if context.user_data.get("referral_received"):
            discount = 300
            new_total = max(total_cost - discount, 0)
            details += f"\nОбщая стоимость со скидкой: {new_total}₽\nПромокод (реферальный) использован. Скидка {discount}₽ применена."
            await context.bot.send_message(chat_id=query.message.chat_id, text=details)
            payment_text = (
                "Заказ проверен нашими менеджерами и готов к оформлению.\n"
                "Доставка по России оплачивается отдельно.\n"
                "Мы выкупаем товар в течение 72 часов после оплаты. Товар будет у нас примерно через 25 дней.\n"
                f"Итоговая стоимость с учетом скидки: {new_total}₽\n"
                "Для оплаты переведите сумму на карту Альфа-Банк: 79955006566\n"
                "Внимательно проверяйте получателя!\n"
                "После оплаты отправьте фото квитанции."
            )
            await context.bot.send_message(chat_id=query.message.chat_id, text=payment_text)
            return ORDER_RECEIPT
        else:
            prompt_text = "Если у вас есть промокод или реферальный код, введите его. Чтобы использовать бонусы, введите 'БОНУС'. Если нет, введите 'Нет'."
            full_text = details + "\n" + prompt_text
            try:
                await query.edit_message_text(full_text)
            except Exception:
                await context.bot.send_message(chat_id=query.message.chat_id, text=full_text)
            return PROMO_INPUT

async def promo_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    promo_input = update.message.text.strip()
    order = context.user_data.get("order")
    discount = 300
    user_id = update.effective_user.id
    if promo_input.lower() == "нет":
        final_price = order["final_price"]
    elif promo_input.lower() == "бонус":
        user_data = db_get_user(user_id)
        bonus_value = user_data["bonus"] if user_data else 0
        if bonus_value > 0:
            final_price = max(order["final_price"] - bonus_value, 0)
            order["discount"] = bonus_value
            order["promo_code_used"] = "БОНУС"
            db_update_user_bonus(user_id, -bonus_value)
            await update.message.reply_text(f"Бонусы применены! Скидка {bonus_value}₽ получена.")
        else:
            await update.message.reply_text("У вас недостаточно бонусов.")
            final_price = order["final_price"]
    else:
        valid = False
        if promo_input in promo_codes:
            data = promo_codes[promo_input]
            if data["type"] == "one-time" and user_id in data["used_by"]:
                valid = False
            else:
                valid = True
                data["used_by"].add(user_id)
        if not valid:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM users WHERE referral_code=?", (promo_input,))
                row = cur.fetchone()
            if row:
                owner = row["user_id"]
                if owner != user_id:
                    valid = True
        if valid:
            final_price = max(order["final_price"] - discount, 0)
            order["discount"] = discount
            order["promo_code_used"] = promo_input
            await update.message.reply_text(f"Код принят! Скидка {discount}₽ применена.")
        else:
            await update.message.reply_text("Введённый код недействителен. Скидка не применена.")
            final_price = order["final_price"]
    order["final_price"] = final_price
    payment_text = (
        "Заказ проверен нашими менеджерами и готов к оформлению.\n"
        "Доставка по России оплачивается отдельно.\n"
        "Мы выкупаем товар в течение 72 часов после оплаты. Товар будет у нас примерно через 25 дней.\n"
        f"Итоговая стоимость с учетом скидки: {final_price}₽\n"
        "Для оплаты переведите сумму на карту Альфа-Банк: 79955006566\n"
        "Внимательно проверяйте получателя!\n"
        "После оплаты отправьте фото квитанции."
    )
    await update.message.reply_text(payment_text)
    return ORDER_RECEIPT

async def order_receipt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    receipt_file_id = update.message.photo[-1].file_id
    basket: List[Dict[str, Any]] = context.user_data.get("basket", [])
    if basket:
        basket[-1]["receipt"] = receipt_file_id
        order = basket[-1]
        order["status"] = "на_подтверждении"
        db_update_order_status(order["order_id"], order["status"])
        # Обращаемся к данным через индексирование
        discount_value = order["discount"] if order["discount"] is not None else 0
        admin_text = (
            f"Заказ №{order['order_id']} перешёл в статус 'на_подтверждении'.\n"
            f"Пользователь: {order['username']} (ID: {order['user_id']})\n"
            f"Название: {order['order_name']}\n"
            f"Ссылка: {order['order_link']}\n"
            f"Итоговая стоимость: {order['final_price']}₽\n"
            f"Скидка: {discount_value}₽\n"
            f"Квитанция: получена"
        )
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=receipt_file_id,
                    caption=admin_text,
                )
            except Exception as e:
                logger.error("Ошибка уведомления админа: %s", e)
        await update.message.reply_text("Квитанция получена. Ваш заказ передан в обработку!")
    else:
        await update.message.reply_text("Ошибка: заказ не найден.")
    return ConversationHandler.END

# ================= ОБРАБОТЧИКИ ЛИЧНОГО КАБИНЕТА =================

async def personal_cabinet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_orders = db_get_user_orders(user_id)
    total_sum = sum(o["final_price"] for o in user_orders) if user_orders else 0
    user_data = db_get_user(user_id)
    if user_data is None or user_data["referral_code"] is None:
        new_ref = generate_random_code()
        db_set_referral_code(user_id, new_ref)
        ref_code = new_ref
        bonus = 0
    else:
        ref_code = user_data["referral_code"]
        bonus = user_data["bonus"]
    text = (
        f"💼 Личный кабинет:\n\n"
        f"История заказов: {len(user_orders)}\n"
        f"Общая сумма заказов: {total_sum}₽\n"
        f"Ваш бонус: {bonus}₽\n\n"
        f"Ваш реферальный код: {ref_code}\n\n"
        "Выберите пункт меню:"
    )
    keyboard = [
        [InlineKeyboardButton("📝 История заказов", callback_data="cabinet_history")],
        [InlineKeyboardButton("🔗 Реферальная программа", callback_data="referral_program")],
        [InlineKeyboardButton("🧮 Новый расчёт", callback_data="new_calc_cabinet")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def cabinet_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    orders_list = db_get_user_orders(user_id)
    if not orders_list:
        text = "У вас пока нет заказов."
    else:
        text = "📝 История заказов:\n\n"
        for o in orders_list:
            text += (
                f"ID: {o['order_id']}\n"
                f"Название: {o['order_name']}\n"
                f"Статус: {o['status']}\n"
                f"Стоимость: {o['final_price']}₽\n\n"
            )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="personal_cabinet")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def referral_program_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    user_data = db_get_user(user_id)
    if user_data is None or user_data["referral_code"] is None:
        new_ref = generate_random_code()
        db_set_referral_code(user_id, new_ref)
        ref_code = new_ref
    else:
        ref_code = user_data["referral_code"]
    referral_link = f"t.me/{context.bot.username}?start={ref_code}"
    text = (
        "🔗 Реферальная программа:\n\n"
        "Приглашайте друзей и получите скидку 300₽ на первый заказ!\n\n"
        f"Ваша реферальная ссылка:\n{referral_link}\n\n"
        "Каждый пользователь может получить скидку по чужому коду только один раз."
    )
    keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="personal_cabinet")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def new_calc_cabinet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        with open("category.jpg", "rb") as photo:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=photo,
                caption="Новый расчёт. Выберите категорию:",
                reply_markup=get_categories_inline_keyboard()
            )
    except Exception as e:
        logger.error("Ошибка при отправке category.jpg: %s", e)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Новый расчёт. Выберите категорию:",
            reply_markup=get_categories_inline_keyboard()
        )

async def personal_cabinet_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    logger.info("Личный кабинет: нажата кнопка %s", data)
    if data == "cabinet_history":
        await cabinet_history_callback(update, context)
    elif data == "referral_program":
        await referral_program_callback(update, context)
    elif data == "new_calc_cabinet":
        await new_calc_cabinet_callback(update, context)
    elif data == "personal_cabinet":
        await personal_cabinet_handler(update, context)

# ================= ОБРАБОТЧИКИ АДМИН-ПАНЕЛИ =================

async def admin_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("Нет доступа.")
        else:
            await update.callback_query.answer("Нет доступа.", show_alert=True)
        return
    keyboard = [
        [InlineKeyboardButton("📦 Заказы", callback_data="admin_menu_orders")],
        [InlineKeyboardButton("🏷️ Промокоды", callback_data="admin_menu_promos")],
        [InlineKeyboardButton("📊 Аналитика", callback_data="admin_menu_analytics")],
    ]
    # Если это сообщение, используем update.message, иначе редактируем сообщение callback
    if update.message:
        await update.message.reply_text("Админ-консоль:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.edit_message_text("Админ-консоль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "admin_main":
        await admin_main_menu_handler(update, context)
    elif data == "admin_menu_orders":
        await admin_orders_list_handler(update, context)
    elif data == "admin_menu_promos":
        keyboard = [
            [InlineKeyboardButton("Просмотр промокодов", callback_data="admin_list_promos")],
            [InlineKeyboardButton("Добавить промокод", callback_data="admin_add_promo")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_main")],
        ]
        await query.edit_message_text("Меню промокодов:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "admin_menu_analytics":
        orders_db = db_get_orders()
        paid_orders = [o for o in orders_db if o["status"].lower() in
                       ["оплачен", "выкуплен", "отправлен в РФ", "прибыл", "отправлен внутри РФ", "доставлен"]]
        total_count = len(paid_orders)
        total_sum = sum(o["final_price"] for o in paid_orders)
        text = f"📊 Аналитика:\nОплаченные заказы: {total_count}\nОбщая сумма: {total_sum}₽"
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_main")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_orders_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    orders = db_get_orders()
    if not orders:
        await query.edit_message_text("Нет заказов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_main")]]))
        return
    keyboard = []
    for order in orders:
        keyboard.append([InlineKeyboardButton(f"ID: {order['order_id']}, {order['order_name']}", callback_data=f"admin_order:{order['order_id']}")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_main")])
    await query.edit_message_text("Список заказов:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # callback_data: "admin_order:{order_id}"
    order_id = query.data.split(":", 1)[1]
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        order = cur.fetchone()
    if not order:
        await query.edit_message_text("Заказ не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu_orders")]]))
        return
    # Обращаемся к значениям через индексирование
    discount_value = order["discount"] if order["discount"] is not None else 0
    details = (
        f"ID: {order['order_id']}\n"
        f"Пользователь: {order['username']} (ID: {order['user_id']})\n"
        f"Категория: {order['category']}\n"
        f"Цена: {order['price_yuan']}\n"
        f"Комиссия: {order['commission']}\n"
        f"Итог: {order['final_price']}\n"
        f"Название: {order['order_name']}\n"
        f"Ссылка: {order['order_link']}\n"
        f"Статус: {order['status']}\n"
        f"Дата: {order['created_at']}\n"
        f"Квитанция: {'Да' if order['receipt'] else 'Нет'}\n"
        f"Скидка: {discount_value}₽\n"
        f"Промокод: {order['promo_code_used'] if order['promo_code_used'] is not None else '-'}"
    )
    statuses = ["создан", "на_подтверждении", "оплачен", "выкуплен", "ждет отправки", "отправлен в РФ", "прибыл", "отправлен внутри РФ", "доставлен"]
    keyboard = []
    row = []
    for status in statuses:
        row.append(InlineKeyboardButton(status.capitalize(), callback_data=f"update:{order['order_id']}:{status}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu_orders")])
    await query.edit_message_text(details, reply_markup=InlineKeyboardMarkup(keyboard))

async def update_order_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.edit_message_text("Неверный формат данных.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu_orders")]]))
        return
    _, order_id, new_status = parts
    db_update_order_status(order_id, new_status)
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        order = cur.fetchone()
    if order:
        client_message = f"Ваш заказ (ID: {order_id}) изменил статус на '{new_status}'."
        try:
            await context.bot.send_message(chat_id=order["user_id"], text=client_message)
        except Exception as e:
            logger.error("Ошибка отправки уведомления клиенту: %s", e)
    await query.edit_message_text(f"Статус заказа {order_id} обновлён на '{new_status}'.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu_orders")]]))

async def payment_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Пожалуйста, отправьте фото квитанции об оплате.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="personal_cabinet")]]))

# Дополнительные админ-команды
async def orders_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    orders_db = db_get_orders()
    text = "Список заказов:\n"
    for o in orders_db:
        text += f"ID: {o['order_id']}, {o['order_name']} — {o['status']}\n"
    await update.message.reply_text(text)

async def order_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Используйте: /order_details <order_id>")
        return
    order_id = args[0]
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        order = cur.fetchone()
    if not order:
        await update.message.reply_text("Заказ не найден.")
        return
    discount_value = order["discount"] if order["discount"] is not None else 0
    details = (
        f"ID: {order['order_id']}\n"
        f"Пользователь: {order['username']} (ID: {order['user_id']})\n"
        f"Категория: {order['category']}\n"
        f"Цена: {order['price_yuan']}\n"
        f"Комиссия: {order['commission']}\n"
        f"Итог: {order['final_price']}\n"
        f"Название: {order['order_name']}\n"
        f"Ссылка: {order['order_link']}\n"
        f"Статус: {order['status']}\n"
        f"Дата: {order['created_at']}\n"
        f"Квитанция: {'Да' if order['receipt'] else 'Нет'}\n"
        f"Скидка: {discount_value}₽\n"
        f"Промокод: {order['promo_code_used'] if order['promo_code_used'] is not None else '-'}"
    )
    await update.message.reply_text(details)

async def addpromo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Используйте: /addpromo <код> <тип: one-time/multi> <скидка>")
        return
    code = args[0]
    promo_type = args[1]
    try:
        discount = int(args[2])
    except ValueError:
        await update.message.reply_text("Скидка должна быть числом.")
        return
    promo_codes[code] = {"type": promo_type, "discount": discount, "used_by": set()}
    await update.message.reply_text(f"Промокод {code} добавлен.")

async def listpromos_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    text = "Активные промокоды:\n"
    for code, d in promo_codes.items():
        text += f"{code} – тип: {d['type']}, скидка: {d['discount']}₽, использован: {len(d['used_by'])} раз(а)\n"
    await update.message.reply_text(text)

# ================= Команды поддержки и меню =================

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Главное меню:", reply_markup=get_main_menu_keyboard())

async def support_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Свяжитесь с нашим менеджером: t.me/blvck_td")

# ================= Основной запуск =================

def main() -> None:
    init_db()
    application = Application.builder().token(botkey).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_CATEGORY: [CallbackQueryHandler(category_chosen)],
            GETTING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, calculate_price)],
            AFTER_CALC: [CallbackQueryHandler(after_calc, pattern="^(new_calc|make_order)$")],
            ORDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_name_handler)],
            ORDER_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_link_handler)],
            ORDER_SCREENSHOT: [MessageHandler(filters.PHOTO, order_screenshot_handler)],
            FINISH_ORDER: [CallbackQueryHandler(order_finalization_callback, pattern="^(add_product|finish_order)$")],
            PROMO_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, promo_input_handler)],
            ORDER_RECEIPT: [MessageHandler(filters.PHOTO, order_receipt_handler)],
        },
        fallbacks=[CommandHandler("cancel", lambda update, context: update.message.reply_text("Операция отменена. Для нового расчёта введите /start."))],
    )

    # Пользовательские команды
    application.add_handler(CommandHandler("menu", menu_handler))
    application.add_handler(CommandHandler("cabinet", personal_cabinet_handler))
    application.add_handler(CommandHandler("calculate", calculate_price))
    application.add_handler(CommandHandler("support", support_handler))
    
    # Обработчик кнопок личного кабинета
    application.add_handler(CallbackQueryHandler(
        personal_cabinet_menu_handler, pattern=r"^(cabinet_history|referral_program|new_calc_cabinet|personal_cabinet)$"
    ))
    
    # Админ-команды (доступ проверяется в функциях)
    application.add_handler(CommandHandler("admin", admin_main_menu_handler))
    application.add_handler(CallbackQueryHandler(admin_menu_handler, pattern=r"^(admin_main|admin_menu_orders|admin_menu_promos|admin_menu_analytics)$"))
    application.add_handler(CallbackQueryHandler(admin_orders_list_handler, pattern=r"^admin_orders_list$"))
    application.add_handler(CallbackQueryHandler(admin_order_callback, pattern=r"^admin_order:\S+"))
    application.add_handler(CallbackQueryHandler(update_order_status_callback, pattern=r"^update:\S+:\S+"))
    application.add_handler(CallbackQueryHandler(payment_confirmation_callback, pattern=r"^confirm_payment$"))
    application.add_handler(CommandHandler("orders_status", orders_status_handler))
    application.add_handler(CommandHandler("order_details", order_details_handler))
    application.add_handler(CommandHandler("addpromo", addpromo_handler))
    application.add_handler(CommandHandler("listpromos", listpromos_handler))
    
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
