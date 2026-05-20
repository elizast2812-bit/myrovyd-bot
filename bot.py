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

SHEET_COLUMNS = [
    "date", "budget_traffic", "daily_budget", "registrations",
    "lead_price", "viewers_peak_1day", "reach_1day",
    "viewers_all_peaks", "reach_all_peaks", "applications",
    "conversion_to_app", "reg_to_app_pct", "sales",
    "conversion_to_payment", "autopayments", "conversion_to_autopayment",
    "avg_check", "total_sales", "roas_fact", "payment_remainder",
    "potential_roas", "avg_app_cost", "ojop_count", "ojop_sum"
]

FUNNEL_CONTEXT = """
КОНТЕКСТ ВОРОНКИ MYROVYD:
Механика: человек видит рекламу (баннер или видео) → переходит на лендинг → регистрируется → попадает в автоворонку → получает рассылку прогрева + напоминалки перед каждым вебинаром → участвует в 3-дневных вебинарах → оставляет заявку → покупает.

Ключевые рычаги по блокам:

ТРАФИК (цена лида, регистрации):
- Цена лида растёт → проблема в креативах (выгорел) или аудитории
- Решения: обновить крео, протестировать новые форматы, сменить аудиторию, проверить лендинг
- Бюджет выше плана → контролировать ежедневный расход

ДОХОДИМОСТЬ (зрители на вебинарах):
- Доходимость падает → проблема в цепочке прогрева (рассылки, напоминалки) или низкая вовлечённость
- Решения: проверить доставляемость писем, обновить тексты напоминалок, усилить оффер "зачем приходить"
- Идеи для теста: тетрадь с заданиями и практиками (повышает вовлечённость), дополнительный прогревающий урок

ЗАЯВКИ И ПРОДАЖИ:
- Мало заявок → слабый оффер на вебинаре, аудитория не прогрета
- Низкая конверсия в оплату → слабый скрипт, нет дожима, долго обрабатывают заявки
- Решения: усилить оффер на 3-м дне, добавить бонусы за быструю оплату, серия дожимающих сообщений

ДЕНЬГИ:
- ROAS ниже 100% → не окупаем трафик
- Потенциальный ROAS выше факта → есть незакрытые заявки, нужен дожим
"""

def detect_funnel(raw_name):
    raw = raw_name.lower().strip()
    for key, val in FUNNEL_MAP.items():
        if key in raw:
            return val
    return raw_name.upper()

def is_tuesday(date_str):
    """Check if date string (dd.mm.yyyy) is a Tuesday"""
    try:
        dt = datetime.strptime(date_str, "%d.%m.%Y")
        return dt.weekday() == 1  # 1 = Tuesday
    except:
        return False

def append_to_sheet(sheet_name, row_data):
    url = f"https://script.google.com/macros/s/{os.environ.get('APPS_SCRIPT_ID', '')}/exec"
    if not os.environ.get('APPS_SCRIPT_ID'):
        return False
    try:
        r = requests.post(url, json={"sheet": sheet_name, "row": row_data}, timeout=15)
        logging.info(f"Sheet write: {r.text}")
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
        if r.status_code == 200 and r.text not in ['[]', '']:
            data = r.json()
            if data and isinstance(data, list):
                return dict(zip(SHEET_COLUMNS, data))
    except Exception as e:
        logging.error(f"Sheet read error: {e}")
    return None

def get_weekly_compare(sheet_name):
    """Get current and previous week data for comparison"""
    url = f"https://script.google.com/macros/s/{os.environ.get('APPS_SCRIPT_ID', '')}/exec?sheet={sheet_name}&action=weekly_compare"
    if not os.environ.get('APPS_SCRIPT_ID'):
        return None, None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200 and r.text not in ['[]', '']:
            data = r.json()
            if isinstance(data, dict):
                current = dict(zip(SHEET_COLUMNS, data["current"])) if data.get("current") else None
                previous = dict(zip(SHEET_COLUMNS, data["previous"])) if data.get("previous") else None
                return current, previous
    except Exception as e:
        logging.error(f"Weekly compare error: {e}")
    return None, None

