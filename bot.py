# -*- coding: utf-8 -*-
import os
import re
import logging
import sqlite3
import requests
import asyncio
import random
import string
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)

# ===== CAP.GURU =====
CAP_GURU_KEY = os.environ.get("CAP_GURU_KEY", "")  # вставь ключ сюда или в env

def capguru_solve_funcaptcha(public_key: str, page_url: str, proxy: dict = None) -> str | None:
    """
    Решает FunCaptcha (Arkose Labs) через cap.guru (AntiCaptcha-совместимый API).
    Возвращает token или None.
    """
    if not CAP_GURU_KEY:
        logging.warning("CAP_GURU_KEY не задан")
        return None

    try:
        # Создаём задачу
        task = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websitePublicKey": public_key,
        }
        if proxy:
            # Если хотим с прокси
            host_port = proxy.get("https", "").replace("http://", "")
            if "@" in host_port:
                creds, hp = host_port.rsplit("@", 1)
                user, pwd = creds.split(":", 1)
                host, port = hp.rsplit(":", 1)
                task["type"] = "FunCaptchaTask"
                task["proxyType"] = "http"
                task["proxyAddress"] = host
                task["proxyPort"] = int(port)
                task["proxyLogin"] = user
                task["proxyPassword"] = pwd

        r = requests.post("https://api.cap.guru/createTask", json={
            "clientKey": CAP_GURU_KEY,
            "task": task,
        }, timeout=15)
        data = r.json()
        task_id = data.get("taskId")
        if not task_id:
            logging.error("cap.guru createTask failed: %s", data)
            return None

        logging.info("cap.guru task created: %s", task_id)

        # Поллим результат до 120 сек
        import time as _t
        for _ in range(24):
            _t.sleep(5)
            r2 = requests.post("https://api.cap.guru/getTaskResult", json={
                "clientKey": CAP_GURU_KEY,
                "taskId": task_id,
            }, timeout=15)
            res = r2.json()
            status = res.get("status")
            logging.info("cap.guru poll: %s", status)
            if status == "ready":
                token = res.get("solution", {}).get("token")
                logging.info("cap.guru token: %s", (token or "")[:60])
                return token
            if status == "failed" or res.get("errorId"):
                logging.error("cap.guru failed: %s", res)
                return None

    except Exception as e:
        logging.error("cap.guru error: %s", e)
    return None

# ===== ПРОКСИ =====
# Формат: host:port:user:pass
_PROXY_LIST = [
    "dc.decodo.com:10001:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10002:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10003:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10004:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10005:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10006:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10007:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10008:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10009:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
    "dc.decodo.com:10010:sp13uinlcj:3lS0aw8eHMcJr~tm4v",
]
_proxy_index = 0
_proxy_lock = __import__("threading").Lock()

def get_proxy():
    """Возвращает следующий прокси по кругу (round-robin)."""
    global _proxy_index
    with _proxy_lock:
        entry = _PROXY_LIST[_proxy_index % len(_PROXY_LIST)]
        _proxy_index += 1
    host, port, user, passwd = entry.split(":")
    proxy_url = f"http://{user}:{passwd}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}

def get_proxy_url():
    """Возвращает строку прокси для Playwright."""
    global _proxy_index
    with _proxy_lock:
        entry = _PROXY_LIST[_proxy_index % len(_PROXY_LIST)]
        _proxy_index += 1
    host, port, user, passwd = entry.split(":")
    return {
        "server": f"http://{host}:{port}",
        "username": user,
        "password": passwd,
    }

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.environ.get("CRYPTO_BOT_TOKEN")
ADMIN_IDS = set(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")  # ID канала для логов

# 20 потоков для одновременной работы
executor = ThreadPoolExecutor(max_workers=20)

pending_payments = {}

WAITING_RULES = 0
WAITING_MENU = 1
WAITING_COOKIE = 2
WAITING_BUY = 3
WAITING_PAYMENT = 4
WAITING_PROMO = 5
WAITING_ADMIN = 6
WAITING_ADMIN_BROADCAST = 7
WAITING_ADMIN_ADD_BALANCE = 8
WAITING_ADMIN_ADD_BALANCE_AMOUNT = 9
WAITING_ADMIN_CREATE_PROMO = 10

PRICE = 0.20
DISCOUNTS = {5: 0.05, 10: 0.10, 25: 0.15}

DB_PATH = "bot_data.db"

# ===== БАЗА ДАННЫХ =====

def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            attempts INTEGER DEFAULT 0,
            spent REAL DEFAULT 0.0,
            lang TEXT DEFAULT 'ru'
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            attempts INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS used_promos (
            user_id INTEGER,
            code TEXT,
            PRIMARY KEY (user_id, code)
        )
    """)
    conn.commit()
    conn.close()

def db_get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, attempts, spent FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def db_upsert_user(user_id, username, attempts_delta=0, spent_delta=0.0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, attempts, spent)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            attempts=attempts + ?,
            spent=spent + ?
    """, (user_id, username, max(0, attempts_delta), spent_delta,
          attempts_delta, spent_delta))
    conn.commit()
    conn.close()

def db_set_attempts(user_id, username, new_attempts):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, attempts, spent)
        VALUES (?, ?, ?, 0.0)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            attempts=?
    """, (user_id, username, new_attempts, new_attempts))
    conn.commit()
    conn.close()

def db_get_attempts(user_id):
    row = db_get_user(user_id)
    return row[1] if row else 0

def db_get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, attempts, spent FROM users ORDER BY spent DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_top(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, spent FROM users WHERE spent > 0 ORDER BY spent DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_promo(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT attempts, uses, max_uses FROM promo_codes WHERE code=?", (code,))
    row = c.fetchone()
    conn.close()
    return row

def db_create_promo(code, attempts, max_uses):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO promo_codes (code, attempts, uses, max_uses)
        VALUES (?, ?, 0, ?)
    """, (code, attempts, max_uses))
    conn.commit()
    conn.close()

