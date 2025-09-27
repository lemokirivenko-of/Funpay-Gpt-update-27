from __future__ import annotations

from typing import Final, Dict, Any, TYPE_CHECKING
from enum import Enum
import re
import json
import logging
import os
import random
import time

from FunPayAPI.updater.events import NewMessageEvent
from FunPayAPI.types import MessageTypes
from FunPayAPI.common.utils import RegularExpressions

if TYPE_CHECKING:
    from cortex import Cortex
    from tg_bot.bot import TGBot

# Try import g4f, attempt install if missing
try:
    from g4f.client import Client
except Exception:
    try:
        from pip._internal.cli.main import main as pip_main
        pip_main(["install", "-U", "g4f"])
        from g4f.client import Client
    except Exception:
        Client = None  # will handle later

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, Message, CallbackQuery, ForceReply
import telebot
from tg_bot import CBT

# Plugin metadata
NAME = "GPT отзывы"
VERSION = "1.2"
DESCRIPTION = "Авто-ответы на отзывы через G4F — гибрид фикс/оригинал."
CREDITS = "Автор: @beedge, фикс: ChatGPT"
UUID = "b93bfb30-03ef-42ef-ad13-d406cc60b353"
SETTINGS_PAGE = True
SETTINGS_PAGE_HANDLER_NAME = "open_plugin_settings_handler"

# Globals
cortex_instance: Cortex | None = None
bot: telebot.TeleBot | None = None
client = Client() if Client is not None else None

user_edit_states: Dict[int, Dict[str, Any]] = {}

LOGGER_PREFIX = "[ChatGPT-Reviews]"
CONFIG_DIR = os.path.join("storage", "plugins", "chatgpt_reviews")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# Settings
MAX_WORDS: Final[int] = 120
MAX_CHARACTERS: Final[int] = 700
MIN_STARS: Final[int] = 0
ANSWER_ONLY_ON_NEW_FEEDBACK: Final[bool] = True
MAX_ATTEMPTS: Final[int] = 5
MINIMUM_RESPONSE_LENGTH: Final[int] = 15

CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fff]')
logger = logging.getLogger("FPC.ChatGPT-Reviews")

# Callbacks/keys
CB_MAIN_MENU = "gpt_main_menu"
CB_EDIT_PROMPT_PREFIX = "gpt_edit_prompt_"
CB_TOGGLE_PREFIX = "gpt_toggle_status_"
CB_CANCEL_EDIT = "gpt_cancel_edit"

DEFAULT_PROMPT = """
Привет! Ты - ИИ Ассистент в нашем интернет-магазине игровых ценностей. 
    Давай посмотрим детали заказа и составим отличный ответ для покупателя! 😊

    Информация о покупателе и заказе:

    - Имя: {name}
    - Товар: {item}
    - Стоимость: {cost} рублей
    - Оценка: {rating} из 5
    - Отзыв: {text}

    Твоя задача:
    - Ответить покупателю в доброжелательном тоне. 🙏 
    - Использовать много эмодзи (даже если это не всегда уместно 😄).
    - Обязательно учесть информацию о покупателе и заказе.
    - Сделать так, чтобы покупатель остался доволен. 😌
    - Написать большой и развернутый ответ. 
    - Пожелать что-нибудь хорошее покупателю. ✨
    - В конце добавить шутку, связанную с покупателем или его заказом. 😂

    Важно:
    - Не упоминать интернет-ресурсы. 
    - Не использовать оскорбления, ненормативную лексику, противозаконную или политическую информацию.
    - НЕ ВЫДАВАТЬ ФРАГМЕНТЫ КОДА ИЛИ ЛИСТИНГИ КОДА НА ЛЮБЫХ ЯЗЫКАХ ПРОГРАММИРОВАНИЯ.
    - НЕ ИСПОЛЬЗОВАТЬ MARKDOWN, HTML ИЛИ ДРУГУЮ РАЗМЕТКУ.
"""

DEFAULT_CONFIG = {
    f"star_{i}": {"prompt": DEFAULT_PROMPT, "enabled": True} for i in range(1, 6)
}
DEFAULT_CONFIG["star_all"] = {"prompt": DEFAULT_PROMPT, "enabled": False}


