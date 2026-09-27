"""
Moneta — multi-user expense tracker: Google Sheets + Finn AI (Claude) + scheduler
"""

import os
import re
import io
import csv
import html
import asyncio
import logging
import functools
from datetime import datetime, date, timedelta

try:
    import openpyxl
    XLSX_OK = True
except ImportError:
    XLSX_OK = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

import ai
import storage
from storage import EXPENSE_CATS, INCOME_CATS, cat_label, now_local, safe_float

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DEFAULT_REMINDER = (int(os.getenv("REMINDER_HOUR", "20")), int(os.getenv("REMINDER_MINUTE", "0")))
HTML = ParseMode.HTML

h = html.escape

# uid -> {pending id: {"amount", "description", "type", "date"}}
pending = {}
_pending_seq = 0
_reminder_times = {}   # uid -> (hour, minute)
_reminded_today = {}   # uid -> date the daily reminder was sent


# ── INPUT PARSING ─────────────────────────────────────────────────────────────
AMOUNT_RE = re.compile(r"^\+?\d+(?:[.,]\d{1,2})?$")
DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?$")


class BadDate(ValueError):
    pass


def parse_entry(text: str, today: date):
    """Parse 'coffee 4.5', '4.5 coffee', 'salary +3411', 'coffee 4.5 25.05'.

    Returns {"amount", "description", "income", "date"} or None if there is no amount.
    A trailing d.m token is a date only when another token is the amount, so
    'coffee 4.5' is 4.50 €, not the 4th of May. Raises BadDate for impossible dates.
    """
    tokens = text.split()
    tx_date = None
    if len(tokens) >= 2:
        m = DATE_RE.match(tokens[-1])
        rest = tokens[:-1]
        amount_left = AMOUNT_RE.match(rest[0]) or AMOUNT_RE.match(rest[-1])
        if m and (m.group(3) or amount_left):
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            try:
                if year:
                    y = int(year)
                    tx_date = date(y + 2000 if y < 100 else y, month, day)
                else:
                    tx_date = date(today.year, month, day)
                    if tx_date > today:
                        tx_date = tx_date.replace(year=today.year - 1)
            except ValueError:
                raise BadDate(tokens[-1])
            tokens = tokens[:-1]

    if not tokens:
        return None
    if AMOUNT_RE.match(tokens[0]):
        raw, desc = tokens[0], " ".join(tokens[1:])
    elif AMOUNT_RE.match(tokens[-1]):
        raw, desc = tokens[-1], " ".join(tokens[:-1])
    else:
        return None
    amount = float(raw.lstrip("+").replace(",", "."))
    if amount <= 0:
        return None
    return {"amount": amount, "description": desc or "—", "income": raw.startswith("+"), "date": tx_date}


# ── HELPERS ───────────────────────────────────────────────────────────────────
async def run(fn, *args, **kwargs):
    """Run blocking storage/AI code off the event loop."""
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


def guard(handler):
    """Turn storage/AI failures into a friendly reply instead of a silent crash."""
    @functools.wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            return await handler(update, ctx)
        except storage.UserLimitError:
            text = "😔 Бот сейчас не принимает новых пользователей."
        except storage.StorageError:
            text = "⚠️ Хранилище (Google Sheets) временно недоступно. Попробуйте чуть позже."
        except ai.AIError as e:
            text = f"🦊 {e}"
        if update.effective_message:
            await update.effective_message.reply_text(text)
    return wrapper


def money(v: float) -> str:
    return f"{v:.2f}€"


def tx_line(r: dict) -> str:
    sign = "+" if r["Type"] == "income" else ("→" if r["Type"] == "transfer" else "-")
    return (f"{h(r['Date'])} | {sign}{r['Amount']:.2f}€ | {h(cat_label(r['Category']))}"
            f" | {h(r['Description'])}")