def analyze_with_claude(image_bytes):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = f"""Ты аналитик автоворонок. Тебе дан еженедельный отчёт.

{FUNNEL_CONTEXT}

В таблице ДВЕ колонки с цифрами: левая — ПЛАН, правая — ФАКТ. Извлекай ТОЛЬКО из правой колонки ФАКТ.

Названия строк → поля JSON:
- "Бюджет трафик" → budget_traffic
- "Ежедневный бюджет" → daily_budget
- "Кол-во регистраций" → registrations
- "Цена лида" → lead_price
- "Кол-во зрителей пик по 1 дню" → viewers_peak_1day
- "Доходимость (по 1му дню)" → reach_1day
- "Кол-во зрителей по всем пиками" или "Кол-во зрителей по всем пикам" → viewers_all_peaks
- "Доходимость (по всем пикам)" → reach_all_peaks
- "Кол-во заявок" → applications
- "Конверсия в заявку (пик всех дней)" или "Конверсия в заявку" → conversion_to_app
- "Процент от регистрации в заявку" → reg_to_app_pct
- "Кол-во продаж" → sales
- "Конверсия в оплату" → conversion_to_payment
- "Кол-во автооплат" → autopayments
- "Конверсия в автооплату" → conversion_to_autopayment
- "Средний чек ФАКТ ОПЛАТ" → avg_check
- "Сумма продаж (факт)" → total_sales
- "ROAS факт" → roas_fact
- "Остаток оплат" → payment_remainder
- "Потенциальный ROAS" → potential_roas
- "Средняя стоимость заявки" → avg_app_cost
- "Кол-во ОЖОП" → ojop_count
- "Сумма ОЖОП" → ojop_sum

Верни ТОЛЬКО валидный JSON без markdown:
{{
  "funnel": "название воронки из цветной шапки",
  "period": "период (например 13-19)",
  "date": "дата факта (например 19.05.2026)",
  "plan": {{
    "budget_traffic": число или null, "daily_budget": число или null,
    "registrations": число или null, "lead_price": число или null,
    "reach_1day": число или null, "reach_all_peaks": число или null,
    "applications": число или null, "conversion_to_app": число или null,
    "reg_to_app_pct": число или null, "sales": число или null,
    "conversion_to_payment": число или null, "autopayments": число или null,
    "conversion_to_autopayment": число или null, "avg_check": число или null,
    "total_sales": число или null, "roas_fact": число или null,
    "potential_roas": число или null, "avg_app_cost": число или null
  }},
  "fact": {{
    "budget_traffic": число или null, "daily_budget": число или null,
    "registrations": число или null, "lead_price": число или null,
    "viewers_peak_1day": число или null, "reach_1day": число или null,
    "viewers_all_peaks": число или null, "reach_all_peaks": число или null,
    "applications": число или null, "conversion_to_app": число или null,
    "reg_to_app_pct": число или null, "sales": число или null,
    "conversion_to_payment": число или null, "autopayments": число или null,
    "conversion_to_autopayment": число или null, "avg_check": число или null,
    "total_sales": число или null, "roas_fact": число или null,
    "payment_remainder": число или null, "potential_roas": число или null,
    "avg_app_cost": число или null, "ojop_count": число или null,
    "ojop_sum": число или null
  }},
  "analysis": {{
    "traffic": "2-3 предложения по трафику с конкретной гипотезой и действием.",
    "reach": "2-3 предложения по доходимости с гипотезой под механику воронки.",
    "sales": "2-3 предложения по заявкам и конверсиям с гипотезой.",
    "money": "1-2 предложения по ROAS и окупаемости.",
    "action_plan": "3-5 конкретных задач через точку с запятой. Формат: действие — причина."
  }}
}}

Правила: % без знака %, $ без знака $, #DIV/0! → null"""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 2500,
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
        try:
            delta = float(fact_val) - float(prev_val)
            if delta != 0:
                sign = "+" if delta > 0 else ""
                arr = "↑" if delta > 0 else "↓"
                result += f" {arr}{sign}{delta:.1f}{suffix} к вчера"
        except (TypeError, ValueError):
            pass
    return result

def fmt_delta(curr, prev, suffix="", higher_is_better=True):
    """Format week-over-week delta"""
    if curr is None or prev is None:
        return f"{curr or '—'}{suffix}"
    try:
        c, p = float(curr), float(prev)
        delta = c - p
        if delta == 0:
            arr = "→"
        elif delta > 0:
            arr = "📈" if higher_is_better else "📉"
        else:
            arr = "📉" if higher_is_better else "📈"
        sign = "+" if delta > 0 else ""
        return f"{arr} {c}{suffix} (б. {p}{suffix}, {sign}{delta:.1f}{suffix})"
    except (TypeError, ValueError):
        return f"{curr}{suffix}"