def db_use_promo(user_id, code):
    """Возвращает (успех, сообщение, кол-во попыток)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT attempts, uses, max_uses FROM promo_codes WHERE code=?", (code,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False, "Промокод не найден!", 0
    attempts, uses, max_uses = row
    if uses >= max_uses:
        conn.close()
        return False, "Промокод уже использован!", 0
    c.execute("SELECT 1 FROM used_promos WHERE user_id=? AND code=?", (user_id, code))
    if c.fetchone():
        conn.close()
        return False, "Вы уже использовали этот промокод!", 0
    c.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
    c.execute("INSERT INTO used_promos (user_id, code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    conn.close()
    return True, "OK", attempts

def db_get_all_promos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, attempts, uses, max_uses FROM promo_codes")
    rows = c.fetchall()
    conn.close()
    return rows

def db_get_lang(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "ru"

def db_set_lang(user_id, lang):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, user_id))
    conn.commit()
    conn.close()


# ===== ТЕКСТЫ =====

TEXTS = {
    "ru": {
        "main_menu_title": "*Roblox Age Verification Bot*",
        "main_menu_sub": "Сервис для получения ссылки by @dedbed12",
        "main_menu_attempts": "Ваши попытки: *{attempts}*",
        "main_menu_action": "Выберите действие ниже:",
        "btn_get_link": "Получить ссылку",
        "btn_buy": "Купить попытки",
        "btn_promo": "Промокод",
        "btn_top": "Топ пользователей",
        "btn_help": "Помощь",
        "btn_support": "Саппорт",
        "btn_admin": "Админ панель",
        "btn_back": "Назад",
        "btn_cancel": "Отмена",
        "btn_to_admin": "В админку",
        "btn_buy_1": "1 попытка - $0.20",
        "btn_buy_5": "5 попыток - $0.95 (5%)",
        "btn_buy_10": "10 попыток - $1.80 (10%)",
        "btn_buy_25": "25 попыток - $4.25 (15%)",
        "help_text": (
            "*Помощь*\n\n"
            "*Как пользоваться:*\n"
            "1. Купите попытки\n"
            "2. Нажмите Получить ссылку\n"
            "3. Отправьте `.ROBLOSECURITY` cookie\n"
            "4. Выберите Camera или ID\n"
            "5. Получите ссылку на верификацию\n\n"
            "*Важно:*\n"
            "- При технической ошибке попытка возвращается\n"
            "- При невалидном cookie попытка возвращается\n"
            "- Переведите страницу на English и нажмите кнопку Reset\n"
            "- Владелец не несёт ответственности за поломку cookie"
        ),
        "top_title": "*Топ пользователей*\n",
        "top_empty": "Пока никто не совершил покупок.",
        "buy_title": "*Покупка попыток*",
        "buy_price": "Цена: *$0.20* за попытку",
        "buy_discount": "Скидки: 5 шт -5%, 10 шт -10%, 25 шт -15%",
        "promo_title": "*Активация промокода*",
        "promo_enter": "Введите промокод:",
        "promo_ok": "Промокод активирован! +*{attempts}* попыток\nВсего: *{total}*",
        "promo_not_found": "Промокод не найден!",
        "promo_used": "Промокод уже использован!",
        "promo_used_by_you": "Вы уже использовали этот промокод!",
        "no_attempts": "Нет попыток!",
        "btn_buy_short": "Купить",
        "checking_cookie": "Проверяю cookie...",
        "invalid_cookie": "Неверный cookie! Попробуй снова.\n\n/start",
        "processing": "Аккаунт: *{name}*\nМетод: *{method}*\nОсталось попыток: *{attempts}*\n\nЖди 20-30 сек...",
        "browser_fallback": "API не ответил, открываю браузер...\nЖди до 30 сек...",
        "error_returned": "Техническая ошибка - попытка возвращена!\n\nПричина: {reason}\n\n/start",
        "not_clicked_returned": "Кнопка не найдена - попытка возвращена!\n\n/start",
        "link_received": "*Ссылка получена!*\n\n⏱ Действует *10 минут* — используй сразу!\n🔗 Одноразовая ссылка:",
        "failed_returned": "❌ Не удалось получить ссылку — попытка возвращена!",
        "method_camera": "Camera",
        "method_id": "ID",
        "service_camera": "Ссылка Camera (Лицо)",
        "service_id": "Ссылка ID (Паспорт)",
        "log_success": "✅ УСПЕШНО",
        "log_fail": "❌ НЕУДАЧА",
        "log_result_ok": "Ссылка получена",
        "log_result_fail": "Не удалось получить ссылку",
        "anon": "Аноним",
        "choose_method": "*Выберите метод верификации:*",
        "btn_camera": "📷 Camera (Лицо)",
        "btn_id": "🪪 ID (Паспорт)",
        "invoice_created": "*Счёт создан!*\n\nСумма: *${amount}*\nПопыток: *{attempts}*\n\nОплати и нажми Проверить оплату:",
        "btn_check_pay": "✅ Проверить оплату",
        "btn_pay": "💳 Оплатить",
        "pay_ok": "*Оплата получена!*\nНачислено *{attempts}* попыток\nВсего: *{total}*",
        "pay_pending": "Оплата ещё не получена. Попробуй позже.",
        "pay_error": "Ошибка создания счёта!",
        "lang_choose": "🌐 Выберите язык / Choose language:",
        "lang_set": "✅ Язык установлен: Русский",
        "cancelled": "Отменено. /start",
        "ask_2fa": "На каком аккаунте получить ссылку?",
        "btn_no_2fa": "🔓 Без 2FA",
        "btn_with_2fa": "🔐 С 2FA",
        "2fa_ask_cookie": "🔐 *Режим 2FA*\n\nОтправь `.ROBLOSECURITY` cookie\nСообщение будет удалено автоматически",
        "2fa_wip": "⚙️ Функция в доработке!\n\nПоддержка аккаунтов с 2FA скоро появится.",
    },
    "en": {
        "main_menu_title": "*Roblox Age Verification Bot*",
        "main_menu_sub": "Service for getting verification link by @dedbed12",
        "main_menu_attempts": "Your attempts: *{attempts}*",
        "main_menu_action": "Choose an action below:",
        "btn_get_link": "Get link",
        "btn_buy": "Buy attempts",
        "btn_promo": "Promo code",
        "btn_top": "Top users",
        "btn_help": "Help",
        "btn_support": "Support",
        "btn_admin": "Admin panel",
        "btn_back": "Back",
        "btn_cancel": "Cancel",
        "btn_to_admin": "To admin",
        "btn_buy_1": "1 attempt - $0.20",
        "btn_buy_5": "5 attempts - $0.95 (5%)",
        "btn_buy_10": "10 attempts - $1.80 (10%)",
        "btn_buy_25": "25 attempts - $4.25 (15%)",
        "help_text": (
            "*Help*\n\n"
            "*How to use:*\n"
            "1. Buy attempts\n"
            "2. Press Get link\n"
            "3. Send your `.ROBLOSECURITY` cookie\n"
            "4. Choose Camera or ID\n"
            "5. Get the verification link\n\n"
            "*Important:*\n"
            "- Attempt is returned on technical error\n"
            "- Attempt is returned on invalid cookie\n"
            "- Translate the page to English and click Reset\n"
            "- Owner is not responsible for cookie damage"
        ),
        "top_title": "*Top users*\n",
        "top_empty": "No purchases yet.",
        "buy_title": "*Buy attempts*",
        "buy_price": "Price: *$0.20* per attempt",
        "buy_discount": "Discounts: 5 pcs -5%, 10 pcs -10%, 25 pcs -15%",
        "promo_title": "*Promo code activation*",
        "promo_enter": "Enter promo code:",
        "promo_ok": "Promo activated! +*{attempts}* attempts\nTotal: *{total}*",
        "promo_not_found": "Promo code not found!",
        "promo_used": "Promo code already used!",
        "promo_used_by_you": "You have already used this promo code!",
        "no_attempts": "No attempts left!",
        "btn_buy_short": "Buy",
        "checking_cookie": "Checking cookie...",
        "invalid_cookie": "Invalid cookie! Try again.\n\n/start",
        "processing": "Account: *{name}*\nMethod: *{method}*\nAttempts left: *{attempts}*\n\nPlease wait 20-30 sec...",
        "browser_fallback": "API didn't respond, opening browser...\nWait up to 30 sec...",
        "error_returned": "Technical error - attempt returned!\n\nReason: {reason}\n\n/start",
        "not_clicked_returned": "Button not found - attempt returned!\n\n/start",
        "link_received": "*Link received!*\n\n⏱ Valid for *10 minutes* — use it now!\n🔗 One-time link:",
        "failed_returned": "❌ Failed to get link — attempt returned!",
        "method_camera": "Camera",
        "method_id": "ID",
        "service_camera": "Camera link (Face)",
        "service_id": "ID link (Passport)",
        "log_success": "✅ SUCCESS",
        "log_fail": "❌ FAILURE",
        "log_result_ok": "Link received",
        "log_result_fail": "Failed to get link",
        "anon": "Anonymous",
        "choose_method": "*Choose verification method:*",
        "btn_camera": "📷 Camera (Face)",
        "btn_id": "🪪 ID (Passport)",
        "invoice_created": "*Invoice created!*\n\nAmount: *${amount}*\nAttempts: *{attempts}*\n\nPay and press Check payment:",
        "btn_check_pay": "✅ Check payment",
        "btn_pay": "💳 Pay",
        "pay_ok": "*Payment received!*\nAdded *{attempts}* attempts\nTotal: *{total}*",
        "pay_pending": "Payment not received yet. Try later.",
        "pay_error": "Invoice creation error!",
        "lang_choose": "🌐 Выберите язык / Choose language:",
        "lang_set": "✅ Language set: English",
        "cancelled": "Cancelled. /start",
        "ask_2fa": "Which account type do you want to verify?",
        "btn_no_2fa": "🔓 Without 2FA",
        "btn_with_2fa": "🔐 With 2FA",
        "2fa_ask_cookie": "🔐 *2FA Mode*\n\nSend your `.ROBLOSECURITY` cookie\nMessage will be deleted automatically",
        "2fa_wip": "⚙️ Feature in development!\n\nSupport for 2FA accounts is coming soon.",
    }
}

def t(user_id, key, **kwargs):
    """Получить текст на языке пользователя"""
    lang = db_get_lang(user_id)
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))
    if kwargs:
        text = text.format(**kwargs)
    return text


# ===== PLAYWRIGHT =====

INJECT_SCRIPT = r"""
(function() {
    window.__capturedData = null;
    var TARGET = 'age-verification-service/v1/persona-id-verification/start-verification';
    function extractLinks(text) {
        var links = [];
        try {
            var data = JSON.parse(text);
            var find = function(obj, path) {
                if (!obj || typeof obj !== 'object') return;
                for (var k in obj) {
                    var cur = path ? path + '.' + k : k;
                    var v = obj[k];
                    if (typeof v === 'string') {
                        if (k.toLowerCase().indexOf('url') !== -1 ||
                            k.toLowerCase().indexOf('link') !== -1 ||
                            k.toLowerCase().indexOf('inquiry') !== -1 ||
                            v.indexOf('http') === 0) {
                            links.push({ path: cur, url: v });
                        }
                    } else if (typeof v === 'object') { find(v, cur); }
                }
            };
            find(data, '');
        } catch(e) {
            var m = text.match(/https?:\/\/[^\s"'<>]+/g);
            if (m) for (var i = 0; i < m.length; i++) links.push({ path: 'match_' + i, url: m[i] });
        }
        return links;
    }
    var _fetch = window.fetch;
    window.fetch = async function() {
        var args = Array.prototype.slice.call(arguments);
        var url = typeof args[0] === 'string' ? args[0] : (args[0] instanceof Request ? args[0].url : String(args[0]));
        var method = ((args[1] && args[1].method) || (args[0] instanceof Request ? args[0].method : 'GET')).toUpperCase();
        var res = await _fetch.apply(this, args);
        if (method === 'POST' && url.indexOf(TARGET) !== -1) {
            try {
                var body = await res.clone().text();
                window.__capturedData = { url: url, body: body, links: extractLinks(body) };
            } catch(e) {}
        }
        return res;
    };
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m, u) { this._m = m; this._u = u; return _open.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function(b) {
        var self = this;
        if (this._m && this._m.toUpperCase() === 'POST' && this._u && this._u.indexOf(TARGET) !== -1) {
            this.addEventListener('loadend', function() {
                if (self.status >= 200 && self.status < 300) {
                    try { window.__capturedData = { url: self._u, body: self.responseText, links: extractLinks(self.responseText) }; } catch(e) {}
                }
            }, { once: true });
        }
        return _send.apply(this, arguments);
    };
})();
"""

CAMERA_TEXTS = [
    "Continue with camera", "Continue with Camera",
    "Continuar con la camara", "Continuar com camera",
    "Continuer avec la camera", "Mit Kamera fortfahren",
    "Continua con la fotocamera", "Продолжить с камерой",
    "Doorgaan met camera", "Kontynuuj z kamera",
    "Kamerayla devam et", "Lanjutkan dengan kamera",
    "camera", "Camera",
]

ID_TEXTS = [
    "Continue with ID", "Continue with Id",
    "Continuar con ID", "Continuar com ID",
    "Continuer avec ID", "Mit Ausweis fortfahren",
    "Continua con ID", "Продолжить с удостоверением",
    "Doorgaan met ID", "Kontynuuj z dowodem",
    "Kimlikle devam et", "Lanjutkan dengan ID",
    "Government ID", "ID document",
]

RESET_TEXTS = [
    "Reset", "Start over", "Try again", "Restart",
    "Restablecer", "Reiniciar", "Reinitialiser",
    "Zurucksetzen", "Reimposta", "Сбросить",
    "Opnieuw", "Zresetuj", "Sifirla", "Atur ulang",
]

CONTINUE_TEXTS = [
    # Основные — для 13+ и других подтверждений
    "Continue", "Continue »", "Continue >",
    # Другие языки
    "Continuar", "Continuer", "Fortfahren",
    "Continua", "Продолжить",
    "Doorgaan", "Kontynuuj", "Devam",
    "Lanjutkan", "Tiep tuc",
    # Без Camera/ID — exclude их в вызове
    "Next", "Proceed", "OK", "Submit",
    "Suivant", "Weiter", "Avanti",
    "Далее", "Вперёд", "Volgende",
]


def click_any_text(page, texts, exclude=None):
    if exclude is None:
        exclude = []
    js = """(args) => {
        var texts = args[0]; var exclude = args[1];
        var els = document.querySelectorAll('button, a, div[role=button], span[role=button]');
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim(); var tl = t.toLowerCase();
            var skip = false;
            for (var e = 0; e < exclude.length; e++) { if (tl.indexOf(exclude[e].toLowerCase()) !== -1) { skip = true; break; } }
            if (skip) continue;
            for (var j = 0; j < texts.length; j++) {
                if (t === texts[j] || tl === texts[j].toLowerCase()) { els[i].click(); return texts[j]; }
            }
        }
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim(); var tl = t.toLowerCase();
            var skip = false;
            for (var e = 0; e < exclude.length; e++) { if (tl.indexOf(exclude[e].toLowerCase()) !== -1) { skip = true; break; } }
            if (skip) continue;
            for (var j = 0; j < texts.length; j++) {
                if (tl.indexOf(texts[j].toLowerCase()) !== -1) { els[i].click(); return texts[j]; }
            }
        }
        return null;
    }"""
    try:
        return page.evaluate(js, [texts, exclude])
    except Exception:
        return None


def build_url(data):
    if not data:
        return None
    body = data.get("body", "")
    links = data.get("links", [])
    for item in links:
        url = item.get("url", "")
        if "withpersona.com" in url and "inquiry-id=" in url and len(url) > 40:
            return url
    inq = re.search(r'inq_[A-Za-z0-9]+', body)
    tok = re.search(r'"sessionToken"\s*:\s*"([^"]{50,})"', body)
    if not tok:
        tok = re.search(r'"session[_-]?[Tt]oken"\s*:\s*"([^"]{50,})"', body)
    if inq:
        url = "https://inquiry.withpersona.com/verify?inquiry-id=" + inq.group(0)
        if tok:
            url += "&session-token=" + tok.group(1)
        return url
    return None


def get_url_via_api(cookie, method):
    """
    Быстрый метод через прямой API запрос (как в консоли браузера).
    Работает без Playwright, занимает ~2 секунды.
    """
    try:
        url = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
        body = {
            "generateLink": True,
            "ageEstimation": True,
            "parentVerification": False
        }

        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        s.proxies.update(get_proxy())
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account#!/info",
        })

        # Шаг 1: Первый запрос без CSRF — получаем токен
        r1 = s.post(url, json=body)
        logging.info("API step1: %d", r1.status_code)

        csrf = r1.headers.get("x-csrf-token")
        logging.info("CSRF: %s", csrf)

        if r1.status_code == 200:
            return _extract_api_url(r1)

        if not csrf:
            return None

        # Шаг 2: Повторяем с CSRF токеном
        s.headers["x-csrf-token"] = csrf
        r2 = s.post(url, json=body)
        logging.info("API step2: %d %s", r2.status_code, r2.text[:200])

        if r2.status_code == 200:
            return _extract_api_url(r2)

        return None

    except Exception as e:
        logging.error("API error: %s", e)
        return None


TWO_FA_JS = """
(async () => {
  const url  = 'https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification';
  const body = JSON.stringify({ generateLink: true, ageEstimation: true, parentVerification: false });

  const send = (csrf) => fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json;charset=utf-8',
      ...(csrf ? { 'x-csrf-token': csrf } : {}),
    },
    body,
  });

  let r = await send(null);
  const csrf = r.headers.get('x-csrf-token');
  console.log('1st:', r.status, 'csrf:', csrf);

  if (csrf) r = await send(csrf);

  const text = await r.text();
  console.log('2nd:', r.status);
  try { console.log(JSON.parse(text)); } catch { console.log(text); }

  // Store result for Python to pick up
  try {
    const parsed = JSON.parse(text);
    window.__2fa_result = parsed;
  } catch {
    window.__2fa_result = { raw: text };
  }
})();
"""


def get_url_via_api_2fa(cookie, method):
    """
    Для 2FA endpoint требует браузерный контекст (Challenge required на прямых запросах).
    Используем Playwright — открываем страницу с cookie, выполняем JS прямо в браузере.
    JS делает fetch() изнутри браузера — обходит challenge автоматически.
    Повторяем JS до 60 раз пока не получим ссылку.
    """
    import time as _t

    # JS для получения CSRF и выполнения запроса
    JS_FETCH = """