def cat_keyboard(tx_type: str, pid: int):
    cats = INCOME_CATS if tx_type == "income" else EXPENSE_CATS
    buttons, row = [], []
    for i, cat in enumerate(cats):
        row.append(InlineKeyboardButton(cat_label(cat), callback_data=f"c:{i}:{pid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data=f"x:{pid}")])
    return InlineKeyboardMarkup(buttons)


def main_keyboard():
    buttons = []
    if WEBAPP_URL:
        buttons.append([InlineKeyboardButton("📊 Открыть приложение", web_app=WebAppInfo(url=WEBAPP_URL))])
    buttons.append([
        InlineKeyboardButton("📋 Статистика", callback_data="cmd:stats"),
        InlineKeyboardButton("📈 Сравнение", callback_data="cmd:compare"),
    ])
    buttons.append([
        InlineKeyboardButton("🦊 Финн", callback_data="cmd:finn"),
        InlineKeyboardButton("❓ Помощь", callback_data="cmd:help"),
    ])
    return InlineKeyboardMarkup(buttons)


def month_label(month: str) -> str:
    names = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
             "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    y, m = month.split("-")
    return f"{names[int(m) - 1]} {y}"


# ── COMMANDS ──────────────────────────────────────────────────────────────────
HELP_TEXT = (
    "📖 <b>Как пользоваться</b>\n\n"
    "<b>Добавить запись:</b>\n"
    "<code>кофе 4.5</code> — расход\n"
    "<code>зарплата +3411</code> — доход\n"
    "<code>кофе 4.5 25.05</code> — с датой\n\n"
    "<b>Команды:</b>\n"
    "/stats <code>[ГГГГ-ММ]</code> — статистика за месяц\n"
    "/compare — этот месяц против прошлого\n"
    "/finn <code>вопрос</code> — ИИ-помощник\n"
    "/last — последние 10 записей\n"
    "/find <code>запрос</code> — поиск\n"
    "/budget <code>категория лимит</code> — бюджет на месяц\n"
    "/settings <code>ЧЧ:ММ</code> — время напоминания\n"
    "/export, /exportxls — выгрузка CSV / Excel\n"
    "/deletedata — удалить все данные\n\n"
    "📷 Импорт скринов из банка и чат с Финном — в приложении (кнопка «Открыть приложение»)."
)


@guard
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await run(storage.get_user_sheet, user.id)
    await update.effective_message.reply_text(
        f"👋 Привет, {h(user.first_name or 'друг')}! Это <b>Moneta</b> — учёт расходов.\n\n"
        "Просто пиши траты в чат: <code>кофе 4.5</code>, доходы — с плюсом: "
        "<code>зарплата +3411</code>.\n\n" + HELP_TEXT,
        parse_mode=HTML, reply_markup=main_keyboard(),
    )


@guard
async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=HTML, reply_markup=main_keyboard())


@guard
async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    month = args[0] if args else now_local().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        await update.effective_message.reply_text("Формат: /stats 2026-05")
        return
    rows = await run(storage.read_transactions, update.effective_user.id)
    exp, inc, by_cat = storage.month_stats(rows, month)
    lines = [f"📊 <b>Статистика: {month_label(month)}</b>\n"]
    if by_cat:
        for cat, amt in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
            bar = "▓" * min(int(amt / 50), 8)
            lines.append(f"{h(cat_label(cat))}: <b>{money(amt)}</b> {bar}")
    else:
        lines.append("<i>Расходов пока нет</i>")
    lines.append(f"\n💸 Расходы: <b>{money(exp)}</b>")
    if inc > 0:
        lines.append(f"💚 Доходы: <b>{money(inc)}</b>")
        lines.append(f"📈 Баланс: <b>{inc - exp:+.2f}€</b>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML)


@guard
async def compare_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await run(storage.read_transactions, update.effective_user.id)
    cur = now_local().strftime("%Y-%m")
    prev = storage.prev_month(cur)
    exp_c, inc_c, by_c = storage.month_stats(rows, cur)
    exp_p, inc_p, by_p = storage.month_stats(rows, prev)
    diff = exp_c - exp_p
    pct = f", {diff / exp_p * 100:+.0f}%" if exp_p > 0 else ""
    lines = [f"📊 <b>{month_label(cur)} против {month_label(prev)}</b>\n",
             f"Расходы: <b>{money(exp_c)}</b> vs {money(exp_p)} ({diff:+.2f}€{pct})",
             f"Доходы: <b>{money(inc_c)}</b> vs {money(inc_p)}\n"]
    for cat in sorted(set(by_c) | set(by_p), key=lambda c: by_c.get(c, 0), reverse=True)[:6]:
        c, p = by_c.get(cat, 0), by_p.get(cat, 0)
        arrow = "↑" if c > p else ("↓" if c < p else "→")
        lines.append(f"{h(cat_label(cat))}: {c:.0f}€ {arrow} ({c - p:+.0f}€)")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=HTML)


EXPORT_FIELDS = ["Date", "Amount", "Category", "Description", "Type", "Month", "Account"]


@guard
async def export_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await run(storage.read_transactions, update.effective_user.id)
    msg = update.effective_message
    if not rows:
        await msg.reply_text("Пока нечего выгружать.")
        return
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    await msg.reply_document(
        document=io.BytesIO(out.getvalue().encode("utf-8-sig")),
        filename=f"finances_{now_local().strftime('%Y%m%d')}.csv",
        caption=f"📤 Ваши записи: {len(rows)}",
    )


@guard
async def exportxls_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not XLSX_OK:
        await export_cmd(update, ctx)
        return
    rows = await run(storage.read_transactions, update.effective_user.id)
    msg = update.effective_message
    if not rows:
        await msg.reply_text("Пока нечего выгружать.")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Дата", "Сумма (€)", "Категория", "Описание", "Тип", "Месяц", "Счёт"])
    for r in rows:
        amount = r["Amount"] if r["Type"] == "income" else -r["Amount"]
        ws.append([r["Date"], amount, cat_label(r["Category"]), r["Description"], r["Type"], r["Month"], r["Account"]])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    await msg.reply_document(document=out, filename=f"finances_{now_local().strftime('%Y%m%d')}.xlsx",
                             caption=f"📊 Отчёт: {len(rows)} записей")


@guard
async def find_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not ctx.args:
        await msg.reply_text("Пример: <code>/find кофе</code>", parse_mode=HTML)
        return
    query = " ".join(ctx.args).lower()
    rows = await run(storage.read_transactions, update.effective_user.id)
    found = [r for r in rows if query in r["Description"].lower()
             or query in r["Category"].lower() or query in cat_label(r["Category"]).lower()]
    if not found:
        await msg.reply_text(f"Ничего не найдено по запросу «{query}»")
        return
    lines = [f"🔍 <b>Найдено по «{h(query)}»</b> ({len(found)}):\n"]
    lines += [tx_line(r) for r in reversed(found[-10:])]
    await msg.reply_text("\n".join(lines), parse_mode=HTML)


@guard
async def last_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = await run(storage.read_transactions, update.effective_user.id)
    msg = update.effective_message
    if not rows:
        await msg.reply_text("Записей пока нет.")
        return
    lines = ["📋 <b>Последние 10 записей:</b>\n"] + [tx_line(r) for r in reversed(rows[-10:])]
    await msg.reply_text("\n".join(lines), parse_mode=HTML)


def match_category(query: str):
    q = query.lower().strip()
    for cat in EXPENSE_CATS:
        if q and (q in cat.lower() or q in cat_label(cat).lower()):
            return cat
    return None


@guard
async def budget_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.effective_message
    args = ctx.args or []
    if len(args) >= 2:
        cat = match_category(" ".join(args[:-1]))
        limit = safe_float(args[-1], -1)
        if not cat or limit <= 0:
            await msg.reply_text("Пример: <code>/budget продукты 400</code>", parse_mode=HTML)
            return
        rec = storage.make_record(uid, now_local(), limit, cat, "__budget__", "budget")
        await run(storage.replace_meta, uid, "budget", [rec], match=lambda r: r["Category"] == cat)
        await msg.reply_text(f"✅ Бюджет: <b>{h(cat_label(cat))}</b> → {money(limit)} в месяц", parse_mode=HTML)
        return
    rows = await run(storage.read_rows, uid)
    budgets = {r["Category"]: r["Amount"] for r in rows if r["Type"] == "budget" and r["Amount"] > 0}
    if not budgets:
        await msg.reply_text("Бюджеты не заданы.\nПример: <code>/budget продукты 400</code>", parse_mode=HTML)
        return
    _, _, by_cat = storage.month_stats(rows, now_local().strftime("%Y-%m"))
    lines = ["🎯 <b>Бюджеты на месяц</b>\n"]
    for cat, lim in budgets.items():
        spent = by_cat.get(cat, 0)
        pct = spent / lim * 100
        filled = min(int(pct / 10), 10)
        status = "🔴" if pct >= 100 else ("🟡" if pct >= 80 else "🟢")
        lines.append(f"{status} {h(cat_label(cat))}\n<code>{'█' * filled}{'░' * (10 - filled)}</code> "
                     f"{pct:.0f}%\n{money(spent)} / {money(lim)}\n")
    await msg.reply_text("\n".join(lines), parse_mode=HTML)


def parse_hhmm(value: str):
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", str(value).strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


@guard
async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = update.effective_message
    args = ctx.args or []
    if args:
        t = parse_hhmm(args[0])
        if not t:
            await msg.reply_text("❌ Неверный формат. Пример: <code>/settings 19:30</code>", parse_mode=HTML)
            return
        await run(storage.set_setting, uid, "reminder_time", f"{t[0]:02d}:{t[1]:02d}")
        _reminder_times[uid] = t
        await msg.reply_text(f"✅ Ежедневное напоминание теперь в <b>{t[0]:02d}:{t[1]:02d}</b>", parse_mode=HTML)
        return
    rows = await run(storage.read_rows, uid)
    t = parse_hhmm(storage.get_setting(rows, "reminder_time", "")) or DEFAULT_REMINDER
    await msg.reply_text(
        "⚙️ <b>Уведомления</b>\n\n"
        f"🌙 Ежедневное напоминание: <b>{t[0]:02d}:{t[1]:02d}</b>\n"
        f"📊 Итоги недели: по воскресеньям в {DEFAULT_REMINDER[0]:02d}:{DEFAULT_REMINDER[1]:02d}\n"
        "📅 Отчёт за месяц: 1-го числа в 10:00\n\n"
        "Изменить время напоминания: <code>/settings ЧЧ:ММ</code>",
        parse_mode=HTML,
    )


@guard
async def deletedata_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, удалить всё", callback_data="deleteconfirm:yes"),
        InlineKeyboardButton("❌ Отмена", callback_data="deleteconfirm:no"),
    ]])
    await update.effective_message.reply_text(
        "⚠️ <b>Точно?</b>\n\nВсе ваши записи, бюджеты и настройки будут удалены безвозвратно.",
        parse_mode=HTML, reply_markup=keyboard,
    )


