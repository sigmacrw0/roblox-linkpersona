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

WAITING_RULES = 0
WAITING_MENU = 1
WAITING_COOKIE = 2
WAITING_BUY = 3
WAITING_PAYMENT = 4

PRICE = 0.20
DISCOUNTS = {5: 0.05, 10: 0.10, 25: 0.15}

INJECT_SCRIPT = (
    "(function() {"
    "window.__capturedData = null;"
    "var TARGET = 'age-verification-service/v1/persona-id-verification/start-verification';"
    "function extractLinks(text) {"
    "  var links = [];"
    "  try {"
    "    var data = JSON.parse(text);"
    "    var find = function(obj, path) {"
    "      if (!obj || typeof obj !== 'object') return;"
    "      for (var k in obj) {"
    "        var cur = path ? path+'.'+k : k;"
    "        var v = obj[k];"
    "        if (typeof v === 'string') {"
    "          if (k.toLowerCase().indexOf('url') !== -1 ||"
    "              k.toLowerCase().indexOf('link') !== -1 ||"
    "              k.toLowerCase().indexOf('inquiry') !== -1 ||"
    "              v.indexOf('http') === 0) {"
    "            links.push({ path: cur, url: v });"
    "          }"
    "        } else if (typeof v === 'object') { find(v, cur); }"
    "      }"
    "    };"
    "    find(data, '');"
    "  } catch(e) {"
    "    var m = text.match(/https?:\\/\\/[^\\s\"'<>]+/g);"
    "    if (m) m.forEach(function(u,i){ links.push({path:'match_'+i, url:u}); });"
    "  }"
    "  return links;"
    "}"
    "var _fetch = window.fetch;"
    "window.fetch = async function() {"
    "  var args = Array.prototype.slice.call(arguments);"
    "  var url = typeof args[0]==='string' ? args[0] :"
    "            args[0] instanceof Request ? args[0].url : String(args[0]);"
    "  var method = ((args[1] && args[1].method) ||"
    "               (args[0] instanceof Request ? args[0].method : 'GET')).toUpperCase();"
    "  var res = await _fetch.apply(this, args);"
    "  if (method === 'POST' && url.indexOf(TARGET) !== -1) {"
    "    try {"
    "      var body = await res.clone().text();"
    "      window.__capturedData = { url: url, body: body, links: extractLinks(body) };"
    "    } catch(e) {}"
    "  }"
    "  return res;"
    "};"
    "var _open = XMLHttpRequest.prototype.open;"
    "var _send = XMLHttpRequest.prototype.send;"
    "XMLHttpRequest.prototype.open = function(m,u){"
    "  this._m=m; this._u=u; return _open.apply(this,arguments);"
    "};"
    "XMLHttpRequest.prototype.send = function(b){"
    "  var self = this;"
    "  if (this._m && this._m.toUpperCase()==='POST' && this._u && this._u.indexOf(TARGET)!==-1){"
    "    this.addEventListener('loadend', function(){"
    "      if (self.status >= 200 && self.status < 300){"
    "        try { window.__capturedData = { url: self._u, body: self.responseText, links: extractLinks(self.responseText) }; } catch(e){}"
    "      }"
    "    }, {once:true});"
    "  }"
    "  return _send.apply(this,arguments);"
    "};"
    "})();"
)


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

            target_text = "Continue with camera" if method == "camera" else "Continue with ID"
            clicked = False
            for _ in range(3):
                try:
                    clicked = page.evaluate(
                        """(txt) => {
                            var els = document.querySelectorAll('button, a, div, span');
                            for (var i=0; i<els.length; i++) {
                                if (els[i].textContent.trim() === txt) {
                                    els[i].click(); return true;
                                }
                            }
                            for (var i=0; i<els.length; i++) {
                                if (els[i].textContent.trim().toLowerCase().indexOf(txt.toLowerCase()) !== -1) {
                                    els[i].click(); return true;
                                }
                            }
                            return false;
                        }""",
                        target_text
                    )
                except Exception:
                    clicked = False
                if clicked:
                    break
                page.wait_for_timeout(2000)

            if not clicked:
                browser.close()
                return "NOT_CLICKED"

            result_url = None
            for _ in range(25):
                page.wait_for_timeout(1000)
                try:
                    raw = page.evaluate("() => window.__capturedData")
                    if raw:
                        result_url = build_url(raw)
                        if result_url:
                            break
                except Exception:
                    pass

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
        [InlineKeyboardButton("Купить попытки", callback_data="buy")],
        [InlineKeyboardButton("Помощь", callback_data="help")],
    ])


async def show_welcome(update, context):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Принять и продолжить", callback_data="accept_rules")]
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
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_main_menu(update, context):
    user_id = update.effective_user.id
    attempts = user_attempts.get(user_id, 0)
    text = (
        "*Roblox Age Verification Bot*\n\n"
        "Сервис для получения ссылки by @dedbed12\n\n"
        "Ваши попытки: *" + str(attempts) + "*\n\n"
        "Выберите действие ниже:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu_kb())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_welcome(update, context)
    return WAITING_RULES


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

    if data == "help":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
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
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="buy")]])
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
                user_attempts[user_id] = user_attempts.get(user_id, 0) + cnt
                await query.edit_message_text(
                    "*Оплата подтверждена!*\n\n"
                    "Зачислено попыток: *" + str(cnt) + "*\n"
                    "Всего попыток: *" + str(user_attempts[user_id]) + "*",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="main_menu")]])
                )
            else:
                await query.answer("Оплата уже обработана!", show_alert=True)
        else:
            await query.answer("Оплата не найдена. Подожди и попробуй снова.", show_alert=True)
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


async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie = update.message.text.strip()
    method = context.user_data.get("method")
    user_id = update.effective_user.id

    if user_id not in accepted_users:
        await update.message.reply_text("Сначала прими правила! /start")
        return ConversationHandler.END

    if not method:
        await update.message.reply_text("Сначала выбери метод! /start")
        return ConversationHandler.END

    attempts = user_attempts.get(user_id, 0)
    if attempts <= 0:
        await update.message.reply_text("У вас нет попыток! Купите через /start")
        return ConversationHandler.END

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.message.reply_text("Проверяю cookie...")

    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    r = s.get("https://users.roblox.com/v1/users/authenticated")

    if r.status_code != 200:
        await msg.edit_text("Неверный cookie - попытка возвращена!\n\n/start чтобы попробовать снова")
        return ConversationHandler.END

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
        await msg.edit_text("Техническая ошибка - попытка возвращена!\n\n/start чтобы попробовать снова")
    elif result == "NOT_CLICKED":
        user_attempts[user_id] += 1
        await msg.edit_text("Кнопка не найдена - попытка возвращена!\n\n/start чтобы попробовать снова")
    elif result and "withpersona.com" in result:
        await msg.edit_text("*Ссылка получена!*\n\nИспользуй сразу - одноразовая!", parse_mode="Markdown")
        await update.message.reply_text(result)
    else:
        user_attempts[user_id] += 1
        await msg.edit_text("Не удалось получить ссылку - попытка возвращена!\n\n/start чтобы попробовать снова")

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
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cookie),
            ],
            WAITING_BUY: [CallbackQueryHandler(handle_callback)],
            WAITING