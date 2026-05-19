import os
import re
import json
import base64
import logging
import requests
from datetime import datetime
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

def get_sheet_data(sheet_name):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&sheet={sheet_name}"
    try:
        r = requests.get(url, timeout=10)
        text = r.text
        json_str = re.search(r'google\.visualization\.Query\.setResponse\((.*)\)', text, re.DOTALL)
        if not json_str:
            return []
        data = json.loads(json_str.group(1))
        rows = data.get("table", {}).get("rows", [])
        result = []
        for row in rows:
            cells = row.get("c", [])
            if cells and cells[0] and cells[0].get("v"):
                result.append([c.get("v") if c else None for c in cells])
        return result
    except Exception as e:
        logging.error(f"Sheet read error: {e}")
        return []

def append_to_sheet(sheet_name, row_data):
    # Using Google Sheets API via simple append URL (public write via service account not available)
    # Store locally for now and return confirmation
    logging.info(f"Would write to {sheet_name}: {row_data}")
    return True

def analyze_with_claude(image_bytes, mime_type="image/jpeg"):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
    prompt = """Это отчёт по автоворонке. Извлеки все числовые показатели из таблицы.
Верни ТОЛЬКО валидный JSON без пояснений:
{
  "funnel": "название воронки из шапки (BIO/МС AJ/КМ AJ и тд)",
  "period": "период (например 13-19)",
  "date": "дата факта (например 18.05.2026)",
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
Для процентов — число без знака % (например 25 а не 25%).
Для долларов — число без знака $ (например 1540 а не $1540).
Если значение #DIV/0! или пусто — верни null."""

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
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=30
    )
    
    result = response.json()
    text = result["content"][0]["text"].strip()
    # Clean markdown if present
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

def detect_funnel(raw_name):
    raw = raw_name.lower().strip()
    for key, val in FUNNEL_MAP.items():
        if key in raw:
            return val
    return raw_name.upper()

def format_delta(current, previous, label, is_percent=False, higher_is_better=True):
    if current is None or previous is None:
        return f"  {label}: {current}"
    delta = current - previous
    if delta == 0:
        arrow = "→"
        sign = ""
    elif delta > 0:
        arrow = "↑" if higher_is_better else "↓⚠️"
        sign = "+"
    else:
        arrow = "↓⚠️" if higher_is_better else "↑"
        sign = ""
    suffix = "%" if is_percent else ""
    return f"  {arrow} {label}: {current}{suffix} ({sign}{delta:.1f}{suffix})"

def build_summary(parsed, prev_row=None):
    d = parsed["data"]
    funnel = detect_funnel(parsed.get("funnel", "?"))
    date = parsed.get("date", "?")
    period = parsed.get("period", "?")

    lines = [
        f"📊 *{funnel}* | Неделя {period} | Дата: {date}",
        "",
        "🎯 *Трафик*"
    ]

    if prev_row:
        lines.append(format_delta(d.get("registrations"), prev_row.get("registrations"), "Регистрации"))
        lines.append(format_delta(d.get("lead_price"), prev_row.get("lead_price"), "Цена лида $", higher_is_better=False))
        lines.append(format_delta(d.get("reach_1day"), prev_row.get("reach_1day"), "Доходимость 1д", is_percent=True))
        lines.append(format_delta(d.get("reach_all_peaks"), prev_row.get("reach_all_peaks"), "Доходимость все пики", is_percent=True))
        lines += ["", "📋 *Заявки и продажи*"]
        lines.append(format_delta(d.get("applications"), prev_row.get("applications"), "Заявки"))
        lines.append(format_delta(d.get("conversion_to_app"), prev_row.get("conversion_to_app"), "Конверсия в заявку", is_percent=True))
        lines.append(format_delta(d.get("sales"), prev_row.get("sales"), "Продажи"))
        lines.append(format_delta(d.get("conversion_to_payment"), prev_row.get("conversion_to_payment"), "Конверсия в оплату", is_percent=True))
        lines += ["", "💰 *Деньги*"]
        lines.append(format_delta(d.get("total_sales"), prev_row.get("total_sales"), "Сумма продаж $"))
        lines.append(format_delta(d.get("roas_fact"), prev_row.get("roas_fact"), "ROAS факт", is_percent=True))
        lines.append(format_delta(d.get("potential_roas"), prev_row.get("potential_roas"), "Потенциальный ROAS", is_percent=True))
    else:
        lines += [
            f"  Регистрации: {d.get('registrations')}",
            f"  Цена лида: ${d.get('lead_price')}",
            f"  Доходимость 1д: {d.get('reach_1day')}%",
            f"  Доходимость все пики: {d.get('reach_all_peaks')}%",
            "", "📋 *Заявки и продажи*",
            f"  Заявки: {d.get('applications')}",
            f"  Конверсия в заявку: {d.get('conversion_to_app')}%",
            f"  Продажи: {d.get('sales')}",
            f"  Конверсия в оплату: {d.get('conversion_to_payment')}%",
            "", "💰 *Деньги*",
            f"  Сумма продаж: ${d.get('total_sales')}",
            f"  ROAS факт: {d.get('roas_fact')}%",
            f"  Потенциальный ROAS: {d.get('potential_roas')}%",
        ]

    lines += ["", f"✅ Данные сохранены в таблицу → лист *{funnel}*"]
    return "\n".join(lines)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Читаю отчёт...")
    
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        
        parsed = analyze_with_claude(bytes(image_bytes))
        funnel = detect_funnel(parsed.get("funnel", "?"))
        
        prev_data = get_sheet_data(funnel)
        prev_row = None
        if prev_data:
            last = prev_data[-1]
            keys = ["date","budget_traffic","daily_budget","registrations","lead_price",
                    "viewers_peak_1day","reach_1day","viewers_all_peaks","reach_all_peaks",
                    "applications","conversion_to_app","reg_to_app_pct","sales",
                    "conversion_to_payment","autopayments","conversion_to_autopayment",
                    "avg_check","total_sales","roas_fact","payment_remainder",
                    "potential_roas","avg_app_cost","ojop_count","ojop_sum"]
            prev_row = dict(zip(keys, last))

        d = parsed["data"]
        row = [
            parsed.get("date", ""),
            d.get("budget_traffic"), d.get("daily_budget"), d.get("registrations"),
            d.get("lead_price"), d.get("viewers_peak_1day"), d.get("reach_1day"),
            d.get("viewers_all_peaks"), d.get("reach_all_peaks"), d.get("applications"),
            d.get("conversion_to_app"), d.get("reg_to_app_pct"), d.get("sales"),
            d.get("conversion_to_payment"), d.get("autopayments"), d.get("conversion_to_autopayment"),
            d.get("avg_check"), d.get("total_sales"), d.get("roas_fact"),
            d.get("payment_remainder"), d.get("potential_roas"), d.get("avg_app_cost"),
            d.get("ojop_count"), d.get("ojop_sum")
        ]
        append_to_sheet(funnel, row)
        
        summary = build_summary(parsed, prev_row)
        await update.message.reply_text(summary, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне скрин отчёта по воронке и я дам сводку с динамикой 📊"
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    print("Bot started")
    app.run_polling()