@guard
async def finn_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    question = " ".join(ctx.args or []).strip()
    if not question:
        await msg.reply_text(
            f"Привет, {h(update.effective_user.first_name or 'друг')}! Я Финн 🦊 — помогу разобраться в тратах.\n\n"
            "Спроси меня:\n"
            "<code>финн как у меня дела в этом месяце?</code>\n"
            "<code>финн где я перерасходую?</code>\n"
            "<code>финн как сэкономить 200 €?</code>\n\n"
            "Полноценный чат — в приложении, вкладка «Финн».",
            parse_mode=HTML,
        )
        return
    uid = update.effective_user.id
    ai.take_quota(uid, "chat")
    await msg.chat.send_action("typing")
    rows = await run(storage.read_rows, uid)
    reply = await run(ai.chat, rows, [{"role": "user", "content": question}])
    await msg.reply_text(f"🦊 {reply}")


# ── MESSAGES ──────────────────────────────────────────────────────────────────
@guard
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _pending_seq
    msg = update.effective_message
    text = msg.text.strip()
    uid = update.effective_user.id

    lower = text.lower()
    for trigger in ("финн", "finn"):
        if lower.startswith(trigger):
            ctx.args = text[len(trigger):].split()
            await finn_cmd(update, ctx)
            return

    try:
        entry = parse_entry(text, now_local().date())
    except BadDate as e:
        await msg.reply_text(f"📅 Такой даты нет: {h(str(e))}. Пример: <code>кофе 4.5 25.05</code>", parse_mode=HTML)
        return
    if not entry:
        await msg.reply_text("Не понял 🤔 Пример: <code>кофе 4.50</code> или <code>зарплата +3411</code>",
                             parse_mode=HTML)
        return

    tx_type = "income" if entry["income"] else "expense"
    _pending_seq += 1
    user_pending = pending.setdefault(uid, {})
    user_pending[_pending_seq] = {**entry, "type": tx_type}
    while len(user_pending) > 20:
        user_pending.pop(next(iter(user_pending)))

    sign = "+" if entry["income"] else "-"
    date_info = f" ({entry['date'].strftime('%d.%m.%Y')})" if entry["date"] else ""
    await msg.reply_text(
        f"{'💚' if entry['income'] else '💸'} <b>{sign}{money(entry['amount'])}</b> — "
        f"{h(entry['description'])}{date_info}\n\nВыберите категорию:",
        reply_markup=cat_keyboard(tx_type, _pending_seq), parse_mode=HTML,
    )


