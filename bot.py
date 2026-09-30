# -*- coding: utf-8 -*-
import os
import re
import logging
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

executor = ThreadPoolExecutor(max_workers=20)

# Хранилища данных
accepted_users = set()
user_attempts = {}
user_spent = {}
user_names = {}
pending_payments = {}
promo_codes = {}
used_promos = {}

# Состояния
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
        this._m = m; this._u = u;
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

# ВАЖНО: не включаем "Continue" т.к. он совпадёт с "Continue with camera/ID"
CONTINUE_TEXTS = [
    "Next", "Proceed", "OK", "Submit",
    "Suivant", "Weiter", "Avanti",
    "Далее", "Вперёд",
    "Volgende", "Nastepny",
]


def click_any_text(page, texts, exclude=None):
    """
    exclude: список слов — если текст кнопки содержит их, пропускаем
    Это нужно чтобы CONTINUE не нажимал "Continue with camera/ID"
    """
    if exclude is None:
        exclude = []
    js = """(args) => {
        var texts = args[0];
        var exclude = args[1];
        var els = document.querySelectorAll('button, a, div[role=button], span[role=button]');
        // Точное совпадение
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim();
            var tl = t.toLowerCase();
            // Проверяем исключения
            var skip = false;
            for (var e = 0; e < exclude.length; e++) {
                if (tl.indexOf(exclude[e].toLowerCase()) !== -1) { skip = true; break; }
            }
            if (skip) continue;
            for (var j = 0; j < texts.length; j++) {
                if (t === texts[j] || tl === texts[j].toLowerCase()) {
                    els[i].click(); return texts[j];
                }
            }
        }
        // Частичное совпадение
        for (var i = 0; i < els.length; i++) {
            var t = els[i].textContent.trim();
            var tl = t.toLowerCase();
            var skip = false;
            for (var e = 0; e < exclude.length; e++) {
                if (tl.indexOf(exclude[e].toLowerCase()) !== -1) { skip = true; break; }
            }
            if (skip) continue;
            for (var j = 0; j < texts.length; j++) {
                if (tl.indexOf(texts[j].toLowerCase()) !== -1) {
                    els[i].click(); return texts[j];
                }
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

            target_texts = CAMERA_TEXTS if method == "camera" else ID_TEXTS

            # Шаг 1: Нажимаем Reset если есть
            reset_clicked = click_any_text(page, RESET_TEXTS)
            if reset_clicked:
                logging.info("Reset: " + str(reset_clicked))
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            # Шаг 2: Нажимаем Camera / ID — 5 попыток
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

            # Шаг 3: Continue если появилась
            # Исключаем camera/ID чтобы не нажать не ту кнопку
            exclude_words = ["camera", "id", "passport", "камер", "паспорт"]
            page.wait_for_timeout(2000)
            cont = click_any_text(page, CONTINUE_TEXTS, exclude=exclude_words)
            if cont:
                logging.info("Continue: " + str(cont))
                page.wait_for_timeout(1500)

            # Шаг 4: Ждём ссылку 30 секунд
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
                    c = click_any_text(page, CONTINUE_TEXTS, exclude=exclude_words)
                    if c:
                        logging.info("Continue шаг " + str(i))

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
        if data.get("ok") and data["result"]["items"]:
            return data["result"]["items"][0]
        return None
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


# ===== ПОКАЗ МЕНЮ =====

async def show_welcome(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть меню", callback_data="main_menu")]
    ])
    text = (
        "*Добро пожаловать!*" + chr(10) + chr(10) +
        "Этот бот помогает получать ссылки для верификации аккаунтов Roblox." + chr(10) + chr(10) +
        "*Важно:*" + chr(10) +
        "- Бот не запрашивает пароль от аккаунта." + chr(10) +
        "- Используйте только официальные cookie Roblox." + chr(10) + chr(10) +
        "*Политика использования*" + chr(10) + chr(10) +
        "1. Пользователь принимает решение об использовании бота самостоятельно." + chr(10) +
        "2. Администрация не несёт ответственности за действия пользователей." + chr(10) +
        "3. Бот предназначен исключительно для законных целей." + chr(10) +
        "4. Пользователь несёт ответственность за свои действия." + chr(10) + chr(10) +
        "Нажмите кнопку ниже для продолжения:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_main_menu(update, context):
    user_id = update.effective_user.id
    attempts = user_attempts.get(user_id, 0)
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
    accepted_users.add(user_id)
    tg = update.effective_user
    user_names[user_id] = tg.username or tg.first_name or "Аноним"
    await show_main_menu(update, context)
    return WAITING_MENU


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    # Главное меню
    if data in ["main_menu", "accept_rules"]:
        await show_main_menu(update, context)
        return WAITING_MENU

    # Помощь
    if data == "help":
        await query.edit_message_text(
            "*Помощь*" + chr(10) + chr(10) +
            "*Как пользоваться:*" + chr(10) +
            "1. Купите попытки" + chr(10) +
            "2. Нажмите Получить ссылку" + chr(10) +
            "3. Отправьте `.ROBLOSECURITY` cookie" + chr(10) +
            "4. Выберите Camera или ID" + chr(10) +
            "5. Получите ссылку" + chr(10) + chr(10) +
            "*Важно:*" + chr(10) +
            "- При технической ошибке попытка возвращается" + chr(10) +
            "- При невалидном cookie попытка возвращается",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="main_menu")]
            ])
        )
        return WAITING_MENU

    # Топ
    if data == "top":
        sorted_users = sorted(user_spent.items(), key=lambda x: x[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = ["*Топ пользователей*" + chr(10)]
        if not sorted_users:
            lines.append("Пока никто не совершил покупок.")
        else:
            for i, (uid, spent) in enumerate(sorted_users[:10]):
                name = user_names.get(uid, "Аноним")
                medal = medals[i] if i < 3 else str(i + 1) + "."
                lines.append(medal + " " + name + " - $" + str(round(spent, 2)))
        await query.edit_message_text(
            chr(10).join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="main_menu")]
            ])
        )
        return WAITING_MENU

    # Промокод
    if data == "promo":
        context.user_data["waiting"] = "promo"
        await query.edit_message_text(
            "*Активация промокода*" + chr(10) + chr(10) + "Введите промокод:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="main_menu")]
            ])
        )
        return WAITING_PROMO

    # Купить
    if data == "buy":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 попытка - $0.20", callback_data="buy_1")],
            [InlineKeyboardButton("5 попыток - $0.95 (5%)", callback_data="buy_5")],
            [InlineKeyboardButton("10 попыток - $1.80 (10%)", callback_data="buy_10")],
            [InlineKeyboardButton("25 попыток - $4.25 (15%)", callback_data="buy_25")],
            [InlineKeyboardButton("Назад", callback_data="main_menu")],
        ])
        await query.edit_message_text(
            "*Покупка попыток*" + chr(10) + chr(10) +
            "Цена: *$0.20* за попытку" + chr(10) + chr(10) +
            "Скидки: 5 шт -5%, 10 шт -10%, 25 шт -15%",
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
                spent = round(cnt * PRICE, 2)
                user_attempts[user_id] = user_attempts.get(user_id, 0) + cnt
                user_spent[user_id] = round(user_spent.get(user_id, 0) + spent, 2)
                tg = update.effective_user
                user_names[user_id] = tg.username or tg.first_name or "Аноним"
                await query.edit_message_text(
                    "*Оплата подтверждена!*" + chr(10) + chr(10) +
                    "Зачислено: *" + str(cnt) + "* попыток" + chr(10) +
                    "Всего: *" + str(user_attempts[user_id]) + "* попыток",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("В меню", callback_data="main_menu")]
                    ])
                )
            else:
                await query.answer("Уже обработано!", show_alert=True)
        else:
            await query.answer("Оплата не найдена. Подожди и попробуй снова.", show_alert=True)
        return WAITING_MENU

    # Получить ссылку
    if data == "get_link":
        attempts = user_attempts.get(user_id, 0)
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
            "Попыток: *" + str(attempts) + "*" + chr(10) + chr(10) +
            "Выберите метод:",
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
        await query.edit_message_text(
            "*Админ панель*" + chr(10) + chr(10) +
            "Пользователей: *" + str(len(user_names)) + "*" + chr(10) +
            "Промокодов: *" + str(len(promo_codes)) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Рассылка", callback_data="admin_broadcast")],
                [InlineKeyboardButton("Добавить баланс", callback_data="admin_balance")],
                [InlineKeyboardButton("Создать промокод", callback_data="admin_promo")],
                [InlineKeyboardButton("Список промокодов", callback_data="admin_promo_list")],
                [InlineKeyboardButton("Назад", callback_data="main_menu")],
            ])
        )
        return WAITING_ADMIN

    if data == "admin_broadcast":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        context.user_data["waiting"] = "broadcast"
        await query.edit_message_text(
            "*Рассылка*" + chr(10) + chr(10) + "Напишите текст рассылки:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN_BROADCAST

    if data == "admin_balance":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        context.user_data["waiting"] = "add_balance_id"
        await query.edit_message_text(
            "*Добавить баланс*" + chr(10) + chr(10) + "Введите Telegram ID пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
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
            "`VIP 25 1` - 25 попыток, 1 раз",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN_CREATE_PROMO

    if data == "admin_promo_list":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        if not promo_codes:
            text = "*Промокодов нет*"
        else:
            lines = ["*Список промокодов*" + chr(10)]
            for c, v in promo_codes.items():
                lines.append("`" + c + "` - " + str(v["attempts"]) + " поп. | " + str(v["uses"]) + "/" + str(v["max_uses"]))
            text = chr(10).join(lines)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Назад", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    return WAITING_MENU


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            await update.message.reply_text("Промокод уже использован!")
            return WAITING_MENU
        if user_id not in used_promos:
            used_promos[user_id] = set()
        if code_upper in used_promos[user_id]:
            await update.message.reply_text("Вы уже использовали этот промокод!")
            return WAITING_MENU
        promo["uses"] += 1
        used_promos[user_id].add(code_upper)
        user_attempts[user_id] = user_attempts.get(user_id, 0) + promo["attempts"]
        await update.message.reply_text(
            "*Промокод активирован!*" + chr(10) + chr(10) +
            "Начислено: *" + str(promo["attempts"]) + "* попыток" + chr(10) +
            "Всего: *" + str(user_attempts[user_id]) + "* попыток",
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
                await update.get_bot().send_message(chat_id=uid, text=text, parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            "*Рассылка завершена*" + chr(10) +
            "Отправлено: " + str(sent) + chr(10) +
            "Ошибок: " + str(failed),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В админку", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    # Добавить баланс — ID
    if waiting == "add_balance_id" and user_id in ADMIN_IDS:
        try:
            target_id = int(text)
            context.user_data["target_id"] = target_id
            context.user_data["waiting"] = "add_balance_amount"
            await update.message.reply_text(
                "ID: *" + str(target_id) + "*" + chr(10) + chr(10) + "Введите количество попыток:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="admin")]
                ])
            )
            return WAITING_ADMIN_ADD_BALANCE_AMOUNT
        except ValueError:
            await update.message.reply_text("Неверный ID! Введите число.")
            return WAITING_ADMIN_ADD_BALANCE

    # Добавить баланс — количество
    if waiting == "add_balance_amount" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        target_id = context.user_data.get("target_id")
        try:
            amount = int(text)
            user_attempts[target_id] = user_attempts.get(target_id, 0) + amount
            await update.message.reply_text(
                "*Баланс пополнен!*" + chr(10) +
                "ID: " + str(target_id) + chr(10) +
                "Добавлено: *" + str(amount) + "* попыток" + chr(10) +
                "Итого: *" + str(user_attempts[target_id]) + "*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("В админку", callback_data="admin")]
                ])
            )
            try:
                await update.get_bot().send_message(
                    chat_id=target_id,
                    text="Вам начислено *" + str(amount) + "* попыток! Всего: *" + str(user_attempts[target_id]) + "*",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("Неверное количество!")
        return WAITING_ADMIN

    # Создать промокод
    if waiting == "create_promo" and user_id in ADMIN_IDS:
        context.user_data["waiting"] = None
        parts = text.strip().split()
        if len(parts) < 1:
            await update.message.reply_text("Неверный формат! Пример: `SUMMER 5 10`", parse_mode="Markdown")
            context.user_data["waiting"] = "create_promo"
            return WAITING_ADMIN_CREATE_PROMO
        new_code = parts[0].upper()
        try:
            attempts_count = int(parts[1]) if len(parts) > 1 else 1
        except Exception:
            attempts_count = 1
        try:
            max_uses = int(parts[2]) if len(parts) > 2 else 1
        except Exception:
            max_uses = 1
        attempts_count = max(1, min(attempts_count, 1000))
        max_uses = max(1, min(max_uses, 10000))
        promo_codes[new_code] = {"attempts": attempts_count, "uses": 0, "max_uses": max_uses}
        await update.message.reply_text(
            "*Промокод создан!*" + chr(10) + chr(10) +
            "Код: `" + new_code + "`" + chr(10) +
            "Попыток: *" + str(attempts_count) + "*" + chr(10) +
            "Использований: *" + str(max_uses) + "*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("В админку", callback_data="admin")]
            ])
        )
        return WAITING_ADMIN

    # Иначе — обрабатываем как cookie
    return await receive_cookie(update, context)


async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie = update.message.text.strip()
    method = context.user_data.get("method")
    user_id = update.effective_user.id

    if not method:
        await show_main_menu(update, context)
        return WAITING_MENU

    attempts = user_attempts.get(user_id, 0)
    if attempts <= 0:
        await update.message.reply_text(
            "Нет попыток! Купите через меню.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Купить", callback_data="buy")]
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
        await msg.edit_text("Неверный cookie! Попробуй снова." + chr(10) + chr(10) + "/start")
        return WAITING_MENU

    user = r.json()
    method_name = "Camera" if method == "camera" else "ID"
    user_attempts[user_id] -= 1

    await msg.edit_text(
        "Аккаунт: *" + user["name"] + "*" + chr(10) +
        "Метод: *" + method_name + "*" + chr(10) +
        "Осталось попыток: *" + str(user_attempts[user_id]) + "*" + chr(10) + chr(10) +
        "Жди 20-30 сек...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, playwright_get_url, cookie, method)

    if isinstance(result, str) and result.startswith("ERROR"):
        user_attempts[user_id] += 1
        err = result[7:150] if len(result) > 7 else "неизвестная"
        await msg.edit_text(
            "Техническая ошибка - попытка возвращена!" + chr(10) + chr(10) +
            "Причина: " + err + chr(10) + chr(10) + "/start"
        )
    elif result == "NOT_CLICKED":
        user_attempts[user_id] += 1
        await msg.edit_text("Кнопка не найдена - попытка возвращена!" + chr(10) + chr(10) + "/start")
    elif result and "withpersona.com" in result:
        await msg.edit_text(
            "*Ссылка получена!*" + chr(10) + chr(10) + "Используй сразу - одноразовая!",
            parse_mode="Markdown"
        )
        await update.message.reply_text(result)
    else:
        user_attempts[user_id] += 1
        await msg.edit_text("Не удалось получить ссылку - попытка возвращена!" + chr(10) + chr(10) + "/start")

    attempts_left = user_attempts.get(user_id, 0)
    await update.message.reply_text(
        "*Roblox Age Verification Bot*" + chr(10) + chr(10) +
        "Ваши попытки: *" + str(attempts_left) + "*" + chr(10) + chr(10) +
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu_kb(user_id)
    )
    return WAITING_MENU


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено. /start")
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
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv)
    print("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
