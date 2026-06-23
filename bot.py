import os
import csv
import io
import re
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["SEARCH_BOT_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME     = os.environ.get("SHEET_NAME", "Заявки")

SHEET_GID = os.environ.get("SHEET_GID", "0")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbw2J0POpkKjE4SDfEyvmvBlY_ThuoBFbs6A27CsNeVv0wplN4khBzc7DNvurDmBJbOjrQ/exec")
APPS_SCRIPT_KEY = os.environ.get("APPS_SCRIPT_KEY", "findbizz2026")
CSV_URL = os.environ.get(
    "CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={SHEET_GID}"
)

ai = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

SEARCH_PROMPT = """Ты — помощник по подбору компаний из базы данных.

Найди ВСЕ подходящие компании по запросу и верни ТОЛЬКО их номера через запятую.

ПРАВИЛА:
- Ищи по смыслу: "строймат"=стройматериалы/СМР, "электро"=электрооборудование, "удо"=удобрения, "запчасти/зч"=запчасти
- Если указан банк — только с этим банком
- Если указана сумма — проверяй диапазон
- Если указан % кэша — фильтруй ("до 16%"=ставка≤16)
- Если указан НДС — фильтруй по ставке
- Если указан цвет ЗСК компании — фильтруй по полю "ЗСК"
- Если указано "принимает от зелёных/жёлтых" — фильтруй по полю "Принимает"
- Если запрос общий без фильтров — показывай все подходящие по назначению

ФОРМАТ — ТОЛЬКО цифры через запятую: 3, 7, 12
Если ничего не найдено — верни: 0"""

sessions: dict = {}

async def fetch_rows() -> list[dict]:
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            if APPS_SCRIPT_URL:
                # Используем Apps Script как прокси
                url = f"{APPS_SCRIPT_URL}?key={APPS_SCRIPT_KEY}"
                r = await client.get(url, timeout=30)
                logger.info(f"Apps Script response: {r.status_code}")
                if r.status_code == 200 and r.text.strip() != "Unauthorized":
                    import json as _json
                    rows = _json.loads(r.text)
                    rows = [row for row in rows if row.get("Название компании", "").strip()]
                    logger.info(f"Loaded {len(rows)} rows via Apps Script")
                    return rows
            # Fallback to CSV
            r = await client.get(CSV_URL, timeout=20)
            if r.status_code != 200:
                return []
            reader = csv.DictReader(io.StringIO(r.text))
            rows = [dict(row) for row in reader if row.get("Название компании", "").strip()]
            logger.info(f"Loaded {len(rows)} rows via CSV")
            return rows
    except Exception as e:
        logger.error(f"Fetch error: {e}")
        return []

def format_rows_compact(rows: list[dict]) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        company = row.get("Название компании", "").strip()
        purpose = row.get("Назначение платежа", "").strip()
        bank    = row.get("Банк", "").strip()
        amount  = row.get("Сумма ОТ и ДО", "").strip()
        vat     = row.get("Ставка НДС", "").strip()
        cash    = row.get("Ставка по кэшу", "").strip()
        zsk     = row.get("Цвет ЗСК", "").strip()
        accepts = row.get("Принимает от", "").strip()
        issue   = row.get("Дата выдачи/срок", "").strip()
        lines.append(f"{i}. {company} | {purpose} | {bank} | {amount} | НДС:{vat} | Кэш:{cash} | ЗСК:{zsk} | Принимает:{accepts} | Срок:{issue}")
    return "\n".join(lines)

def search_with_claude(query: str, rows: list[dict]) -> list[int]:
    rows_text = format_rows_compact(rows)
    response = ai.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=SEARCH_PROMPT,
        messages=[{"role": "user", "content": f"Запрос: {query}\n\nКомпании:\n{rows_text}"}],
    )
    raw = response.content[0].text.strip()
    logger.info(f"Claude: {raw[:200]}")
    if raw == "0" or not raw:
        return []
    indices = [int(n) for n in re.findall(r'\d+', raw)]
    return [i for i in indices if 1 <= i <= len(rows)]

def apply_markup(cash_str: str, markup: float) -> str:
    nums = re.findall(r'\d+(?:[.,]\d+)?', cash_str)
    if len(nums) == 1:
        r = float(nums[0].replace(',', '.'))
        return f"{r + markup:.1f}%"
    elif len(nums) >= 2:
        r1 = float(nums[0].replace(',', '.'))
        r2 = float(nums[1].replace(',', '.'))
        return f"{r1 + markup:.1f}-{r2 + markup:.1f}%"
    return cash_str