class G4FModels(Enum):
    # keep a short, safe list and rely on retries
    GPT4O = "gpt-4o"
    GPT4O_MINI = "gpt-4o-mini"
    GPT35 = "gpt-3.5-turbo"

# ---------------------------
# Config helpers
# ---------------------------
def ensure_config_dir():
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)

def load_config() -> Dict[str, Any]:
    ensure_config_dir()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            is_updated = False
            for key, value in DEFAULT_CONFIG.items():
                if key not in config:
                    config[key] = value
                    is_updated = True
            if is_updated:
                save_config(config)
            return config
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при загрузке {CONFIG_FILE}: {e}. Возврат к дефолту.")
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

def save_config(config: Dict[str, Any]) -> bool:
    ensure_config_dir()
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении конфигурации: {e}")
        return False

def get_prompt_for_stars(stars: int) -> str:
    config = load_config()
    key = "star_all" if stars == 0 else f"star_{stars}"
    return config.get(key, {}).get("prompt", DEFAULT_PROMPT)

def is_star_enabled(stars: int) -> bool:
    config = load_config()
    key = "star_all" if stars == 0 else f"star_{stars}"
    return config.get(key, {}).get("enabled", False)

def toggle_star(stars: int) -> bool:
    config = load_config()
    if stars == 0:
        config["star_all"]["enabled"] = not config["star_all"]["enabled"]
        if config["star_all"]["enabled"]:
            for i in range(1, 6):
                config[f"star_{i}"]["enabled"] = False
    else:
        config[f"star_{stars}"]["enabled"] = not config[f"star_{stars}"]["enabled"]
        if all(not config[f"star_{i}"]["enabled"] for i in range(1, 6)):
            config["star_all"]["enabled"] = True
    return save_config(config)

# ---------------------------
# Core logic
# ---------------------------
def log_info(text: str) -> None:
    logger.info(f"{LOGGER_PREFIX} {text}")

def log_error(text: str, e: Exception) -> None:
    logger.error(f"{LOGGER_PREFIX} {text}: {e}", exc_info=True)

def replace_items(prompt: str, order) -> str:
    # Подставляем данные заказа; если данных нет — оставляем пустую строку
    replacements = {
        "{category}": getattr(order.subcategory, "name", "") if hasattr(order, "subcategory") else "",
        "{categoryfull}": getattr(order.subcategory, "fullname", "") if hasattr(order, "subcategory") else "",
        "{cost}": str(order.sum) if getattr(order, "sum", None) is not None else "",
        "{disc}": getattr(order, "short_description", ""),
        "{rating}": str(getattr(order.review, "stars", "")) if getattr(order, "review", None) else "",
        "{name}": getattr(order, "buyer_username", "Покупатель"),
        "{item}": getattr(order, "title", ""),
        "{text}": getattr(order.review, "text", "") if getattr(order, "review", None) else ""
    }
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, str(value))
    return prompt

def need_regenerate(content: str) -> bool:
    if not isinstance(content, str) or not content:
        return True
    if re.search(CHINESE_PATTERN, content):
        return True
    content = content.replace("Generated by BLACKBOX.AI, try unlimited chat https://www.blackbox.ai", "")
    if len(content.strip()) < MINIMUM_RESPONSE_LENGTH:
        return True
    bad_phrases = ["blackbox.ai", "unable to decode", "model not found", "request ended", "```"]
    return any(phrase in content.lower() for phrase in bad_phrases)

def g4f_generate_response(prompt: str) -> str:
    if client is None:
        return ""
    models_to_try = list(G4FModels)
    for attempt in range(MAX_ATTEMPTS):
        model = random.choice(models_to_try)
        try:
            response = client.chat.completions.create(
                model=model.value,
                messages=[{"role": "user", "content": prompt}]
            )
            content = response.choices[0].message.content
            if not need_regenerate(content):
                return content
            log_info(f"Ответ от {model.value} неудовлетворителен, пробую снова...")
        except Exception as e:
            log_error(f"Ошибка при генерации ответа с моделью {model.value} (попытка {attempt+1})", e)
            time.sleep(1)
    log_error("Не удалось сгенерировать корректный ответ после нескольких попыток.", Exception("Max attempts reached"))
    return ""

