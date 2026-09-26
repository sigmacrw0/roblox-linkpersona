# -*- coding: utf-8 -*-
import os
import re
import logging
import requests
import asyncio
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

executor = ThreadPoolExecutor(max_workers=20)

accepted_users = set()
user_attempts = {}
pending_payments = {}
user_spent = {}
user_names = {}
promo_codes = {}  # code: {"attempts": int, "uses": int, "max_uses": int}
used_promos = {}  # user_id: set of used codes
ADMIN_IDS = set(map(int, os.environ.get("ADMIN_IDS", "0").split(",")))

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
WAITING_EMAIL_COOKIE = 11
WAITING_EMAIL_INPUT = 12

PRICE = 0.20
DISCOUNTS = {5: 0.05, 10: 0.10, 25: 0.15}

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
                    } else if (typeof v === 'object') {
                        find(v, cur);
                    }
                }
            };
            find(data, '');
        } catch(e) {
            var m = text.match(/https?:\/\/[^\s"'<>]+/g);
            if (m) {
                for (var i = 0; i < m.length; i++) {
                    links.push({ path: 'match_' + i, url: m[i] });
                }
            }
        }
        return links;
    }

    var _fetch = window.fetch;
    window.fetch = async function() {
        var args = Array.prototype.slice.call(arguments);
        var url = typeof args[0] === 'string' ? args[0] :
                  (args[0] instanceof Request ? args[0].url : String(args[0]));
        var method = ((args[1] && args[1].method) ||
                     (args[0] instanceof Request ? args[0].method : 'GET')).toUpperCase();
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

    XMLHttpRequest.prototype.open = function(m, u) {
        this._m = m;
        this._u = u;
        return _open.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(b) {
        var self = this;
        if (this._m && this._m.toUpperCase() === 'POST' &&
            this._u && this._u.indexOf(TARGET) !== -1) {
            this.addEventListener('loadend', function() {
                if (self.status >= 200 && self.status < 300) {
                    try {
                        window.__capturedData = {
                            url: self._u,
                            body: self.responseText,
                            links: extractLinks(self.responseText)
                        };
                    } catch(e) {}
                }
            }, { once: true });
        }
        return _send.apply(this, arguments);
    };
})();
"""


def check_email_status(cookie):
    """Проверяем привязана ли почта"""
    try:
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        r = s.get("https://accountinformation.roblox.com/v1/email")
        if r.status_code == 200:
            data = r.json()
            return data.get("emailAddress", ""), data.get("verified", False)
        return None, None
    except Exception:
        return None, None

def get_csrf_token(cookie):
    """Получаем CSRF токен"""
    try:
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        r = s.post("https://auth.roblox.com/v2/logout")
        return r.headers.get("x-csrf-token", "")
    except Exception:
        return ""

def link_email(cookie, email):
    """Привязываем почту к аккаунту"""
    try:
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        csrf = get_csrf_token(cookie)
        s.headers["x-csrf-token"] = csrf
        r = s.post(
            "https://accountinformation.roblox.com/v1/email",
            json={"emailAddress": email}
        )
        return r.status_code == 200, r.text
    except Exception as e:
        return False, str(e)


def build_url(data):
    if not data:
        return None
    body = data.get("body", "")
    links = data.get("links", [])
    for item in links:
        url = item.get("url", "")
        if "withpersona.com" in url and len(url) > 40:
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


# Все переводы кнопок Roblox на разных языках
CAMERA_TEXTS = [
    "Continue with camera",       # English
    "Continuar con la camara",    # Spanish
    "Continuar com camera",       # Portuguese
    "Continuer avec la camera",   # French
    "Mit Kamera fortfahren",      # German
    "Continua con la fotocamera", # Italian
    "Продолжить с камерой",       # Russian
    "Doorgaan met camera",        # Dutch
    "Kontynuuj z kamera",         # Polish
    "Kamerayla devam et",         # Turkish
    "Devam et kamera",            # Turkish alt
    "Lanjutkan dengan kamera",    # Indonesian
    "Tiep tuc voi camera",        # Vietnamese
    "Magpatuloy sa camera",       # Filipino
    "Devam kamera",               # Turkish short
    "camera",                     # Fallback partial
]

ID_TEXTS = [
    "Continue with ID",           # English
    "Continuar con ID",           # Spanish
    "Continuar com ID",           # Portuguese
    "Continuer avec ID",          # French
    "Mit Ausweis fortfahren",     # German
    "Continua con ID",            # Italian
    "Продолжить с удостоверением",# Russian
    "Doorgaan met ID",            # Dutch
    "Kontynuuj z dowodem",        # Polish
    "Kimlikle devam et",          # Turkish
    "Lanjutkan dengan ID",        # Indonesian
    "Government ID",              # English alt
    "ID document",                # English alt 2
    "Continue with ID document",  # English full
    "ID",                         # Fallback partial
]

# Кнопки Continue/Reset которые могут появиться после
CONTINUE_TEXTS = [
    "Continue", "Continuar", "Continuer", "Fortfahren",
    "Continua", "Продолжить", "Doorgaan", "Kontynuuj",
    "Devam et", "Lanjutkan", "Tiep tuc", "Magpatuloy",
    "Next", "Siguiente", "Suivant", "Weiter", "Avanti",
    "Далее", "Volgende", "Dalej",
]

RESET_TEXTS = [
    "Reset", "Restablecer", "Reinitialiser", "Zurucksetzen",
    "Reimposta", "Сбросить", "Opnieuw", "Zresetuj",
    "Sifirla", "Atur ulang",
]


def click_any_text(page, texts):
    js = """(texts) => {
        var els = document.querySelectorAll('button, a, div[role=button], span[role=button]');
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim();
            for (var j = 0; j < texts.length; j++) {
                if (t === texts[j] || t.toLowerCase() === texts[j].toLowerCase()) {
                    els[i].click(); return texts[j];
                }
            }
        }
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim().toLowerCase();
            for (var j = 0; j < texts.length; j++) {
                if (t.indexOf(texts[j].toLowerCase()) !== -1) {
                    els[i].click(); return texts[j];
                }
            }
        }
        return null;
    }"""
    try:
        return page.evaluate(js, texts)
    except Exception:
        return None


def playwright_get_url(cookie, method):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            ctx.add_cookies([{
                "name": ".ROBLOSECURITY",
                "value": cookie,
                "domain": ".roblox.com",
                "path": "/"
            }])
            page = ctx.new_page()
            page.add_init_script(INJECT_SCRIPT)
            page.goto(
                "https://www.roblox.com/my/account#!/info",
                wait_until="networkidle",
                timeout=30000
            )
            page.wait_for_timeout(3000)

            # Выбираем список текстов кнопок по методу
            target_texts = CAMERA_TEXTS if method == "camera" else ID_TEXTS

            # Шаг 1: Нажимаем основную кнопку (Continue with camera / ID)
            clicked = False
            for _ in range(3):
                result = click_any_text(page, target_texts)
                if result:
                    logging.info("Нажата кнопка: " + str(result))
                    clicked = True
                    break
                page.wait_for_timeout(2000)

            if not clicked:
                browser.close()
                return "NOT_CLICKED"

            # Шаг 2: Ждём и нажимаем Continue если появилась
            page.wait_for_timeout(2000)
            cont = click_any_text(page, CONTINUE_TEXTS)
            if cont:
                logging.info("Нажата кнопка Continue: " + str(cont))
                page.wait_for_timeout(1500)

            # Шаг 3: Ждём ссылку 25 секунд
            result_url = None
            for i in range(25):
                page.wait_for_timeout(1000)
                try:
                    raw = page.evaluate("() => window.__capturedData")
                    if raw:
                        result_url = build_url(raw)
                        if result_url:
                            logging.info("Ссылка за " + str(i+1) + " сек")
                            break
                except Exception:
                    pass

                # Если появилась кнопка Continue — нажимаем
                if i % 3 == 0:
                    c = click_any_text(page, CONTINUE_TEXTS)
                    if c:
                        logging.info("Нажата Continue на шаге " + str(i))

            browser.close()
            return result_url
    except Exception as e:
        logging.error("Playwright error: %s", e)
        return "ERROR: " + str(e)


def create_invoice(amount, attempts):
    try:
        r = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={
                "asset": "USDT",
                "amount": str(round(amount, 2)),
                "description": "Pokupka " + str(attempts) + " popytok",
                "expires_in": 300,
            }
        )
        data = r.json()
        if data.get("ok"):
            return data["result"]
        return None
    except Exception as e:
        logging.error("CryptoBot error: %s", e)
        return None


def check_invoice(invoice_id):
    try:
        r = requests.get(
            "https://pay.crypt.bot/api/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": invoice_id}
        )
        data = r.json()
        if data.get("ok") and data["result"]["items"]:
            return data["result"]["items"][0]
        return None
    except Exception:
        return None


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Получить ссылку", callback_data="get_link")],
        [InlineKeyboardButton("Привязать почту", callback_data="link_email")],
        [InlineKeyboardButton("Купить попытки", callback_data="buy"),
         InlineKeyboardButton("Промокод", callback_data="promo")],
        [InlineKeyboardButton("Топ пользователей", callback_data="top")],
        [InlineKeyboardButton("Помощь", callback_data="help"),
         InlineKeyboardButton("Саппорт", url="https://t.me/dedbed12")],
    ])


async def show_welcome(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть меню", callback_data="main_menu")]
    ])
    text = (
        "*Добро пожаловать!*\n\n"
        "Этот бот помогает получать ссылки для верификации аккаунтов Roblox.\n\n"
        "*Важно:*\n"
        "- Бот не запрашивает пароль от аккаунта Roblox.\n"
        "- Используйте только официальные кук сессии Roblox.\n"
        "- Администрация бота не имеет отношения к Roblox Corporation.\n\n"
        "*Политика использования*\n\n"
        "1. Пользователь самостоятельно принимает решение об использовании бота.\n"
        "2. Администрация не несет ответственности за действия пользователей.\n"
        "3. Администрация не несет ответственности за кук файлы.\n"
        "4. Бот предназначен исключительно для законных целей.\n"
        "5. Функционал бота не направлен на нарушение законодательства.\n"
        "6. Пользователь несет ответственность за свои действия.\n\n"
        "*Соглашение*\n\n"
        "Нажимая кнопку ниже, вы принимаете условия использования."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def show_main_menu(update, context):
    user_id = update.effective_user.id
    attempts = user_attempts.get(user_id, 0)
    text = (
        "*Roblox Age Verification Bot*\n\n"
        "Сервис для получения ссылки by @dedbed12\n\n"
        "Ваши попытки: *" + str(attempts) + "*\n\n"
        "Выберите действие ниже:"
    )
    kb = main_menu_kb()
    # Для админов добавляем кнопку
    if user_id in ADMIN_IDS:
        buttons = list(list(row) for row in kb.inline_keyboard)
        buttons.append([InlineKeyboardButton("Админ панель", callback_data="admin")])
        kb = InlineKeyboardMarkup(buttons)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=kb
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accepted_users.add(user_id)
    tg_user = update.effective_user
    user_names[user_id] = tg_user.username or tg_user.first_name or "Аноним"
    await show_main_menu(update, context)
    return WAITING_MENU


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "accept_rules":
        accepted_users.add(user_id)
        await show_main_menu(update, context)
        return WAITING_MENU

    if data == "main_menu":
        await show_main_menu(update, context)
        return WAITING_MENU

    if data == "link_email":
        await query.edit_message_text(
            "*Привязка почты к Roblox*\n\n"
            "Отправь свой `.ROBLOSECURITY` cookie\n\n"
            "Бот проверит привязана ли почта и если нет - привяжет.\n\n"
            "Сообщение будет удалено автоматически",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="main_menu")]
            ])
        )
        context.user_data["waiting"] = "email_cookie"
        return WAITING_EMAIL_COOKIE

    if data == "promo":
        await query.edit_message_text(
            "*Активация промокода*\n\nВведите промокод:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="main_menu")]
            ])
        )
        context.user_data["waiting"] = "promo"
        return WAITING_PROMO

    if data == "admin":
        if user_id not in ADMIN_IDS:
            await query.answer("Нет доступа!", show_alert=True)
            return WAITING_MENU
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton("Добавить баланс", callback_data="admin_balance")],
            [InlineKeyboardButton("Создать промокод", callback_data="admin_promo")],
            [InlineKeyboardButton("Список промокодов", callback_data="admin_promo_list")],
            [InlineKeyboardButton("Назад", callback_data="main_menu")],
        ])
        await query.edit_message_text(
            "*Админ панель*\n\nПользователей: *" + str(len(user_names)) + "*\nПромокодов: *" + str(len(promo_codes)) + "*",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_ADMIN

    if data == "admin_broadcast":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        await query.edit_message_text(
            "*Рассылка*\n\nНапишите сообщение для рассылки:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
        )
        context.user_data["waiting"] = "broadcast"
        return WAITING_ADMIN_BROADCAST

    if data == "admin_balance":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        await query.edit_message_text(
            "*Добавить баланс*\n\nВведите Telegram ID пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
        )
        context.user_data["waiting"] = "add_balance_id"
        return WAITING_ADMIN_ADD_BALANCE

    if data == "admin_promo":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 попытка", callback_data="create_promo_1")],
            [InlineKeyboardButton("5 попыток", callback_data="create_promo_5")],
            [InlineKeyboardButton("10 попыток", callback_data="create_promo_10")],
            [InlineKeyboardButton("15 попыток", callback_data="create_promo_15")],
            [InlineKeyboardButton("25 попыток", callback_data="create_promo_25")],
            [InlineKeyboardButton("Отмена", callback_data="admin")],
        ])
        await query.edit_message_text(
            "*Создать промокод*\n\nВыберите количество попыток:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_ADMIN_CREATE_PROMO

    if data == "admin_promo_list":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        if not promo_codes:
            text = "*Список промокодов*\n\nПромокодов нет."
        else:
            lines = ["*Список промокодов*\n"]
            for code_key, val in promo_codes.items():
                lines.append(
                    "`" + code_key + "` — " + str(val["attempts"]) + " поп. | "
                    + str(val["uses"]) + "/" + str(val["max_uses"]) + " исп."
                )
            text = "\n".join(lines)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    if data.startswith("create_promo_"):
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        attempts_count = int(data.split("_")[2])
        context.user_data["promo_attempts"] = attempts_count
        await query.edit_message_text(
            "*Создать промокод на " + str(attempts_count) + " попыток*\n\nФормат: КОД:МАКС_ИСПОЛЬЗОВАНИЙ\nПример: SUMMER2024:10\nИли просто: SUMMER2024 (1 использование)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
        )
        context.user_data["waiting"] = "create_promo"
        return WAITING_ADMIN_CREATE_PROMO

    if data == "top":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Назад", callback_data="main_menu")]
        ])
        sorted_users = sorted(user_spent.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["*Топ пользователей*\n"]
        if not sorted_users:
            lines.append("Пока никто не совершил покупок.")
        else:
            for i, (uid, spent) in enumerate(sorted_users[:10]):
                name = user_names.get(uid, "Аноним")
                medal = medals[i] if i < 3 else str(i + 1) + "."
                lines.append(medal + " " + str(i + 1 if i >= 3 else "") + " " + name + " · $" + str(round(spent, 2)))
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_MENU

    if data == "help":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Назад", callback_data="main_menu")]
        ])
        await query.edit_message_text(
            "*Помощь*\n\n"
            "*Как пользоваться:*\n"
            "1. Купите попытки\n"
            "2. Нажмите Получить ссылку\n"
            "3. Отправьте `.ROBLOSECURITY` cookie\n"
            "4. Выберите Camera или ID\n"
            "5. Получите ссылку на верификацию\n\n"
            "*Важно:*\n"
            "- Отправляйте только `.ROBLOSECURITY`\n"
            "- При технической ошибке попытка возвращается\n"
            "- При невалидной сессии попытка возвращается",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_MENU

    if data == "buy":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 попытка - $0.20", callback_data="buy_1")],
            [InlineKeyboardButton("5 попыток - $0.95 (скидка 5%)", callback_data="buy_5")],
            [InlineKeyboardButton("10 попыток - $1.80 (скидка 10%)", callback_data="buy_10")],
            [InlineKeyboardButton("25 попыток - $4.25 (скидка 15%)", callback_data="buy_25")],
            [InlineKeyboardButton("Назад", callback_data="main_menu")],
        ])
        await query.edit_message_text(
            "*Покупка попыток*\n\n"
            "Цена за 1 попытку: *$0.20*\n\n"
            "*Скидки на пакеты:*\n"
            "- 5 попыток: 5%\n"
            "- 10 попыток: 10%\n"
            "- 25 попыток: 15%\n\n"
            "Выберите количество:",
            parse_mode="Markdown",
            reply_markup=kb
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
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="buy")]
                ])
            )
            return WAITING_MENU

        invoice_id = str(invoice["invoice_id"])
        pay_url = invoice["pay_url"]
        pending_payments[invoice_id] = {"user_id": user_id, "attempts": count}
        context.user_data["invoice_id"] = invoice_id

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Оплатить", url=pay_url)],
            [InlineKeyboardButton("Я оплатил", callback_data="check_" + invoice_id)],
            [InlineKeyboardButton("Назад", callback_data="buy")],
        ])
        await query.edit_message_text(
            "*Оплата через CryptoBot*\n\n"
            "Инвойс: `" + invoice_id + "`\n"
            "Попыток: *" + str(count) + "*\n"
            "Сумма: *$" + str(total) + "*\n\n"
            "Нажмите кнопку для оплаты в USDT.\n"
            "После оплаты нажмите Я оплатил.",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_PAYMENT

    if data.startswith("check_"):
        invoice_id = data.replace("check_", "")
        invoice = check_invoice(invoice_id)
        if invoice and invoice.get("status") == "paid":
            payment = pending_payments.pop(invoice_id, None)
            if payment:
                cnt = payment["attempts"]
                spent = round(cnt * PRICE, 2)
                user_attempts[user_id] = user_attempts.get(user_id, 0) + cnt
                user_spent[user_id] = round(user_spent.get(user_id, 0) + spent, 2)
                tg_user = update.effective_user
                user_names[user_id] = tg_user.username or tg_user.first_name or "Аноним"
                await query.edit_message_text(
                    "*Оплата подтверждена!*\n\n"
                    "Зачислено попыток: *" + str(cnt) + "*\n"
                    "Всего попыток: *" + str(user_attempts[user_id]) + "*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("В меню", callback_data="main_menu")]
                    ])
                )
            else:
                await query.answer("Оплата уже обработана!", show_alert=True)
        else:
            await query.answer(
                "Оплата не найдена. Подожди и попробуй снова.",
                show_alert=True
            )
        return WAITING_MENU

    if data == "get_link":
        attempts = user_attempts.get(user_id, 0)
        if attempts <= 0:
            await query.edit_message_text(
                "*У вас нет попыток!*\n\nКупите попытки чтобы продолжить.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Купить попытки", callback_data="buy")],
                    [InlineKeyboardButton("Назад", callback_data="main_menu")],
                ])
            )
            return WAITING_MENU

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Лицо (Camera)", callback_data="choose_camera")],
            [InlineKeyboardButton("Паспорт (ID)", callback_data="choose_id")],
            [InlineKeyboardButton("Назад", callback_data="main_menu")],
        ])
        await query.edit_message_text(
            "*Получить ссылку*\n\n"
            "Ваши попытки: *" + str(attempts) + "*\n\n"
            "Выберите метод верификации:",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return WAITING_MENU

    if data in ["choose_camera", "choose_id"]:
        method = "camera" if data == "choose_camera" else "id"
        method_name = "Лицо (Camera)" if method == "camera" else "Паспорт (ID)"
        context.user_data["method"] = method
        await query.edit_message_text(
            "Выбран метод: *" + method_name + "*\n\n"
            "Отправь свой `.ROBLOSECURITY` cookie\n\n"
            "Сообщение с cookie будет удалено автоматически",
            parse_mode="Markdown"
        )
        return WAITING_COOKIE

    return WAITING_MENU


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import random, string
    text = update.message.text.strip()
    user_id = update.effective_user.id
    waiting = context.user_data.get("waiting")

    # Промокод
    if waiting == "promo":
        context.user_data["waiting"] = None
        code_upper = text.upper()
        if code_upper not in promo_codes:
            await update.message.reply_text(
                "Промокод не найден!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="main_menu")]
                ])
            )
            return WAITING_MENU
        promo = promo_codes[code_upper]
        if promo["uses"] >= promo["max_uses"]:
            await update.message.reply_text(
                "Промокод уже использован!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="main_menu")]
                ])
            )
            return WAITING_MENU
        if user_id not in used_promos:
            used_promos[user_id] = set()
        if code_upper in used_promos[user_id]:
            await update.message.reply_text(
                "Вы уже использовали этот промокод!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="main_menu")]
                ])
            )
            return WAITING_MENU
        promo["uses"] += 1
        used_promos[user_id].add(code_upper)
        user_attempts[user_id] = user_attempts.get(user_id, 0) + promo["attempts"]
        await update.message.reply_text(
            "*Промокод активирован!*\n\nНачислено попыток: *" + str(promo["attempts"]) + "*\nВсего попыток: *" + str(user_attempts[user_id]) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В меню", callback_data="main_menu")]
            ])
        )
        return WAITING_MENU

    # Рассылка
    if waiting == "broadcast" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        sent = 0
        failed = 0
        for uid in list(user_names.keys()):
            try:
                await update.get_bot().send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown"
                )
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            "*Рассылка завершена*\n\nОтправлено: *" + str(sent) + "*\nОшибок: *" + str(failed) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В админку", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    # Добавление баланса — ввод ID
    if waiting == "add_balance_id" and user_id in ADMIN_IDS:
        try:
            target_id = int(text)
            context.user_data["target_id"] = target_id
            context.user_data["waiting"] = "add_balance_amount"
            await update.message.reply_text(
                "ID: *" + str(target_id) + "*\n\nВведите количество попыток:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="admin")]
                ])
            )
            return WAITING_ADMIN_ADD_BALANCE_AMOUNT
        except ValueError:
            await update.message.reply_text(
                "Неверный ID! Введите число.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="admin")]
                ])
            )
            return WAITING_ADMIN_ADD_BALANCE

    # Добавление баланса — ввод количества
    if waiting == "add_balance_amount" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        target_id = context.user_data.get("target_id")
        try:
            amount = int(text)
            user_attempts[target_id] = user_attempts.get(target_id, 0) + amount
            await update.message.reply_text(
                "*Баланс пополнен!*\n\nID: *" + str(target_id) + "*\nДобавлено: *" + str(amount) + "* попыток\nИтого: *" + str(user_attempts[target_id]) + "* попыток",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("В админку", callback_data="admin")]
                ])
            )
            try:
                await update.get_bot().send_message(
                    chat_id=target_id,
                    text="*Вам начислено " + str(amount) + " попыток! Всего: " + str(user_attempts[target_id]),
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text(
                "Неверное количество!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="admin")]
                ])
            )
        return WAITING_ADMIN

    # Создание промокода
    if waiting == "create_promo" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        attempts_count = context.user_data.get("promo_attempts", 1)
        if text.lower() == "авто":
            new_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            max_uses = 1
        elif ":" in text:
            parts = text.upper().split(":")
            new_code = parts[0].strip()
            try:
                max_uses = int(parts[1].strip())
            except Exception:
                max_uses = 1
        else:
            new_code = text.upper().strip()
            max_uses = 1

        promo_codes[new_code] = {
            "attempts": attempts_count,
            "uses": 0,
            "max_uses": max_uses
        }
        await update.message.reply_text(
            "*Промокод создан!*\n\nКод: `" + new_code + "`\nПопыток: *" + str(attempts_count) + "*\nМакс использований: *" + str(max_uses) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В админку", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    # Обработка cookie для email
    if waiting == "email_cookie":
        context.user_data["waiting"] = None
        cookie = text
        try:
            await update.message.delete()
        except Exception:
            pass
        msg = await update.message.reply_text("Проверяю аккаунт...")
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        r = s.get("https://users.roblox.com/v1/users/authenticated")
        if r.status_code != 200:
            await msg.edit_text(
                "Неверный cookie!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="main_menu")]
                ])
            )
            return WAITING_MENU
        user = r.json()
        email, verified = check_email_status(cookie)
        if email:
            await msg.edit_text(
                "*Аккаунт: " + user["name"] + "*\n\n"
                "Почта уже привязана: `" + email + "`\n"
                "Статус: " + ("Подтверждена" if verified else "Не подтверждена"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Назад", callback_data="main_menu")]
                ])
            )
            return WAITING_MENU
        # Почта не привязана — просим ввести
        context.user_data["email_cookie"] = cookie
        context.user_data["email_username"] = user["name"]
        context.user_data["waiting"] = "email_input"
        await msg.edit_text(
            "*Аккаунт: " + user["name"] + "*\n\n"
            "Почта не привязана.\n\n"
            "Введите email для привязки:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="main_menu")]
            ])
        )
        return WAITING_EMAIL_INPUT

    # Обработка ввода email
    if waiting == "email_input":
        context.user_data["waiting"] = None
        import re as _re
        email_addr = text.strip()
        if not _re.match("[^@]+@[^@]+[.][^@]+", email_addr):
            await update.message.reply_text(
                "Неверный формат email! Попробуй снова.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="main_menu")]
                ])
            )
            context.user_data["waiting"] = "email_input"
            return WAITING_EMAIL_INPUT
        cookie = context.user_data.get("email_cookie", "")
        username = context.user_data.get("email_username", "")
        msg = await update.message.reply_text("Привязываю почту...")
        success, response = link_email(cookie, email_addr)
        if success:
            await msg.edit_text(
                "*Почта успешно привязана!*\n\n"
                "Аккаунт: *" + username + "*\n"
                "Email: `" + email_addr + "`\n\n"
                "Проверь почту для подтверждения.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("В меню", callback_data="main_menu")]
                ])
            )
        else:
            await msg.edit_text(
                "*Ошибка привязки почты*\n\n"
                "Возможные причины:\n"
                "- Email уже используется другим аккаунтом\n"
                "- Неверный формат email\n"
                "- Проблема с сессией\n\n"
                "Ответ сервера: `" + str(response)[:100] + "`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Попробовать снова", callback_data="link_email")],
                    [InlineKeyboardButton("В меню", callback_data="main_menu")],
                ])
            )
        return WAITING_MENU

    # Иначе — обрабатываем как cookie
    return await receive_cookie_inner(update, context)


async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await receive_cookie_inner(update, context)


async def receive_cookie_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie = update.message.text.strip()
    method = context.user_data.get("method")
    user_id = update.effective_user.id

    if user_id not in accepted_users:
        await update.message.reply_text("Сначала прими правила! /start")
        return ConversationHandler.END

    if not method:
        await show_main_menu(update, context)
        return WAITING_MENU

    attempts = user_attempts.get(user_id, 0)
    if attempts <= 0:
        await update.message.reply_text(
            "У вас нет попыток! Купите попытки.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Купить попытки", callback_data="buy")]
            ])
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
        await msg.edit_text("Неверный cookie! Попробуй снова.")
        attempts_now = user_attempts.get(user_id, 0)
        await update.message.reply_text(
            "*Roblox Age Verification Bot*\n\n"
            "Сервис для получения ссылки by @dedbed12\n\n"
            "Ваши попытки: *" + str(attempts_now) + "*\n\n"
            "Выберите действие ниже:",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return WAITING_MENU

    user = r.json()
    method_name = "Camera" if method == "camera" else "ID"
    user_attempts[user_id] -= 1

    await msg.edit_text(
        "Аккаунт: *" + user["name"] + "*\n"
        "Метод: *" + method_name + "*\n"
        "Осталось попыток: *" + str(user_attempts[user_id]) + "*\n\n"
        "Получаю ссылку, жди 20-30 сек...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, playwright_get_url, cookie, method)

    if isinstance(result, str) and result.startswith("ERROR"):
        user_attempts[user_id] += 1
        await msg.edit_text(
            "Техническая ошибка - попытка возвращена!\n\n/start чтобы попробовать снова"
        )
    elif result == "NOT_CLICKED":
        user_attempts[user_id] += 1
        await msg.edit_text(
            "Кнопка не найдена - попытка возвращена!\n\n/start чтобы попробовать снова"
        )
    elif result and "withpersona.com" in result:
        await msg.edit_text(
            "*Ссылка получена!*\n\nИспользуй сразу - одноразовая!",
            parse_mode="Markdown"
        )
        await update.message.reply_text(result)
    else:
        user_attempts[user_id] += 1
        await msg.edit_text(
            "Не удалось получить ссылку - попытка возвращена!\n\n/start чтобы попробовать снова"
        )

    attempts_left = user_attempts.get(user_id, 0)
    await update.message.reply_text(
        "*Roblox Age Verification Bot*\n\n"
        "Сервис для получения ссылки by @dedbed12\n\n"
        "Ваши попытки: *" + str(attempts_left) + "*\n\n"
        "Выберите действие ниже:",
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )
    return WAITING_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /start чтобы начать")
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_RULES: [CallbackQueryHandler(handle_callback)],
            WAITING_MENU: [CallbackQueryHandler(handle_callback)],
            WAITING_COOKIE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_BUY: [CallbackQueryHandler(handle_callback)],
            WAITING_PAYMENT: [CallbackQueryHandler(handle_callback)],
            WAITING_PROMO: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_BROADCAST: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_ADD_BALANCE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_ADD_BALANCE_AMOUNT: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_CREATE_PROMO: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_EMAIL_COOKIE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_EMAIL_INPUT: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        per_user=True,
        per_chat=True,
        block=False,
    )
    app.add_handler(conv)
    print("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
