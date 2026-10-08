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

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.environ.get("CRYPTO_BOT_TOKEN")
ADMIN_IDS = set(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))

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
            spent REAL DEFAULT 0.0
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


def _extract_api_url(response):
    """Извлекаем ссылку из ответа API"""
    try:
        data = response.json()
        logging.info("API response: %s", str(data)[:300])

        # Ищем ссылку в разных полях
        link = (
            data.get("verificationUrl") or
            data.get("redirectUrl") or
            data.get("url") or
            data.get("personaUrl") or
            data.get("sessionUrl") or
            data.get("inquiryUrl")
        )

        if link and "withpersona.com" in link:
            return link

        # Ищем inquiry-id и session-token
        inq_id = data.get("inquiryId") or data.get("inquiry_id")
        session_token = data.get("sessionToken") or data.get("session_token")

        if inq_id:
            url = "https://inquiry.withpersona.com/verify?inquiry-id=" + str(inq_id)
            if session_token:
                url += "&session-token=" + str(session_token)
            return url

        # Рекурсивный поиск по всему JSON
        def find_in_obj(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and "withpersona.com" in v and "inquiry-id=" in v:
                        return v
                    if isinstance(v, str) and "inq_" in v:
                        pass
                    result = find_in_obj(v)
                    if result:
                        return result
            elif isinstance(obj, list):
                for item in obj:
                    result = find_in_obj(item)
                    if result:
                        return result
            return None

        return find_in_obj(data)

    except Exception as e:
        logging.error("Extract error: %s", e)
        return None



def get_link_via_api(cookie, method):
    """
    Быстрый способ через прямой API запрос (как в консольном скрипте).
    Не требует браузера. Работает без 2FA если cookie валидный.
    """
    try:
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
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
                viewport={"width": 1280, "height": 800}
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


# ===== КЛАВИАТУРЫ =====

def main_menu_kb(user_id=None):
    buttons = [
        [InlineKeyboardButton("Получить ссылку", callback_data="get_link")],
        [InlineKeyboardButton("Купить попытки", callback_data="buy"),
         InlineKeyboardButton("Промокод", callback_data="promo")],
        [InlineKeyboardButton("Топ пользователей", callback_data="top")],
        [InlineKeyboardButton("Помощь", callback_data="help"),
         InlineKeyboardButton("Саппорт", url="https://t.me/dedbed12")],
    ]
    if user_id and user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton("Админ панель", callback_data="admin")])
    return InlineKeyboardMarkup(buttons)