def v(f, key):
    val = f.get(key)
    return val if val is not None else "—"

def build_weekly_comparison(current, previous, funnel, period):
    """Build week-over-week comparison block"""
    if not previous:
        return ""

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📅 *Итог недели {period} vs прошлая неделя*",
        "",
        "🎯 *Трафик*",
        f"  Регистрации: {fmt_delta(current.get('registrations'), previous.get('registrations'))}",
        f"  Цена лида: {fmt_delta(current.get('lead_price'), previous.get('lead_price'), '$', higher_is_better=False)}",
        "",
        "👁 *Доходимость*",
        f"  Доходимость 1д: {fmt_delta(current.get('reach_1day'), previous.get('reach_1day'), '%')}",
        f"  Доходимость все пики: {fmt_delta(current.get('reach_all_peaks'), previous.get('reach_all_peaks'), '%')}",
        "",
        "📋 *Заявки и продажи*",
        f"  Заявки: {fmt_delta(current.get('applications'), previous.get('applications'))}",
        f"  Конверсия в заявку: {fmt_delta(current.get('conversion_to_app'), previous.get('conversion_to_app'), '%')}",
        f"  Продажи: {fmt_delta(current.get('sales'), previous.get('sales'))}",
        f"  Конверсия в оплату: {fmt_delta(current.get('conversion_to_payment'), previous.get('conversion_to_payment'), '%')}",
        "",
        "💰 *Деньги*",
        f"  Сумма продаж: {fmt_delta(current.get('total_sales'), previous.get('total_sales'), '$')}",
        f"  ROAS: {fmt_delta(current.get('roas_fact'), previous.get('roas_fact'), '%')}",
    ]

    # Auto-summary
    positives = []
    negatives = []

    checks = [
        ("registrations", "регистрации", True, ""),
        ("lead_price", "цена лида", False, "$"),
        ("reach_all_peaks", "доходимость", True, "%"),
        ("conversion_to_app", "конверсия в заявку", True, "%"),
        ("sales", "продажи", True, ""),
        ("roas_fact", "ROAS", True, "%"),
    ]

    for key, label, higher_good, sfx in checks:
        c_val = current.get(key)
        p_val = previous.get(key)
        if c_val is not None and p_val is not None:
            try:
                delta = float(c_val) - float(p_val)
                if higher_good and delta > 0:
                    positives.append(label)
                elif higher_good and delta < 0:
                    negatives.append(label)
                elif not higher_good and delta < 0:
                    positives.append(label)
                elif not higher_good and delta > 0:
                    negatives.append(label)
            except:
                pass

    lines.append("")
    if positives:
        lines.append(f"✅ *Выросло:* {', '.join(positives)}")
    if negatives:
        lines.append(f"🔴 *Просело:* {', '.join(negatives)}")
    if not positives and not negatives:
        lines.append("→ *Динамика стабильная*")

    return "\n".join(lines)