def smart_truncate(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    truncated_text = text[:max_length]
    last_sentence_end = -1
    for sep in ['.', '!', '?', '…']:
        pos = truncated_text.rfind(sep)
        if pos > last_sentence_end:
            last_sentence_end = pos
    if last_sentence_end > 0:
        return truncated_text[:last_sentence_end + 1]
    last_space = truncated_text.rfind(' ')
    if last_space > 0:
        return truncated_text[:last_space] + "..."
    return truncated_text + "..."

def message_handler(cortex: Cortex, event: NewMessageEvent) -> None:
    try:
        if ANSWER_ONLY_ON_NEW_FEEDBACK and event.message.type != MessageTypes.NEW_FEEDBACK:
            return
        if event.message.type not in [MessageTypes.NEW_FEEDBACK, MessageTypes.FEEDBACK_CHANGED]:
            return
        order_id = RegularExpressions().ORDER_ID.findall(str(event.message))[0][1:]
        order = cortex.account.get_order(order_id)
        if not order or not order.review:
            return
        stars = order.review.stars
        if MIN_STARS > 0 and stars < MIN_STARS:
            return
        if not is_star_enabled(stars) and not is_star_enabled(0):
            return
        prompt_template = get_prompt_for_stars(0) if is_star_enabled(0) else get_prompt_for_stars(stars)
        final_prompt = replace_items(prompt_template, order)
        response_text = g4f_generate_response(final_prompt)
        if not response_text:
            log_error(f"Не удалось сгенерировать ответ для заказа #{order.id}.", Exception("Empty response"))
            return
        response_text = " ".join(response_text.splitlines())
        # Убираем "Не указано" — если нет названия товара, не добавляем пустые заголовки
        title = order.title if getattr(order, "title", None) else ""
        if title:
            response_details = f"(づ ◕‿◕ )づ 🛍 [{title}]\n\n{response_text}"
        else:
            response_details = f"(づ ◕‿◕ )づ 🛍\n\n{response_text}"
        final_response = smart_truncate(response_details, MAX_CHARACTERS)
        log_info(f"Сгенерирован ответ для отзыва на заказ #{order.id} ({stars}⭐). Длина: {len(final_response)}.")
        cortex.account.send_review(order_id=order.id, text=final_response)
    except Exception as e:
        log_error("Ошибка в обработчике отзывов", e)

# ---------------------------
# Telegram integration
# ---------------------------
def get_main_keyboard(current_offset: int) -> K:
    kb = K(row_width=2)
    config = load_config()
    for i in range(1, 6):
        is_enabled = config[f"star_{i}"]["enabled"]
        status_emoji = "🟢" if is_enabled else "🔴"
        kb.add(
            B(f"{'⭐' * i}", callback_data=f"{CB_EDIT_PROMPT_PREFIX}{i}"),
            B(f"{status_emoji} Вкл/Выкл", callback_data=f"{CB_TOGGLE_PREFIX}{i}")
        )
    is_all_enabled = config["star_all"]["enabled"]
    all_status_emoji = "🟢" if is_all_enabled else "🔴"
    kb.row(
        B("✨ Все звезды", callback_data=f"{CB_EDIT_PROMPT_PREFIX}0"),
        B(f"{all_status_emoji} Вкл/Выкл", callback_data=f"{CB_TOGGLE_PREFIX}0")
    )
    kb.row(B("⬅️ Назад к плагинам", callback_data=f"{CBT.PLUGINS_LIST}:{current_offset}"))
    return kb

def show_main_menu(chat_id: int, message_id: int | None, current_offset: int):
    if not bot: return
    keyboard = get_main_keyboard(current_offset)
    text = """🤖 <b>Управление GPT-ответами на отзывы</b>

Здесь вы можете настроить, на какие отзывы и как будет отвечать бот.

• Нажмите на <b>звездочки</b> (⭐), чтобы отредактировать текст-промпт.
• Нажмите на <b>статус</b> (🟢/🔴), чтобы включить или отключить автоответ.

✨ <b>Все звезды</b> — общий шаблон для всех отзывов.
"""
    try:
        if message_id:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard, parse_mode="HTML")
        else:
            bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        log_error("Не удалось показать меню", e)

