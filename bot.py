import requests
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, filters, ContextTypes, ConversationHandler)

logging.basicConfig(level=logging.INFO)
import os
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WAITING_COOKIE, WAITING_CHOICE = range(2)

# Увеличиваем до 20 потоков
executor = ThreadPoolExecutor(max_workers=20)

INJECT_SCRIPT = """
(function() {
    window.__capturedData = null;
    const TARGET = 'age-verification-service/v1/persona-id-verification/start-verification';

    function extractLinks(text) {
        const links = [];
        try {
            const data = JSON.parse(text);
            const find = (obj, path) => {
                if (!obj || typeof obj !== 'object') return;
                for (const [k, v] of Object.entries(obj)) {
                    const cur = path ? path+'.'+k : k;
                    if (typeof v === 'string') {
                        if (k.toLowerCase().includes('url') ||
                            k.toLowerCase().includes('link') ||
                            k.toLowerCase().includes('inquiry') ||
                            v.startsWith('http')) {
                            links.push({ path: cur, url: v });
                        }
                    } else if (typeof v === 'object') find(v, cur);
                }
            };
            find(data, '');
        } catch(e) {
            const m = text.match(/https?:\\/\\/[^\\s"'<>]+/g);
            if (m) m.forEach((u,i) => links.push({ path:'match_'+i, url:u }));
        }
        return links;
    }

    const _fetch = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0]==='string' ? args[0] :
                    args[0] instanceof Request ? args[0].url : String(args[0]);
        const method = (args[1]?.method ||
                       (args[0] instanceof Request ? args[0].method : 'GET')).toUpperCase();
        const res = await _fetch.apply(this, args);
        if (method === 'POST' && url.includes(TARGET)) {
            try {
                const body = await res.clone().text();
                window.__capturedData = { url, body, links: extractLinks(body) };
            } catch(e) {}
        }
        return res;
    };

    const _open = XMLHttpRequest.prototype.open;
    const _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(m,u) {
        this._m=m; this._u=u; return _open.apply(this,arguments);
    };
    XMLHttpRequest.prototype.send = function(b) {
        if (this._m?.toUpperCase()==='POST' && (this._u||'').includes(TARGET)) {
            this.addEventListener('loadend', function() {
                if (this.status >= 200 && this.status < 300) {
                    try {
                        window.__capturedData = {
                            url: this._u,
                            body: this.responseText,
                            links: extractLinks(this.responseText)
                        };
                    } catch(e) {}
                }
            }, {once:true});
        }
        return _send.apply(this,arguments);
    };
})();
"""

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
        url = f"https://inquiry.withpersona.com/verify?inquiry-id={inq.group(0)}"
        if tok:
            url += f"&session-token={tok.group(1)}"
        return url
    return None

def playwright_get_url(cookie: str, method: str):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            context.add_cookies([{
                "name": ".ROBLOSECURITY",
                "value": cookie,
                "domain": ".roblox.com",
                "path": "/"
            }])

            page = context.new_page()
            page.add_init_script(INJECT_SCRIPT)

            page.goto(
                "https://www.roblox.com/my/account#!/info",
                wait_until="networkidle",
                timeout=30000
            )
            page.wait_for_timeout(3000)

            target_text = "Continue with camera" if method == "camera" else "Continue with ID"

            clicked = False
            for attempt in range(3):
                try:
                    clicked = page.evaluate(f"""
                        () => {{
                            const els = document.querySelectorAll('button, a, div, span');
                            for (let el of els) {{
                                if (el.textContent.trim() === '{target_text}') {{
                                    el.click(); return true;
                                }}
                            }}
                            for (let el of els) {{
                                if (el.textContent.trim().toLowerCase().includes('{target_text.lower()}')) {{
                                    el.click(); return true;
                                }}
                            }}
                            return false;
                        }}
                    """)
                except:
                    clicked = False

                if clicked:
                    logging.info(f"✅ Клик #{attempt+1}")
                    break
                page.wait_for_timeout(2000)

            if not clicked:
                browser.close()
                return "NOT_CLICKED"

            result_url = None
            for i in range(25):
                page.wait_for_timeout(1000)
                try:
                    raw = page.evaluate("() => window.__capturedData")
                    if raw:
                        result_url = build_url(raw)
                        if result_url:
                            logging.info(f"✅ Ссылка за {i+1} сек: {result_url}")
                            break
                except:
                    pass
                logging.info(f"Жду... {i+1}/25")

            browser.close()
            return result_url

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        return f"ERROR: {e}"

import asyncio

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Лицо (Camera)", callback_data="choose_camera")],
        [InlineKeyboardButton("🪪 Паспорт (ID)", callback_data="choose_id")],
    ])
    await update.message.reply_text(
        "👋 *Roblox Age Verification Bot*\n\n"
        "Сервис для получения ссылки by @dedbed12\n\n"
        "🔢 Ваши попытки: *999*\n\n"
        "Выберите действие ниже:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return WAITING_COOKIE

async def choose_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = "camera" if query.data == "choose_camera" else "id"
    method_name = "📷 Лицо (Camera)" if method == "camera" else "🪪 Паспорт (ID)"
    context.user_data["method"] = method
    await query.edit_message_text(
        f"✅ Выбран метод: *{method_name}*\n\n"
        f"Теперь отправь свой `.ROBLOSECURITY` cookie\n\n"
        f"⚠️ Сообщение с cookie будет удалено автоматически",
        parse_mode="Markdown"
    )
    return WAITING_COOKIE

async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie = update.message.text.strip()
    method = context.user_data.get("method")

    if not method:
        await update.message.reply_text("❌ Сначала выбери метод! /start")
        return ConversationHandler.END

    try:
        await update.message.delete()
    except:
        pass

    msg = await update.message.reply_text("🔄 Проверяю cookie...")

    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    r = s.get("https://users.roblox.com/v1/users/authenticated")

    if r.status_code != 200:
        await msg.edit_text("❌ Неверный cookie!\n\n/start чтобы попробовать снова")
        return ConversationHandler.END

    user = r.json()
    method_name = "📷 Camera" if method == "camera" else "🪪 ID"

    await msg.edit_text(
        f"✅ Аккаунт: *{user['name']}*\n"
        f"🔍 Метод: *{method_name}*\n\n"
        f"⏳ Получаю ссылку, жди 20-30 сек...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        executor, playwright_get_url, cookie, method
    )

    if isinstance(result, str) and result.startswith("ERROR"):
        await msg.edit_text(
            "❌ Ошибка браузера\n\n/start чтобы попробовать снова"
        )
    elif result == "NOT_CLICKED":
        await msg.edit_text(
            "❌ Кнопка не найдена\n\n/start чтобы попробовать снова"
        )
    elif result and "withpersona.com" in result:
        await msg.edit_text(
            "✅ *Ссылка получена!*\n\n"
            "⚠️ Используй сразу — одноразовая!",
            parse_mode="Markdown"
        )
        await update.message.reply_text(result)
    else:
        await msg.edit_text(
            "❌ Не удалось получить ссылку\n\n"
            "/start чтобы попробовать снова"
        )

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. /start чтобы начать")
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_COOKIE: [
                CallbackQueryHandler(choose_method, pattern="^choose_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cookie),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
        block=False,
    )
    app.add_handler(conv)
    print("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()