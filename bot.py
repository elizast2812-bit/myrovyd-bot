import os
import re
import json
import base64
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]

FUNNEL_MAP = {
    "bio": "ЦЛ", "цл": "ЦЛ", "целитель": "ЦЛ",
    "мс": "МС", "mc": "МС", "магия": "МС",
    "км": "КМ", "km": "КМ", "кризис": "КМ"
}

SHEET_COLUMNS = [
    "date", "budget_traffic", "daily_budget", "registrations",
    "lead_price", "viewers_peak_1day", "reach_1day",
    "viewers_all_peaks", "reach_all_peaks", "applications",
    "conversion_to_app", "reg_to_app_pct", "sales",
    "conversion_to_payment", "autopayments", "conversion_to_autopayment",
    "avg_check", "total_sales", "roas_fact", "payment_remainder",
    "potential_roas", "avg_app_cost", "ojop_count", "ojop_sum"
]

def detect_funnel(raw_name):
    raw = raw_name.lower().strip()
    for key, val in FUNNEL_MAP.items():
        if key in raw:
            return val
    return raw_name.upper()

def append_to_sheet(sheet_name, row_data):
    """Append row to Google Sheets via simple HTTP (no auth needed for append via webhook)"""
    url = f"https://script.google.com/macros/s/{os.environ.get('APPS_SCRIPT_ID', '')}/exec"
    if not os.environ.get('APPS_SCRIPT_ID'):
        logging.info(f"No APPS_SCRIPT_ID set, skipping sheet write. Data: {row_data}")
        return False
    try:
        r = requests.post(url, json={"sheet": sheet_name, "row": row_data}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Sheet write error: {e}")
        return False

def get_last_row(sheet_name):
    """Get last row from sheet for delta calculation"""
    url = f"https://script.google.com/macros/s/{os.environ.get('APPS_SCRIPT_ID', '')}/exec?sheet={sheet_name}&action=last"
    if not os.environ.get('APPS_SCRIPT_ID'):
        return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                return dict(zip(SHEET_COLUMNS, data))
    except Exception as e:
        logging.error(f"Sheet read error: {e}")
    return None

def analyze_with_claude(image_bytes):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """Это отчёт по автоворонке. В таблице две колонки с цифрами: левая — ПЛАН (базовый), правая — ФАКТ (актуальный).
Извлеки показатели ТОЛЬКО из правой колонки ФАКТ.
Верни ТОЛЬКО валидный JSON без пояснений, markdown и форматирования:
{
  "funnel": "название воронки из зелёной шапки таблицы",
  "period": "период недели (например 13-19)",
  "date": "дата из правой колонки шапки (например 18.05.2026)",
  "data": {
    "budget_traffic": число или null,
    "daily_budget": число или null,
    "registrations": число или null,
    "lead_price": число или null,
    "viewers_peak_1day": число или null,
    "reach_1day": число или null,
    "viewers_all_peaks": число или null,
    "reach_all_peaks": число или null,
    "applications": число или null,
    "conversion_to_app": число или null,
    "reg_to_app_pct": число или null,
    "sales": число или null,
    "conversion_to_payment": число или null,
    "autopayments": число или null,
    "conversion_to_autopayment": число или null,
    "avg_check": число или null,
    "total_sales": число или null,
    "roas_fact": число или null,
    "payment_remainder": число или null,
    "potential_roas": число или null,
    "avg_app_cost": число или null,
    "ojop_count": число или null,
    "ojop_sum": число или null
  }
}
Правила:
- Проценты: число без знака % (55 а не 55%)
- Доллары: число без знака $ (1540 а не $1540)
- #DIV/0!, пусто, прочерк → null
- Берём ТОЛЬКО правую колонку факта, не план"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=30
    )

    result = response.json()
    text = result["content"][0]["text"].strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

def fmt(val, suffix="", higher_is_better=True, prev=None):
    if val is None:
        return "—"
    if prev is None:
        return f"{val}{suffix}"
    delta = val - prev
    if delta == 0:
        arrow = "→"
    elif delta > 0:
        arrow = "↑" if higher_is_better else "↓⚠️"
    else:
        arrow = "↓⚠️" if higher_is_better else "↑"
    sign = "+" if delta > 0 else ""
    return f"{arrow} {val}{suffix} ({sign}{delta:.1f}{suffix})"

def build_summary(parsed, prev=None):
    d = parsed["data"]
    funnel = detect_funnel(parsed.get("funnel", "?"))
    date = parsed.get("date", "?")
    period = parsed.get("period", "?")

    p = prev or {}

    lines = [
        f"📊 *{funnel}* | Неделя {period} | Дата: {date}",
        "",
        "🎯 *Трафик*",
        f"  Регистрации: {fmt(d.get('registrations'), prev=p.get('registrations'))}",
        f"  Цена лида: ${fmt(d.get('lead_price'), higher_is_better=False, prev=p.get('lead_price'))}",
        f"  Доходимость 1д: {fmt(d.get('reach_1day'), '%', prev=p.get('reach_1day'))}",
        f"  Доходимость все пики: {fmt(d.get('reach_all_peaks'), '%', prev=p.get('reach_all_peaks'))}",
        "",
        "📋 *Заявки и продажи*",
        f"  Заявки: {fmt(d.get('applications'), prev=p.get('applications'))}",
        f"  Конверсия в заявку: {fmt(d.get('conversion_to_app'), '%', prev=p.get('conversion_to_app'))}",
        f"  % рег в заявку: {fmt(d.get('reg_to_app_pct'), '%', prev=p.get('reg_to_app_pct'))}",
        f"  Продажи: {fmt(d.get('sales'), prev=p.get('sales'))}",
        f"  Конверсия в оплату: {fmt(d.get('conversion_to_payment'), '%', prev=p.get('conversion_to_payment'))}",
        f"  Автооплаты: {fmt(d.get('autopayments'), prev=p.get('autopayments'))}",
        "",
        "💰 *Деньги*",
        f"  Средний чек: ${fmt(d.get('avg_check'), prev=p.get('avg_check'))}",
        f"  Сумма продаж: ${fmt(d.get('total_sales'), prev=p.get('total_sales'))}",
        f"  ROAS факт: {fmt(d.get('roas_fact'), '%', prev=p.get('roas_fact'))}",
        f"  Потенциальный ROAS: {fmt(d.get('potential_roas'), '%', prev=p.get('potential_roas'))}",
        f"  Ср. стоимость заявки: ${fmt(d.get('avg_app_cost'), higher_is_better=False, prev=p.get('avg_app_cost'))}",
    ]

    if d.get('ojop_count') or d.get('ojop_sum'):
        lines += [
            "",
            "🔄 *ОЖОП*",
            f"  Кол-во: {d.get('ojop_count', '—')}",
            f"  Сумма: ${d.get('ojop_sum', '—')}",
        ]

    lines += ["", f"✅ Данные сохранены → лист *{funnel}*"]
    return "\n".join(lines)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Читаю отчёт...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        parsed = analyze_with_claude(bytes(image_bytes))
        funnel = detect_funnel(parsed.get("funnel", "?"))

        prev = get_last_row(funnel)

        d = parsed["data"]
        row = [parsed.get("date", "")] + [d.get(k) for k in SHEET_COLUMNS[1:]]
        append_to_sheet(funnel, row)

        summary = build_summary(parsed, prev)
        await update.message.reply_text(summary, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка при обработке: {str(e)[:200]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне скрин отчёта по воронке (КМ, МС или ЦЛ) — дам сводку с динамикой 📊"
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    print("Bot started")
    app.run_polling()