def process_new_prompt(message: Message):
    if not bot: return
    user_id = message.from_user.id
    state = user_edit_states.get(user_id)
    if not state:
        return
    star_num = state["star_num"]
    original_message_id = state["message_id"]
    original_offset = state["offset"]
    config = load_config()
    prompt_key = "star_all" if star_num == 0 else f"star_{star_num}"
    config[prompt_key]["prompt"] = message.text.strip()
    if save_config(config):
        star_text = "всех оценок" if star_num == 0 else f"{star_num}⭐"
        bot.send_message(message.chat.id, f"✅ Промпт для <b>{star_text}</b> обновлен!", parse_mode="HTML")
    else:
        bot.send_message(message.chat.id, "❌ Ошибка при сохранении.")
    show_main_menu(message.chat.id, original_message_id, original_offset)
    del user_edit_states[user_id]

def open_plugin_settings_handler(c: Cortex, call: CallbackQuery):
    try:
        current_offset = int(call.data.split(':')[-1])
    except (IndexError, ValueError):
        current_offset = 0
    show_main_menu(call.message.chat.id, call.message.message_id, current_offset)
    bot.answer_callback_query(call.id)

def init_commands(c_: Cortex):
    global cortex_instance, bot
    cortex_instance = c_
    if not hasattr(cortex_instance, 'telegram') or not hasattr(cortex_instance.telegram, 'bot'):
        log_error("Бот не инициализирован.", Exception("Telegram bot not found"))
        return
    bot = cortex_instance.telegram.bot
    log_info("Инициализация обработчиков Telegram.")

    @bot.callback_query_handler(func=lambda call: call.data.startswith(CB_TOGGLE_PREFIX))
    def cb_toggle_star(call: CallbackQuery):
        star_num = int(call.data.split('_')[-1])
        if star_num == 0:
            if any(is_star_enabled(i) for i in range(1, 6)):
                bot.answer_callback_query(call.id, "⚠️ Сначала отключите отдельные звезды!", show_alert=True)
                return
        if toggle_star(star_num):
            show_main_menu(call.message.chat.id, call.message.message_id, 0)
        else:
            bot.answer_callback_query(call.id, "❌ Ошибка сохранения!", show_alert=True)

    @bot.callback_query_handler(func=lambda call: call.data.startswith(CB_EDIT_PROMPT_PREFIX))
    def cb_edit_prompt(call: CallbackQuery):
        star_num = int(call.data.split('_')[-1])
        offset = 0
        user_edit_states[call.from_user.id] = {"star_num": star_num, "message_id": call.message.message_id, "offset": offset}
        current_prompt = get_prompt_for_stars(star_num)
        star_text = "всех оценок" if star_num == 0 else f"{'⭐' * star_num}"
        cancel_button = K().add(B("🚫 Отмена", callback_data=CB_CANCEL_EDIT))
        bot.edit_message_text(
            f"📝 <b>Редактирование промпта для {star_text}</b>\n\n"
            f"<b>Текущий:</b>\n<code>{current_prompt}</code>\n\n"
            "Отправьте новый текст промпта:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=cancel_button,
            parse_mode="HTML"
        )
        msg = bot.send_message(call.message.chat.id, "👇 Введите новый промпт:", reply_markup=ForceReply(selective=True))
        bot.register_for_reply(msg, process_new_prompt)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == CB_CANCEL_EDIT)
    def cb_cancel_edit(call: CallbackQuery):
        state = user_edit_states.pop(call.from_user.id, None)
        message_id = state.get("message_id") if state else call.message.message_id
        offset = state.get("offset", 0) if state else 0
        show_main_menu(call.message.chat.id, message_id, offset)
        bot.answer_callback_query(call.id, "Отменено.")

BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_PRE_INIT = [init_commands]
BIND_TO_DELETE = None