async def show_main_menu(update, context):
    user_id = update.effective_user.id
    attempts = db_get_attempts(user_id)
    text = (
        "*Roblox Age Verification Bot*" + chr(10) + chr(10) +
        "Сервис для получения ссылки by @dedbed12" + chr(10) + chr(10) +
        "Ваши попытки: *" + str(attempts) + "*" + chr(10) + chr(10) +
        "Выберите действие ниже:"
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

    if data in ["main_menu", "accept_rules"]:
        await show_main_menu(update, context)
        return WAITING_MENU

    if data == "help":
        await query.edit_message_text(
            "*Помощь*" + chr(10) + chr(10) +
            "*Как пользоваться:*" + chr(10) +
            "1. Купите попытки" + chr(10) +
            "2. Нажмите Получить ссылку" + chr(10) +
            "3. Отправьте `.ROBLOSECURITY` cookie" + chr(10) +
            "4. Выберите Camera или ID" + chr(10) +
            "5. Получите ссылку на верификацию" + chr(10) + chr(10) +
            "*Важно:*" + chr(10) +
            "- При технической ошибке попытка возвращается" + chr(10) +
            "- При невалидном cookie попытка возвращается" + chr(10) +
            "- Переведите страницу на English и нажмите кнопку Reset" + chr(10) +
            "- Владелец не несёт ответственности за поломку cookie",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
        )
        return WAITING_MENU

    if data == "top":
        rows = db_get_top(10)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["*Топ пользователей*" + chr(10)]
        if not rows:
            lines.append("Пока никто не совершил покупок.")
        else:
            for i, (uid, uname, spent) in enumerate(rows):
                medal = medals[i] if i < 3 else str(i + 1) + "."
                lines.append(medal + " " + (uname or "Аноним") + " - $" + str(round(spent, 2)))
        await query.edit_message_text(
            chr(10).join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
        )
        return WAITING_MENU

    if data == "promo":
        context.user_data["waiting"] = "promo"
        await query.edit_message_text(
            "*Активация промокода*" + chr(10) + chr(10) + "Введите промокод:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
        )
        return WAITING_PROMO

    if data == "buy":
        await query.edit_message_text(
            "*Покупка попыток*" + chr(10) + chr(10) +
            "Цена: *$0.20* за попытку" + chr(10) + chr(10) +
            "Скидки: 5 шт -5%, 10 шт -10%, 25 шт -15%",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1 попытка - $0.20", callback_data="buy_1")],
                [InlineKeyboardButton("5 попыток - $0.95 (5%)", callback_data="buy_5")],
                [InlineKeyboardButton("10 попыток - $1.80 (10%)", callback_data="buy_10")],
                [InlineKeyboardButton("25 попыток - $4.25 (15%)", callback_data="buy_25")],
                [InlineKeyboardButton("Назад", callback_data="main_menu")],
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
                "Ошибка создания инвойса. Попробуй позже.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="buy")]])
            )
            return WAITING_MENU
        invoice_id = str(invoice["invoice_id"])
        pay_url = invoice["pay_url"]
        pending_payments[invoice_id] = {"user_id": user_id, "attempts": count, "total": total}
        await query.edit_message_text(
            "*Оплата через CryptoBot*" + chr(10) + chr(10) +
            "Инвойс: `" + invoice_id + "`" + chr(10) +
            "Попыток: *" + str(count) + "*" + chr(10) +
            "Сумма: *$" + str(total) + "*" + chr(10) + chr(10) +
            "Оплата в USDT. После оплаты нажмите Я оплатил.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Оплатить", url=pay_url)],
                [InlineKeyboardButton("Я оплатил", callback_data="check_" + invoice_id)],
                [InlineKeyboardButton("Назад", callback_data="buy")],
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
                    "*Оплата подтверждена!*" + chr(10) + chr(10) +
                    "Зачислено: *" + str(cnt) + "* попыток" + chr(10) +
                    "Всего: *" + str(new_attempts) + "* попыток",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="main_menu")]])
                )
            else:
                await query.answer("Уже обработано!", show_alert=True)
        else:
            await query.answer("Оплата не найдена. Подожди и попробуй снова.", show_alert=True)
        return WAITING_MENU

    if data == "get_link":
        attempts = db_get_attempts(user_id)
        if attempts <= 0:
            await query.edit_message_text(
                "*Нет попыток!*" + chr(10) + chr(10) + "Купите попытки чтобы продолжить.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Купить попытки", callback_data="buy")],
                    [InlineKeyboardButton("Назад", callback_data="main_menu")],
                ])
            )
            return WAITING_MENU
        await query.edit_message_text(
            "*Получить ссылку*" + chr(10) + chr(10) +
            "Попыток: *" + str(attempts) + "*" + chr(10) + chr(10) + "Выберите метод:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Лицо (Camera)", callback_data="choose_camera")],
                [InlineKeyboardButton("Паспорт (ID)", callback_data="choose_id")],
                [InlineKeyboardButton("Назад", callback_data="main_menu")],
            ])
        )
        return WAITING_MENU

    if data in ["choose_camera", "choose_id"]:
        method = "camera" if data == "choose_camera" else "id"
        context.user_data["method"] = method
        await query.edit_message_text(
            "Метод: *" + ("Camera" if method == "camera" else "ID") + "*" + chr(10) + chr(10) +
            "Отправь `.ROBLOSECURITY` cookie" + chr(10) +
            "Сообщение будет удалено автоматически",
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
        ok, msg, atts = db_use_promo(user_id, code_upper)
        if ok:
            db_upsert_user(user_id, username, attempts_delta=atts)
            new_attempts = db_get_attempts(user_id)
            await update.message.reply_text(
                "*Промокод активирован!*" + chr(10) + chr(10) +
                "Начислено: *" + str(atts) + "* попыток" + chr(10) +
                "Всего: *" + str(new_attempts) + "* попыток",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="main_menu")]])
            )
        else:
            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
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
            "Нет попыток!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Купить", callback_data="buy")]])
        )
        return WAITING_MENU

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("Проверяю cookie...")

    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    r = s.get("https://users.roblox.com/v1/users/authenticated")

    if r.status_code != 200:
        await msg.edit_text("Неверный cookie! Попробуй снова." + chr(10) + chr(10) + "/start")
        return WAITING_MENU

    user = r.json()
    method_name = "Camera" if method == "camera" else "ID"

    # Списываем попытку
    db_upsert_user(user_id, username, attempts_delta=-1)

    await msg.edit_text(
        "Аккаунт: *" + user["name"] + "*" + chr(10) +
        "Метод: *" + method_name + "*" + chr(10) +
        "Осталось попыток: *" + str(db_get_attempts(user_id)) + "*" + chr(10) + chr(10) +
        "Жди 20-30 сек...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()

    # Сначала быстрый API метод (~2 сек)
    result = await loop.run_in_executor(executor, get_url_via_api, cookie, method)
    logging.info("API result: %s", result)

    # Если API не сработал — используем Playwright (~30 сек)
    if not result:
        await msg.edit_text(
            "API не ответил, открываю браузер..." + chr(10) +
            "Жди до 30 сек...",
        )
        result = await loop.run_in_executor(executor, playwright_get_url, cookie, method)

    if isinstance(result, str) and result.startswith("ERROR"):
        db_upsert_user(user_id, username, attempts_delta=1)
        err = result[7:200]
        await msg.edit_text(
            "Техническая ошибка - попытка возвращена!" + chr(10) + chr(10) +
            "Причина: " + err + chr(10) + chr(10) + "/start"
        )
    elif result == "NOT_CLICKED":
        db_upsert_user(user_id, username, attempts_delta=1)
        await msg.edit_text("Кнопка не найдена - попытка возвращена!" + chr(10) + chr(10) + "/start")
    elif result and "withpersona.com" in result:
        await msg.edit_text(
            "*Ссылка получена!*" + chr(10) + chr(10) + "Используй сразу - одноразовая!",
            parse_mode="Markdown"
        )
        await update.message.reply_text(result)
    else:
        db_upsert_user(user_id, username, attempts_delta=1)
        await msg.edit_text("Не удалось получить ссылку - попытка возвращена!" + chr(10) + chr(10) + "/start")

    await update.message.reply_text(
        "*Roblox Age Verification Bot*" + chr(10) + chr(10) +
        "Ваши попытки: *" + str(db_get_attempts(user_id)) + "*" + chr(10) + chr(10) +
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(user_id)
    )
    return WAITING_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /start")
    return ConversationHandler.END


def main():
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
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
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        per_user=True,
        per_chat=True,
        block=False,
    )
    app.add_handler(conv)
    print("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
