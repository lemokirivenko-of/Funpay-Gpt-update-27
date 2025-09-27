
from __future__ import annotations

import requests
from typing import TYPE_CHECKING, Optional, Dict, List
from cardinal import Cardinal  # type: ignore

if TYPE_CHECKING:
    from cardinal import Cardinal  # type: ignore

from FunPayAPI.updater.events import NewMessageEvent  # type: ignore
from FunPayAPI.types import MessageTypes  # type: ignore
import logging
import json
from functools import lru_cache
import hashlib
import datetime
import random

try:
    from g4f.client import Client  # type: ignore
except ImportError:
    from pip._internal.cli.main import main
    main(["install", "g4f"])
    from g4f.client import Client

logger = logging.getLogger("FPC.ChatGPT-Seller")
LOGGER_PREFIX = "ChatGPT-Seller"
logger.info(f"{LOGGER_PREFIX} Активен")

NAME = "ChatGPT-Seller-Pro"
VERSION = "1.0.1"
DESCRIPTION = """
ChatGPT Seller Pro (fix):
- Подпись бота только при первом сообщении в чате.
- Сценарии: покупка, продажа, связь с продавцом, приветствия, благодарности.
- Анти-дублирование: повторный вопрос → напоминание ждать ответа.
- Короткие, дружелюбные и маркетинговые тексты.
"""
CREDITS = "@Lemon_Kirovenko"
UUID = "bb3e52b2-55e7-4b5e-bf64-92c1c117ac01"
SETTINGS_PAGE = True

SETTINGS = {
    "api_key": "",
    "send_response": True,
    "black_list_handle": True,
    "prompt": "Ты — помощник-продавца на FunPay. Отвечай коротко, дружелюбно, по делу. Без лишней официальщины."
}

last_responses: Dict[int, str] = {}
BOT_INTRO_SENT: Dict[int, bool] = {}  # хранит, показывалась ли подпись в этом чате
RESPONSE_CACHE: Dict = {}

BOT_SIGNATURE = "🤖 Сейчас с вами общается бот, а не продавец."

# --- Вспомогательные функции ---
def create_cache_key(messages: list, model: str) -> str:
    message_str = json.dumps(
        [(msg['role'], msg['content']) for msg in messages],
        sort_keys=True
    )
    return hashlib.md5(f"{model}:{message_str}".encode()).hexdigest()

@lru_cache(maxsize=1024)
def get_cached_response(cache_key: str) -> Optional[str]:
    return RESPONSE_CACHE.get(cache_key)

def shorten_text(text: str, max_sentences: int = 3) -> str:
    sentences = text.split(".")
    if len(sentences) > max_sentences:
        return ".".join(sentences[:max_sentences]).strip() + "..."
    return text.strip()

def generate_response(messages: list, model: str) -> Optional[str]:
    try:
        cache_key = create_cache_key(messages, model)
        cached_response = get_cached_response(cache_key)
        if cached_response is not None:
            return cached_response

        response = Client().chat.completions.create(model=model, messages=messages)
        response_content = response.choices[0].message.content

        response_content = shorten_text(response_content)

        RESPONSE_CACHE[cache_key] = response_content
        return response_content
    except Exception as e:
        logger.error(f"Error generating response: {e}")
        return None

def time_based_greeting() -> str:
    now = datetime.datetime.now().hour
    if 7 <= now < 12:
        return "Доброе утро ☀️"
    elif 12 <= now < 18:
        return "Добрый день 👋"
    elif 18 <= now < 23:
        return "Добрый вечер 🌙"
    else:
        return "Здравствуйте! Ночь 🌙, отвечу утром 😉"

def random_choice(options: List[str]) -> str:
    return random.choice(options)

# --- Основная логика ответа ---
def create_response(chat_id: int, message_text: str, prompt: str) -> Optional[str]:
    try:
        text = (message_text or "").lower()

        # Анти-дублирование
        if last_responses.get(chat_id) and last_responses[chat_id].lower() in text:
            return "✅ Уже писал об этом, дождитесь ответа."

        # Приветствия
        if any(word in text for word in ["привет", "здравствуй", "хай", "hello", "hi"]):
            return f"{time_based_greeting()} 🚀 Быстрые сделки и безопасные покупки с Sexy5Mil"

        # Продажа аккаунтов
        if any(word in text for word in ["продам", "продаю", "продать", "скупка", "хочу продать"]):
            return "Скупаем аккаунты! Ожидайте, мы свяжемся для уточнения деталей."

        # Покупка аккаунтов
        if any(word in text for word in ["купить", "покупка", "лот", "ссылка", "хочу купить"]):
            return "Зайдите на мой профиль и выберите подходящий товар. Всё удобно и быстро 😉"

        # Связь с продавцом
        if any(word in text for word in ["связаться", "менеджер", "реальный человек", "продавец"]):
            return "Я передам запрос владельцу, ожидайте ответа."

        # Благодарности / подтверждения
        if text in ["спасибо", "спс", "thx", "thanks"]:
            return random_choice(["Рад помочь 🙂", "Всегда пожалуйста 👍", "Обращайтесь 😉"])
        if text in ["ок", "хорошо", "ладно", "понял", "ясно", "ага"]:
            return random_choice(["Хорошо 👍", "Окей 😉", "Понял ✅"])

        # GPT fallback
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": message_text},
        ]
        return generate_response(messages, model="gpt-4o-mini")

    except Exception as e:
        logger.error(f"Ошибка при создании ответа: {e}")
        return None

def format_response(chat_id: int, response: str) -> str:
    # Добавляем подпись только один раз в чате
    if not BOT_INTRO_SENT.get(chat_id, False):
        BOT_INTRO_SENT[chat_id] = True
        return f"{response}\n\n{BOT_SIGNATURE}"
    return response

def handle_message(c: Cardinal, chat_id: int, message_text: str) -> None:
    response = create_response(chat_id, message_text, SETTINGS["prompt"])
    if not response:
        return

    response = format_response(chat_id, response)

    if last_responses.get(chat_id) == response:
        logger.info(f"Повторный ответ для chat_id {chat_id}, не отправляем.")
        return

    last_responses[chat_id] = response
    c.send_message(chat_id, response)

def contains_url(text: str) -> bool:
    return "http" in text or "https" in text

# --- Хуки Cardinal ---
def bind_to_new_message(c: Cardinal, e: NewMessageEvent):
    try:
        if SETTINGS["send_response"]:
            if e.message.chat_name in c.blacklist and not SETTINGS['black_list_handle']:
                return

            msg = e.message
            if e.message.type != MessageTypes.NON_SYSTEM or e.message.author_id == c.account.id:
                return

            msg = msg.text.lower()
            if contains_url(msg):
                return

            handle_message(c, e.message.chat_id, msg)
    except Exception as e:
        logger.error(e)

def init(c: Cardinal):
    with open("storage/plugins/GPTseller.json", "w", encoding="UTF-8") as f:
        global SETTINGS
        f.write(json.dumps(SETTINGS, indent=4, ensure_ascii=False))

BIND_TO_NEW_MESSAGE = [bind_to_new_message]
BIND_TO_DELETE = None
BIND_TO_PRE_INIT = [init]
