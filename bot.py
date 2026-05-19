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
    url = f"https://script.google.com/macros/s/{os.environ.get('APPS_SCRIPT_ID', '')}/exec"
    if not os.environ.get('APPS_SCRIPT_ID'):
        logging.info(f"No APPS_SCRIPT_ID, skipping sheet write")
        return False
    try:
        r = requests.post(url, json={"sheet": sheet_name, "row": row_data}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logging.error(f"Sheet write error: {e}")
        return False

def get_last_row(sheet_name):
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

    prompt = """Это еженедельный отчёт по автоворонке. В таблице две колонки цифр: левая — ПЛАН (недельный), правая — ФАКТ (накопленный на дату).

Извлеки данные и верни ТОЛЬКО валидный JSON без markdown и пояснений:
{
  "funnel": "название воронки из зелёной шапки",
  "period": "период (например 13-19)",
  "date": "дата факта из шапки правой колонки",
  "plan": {
    "budget_traffic": число или null,
    "daily_budget": число или null,
    "registrations": число или null,
    "lead_price": число или null,
    "reach_1day": число или null,
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
    "potential_roas": число или null,
    "avg_app_cost": число или null
  },
  "fact": {
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
  },
  "analysis": "Аналитический вывод на русском (3-5 предложений). Сравни факт с планом по % показателям (доходимость, конверсии, ROAS) — выполняем план или нет. Отметь что идёт хорошо, где просадки. Стиль: лаконично, по делу, как опытный маркетолог. НЕ сравнивай бюджет трафик и кол-во регистраций (они накопительные)."
}

Правила:
- Проценты: число без знака % (55 а не 55%)
- Доллары: число без знака $ (1540 а не $1540)
- #DIV/0!, пусто, прочерк → null
- viewers_all_peaks — строка "Кол-во зрителей по всем пиками" или "Кол-во зрителей по всем пикам" — правая колонка факта
- viewers_peak_1day — строка "Кол-во зрителей пик по 1 дню" или "Кол-во зрителей пик по 1 дню" — правая колонка факта"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 2000,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=40
    )

    result = response.json()
    text = result["content"][0]["text"].strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)

def pct_status(fact_val, plan_val, higher_is_better=True):
    if fact_val is None or plan_val is None or plan_val == 0:
        return "—", None
    pct = (fact_val / plan_val) * 100
    if higher_is_better:
        if pct >= 100: emoji = "✅"
        elif pct >= 80: emoji = "🟡"
        else: emoji = "🔴"
    else:
        if pct <= 100: emoji = "✅"
        elif pct <= 120: emoji = "🟡"
        else: emoji = "🔴"
    return emoji, pct

def fmt_with_plan(fact_val, plan_val, suffix="", higher_is_better=True, prev_val=None):
    if fact_val is None:
        return "—"
    result = f"{fact_val}{suffix}"
    if plan_val is not None and plan_val != 0:
        emoji, pct = pct_status(fact_val, plan_val, higher_is_better)
        if pct is not None:
            diff = fact_val - plan_val
            sign = "+" if diff > 0 else ""
            result += f" {emoji} (план {plan_val}{suffix}, {sign}{diff:.1f}{suffix})"
    if prev_val is not None:
        delta = fact_val - prev_val
        if delta != 0:
            sign = "+" if delta > 0 else ""
            arr = "↑" if delta > 0 else "↓"
            result += f" {arr}{sign}{delta:.1f}{suffix} к вчера"
    return result

def build_summary(parsed, prev=None):
    f = parsed.get("fact", {})
    p = parsed.get("plan", {})
    funnel = detect_funnel(parsed.get("funnel", "?"))
    date = parsed.get("date", "?")
    period = parsed.get("period", "?")
    analysis = parsed.get("analysis", "")
    prev = prev or {}

    def val(key, default="—"):
        v = f.get(key)
        return v if v is not None else default

    lines = [
        f"📊 *{funnel}* | Неделя {period} | {date}",
        "",
        "💸 *Бюджет*",
        f"  Бюджет трафик: ${val('budget_traffic')} (план ${p.get('budget_traffic', '—')})",
        f"  Ежедневный бюджет: {fmt_with_plan(f.get('daily_budget'), p.get('daily_budget'), '$', higher_is_better=False)}",
        "",
        "🎯 *Трафик*",
        f"  Регистрации: {val('registrations')} (план {p.get('registrations', '—')})",
        f"  Цена лида: {fmt_with_plan(f.get('lead_price'), p.get('lead_price'), '$', higher_is_better=False, prev_val=prev.get('lead_price'))}",
        f"  Зрители пик 1д: {val('viewers_peak_1day')}",
        f"  Доходимость 1д: {fmt_with_plan(f.get('reach_1day'), p.get('reach_1day'), '%', prev_val=prev.get('reach_1day'))}",
        f"  Зрители все пики: {val('viewers_all_peaks')}",
        f"  Доходимость все пики: {fmt_with_plan(f.get('reach_all_peaks'), p.get('reach_all_peaks'), '%', prev_val=prev.get('reach_all_peaks'))}",
        "",
        "📋 *Заявки и продажи*",
        f"  Заявки: {val('applications')} (план {p.get('applications', '—')})",
        f"  Конверсия в заявку: {fmt_with_plan(f.get('conversion_to_app'), p.get('conversion_to_app'), '%', prev_val=prev.get('conversion_to_app'))}",
        f"  % рег в заявку: {fmt_with_plan(f.get('reg_to_app_pct'), p.get('reg_to_app_pct'), '%', prev_val=prev.get('reg_to_app_pct'))}",
        f"  Продажи: {val('sales')} (план {p.get('sales', '—')})",
        f"  Конверсия в оплату: {fmt_with_plan(f.get('conversion_to_payment'), p.get('conversion_to_payment'), '%', prev_val=prev.get('conversion_to_payment'))}",
        f"  Автооплаты: {val('autopayments')} (план {p.get('autopayments', '—')})",
        f"  Конверсия в автооплату: {fmt_with_plan(f.get('conversion_to_autopayment'), p.get('conversion_to_autopayment'), '%', prev_val=prev.get('conversion_to_autopayment'))}",
        "",
        "💰 *Деньги*",
        f"  Средний чек: ${val('avg_check')} (план ${p.get('avg_check', '—')})",
        f"  Сумма продаж: ${val('total_sales')} (план ${p.get('total_sales', '—')})",
        f"  ROAS факт: {fmt_with_plan(f.get('roas_fact'), p.get('roas_fact'), '%', prev_val=prev.get('roas_fact'))}",
        f"  Потенциальный ROAS: {fmt_with_plan(f.get('potential_roas'), p.get('potential_roas'), '%', prev_val=prev.get('potential_roas'))}",
        f"  Ср. стоимость заявки: ${fmt_with_plan(f.get('avg_app_cost'), p.get('avg_app_cost'), higher_is_better=False, prev_val=prev.get('avg_app_cost'))}",
    ]

    if f.get('ojop_count') or f.get('ojop_sum'):
        lines += [
            "",
            "🔄 *ОЖОП*",
            f"  Кол-во: {val('ojop_count')}",
            f"  Сумма: ${val('ojop_sum')}",
        ]

    if analysis:
        lines += ["", "📍 *Вывод*", analysis]

    lines += ["", "✅ Сохранено → лист *" + funnel + "*"]
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

        f = parsed.get("fact", {})
        row = [parsed.get("date", "")] + [f.get(k) for k in SHEET_COLUMNS[1:]]
        append_to_sheet(funnel, row)

        summary = build_summary(parsed, prev)
        await update.message.reply_text(summary, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:300]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь скрин отчёта по воронке (КМ, МС или ЦЛ) — дам сводку с анализом план/факт 📊"
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    print("Bot started")
    app.run_polling()