# ── CALLBACKS ─────────────────────────────────────────────────────────────────
@guard
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data or ""

    if data.startswith("cmd:"):
        ctx.args = []
        handler = {"stats": stats_cmd, "compare": compare_cmd, "finn": finn_cmd, "help": help_cmd}.get(data[4:])
        if handler:
            await handler(update, ctx)
        return

    if data.startswith("deleteconfirm:"):
        if data.endswith(":yes"):
            result = await run(storage.delete_user_data, uid)
            _reminder_times.pop(uid, None)
            pending.pop(uid, None)
            text = "✅ Все ваши данные удалены." if result != "none" else "У вас не было сохранённых данных."
        else:
            text = "❌ Отменено. Данные на месте."
        await query.edit_message_text(text)
        return

    parts = data.split(":")
    if parts[0] == "x" and len(parts) == 2:
        pending.get(uid, {}).pop(int(parts[1]), None)
        await query.edit_message_text("❌ Отменено.")
        return

    if parts[0] != "c" or len(parts) != 3:
        return
    info = pending.get(uid, {}).get(int(parts[2]))
    if not info:
        await query.edit_message_text("⌛ Эта запись устарела — отправьте её ещё раз.")
        return
    cats = INCOME_CATS if info["type"] == "income" else EXPENSE_CATS
    category = cats[int(parts[1])]
    when = info["date"] or now_local()
    # Kept in `pending` until saved, so the button can be pressed again if Sheets fails
    await run(storage.add_transaction, uid, when, info["amount"], category, info["description"],
              info["type"], source="bot")
    pending[uid].pop(int(parts[2]), None)
    sign = "+" if info["type"] == "income" else "-"
    await query.edit_message_text(
        f"✅ Сохранено!\n<b>{sign}{money(info['amount'])}</b> — {h(info['description'])}\n"
        f"Категория: {h(cat_label(category))}\nДата: {when.strftime('%d.%m.%Y')}",
        parse_mode=HTML,
    )


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Что-то пошло не так. Попробуйте ещё раз.")
        except Exception:
            pass


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def load_reminder_times() -> dict:
    """Reminder time of every user (one sheet read per user — run hourly, not every 5 min)."""
    times = {}
    for uid in storage.list_user_ids():
        try:
            rows = storage.read_rows(uid)
        except storage.StorageError as e:
            logger.warning(f"Reminder settings for {uid} unavailable: {e}")
            times[uid] = _reminder_times.get(uid, DEFAULT_REMINDER)
            continue
        times[uid] = parse_hhmm(storage.get_setting(rows, "reminder_time", "")) or DEFAULT_REMINDER
    return times