def build_summary(parsed, prev=None, weekly_curr=None, weekly_prev=None):
    f = parsed.get("fact", {})
    p = parsed.get("plan", {})
    analysis = parsed.get("analysis", {})
    funnel = detect_funnel(parsed.get("funnel", "?"))
    date = parsed.get("date", "?")
    period = parsed.get("period", "?")
    prev = prev or {}

    lines = [
        f"📊 *{funnel}* | Неделя {period} | {date}",
        "",
        "💸 *Бюджет и трафик*",
        f"  Бюджет трафик: ${v(f,'budget_traffic')} (план ${p.get('budget_traffic','—')})",
        f"  Ежедневный бюджет: {fmt_with_plan(f.get('daily_budget'), p.get('daily_budget'), '$', higher_is_better=False)}",
        f"  Регистрации: {v(f,'registrations')} (план {p.get('registrations','—')})",
        f"  Цена лида: {fmt_with_plan(f.get('lead_price'), p.get('lead_price'), '$', higher_is_better=False, prev_val=prev.get('lead_price'))}",
    ]
    if analysis.get("traffic"):
        lines += ["", f"  💡 _{analysis['traffic']}_"]

    lines += [
        "",
        "👁 *Доходимость*",
        f"  Зрители пик 1д: {v(f,'viewers_peak_1day')}",
        f"  Доходимость 1д: {fmt_with_plan(f.get('reach_1day'), p.get('reach_1day'), '%', prev_val=prev.get('reach_1day'))}",
        f"  Зрители все пики: {v(f,'viewers_all_peaks')}",
        f"  Доходимость все пики: {fmt_with_plan(f.get('reach_all_peaks'), p.get('reach_all_peaks'), '%', prev_val=prev.get('reach_all_peaks'))}",
    ]
    if analysis.get("reach"):
        lines += ["", f"  💡 _{analysis['reach']}_"]

    lines += [
        "",
        "📋 *Заявки и продажи*",
        f"  Заявки: {v(f,'applications')} (план {p.get('applications','—')})",
        f"  Конверсия в заявку: {fmt_with_plan(f.get('conversion_to_app'), p.get('conversion_to_app'), '%', prev_val=prev.get('conversion_to_app'))}",
        f"  % рег в заявку: {fmt_with_plan(f.get('reg_to_app_pct'), p.get('reg_to_app_pct'), '%', prev_val=prev.get('reg_to_app_pct'))}",
        f"  Продажи: {v(f,'sales')} (план {p.get('sales','—')})",
        f"  Конверсия в оплату: {fmt_with_plan(f.get('conversion_to_payment'), p.get('conversion_to_payment'), '%', prev_val=prev.get('conversion_to_payment'))}",
        f"  Автооплаты: {v(f,'autopayments')} (план {p.get('autopayments','—')})",
        f"  Конверсия в автооплату: {fmt_with_plan(f.get('conversion_to_autopayment'), p.get('conversion_to_autopayment'), '%', prev_val=prev.get('conversion_to_autopayment'))}",
    ]
    if analysis.get("sales"):
        lines += ["", f"  💡 _{analysis['sales']}_"]

    lines += [
        "",
        "💰 *Деньги*",
        f"  Средний чек: ${v(f,'avg_check')} (план ${p.get('avg_check','—')})",
        f"  Сумма продаж: ${v(f,'total_sales')} (план ${p.get('total_sales','—')})",
        f"  ROAS факт: {fmt_with_plan(f.get('roas_fact'), p.get('roas_fact'), '%', prev_val=prev.get('roas_fact'))}",
        f"  Потенциальный ROAS: {fmt_with_plan(f.get('potential_roas'), p.get('potential_roas'), '%', prev_val=prev.get('potential_roas'))}",
        f"  Ср. стоимость заявки: ${fmt_with_plan(f.get('avg_app_cost'), p.get('avg_app_cost'), higher_is_better=False, prev_val=prev.get('avg_app_cost'))}",
    ]
    if analysis.get("money"):
        lines += ["", f"  💡 _{analysis['money']}_"]

    if f.get('ojop_count') or f.get('ojop_sum'):
        lines += [
            "",
            "🔄 *ОЖОП*",
            f"  Кол-во: {v(f,'ojop_count')}",
            f"  Сумма: ${v(f,'ojop_sum')}",
        ]

    if analysis.get("action_plan"):
        lines += [
            "",
            "📌 *План действий*",
            f"_{analysis['action_plan']}_",
        ]

    # Add weekly comparison if it's Tuesday
    if weekly_curr and weekly_prev:
        funnel_name = detect_funnel(parsed.get("funnel", "?"))
        weekly_block = build_weekly_comparison(weekly_curr, weekly_prev, funnel_name, period)
        if weekly_block:
            lines.append(weekly_block)

    lines += ["", "✅ Сохранено → лист *" + detect_funnel(parsed.get("funnel", "?")) + "*"]
    return "\n".join(lines)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Читаю отчёт...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        parsed = analyze_with_claude(bytes(image_bytes))
        funnel = detect_funnel(parsed.get("funnel", "?"))
        date_str = parsed.get("date", "")

        # Get prev day data for delta
        prev = get_last_row(funnel)

        # Write to sheet
        f = parsed.get("fact", {})
        row = [date_str] + [f.get(k) for k in SHEET_COLUMNS[1:]]
        append_to_sheet(funnel, row)

        # Check if Tuesday — get weekly comparison
        weekly_curr, weekly_prev = None, None
        if is_tuesday(date_str):
            weekly_curr, weekly_prev = get_weekly_compare(funnel)

        summary = build_summary(parsed, prev, weekly_curr, weekly_prev)
        await update.message.reply_text(summary, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:300]}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь скрин отчёта по воронке (КМ, МС или ЦЛ) — дам сводку с анализом план/факт 📊\n\nПо вторникам автоматически добавляю сравнение с прошлой неделей 📅"
    )

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    print("Bot started")
    app.run_polling()