async () => {
  const url  = 'https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification';
  const body = JSON.stringify({ generateLink: true, ageEstimation: true, parentVerification: false });

  const send = (csrf, arkoseToken) => fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json;charset=utf-8',
      ...(csrf        ? { 'x-csrf-token': csrf } : {}),
      ...(arkoseToken ? { 'rblx-challenge-metadata': JSON.stringify({unifiedCaptchaId:'', dataExchangeBlob:'', arkoseToken}) } : {}),
    },
    body,
  });

  let r = await send(null, null);
  const csrf = r.headers.get('x-csrf-token');
  const challengeId = r.headers.get('rblx-challenge-id');
  const challengeType = r.headers.get('rblx-challenge-type');

  if (csrf) r = await send(csrf, null);

  const text = await r.text();
  const status2 = r.status;
  const challengeId2 = r.headers.get('rblx-challenge-id');
  const challengeType2 = r.headers.get('rblx-challenge-type');

  return {
    status: status2,
    body: text,
    csrf,
    challengeId: challengeId2 || challengeId,
    challengeType: challengeType2 || challengeType,
  };
}
"""

    # JS для отправки запроса с аркоз токеном
    JS_WITH_ARKOSE = """
async (arkoseToken, csrf) => {
  const url = 'https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification';
  const body = JSON.stringify({ generateLink: true, ageEstimation: true, parentVerification: false });

  const r = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json;charset=utf-8',
      'x-csrf-token': csrf,
      'rblx-challenge-metadata': JSON.stringify({ unifiedCaptchaId: '', dataExchangeBlob: '', arkoseToken }),
      'rblx-challenge-id': '',
      'rblx-challenge-type': 'arkose',
    },
    body,
  });

  return { status: r.status, body: await r.text() };
}
"""

    import json as _json

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                proxy=get_proxy_url(),
            )
            ctx.add_cookies([{
                "name": ".ROBLOSECURITY",
                "value": cookie,
                "domain": ".roblox.com",
                "path": "/",
            }])
            page = ctx.new_page()
            page.goto("https://www.roblox.com/my/account#!/info",
                      wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            import time as _t
            deadline = _t.time() + 600  # 10 минут
            attempt = 0

            while _t.time() < deadline:
                if page.is_closed():
                    break
                attempt += 1
                try:
                    result = page.evaluate(JS_FETCH)
                    status         = result.get("status")
                    body           = result.get("body", "")
                    csrf           = result.get("csrf")
                    challenge_id   = result.get("challengeId")
                    challenge_type = result.get("challengeType") or ""
                    logging.info("2FA attempt#%d status=%d challenge=%s body=%s",
                                 attempt, status, challenge_type, body[:200])

                    # Успех
                    if status == 200:
                        try:
                            data = _json.loads(body)
                        except Exception:
                            data = {}
                        link = extract_link_from_api(data)
                        if link:
                            logging.info("2FA: got link on attempt#%d", attempt)
                            browser.close()
                            return link

                    # twostepverification — ждём пока пользователь подтвердит email/app
                    if status == 403 and "twostepverification" in challenge_type.lower():
                        logging.info("2FA: twostepverification detected, waiting for user to confirm...")
                        page.wait_for_timeout(4000)
                        continue

                    # Arkose FunCaptcha — решаем через cap.guru
                    if status == 403 and CAP_GURU_KEY and challenge_type and "arkose" in challenge_type.lower():
                        logging.info("2FA: arkose challenge, solving via cap.guru...")
                        arkose_token = capguru_solve_funcaptcha(
                            public_key="476068BF-9607-4799-B53D-966BE98E2B81",
                            page_url="https://www.roblox.com",
                            proxy=get_proxy(),
                        )
                        if arkose_token and csrf:
                            r2 = page.evaluate(JS_WITH_ARKOSE, arkose_token, csrf)
                            s2 = r2.get("status")
                            b2 = r2.get("body", "")
                            logging.info("2FA arkose result: status=%d body=%s", s2, b2[:200])
                            if s2 == 200:
                                try:
                                    data = _json.loads(b2)
                                except Exception:
                                    data = {}
                                link = extract_link_from_api(data)
                                if link:
                                    logging.info("2FA: got link via cap.guru on attempt#%d", attempt)
                                    browser.close()
                                    return link
                        page.wait_for_timeout(1000)
                        continue

                    # 429 — притормозить
                    if status == 429:
                        page.wait_for_timeout(4000)
                    else:
                        page.wait_for_timeout(800)

                except Exception as e:
                    logging.warning("2FA attempt#%d err: %s", attempt, e)
                    page.wait_for_timeout(1000)

            browser.close()

    except Exception as e:
        logging.error("2FA Playwright error: %s", e)

    return None


def _extract_api_url(response):
    """Извлекаем ссылку из ответа API — ищем любой https URL в JSON"""
    try:
        raw = response.text
        logging.info("FULL API response [%d]: %s", response.status_code, raw[:500])

        data = response.json()

        def find_in_obj(obj):
            if isinstance(obj, str):
                if "withpersona.com" in obj or "persona.com" in obj:
                    return obj
                if obj.startswith("https://") and ("verify" in obj or "inquiry" in obj or "inq_" in obj):
                    return obj
            elif isinstance(obj, dict):
                # Приоритетные поля
                for key in ("verificationUrl", "redirectUrl", "url", "personaUrl",
                            "sessionUrl", "inquiryUrl", "link", "verifyUrl"):
                    val = obj.get(key)
                    if val and isinstance(val, str) and val.startswith("http"):
                        logging.info("Found link in key '%s': %s", key, val)
                        return val
                # Рекурсивно по всем полям
                for v in obj.values():
                    r = find_in_obj(v)
                    if r:
                        return r
                # Собираем inquiry-id + session-token
                inq_id = obj.get("inquiryId") or obj.get("inquiry_id") or obj.get("inqId")
                if inq_id:
                    token = obj.get("sessionToken") or obj.get("session_token") or obj.get("token") or ""
                    link = "https://inquiry.withpersona.com/verify?inquiry-id=" + str(inq_id)
                    if token:
                        link += "&session-token=" + str(token)
                    logging.info("Built link from inquiryId: %s", link)
                    return link
            elif isinstance(obj, list):
                for item in obj:
                    r = find_in_obj(item)
                    if r:
                        return r
            return None

        return find_in_obj(data)

    except Exception as e:
        logging.error("Extract error: %s | raw: %s", e, response.text[:300])
        return None



def get_link_via_api(cookie, method):
    """
    Быстрый способ через прямой API запрос (как в консольном скрипте).
    Не требует браузера. Работает без 2FA если cookie валидный.
    """
    try:
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        s.proxies.update(get_proxy())
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/my/account#!/info",
        })

        url = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
        body = {
            "generateLink": True,
            "ageEstimation": True,
            "parentVerification": False
        }

        # Шаг 1: Первый запрос без CSRF — получаем токен
        r1 = s.post(url, json=body)
        csrf = r1.headers.get("x-csrf-token") or r1.headers.get("X-CSRF-Token")
        logging.info("API step1: %d csrf=%s", r1.status_code, csrf)

        if r1.status_code == 200:
            data = r1.json()
            link = extract_link_from_api(data)
            if link:
                return link

        # Шаг 2: Повтор с CSRF токеном
        if csrf:
            s.headers["x-csrf-token"] = csrf
            r2 = s.post(url, json=body)
            logging.info("API step2: %d", r2.status_code)
            if r2.status_code == 200:
                data = r2.json()
                logging.info("API response: %s", str(data)[:300])
                link = extract_link_from_api(data)
                if link:
                    return link

        return None
    except Exception as e:
        logging.error("API error: %s", e)
        return None


def extract_link_from_api(data):
    """Ищем ссылку в JSON ответе"""
    if not isinstance(data, dict):
        return None

    # Прямые поля
    for key in ["verificationUrl", "redirectUrl", "url", "link",
                "personaUrl", "inquiryUrl", "sessionUrl"]:
        val = data.get(key, "")
        if val and "withpersona.com" in val:
            return val

    # Строим из inquiry-id если есть
    inq_id = data.get("inquiryId") or data.get("inquiry_id") or data.get("sessionIdentifier")
    session_token = data.get("sessionToken") or data.get("session_token")

    if inq_id and inq_id.startswith("inq_"):
        link = "https://inquiry.withpersona.com/verify?inquiry-id=" + inq_id
        if session_token:
            link += "&session-token=" + session_token
        return link

    # Ищем рекурсивно
    def find_deep(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and "withpersona.com" in v and "inquiry-id=" in v:
                    return v
                result = find_deep(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = find_deep(item)
                if result:
                    return result
        return None

    return find_deep(data)


def playwright_get_url(cookie, method):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                proxy=get_proxy_url(),
            )
            ctx.add_cookies([{"name": ".ROBLOSECURITY", "value": cookie, "domain": ".roblox.com", "path": "/"}])
            page = ctx.new_page()
            page.add_init_script(INJECT_SCRIPT)
            page.goto("https://www.roblox.com/my/account#!/info", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

            target_texts = CAMERA_TEXTS if method == "camera" else ID_TEXTS
            exclude_words = ["camera", "id", "passport", "камер", "паспорт"]

            reset_clicked = click_any_text(page, RESET_TEXTS)
            if reset_clicked:
                logging.info("Reset: " + str(reset_clicked))
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            clicked = False
            for attempt in range(5):
                result = click_any_text(page, target_texts)
                if result:
                    logging.info("Кнопка: " + str(result))
                    clicked = True
                    break
                page.wait_for_timeout(2000)
                if attempt % 2 == 1:
                    r = click_any_text(page, RESET_TEXTS)
                    if r:
                        page.wait_for_timeout(2000)

            if not clicked:
                browser.close()
                return "NOT_CLICKED"

            page.wait_for_timeout(2000)
            cont = click_any_text(page, CONTINUE_TEXTS, exclude=exclude_words)
            if cont:
                page.wait_for_timeout(1500)

            result_url = None
            for i in range(30):
                page.wait_for_timeout(1000)
                try:
                    raw = page.evaluate("() => window.__capturedData")
                    if raw:
                        result_url = build_url(raw)
                        if result_url:
                            logging.info("Ссылка за " + str(i + 1) + " сек")
                            break
                except Exception:
                    pass
                if i % 3 == 0:
                    click_any_text(page, CONTINUE_TEXTS, exclude=exclude_words)

            browser.close()
            return result_url
    except Exception as e:
        logging.error("Playwright error: %s", e)
        return "ERROR: " + str(e)


# ===== CRYPTOBOT =====

def create_invoice(amount, attempts):
    try:
        r = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={"asset": "USDT", "amount": str(round(amount, 2)),
                  "description": "Pokupka " + str(attempts) + " popytok", "expires_in": 300}
        )
        data = r.json()
        return data["result"] if data.get("ok") else None
    except Exception as e:
        logging.error("CryptoBot: %s", e)
        return None


def check_invoice(invoice_id):
    try:
        r = requests.get(
            "https://pay.crypt.bot/api/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": invoice_id}
        )
        data = r.json()
        return data["result"]["items"][0] if data.get("ok") and data["result"]["items"] else None
    except Exception:
        return None


# ===== ЛОГИ =====

import time as _time

async def send_log(bot, user_id, username, method, result, elapsed_sec, success):
    """Отправляем лог в канал — только успехи"""
    if not LOG_CHANNEL_ID:
        return
    if not success:
        return
    try:
        if success:
            header = t(user_id, "log_success")
            service = t(user_id, "service_camera") if method == "camera" else t(user_id, "service_id")
            result_text = t(user_id, "log_result_ok")
            cost = "$0.20"
        else:
            header = t(user_id, "log_fail")
            service = t(user_id, "service_camera") if method == "camera" else t(user_id, "service_id")
            result_text = str(result)[:50] if result else t(user_id, "log_result_fail")
            cost = "$0.20"

        uname_part = ("@" + username) if (username and not username.isdigit()) else ""
        result_icon = "✅" if success else "❌"

        msg = (
            header + chr(10) +
            "━━━━━━━━━━━━━━━━━━━━━━" + chr(10) +
            "👤 ID: " + str(user_id) +
            (" | " + uname_part if uname_part else "") + chr(10) +
            "⚙️ Услуга: " + service + chr(10) +
            result_icon + " Результат: " + result_text + chr(10) +
            "⏲️ Время: " + str(round(elapsed_sec)) + " сек" + chr(10) +
            "💳 Сумма: " + cost
        )

        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=msg
        )
    except Exception as e:
        logging.error("Log channel error: %s", e)


# ===== КЛАВИАТУРЫ =====

def main_menu_kb(user_id=None):
    lang = db_get_lang(user_id) if user_id else "ru"
    T = TEXTS[lang]
    buttons = [
        [InlineKeyboardButton(T["btn_get_link"], callback_data="get_link")],
        [InlineKeyboardButton(T["btn_buy"], callback_data="buy"),
         InlineKeyboardButton(T["btn_promo"], callback_data="promo")],
        [InlineKeyboardButton(T["btn_top"], callback_data="top")],
        [InlineKeyboardButton(T["btn_help"], callback_data="help"),
         InlineKeyboardButton(T["btn_support"], url="https://t.me/dedbed12")],
    ]
    if user_id and user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(T["btn_admin"], callback_data="admin")])
    return InlineKeyboardMarkup(buttons)


async def show_main_menu(update, context):
    user_id = update.effective_user.id
    attempts = db_get_attempts(user_id)
    text = (
        t(user_id, "main_menu_title") + chr(10) + chr(10) +
        t(user_id, "main_menu_sub") + chr(10) + chr(10) +
        t(user_id, "main_menu_attempts", attempts=attempts) + chr(10) + chr(10) +
        t(user_id, "main_menu_action")
    )
    kb = main_menu_kb(user_id)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


# ===== HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tg = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    db_upsert_user(user_id, username)
    await show_main_menu(update, context)
    return WAITING_MENU


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    tg = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    data = query.data

    if data.startswith("set_lang_"):
        lang = data.split("_")[2]
        db_set_lang(user_id, lang)
        await query.edit_message_text(
            TEXTS[lang]["lang_set"],
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS[lang]["btn_back"], callback_data="main_menu")
            ]])
        )
        return WAITING_MENU

    if data in ["main_menu", "accept_rules"]:
        await show_main_menu(update, context)
        return WAITING_MENU

    if data == "help":
        await query.edit_message_text(
            t(user_id, "help_text"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
        )
        return WAITING_MENU

    if data == "top":
        rows = db_get_top(10)
        medals = ["🥇", "🥈", "🥉"]
        lines = [t(user_id, "top_title")]
        if not rows:
            lines.append(t(user_id, "top_empty"))
        else:
            for i, (uid, uname, spent) in enumerate(rows):
                medal = medals[i] if i < 3 else str(i + 1) + "."
                lines.append(medal + " " + (uname or t(user_id, "anon")) + " - $" + str(round(spent, 2)))
        await query.edit_message_text(
            chr(10).join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
        )
        return WAITING_MENU

    if data == "promo":
        context.user_data["waiting"] = "promo"
        await query.edit_message_text(
            t(user_id, "promo_title") + chr(10) + chr(10) + t(user_id, "promo_enter"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
        )
        return WAITING_PROMO

    if data == "buy":
        await query.edit_message_text(
            t(user_id, "buy_title") + chr(10) + chr(10) +
            t(user_id, "buy_price") + chr(10) + chr(10) +
            t(user_id, "buy_discount"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_buy_1"), callback_data="buy_1")],
                [InlineKeyboardButton(t(user_id, "btn_buy_5"), callback_data="buy_5")],
                [InlineKeyboardButton(t(user_id, "btn_buy_10"), callback_data="buy_10")],
                [InlineKeyboardButton(t(user_id, "btn_buy_25"), callback_data="buy_25")],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
            ])
        )
        return WAITING_BUY

    if data.startswith("buy_"):
        count = int(data.split("_")[1])
        discount = DISCOUNTS.get(count, 0)
        total = round(count * PRICE * (1 - discount), 2)
        await query.edit_message_text("Создаю инвойс...")
        invoice = create_invoice(total, count)
        if not invoice:
            await query.edit_message_text(
                t(user_id, "pay_error"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy")]])
            )
            return WAITING_MENU
        invoice_id = str(invoice["invoice_id"])
        pay_url = invoice["pay_url"]
        pending_payments[invoice_id] = {"user_id": user_id, "attempts": count, "total": total}
        await query.edit_message_text(
            t(user_id, "invoice_created", amount=total, attempts=count),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_pay"), url=pay_url)],
                [InlineKeyboardButton(t(user_id, "btn_check_pay"), callback_data="check_" + invoice_id)],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy")],
            ])
        )
        return WAITING_PAYMENT

    if data.startswith("check_"):
        invoice_id = data.replace("check_", "")
        invoice = check_invoice(invoice_id)
        if invoice and invoice.get("status") == "paid":
            payment = pending_payments.pop(invoice_id, None)
            if payment:
                cnt = payment["attempts"]
                total = payment.get("total", cnt * PRICE)
                db_upsert_user(user_id, username, attempts_delta=cnt, spent_delta=total)
                new_attempts = db_get_attempts(user_id)
                await query.edit_message_text(
                    t(user_id, "pay_ok", attempts=cnt, total=new_attempts),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
                )
            else:
                await query.answer("Already processed!" if db_get_lang(user_id) == "en" else "Уже обработано!", show_alert=True)
        else:
            await query.answer(t(user_id, "pay_pending"), show_alert=True)
        return WAITING_MENU

    if data == "get_link":
        attempts = db_get_attempts(user_id)
        if attempts <= 0:
            await query.edit_message_text(
                "*" + t(user_id, "no_attempts") + "*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(user_id, "btn_buy"), callback_data="buy")],
                    [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
                ])
            )
            return WAITING_MENU
        # Шаг 1 — спрашиваем про 2FA
        await query.edit_message_text(
            t(user_id, "ask_2fa"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_no_2fa"), callback_data="2fa_no"),
                 InlineKeyboardButton(t(user_id, "btn_with_2fa"), callback_data="2fa_yes")],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
            ])
        )
        return WAITING_MENU

    if data == "2fa_yes":
        # Шаг 2 — выбор метода (Camera / ID) для 2FA аккаунта
        await query.edit_message_text(
            t(user_id, "choose_method"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_camera"), callback_data="choose_camera_2fa")],
                [InlineKeyboardButton(t(user_id, "btn_id"), callback_data="choose_id_2fa")],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="get_link")],
            ])
        )
        return WAITING_MENU

    if data in ["choose_camera_2fa", "choose_id_2fa"]:
        method = "camera" if data == "choose_camera_2fa" else "id"
        context.user_data["method"] = method
        context.user_data["has_2fa"] = True
        method_name = t(user_id, "method_camera") if method == "camera" else t(user_id, "method_id")
        await query.edit_message_text(
            t(user_id, "2fa_ask_cookie"),
            parse_mode="Markdown"
        )
        return WAITING_COOKIE

    if data == "2fa_no":
        # Шаг 2 — выбор метода (Camera / ID)
        await query.edit_message_text(
            t(user_id, "choose_method"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_camera"), callback_data="choose_camera")],
                [InlineKeyboardButton(t(user_id, "btn_id"), callback_data="choose_id")],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="get_link")],
            ])
        )
        return WAITING_MENU

    if data in ["choose_camera", "choose_id"]:
        method = "camera" if data == "choose_camera" else "id"
        context.user_data["method"] = method
        method_name = t(user_id, "method_camera") if method == "camera" else t(user_id, "method_id")
        await query.edit_message_text(
            "Method: *" + method_name + "*\n\nSend your `.ROBLOSECURITY` cookie\nMessage will be deleted automatically"
            if db_get_lang(user_id) == "en" else
            "Метод: *" + method_name + "*\n\nОтправь `.ROBLOSECURITY` cookie\nСообщение будет удалено автоматически",
            parse_mode="Markdown"
        )
        return WAITING_COOKIE

    # ===== АДМИН =====

    if data == "admin":
        if user_id not in ADMIN_IDS:
            await query.answer("Нет доступа!", show_alert=True)
            return WAITING_MENU
        all_users = db_get_all_users()
        total_users = len(all_users)
        total_attempts = sum(r[2] for r in all_users)
        total_spent = sum(r[3] for r in all_users)
        promos = db_get_all_promos()
        await query.edit_message_text(
            "*Админ панель*" + chr(10) + chr(10) +
            "Пользователей: *" + str(total_users) + "*" + chr(10) +
            "Всего попыток: *" + str(total_attempts) + "*" + chr(10) +
            "Всего продаж: *$" + str(round(total_spent, 2)) + "*" + chr(10) +
            "Промокодов: *" + str(len(promos)) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Рассылка", callback_data="admin_broadcast")],
                [InlineKeyboardButton("Добавить баланс", callback_data="admin_balance")],
                [InlineKeyboardButton("Создать промокод", callback_data="admin_promo")],
                [InlineKeyboardButton("Список промокодов", callback_data="admin_promo_list")],
                [InlineKeyboardButton("Список пользователей", callback_data="admin_users")],
                [InlineKeyboardButton("Назад", callback_data="main_menu")],
            ])
        )
        return WAITING_ADMIN

    if data == "admin_users":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        rows = db_get_all_users()
        if not rows:
            text = "*Пользователей нет*"
        else:
            lines = ["*Пользователи* (топ 20 по тратам)" + chr(10)]
            for uid, uname, atts, spent in rows[:20]:
                lines.append(
                    "ID: `" + str(uid) + "` | " + (uname or "?") +
                    " | попыток: " + str(atts) +
                    " | $" + str(round(spent, 2))
                )
            text = chr(10).join(lines)
        if len(text) > 4000:
            text = text[:4000] + "..."
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="admin")]])
        )
        return WAITING_ADMIN

    if data == "admin_broadcast":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        context.user_data["waiting"] = "broadcast"
        await query.edit_message_text(
            "*Рассылка*" + chr(10) + chr(10) + "Напишите текст рассылки:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin")]])
        )
        return WAITING_ADMIN_BROADCAST

    if data == "admin_balance":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        context.user_data["waiting"] = "add_balance_id"
        await query.edit_message_text(
            "*Добавить баланс*" + chr(10) + chr(10) + "Введите Telegram ID пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin")]])
        )
        return WAITING_ADMIN_ADD_BALANCE

    if data == "admin_promo":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        context.user_data["waiting"] = "create_promo"
        await query.edit_message_text(
            "*Создать промокод*" + chr(10) + chr(10) +
            "Формат: `КОД ПОПЫТКИ ИСПОЛЬЗОВАНИЙ`" + chr(10) + chr(10) +
            "Примеры:" + chr(10) +
            "`SUMMER 5 10` - 5 попыток, 10 раз" + chr(10) +
            "`VIP 25 1` - 25 попыток, 1 раз" + chr(10) +
            "`FREE 1 100` - 1 попытка, 100 раз",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin")]])
        )
        return WAITING_ADMIN_CREATE_PROMO

    if data == "admin_promo_list":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        rows = db_get_all_promos()
        if not rows:
            text = "*Промокодов нет*"
        else:
            lines = ["*Список промокодов*" + chr(10)]
            for code, atts, uses, max_uses in rows:
                lines.append("`" + code + "` - " + str(atts) + " поп. | " + str(uses) + "/" + str(max_uses))
            text = chr(10).join(lines)
        await query.edit_message_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="admin")]])
        )
        return WAITING_ADMIN

    return WAITING_MENU


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    tg = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    waiting = context.user_data.get("waiting")

    if waiting == "promo":
        context.user_data["waiting"] = None
        code_upper = text.upper()
        ok, msg_key, atts = db_use_promo(user_id, code_upper)
        if ok:
            db_upsert_user(user_id, username, attempts_delta=atts)
            new_attempts = db_get_attempts(user_id)
            await update.message.reply_text(
                t(user_id, "promo_ok", attempts=atts, total=new_attempts),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
            )
        else:
            # msg_key is one of: "Промокод не найден!", "Промокод уже использован!", "Вы уже использовали этот промокод!"
            if "не найден" in msg_key or "not found" in msg_key:
                reply_msg = t(user_id, "promo_not_found")
            elif "уже использован" in msg_key and "Вы" not in msg_key:
                reply_msg = t(user_id, "promo_used")
            else:
                reply_msg = t(user_id, "promo_used_by_you")
            await update.message.reply_text(
                reply_msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")]])
            )
        return WAITING_MENU

    if waiting == "broadcast" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        rows = db_get_all_users()
        sent = 0
        failed = 0
        for row in rows:
            try:
                await update.get_bot().send_message(chat_id=row[0], text=text, parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            "*Рассылка завершена*" + chr(10) + "Отправлено: " + str(sent) + chr(10) + "Ошибок: " + str(failed),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В админку", callback_data="admin")]])
        )
        return WAITING_ADMIN

    if waiting == "add_balance_id" and user_id in ADMIN_IDS:
        try:
            target_id = int(text)
            context.user_data["target_id"] = target_id
            context.user_data["waiting"] = "add_balance_amount"
            await update.message.reply_text(
                "ID: *" + str(target_id) + "*" + chr(10) + chr(10) + "Введите количество попыток:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin")]])
            )
            return WAITING_ADMIN_ADD_BALANCE_AMOUNT
        except ValueError:
            await update.message.reply_text("Неверный ID!")
            return WAITING_ADMIN_ADD_BALANCE

    if waiting == "add_balance_amount" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        target_id = context.user_data.get("target_id")
        try:
            amount = int(text)
            row = db_get_user(target_id)
            target_name = row[0] if row else "Пользователь"
            db_upsert_user(target_id, target_name, attempts_delta=amount)
            new_atts = db_get_attempts(target_id)
            await update.message.reply_text(
                "*Баланс пополнен!*" + chr(10) +
                "ID: `" + str(target_id) + "`" + chr(10) +
                "Добавлено: *" + str(amount) + "* попыток" + chr(10) +
                "Итого: *" + str(new_atts) + "*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В админку", callback_data="admin")]])
            )
            try:
                await update.get_bot().send_message(
                    chat_id=target_id,
                    text="Вам начислено *" + str(amount) + "* попыток! Всего: *" + str(new_atts) + "*",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("Неверное количество!")
        return WAITING_ADMIN

    if waiting == "create_promo" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        parts = text.strip().split()
        if not parts:
            await update.message.reply_text("Неверный формат! Пример: `SUMMER 5 10`", parse_mode="Markdown")
            context.user_data["waiting"] = "create_promo"
            return WAITING_ADMIN_CREATE_PROMO
        new_code = parts[0].upper()
        try:
            atts = max(1, min(int(parts[1]) if len(parts) > 1 else 1, 1000))
        except Exception:
            atts = 1
        try:
            max_uses = max(1, min(int(parts[2]) if len(parts) > 2 else 1, 10000))
        except Exception:
            max_uses = 1
        db_create_promo(new_code, atts, max_uses)
        await update.message.reply_text(
            "*Промокод создан!*" + chr(10) + chr(10) +
            "Код: `" + new_code + "`" + chr(10) +
            "Попыток: *" + str(atts) + "*" + chr(10) +
            "Использований: *" + str(max_uses) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В админку", callback_data="admin")]])
        )
        return WAITING_ADMIN

    return await receive_cookie(update, context)


async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie = update.message.text.strip()
    method = context.user_data.get("method")
    user_id = update.effective_user.id
    tg = update.effective_user
    username = tg.username or tg.first_name or "Аноним"

    if not method:
        await show_main_menu(update, context)
        return WAITING_MENU

    attempts = db_get_attempts(user_id)
    if attempts <= 0:
        await update.message.reply_text(
            t(user_id, "no_attempts"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "btn_buy_short"), callback_data="buy")]])
        )
        return WAITING_MENU

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text(t(user_id, "checking_cookie"))

    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    s.proxies.update(get_proxy())
    r = s.get("https://users.roblox.com/v1/users/authenticated")

    if r.status_code != 200:
        await msg.edit_text(t(user_id, "invalid_cookie"))
        return WAITING_MENU

    user = r.json()
    method_name = t(user_id, "method_camera") if method == "camera" else t(user_id, "method_id")

    # Списываем попытку
    db_upsert_user(user_id, username, attempts_delta=-1)

    loop = asyncio.get_event_loop()
    _start_time = _time.time()
    has_2fa = context.user_data.get("has_2fa", False)

    if has_2fa:
        processing_text = (
            "Аккаунт: *{name}*\nМетод: *{method}*\nОсталось попыток: *{attempts}*\n\n⚡ 2FA режим - Пытаюсь получить ссылку"
            if db_get_lang(user_id) == "ru" else
            "Account: *{name}*\nMethod: *{method}*\nAttempts left: *{attempts}*\n\n⚡ 2FA mode - Trying to get link"
        ).format(name=user["name"], method=method_name, attempts=db_get_attempts(user_id))
    else:
        processing_text = t(user_id, "processing", name=user["name"], method=method_name, attempts=db_get_attempts(user_id))

    await msg.edit_text(processing_text, parse_mode="Markdown")

    if has_2fa:
        # Только быстрые API запросы, без браузера и без fallback
        result = await loop.run_in_executor(executor, get_url_via_api_2fa, cookie, method)
        logging.info("2FA API result: %s", result)
    else:
        # Сначала быстрый API метод (~2 сек)
        result = await loop.run_in_executor(executor, get_url_via_api, cookie, method)
        logging.info("API result: %s", result)

        # Если API не сработал — используем Playwright (~30 сек)
        if not result:
            await msg.edit_text(t(user_id, "browser_fallback"))
            result = await loop.run_in_executor(executor, playwright_get_url, cookie, method)

    elapsed = round(_time.time() - _start_time)

    if result and "withpersona.com" in result:
        # Успех
        await msg.edit_text(t(user_id, "link_received"), parse_mode="Markdown")
        await update.message.reply_text(result)
        await send_log(update.get_bot(), user_id, username, method, t(user_id, "log_result_ok"), elapsed, True)
        context.user_data["has_2fa"] = False
        await update.message.reply_text(
            t(user_id, "main_menu_title") + chr(10) + chr(10) +
            t(user_id, "main_menu_attempts", attempts=db_get_attempts(user_id)) + chr(10) + chr(10) +
            t(user_id, "main_menu_action"),
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id)
        )
        return WAITING_MENU

    # Неудача — возвращаем попытку
    db_upsert_user(user_id, username, attempts_delta=1)
    await send_log(update.get_bot(), user_id, username, method, t(user_id, "log_result_fail"), elapsed, False)

    if has_2fa:
        # Для 2FA: сразу снова запрашиваем cookie, не уходим в меню
        method_name = t(user_id, "method_camera") if method == "camera" else t(user_id, "method_id")
        retry_text = (
            "❌ Не удалось — попытка возвращена!\n\n"
            "Метод: *{method}*\n\nОтправь cookie снова:"
            if db_get_lang(user_id) == "ru" else
            "❌ Failed — attempt returned!\n\n"
            "Method: *{method}*\n\nSend cookie again:"
        ).format(method=method_name)
        await msg.edit_text(retry_text, parse_mode="Markdown")
        # has_2fa и method остаются в context.user_data — сразу ждём cookie
        return WAITING_COOKIE
    else:
        if isinstance(result, str) and result.startswith("ERROR"):
            err = result[7:200]
            await msg.edit_text(t(user_id, "error_returned", reason=err))
        elif result == "NOT_CLICKED":
            await msg.edit_text(t(user_id, "not_clicked_returned"))
        else:
            await msg.edit_text(t(user_id, "failed_returned"))

        await update.message.reply_text(
            t(user_id, "main_menu_title") + chr(10) + chr(10) +
            t(user_id, "main_menu_attempts", attempts=db_get_attempts(user_id)) + chr(10) + chr(10) +
            t(user_id, "main_menu_action"),
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id)
        )
        return WAITING_MENU


async def lan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(
        TEXTS["ru"]["lang_choose"],
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru"),
             InlineKeyboardButton("🇬🇧 English", callback_data="set_lang_en")]
        ])
    )
    return WAITING_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(t(user_id, "cancelled"))
    return ConversationHandler.END


def main():
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("lan", lan_command)],
        states={
            WAITING_RULES:                  [CallbackQueryHandler(handle_callback)],
            WAITING_MENU:                   [CallbackQueryHandler(handle_callback)],
            WAITING_COOKIE:                 [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_BUY:                    [CallbackQueryHandler(handle_callback)],
            WAITING_PAYMENT:                [CallbackQueryHandler(handle_callback)],
            WAITING_PROMO:                  [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_ADMIN:                  [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_ADMIN_BROADCAST:        [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_ADMIN_ADD_BALANCE:      [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_ADMIN_ADD_BALANCE_AMOUNT:[CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
            WAITING_ADMIN_CREATE_PROMO:     [CallbackQueryHandler(handle_callback),
                                             MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start), CommandHandler("lan", lan_command)],
        per_user=True,
        per_chat=True,
        block=False,
    )
    app.add_handler(conv)
    print("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