def make_breakdown(cash_str: str, markup: float, source: str = "") -> str:
    nums = re.findall(r'\d+(?:[.,]\d+)?', cash_str)
    lines = []
    if len(nums) == 1:
        cost = float(nums[0].replace(',', '.'))
        lines = [
            "💡 *Расшифровка ставки:*",
            f"Себестоимость: {cost:.1f}%",
            f"Наценка: +{markup:.0f}%",
            f"Итого клиенту: *{cost + markup:.1f}%*",
        ]
    elif len(nums) >= 2:
        c1 = float(nums[0].replace(',', '.'))
        c2 = float(nums[1].replace(',', '.'))
        lines = [
            "💡 *Расшифровка ставки:*",
            f"Себестоимость: {c1:.1f}-{c2:.1f}%",
            f"Наценка: +{markup:.0f}%",
            f"Итого клиенту: *{c1+markup:.1f}-{c2+markup:.1f}%*",
        ]
    if source:
        lines.append(f"📢 Источник: {source}")
    return "\n".join(lines)

def format_company_card(row: dict, index: int, markup: float = 0) -> str:
    company      = row.get("Название компании", "—")
    inn          = row.get("ИНН", "")
    website      = row.get("Сайт", "")
    zsk_color    = row.get("Цвет ЗСК", "")
    accepts_from = row.get("Принимает от", "")
    purpose      = row.get("Назначение платежа", "")
    amount       = row.get("Сумма ОТ и ДО", "")
    bank         = row.get("Банк", "")
    vat          = row.get("Ставка НДС", "")
    cash_raw     = row.get("Ставка по кэшу", "").strip()
    issue        = row.get("Дата выдачи/срок", "")
    comment      = row.get("Комментарии", "")

    cash_display = apply_markup(cash_raw, markup) if markup > 0 and cash_raw else cash_raw

    lines = [f"*#{index} {company}*\n"]
    if inn:          lines.append(f"🔢 ИНН: {inn}")
    if zsk_color:    lines.append(f"🎨 Цвет ЗСК: {zsk_color}")
    if accepts_from: lines.append(f"✅ Принимает от: {accepts_from}")
    if purpose:      lines.append(f"💳 Назначение: {purpose}")
    if amount:       lines.append(f"💰 Сумма: {amount}")
    if bank:         lines.append(f"🏦 Банк: {bank}")
    if vat:          lines.append(f"📊 НДС: {vat}")
    if cash_display: lines.append(f"📈 Кэш: {cash_display}")
    if issue:        lines.append(f"📅 Срок: {issue}")
    if comment:      lines.append(f"💬 Комментарий: {comment}")
    if website:      lines.append(f"🌐 Сайт: {website}")
    return "\n".join(lines)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Найду компании из базы по запросу.\n\n"
        "Примеры:\n"
        "• _строймат до 16%_\n"
        "• _удобрения, Альфа банк_\n"
        "• _СМР 5 млн, НДС 22%_\n"
        "• _принимают от жёлтых_",
        parse_mode="Markdown"
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = (update.message.text or "").strip()
    if not query:
        return

    searching_msg = await update.message.reply_text("🔍 Ищу подходящие компании...")
    rows = await fetch_rows()
    if not rows:
        await searching_msg.edit_text("⚠️ Не удалось получить данные из базы.")
        return

    try:
        indices = search_with_claude(query, rows)
    except Exception as e:
        logger.error(f"Search error: {e}")
        await searching_msg.edit_text("⚠️ Ошибка поиска. Попробуй ещё раз.")
        return

    await searching_msg.delete()

    if not indices:
        await update.message.reply_text("❌ По запросу компаний не найдено.")
        return

    found_rows = [rows[i-1] for i in indices]
    sessions[user_id] = {"rows": found_rows}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0%", callback_data="markup_0"),
            InlineKeyboardButton("+1%", callback_data="markup_1"),
            InlineKeyboardButton("+2%", callback_data="markup_2"),
        ],
        [
            InlineKeyboardButton("+3%", callback_data="markup_3"),
            InlineKeyboardButton("+4%", callback_data="markup_4"),
            InlineKeyboardButton("+5%", callback_data="markup_5"),
        ],
    ])
    await update.message.reply_text(
        f"✅ Найдено компаний: *{len(found_rows)}*\n\n💰 Какую наценку добавить к ставке кэша?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def handle_markup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("⚠️ Сессия устарела.")
        return

    markup = float(query.data.replace("markup_", ""))
    found_rows = session["rows"]
    await query.message.delete()

    for i, row in enumerate(found_rows, 1):
        card = format_company_card(row, i, markup)
        try:
            await query.message.reply_text(card, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Send error: {e}")
            await query.message.reply_text(card)

        cash_raw = row.get("Ставка по кэшу", "").strip()
        source = row.get("Источник (группа)", "").strip()
        if markup > 0 and cash_raw:
            breakdown = make_breakdown(cash_raw, markup, source)
        elif source:
            breakdown = f"📢 Источник: {source}"
        else:
            breakdown = ""
        if breakdown:
            try:
                await query.message.reply_text(breakdown, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Breakdown error: {e}")

    await query.message.reply_text(
        f"📋 Всего: *{len(found_rows)}* компаний",
        parse_mode="Markdown"
    )
    del sessions[user_id]

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(handle_markup, pattern="^markup_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    logger.info("Поисковый бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