async def refresh_reminders(app):
    try:
        times = await run(load_reminder_times)
    except storage.StorageError as e:
        logger.warning(f"Reminder refresh failed: {e}")
        return
    _reminder_times.clear()
    _reminder_times.update(times)
    logger.info(f"Reminder times loaded for {len(times)} users")


async def send_daily_reminder(app):
    now = now_local()
    for uid, (hour, minute) in list(_reminder_times.items()):
        if _reminded_today.get(uid) == now.date():
            continue
        if now.hour != hour or now.minute // 5 != minute // 5:
            continue
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=("🌙 <b>Вечерняя проверка!</b>\n\nНе забудьте записать сегодняшние траты 📝\n"
                      "<code>кофе 4.5</code> — расход, <code>зарплата +3000</code> — доход.\n\n"
                      "Изменить время: /settings"),
                parse_mode=HTML,
            )
            _reminded_today[uid] = now.date()
        except Exception as e:
            logger.warning(f"Daily reminder failed for {uid}: {e}")


def week_summary(rows: list, start: date, end: date) -> str:
    week = [r for r in rows if (d := storage.parse_date(r["Date"])) and start <= d <= end]
    exp, inc = 0.0, 0.0
    by_cat = {}
    for r in week:
        if r["Type"] == "expense":
            exp += r["Amount"]
            by_cat[r["Category"]] = by_cat.get(r["Category"], 0) + r["Amount"]
        elif r["Type"] == "income":
            inc += r["Amount"]
    if not exp and not inc:
        return ""
    lines = [f"📊 <b>Итоги недели</b> ({start.strftime('%d.%m')} — {end.strftime('%d.%m')})\n",
             f"💸 Потрачено: <b>{money(exp)}</b>"]
    if inc > 0:
        lines.append(f"💚 Получено: <b>{money(inc)}</b>")
    lines.append(f"📈 Итог: <b>{inc - exp:+.2f}€</b>\n")
    top = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:3]
    if top:
        lines.append("<b>Больше всего:</b>")
        lines += [f"  {h(cat_label(c))}: {money(a)} ({a / exp * 100:.0f}%)" for c, a in top]
    return "\n".join(lines)


async def send_weekly_stats(app):
    today = now_local().date()
    start = today - timedelta(days=today.weekday())
    for uid in list(_reminder_times):
        try:
            text = week_summary(await run(storage.read_transactions, uid), start, today)
            if text:
                await app.bot.send_message(chat_id=uid, text=text, parse_mode=HTML)
        except Exception as e:
            logger.warning(f"Weekly stats failed for {uid}: {e}")


def month_report(rows: list, month: str) -> str:
    exp, inc, by_cat = storage.month_stats(rows, month)
    if not exp and not inc:
        return ""
    exp_p, _, _ = storage.month_stats(rows, storage.prev_month(month))
    lines = [f"📅 <b>Отчёт за {month_label(month)}</b>\n", f"💸 Потрачено: <b>{money(exp)}</b>"]
    if inc > 0:
        lines.append(f"💚 Получено: <b>{money(inc)}</b>")
        lines.append(f"📊 Баланс: <b>{inc - exp:+.2f}€</b>")
    if exp_p > 0:
        diff = exp - exp_p
        lines.append(f"\nПо сравнению с прошлым месяцем: {diff:+.2f}€ ({diff / exp_p * 100:+.0f}%)")
    top = sorted(by_cat.items(), key=lambda x: x[1], reverse=True)[:5]
    if top:
        lines.append("\n<b>Расходы по категориям:</b>")
        lines += [f"{h(cat_label(c))}: <b>{money(a)}</b> ({a / exp * 100:.0f}%)" for c, a in top]
    lines.append("\nСпросите Финна в приложении, где можно сэкономить 🦊")
    return "\n".join(lines)


async def send_monthly_stats(app):
    month = storage.prev_month(now_local().strftime("%Y-%m"))
    for uid in list(_reminder_times):
        try:
            text = month_report(await run(storage.read_transactions, uid), month)
            if text:
                await app.bot.send_message(chat_id=uid, text=text, parse_mode=HTML)
        except Exception as e:
            logger.warning(f"Monthly stats failed for {uid}: {e}")


async def post_init(application):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    tz = storage.TIMEZONE
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(refresh_reminders, CronTrigger(minute=2, timezone=tz), args=[application],
                      id="refresh_reminders", next_run_time=datetime.now(tz))
    scheduler.add_job(send_daily_reminder, CronTrigger(minute="*/5", timezone=tz), args=[application],
                      id="daily_reminder")
    scheduler.add_job(send_weekly_stats, CronTrigger(day_of_week="sun", hour=DEFAULT_REMINDER[0],
                                                     minute=DEFAULT_REMINDER[1], timezone=tz),
                      args=[application], id="weekly_stats")
    scheduler.add_job(send_monthly_stats, CronTrigger(day=1, hour=10, timezone=tz),
                      args=[application], id="monthly_stats")
    scheduler.start()
    logger.info("Scheduler started")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    for name, fn in [("start", start), ("help", help_cmd), ("stats", stats_cmd), ("compare", compare_cmd),
                     ("export", export_cmd), ("exportxls", exportxls_cmd), ("find", find_cmd),
                     ("last", last_cmd), ("budget", budget_cmd), ("settings", settings_cmd),
                     ("deletedata", deletedata_cmd), ("finn", finn_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
    logger.info("Moneta bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
