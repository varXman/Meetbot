import datetime
import html
import logging
import re
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config_manager as cm
import scheduler
import sessions

log = logging.getLogger("telegram_bot")

_cfg = cm.load_config()
TOKEN = _cfg["telegram"]["token"]
CHAT_ID = int(_cfg["telegram"]["chat_id"])

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DOW_EN = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

SELECTED_DATE = {}

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Список"), KeyboardButton(text="📅 Расписание")],
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🛑 Стоп все")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)

HELP_TEXT = (
    "ℹ️ <b>Помощь</b>\n"
    "\n"
    "📋 Список — все встречи со ссылками (без расписания)\n"
    "📅 Расписание — расписание с выбором даты (кнопки ‹ › или дата текстом)\n"
    "📊 Статус — активные сессии\n"
    "🛑 Стоп все — остановить всё\n"
    "\n"
    "Команды:\n"
    "/add «id» «дата|дни» «ЧЧ:ММ» [минуты]\n"
    "   /add ukrdil 11.09 10:00 90 — разово 11 сентября\n"
    "   /add pdr mon,wed 11:30 45 — каждый пн и ср\n"
    "/del «код» — удалить запись (код вида a1b2, виден в расписании)\n"
    "/clear — очистить всё расписание\n"
    "/reload — пересобрать планировщик\n"
    "/join «id» [минуты] — войти сейчас\n"
    "/stop «id» — остановить сессию\n"
    "/stopall — остановить все\n"
    "\n"
    "Дату можно прислать просто текстом: 11.09 или 11.09.2026"
)


def fmt_date_ru(d):
    return f"{DOW_RU[d.weekday()]} {d.day:02d}.{d.month:02d}.{d.year}"


def parse_date_str(s):
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", s)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.date.today().year
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def end_time(t, dur):
    h, m = map(int, t.split(":"))
    total = h * 60 + m + int(dur)
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def meeting_title(cfg, mid):
    m = cm.find_meeting(cfg, mid)
    return m["title"] if m else mid


def entries_for_date(cfg, d):
    iso = d.isoformat()
    dow = DOW_EN[d.weekday()]
    out = []
    for e in cfg.get("schedule") or []:
        if e.get("date") == iso:
            out.append((e, "разово"))
        elif e.get("days") and dow in e["days"]:
            out.append((e, "каждые " + ", ".join(e["days"])))
    out.sort(key=lambda t: t[0].get("time", "00:00"))
    return out


def new_sid(cfg):
    existing = {str(e.get("sid")) for e in cfg.get("schedule") or []}
    while True:
        s = uuid.uuid4().hex[:4]
        if s not in existing:
            return s


def list_view(cfg):
    meetings = cfg.get("meetings") or []
    lines = [f"📋 <b>Встречи: {len(meetings)}</b>", ""]
    for i, m in enumerate(meetings, 1):
        lines.append(f"{i}. {html.escape(m['title'])}\n   <code>{m['id']}</code> · {m['url']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🚀 {i}. {m['title'][:28]}", callback_data="join:" + m["id"])]
        for i, m in enumerate(meetings, 1)
    ])
    return "\n".join(lines), kb


def date_view(cfg, d):
    entries = entries_for_date(cfg, d)
    lines = [f"📅 <b>Расписание на {fmt_date_ru(d)}</b>", ""]
    if not entries:
        lines.append("Пусто.")
        lines.append(f"Добавить: /add «id» {d.day:02d}.{d.month:02d} «ЧЧ:ММ» [минуты]")
    else:
        for i, (e, note) in enumerate(entries, 1):
            mid = e["meeting"]
            dur = int(e.get("duration", 90))
            lines.append(
                f"{i}. {e['time']}–{end_time(e['time'], dur)} "
                f"{html.escape(meeting_title(cfg, mid))}\n"
                f"    <code>{mid}</code> · {note} · #{e.get('sid', '?')}"
            )
    lines.append("")
    lines.append("Дату можно прислать текстом: 11.09 или 11.09.2026")
    rows = [
        [
            InlineKeyboardButton(
                text="‹ Пред день",
                callback_data="d:" + (d - datetime.timedelta(days=1)).isoformat(),
            ),
            InlineKeyboardButton(
                text="Сегодня",
                callback_data="d:" + datetime.date.today().isoformat(),
            ),
            InlineKeyboardButton(
                text="След день ›",
                callback_data="d:" + (d + datetime.timedelta(days=1)).isoformat(),
            ),
        ],
        [InlineKeyboardButton(text="📑 Все записи", callback_data="all")],
    ]
    rows += [
        [InlineKeyboardButton(text=f"🗑 {i}. {e['meeting']} {e['time']}", callback_data="del:" + str(e.get("sid")))]
        for i, (e, _) in enumerate(entries, 1)
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def all_view(cfg):
    sched = cfg.get("schedule") or []
    lines = [f"📑 <b>Все записи расписания: {len(sched)}</b>", ""]
    if not sched:
        lines.append("Пусто — добавь через /add")
    once = [e for e in sched if e.get("date")]
    weekly = [e for e in sched if e.get("days")]
    if once:
        lines.append("<b>По датам:</b>")
        for e in sorted(once, key=lambda x: (x["date"], x["time"])):
            lines.append(
                f"  {e['date']} {e['time']} ({e.get('duration', 90)} мин) "
                f"{html.escape(meeting_title(cfg, e['meeting']))} <code>{e['meeting']}</code> #{e.get('sid')}"
            )
    if weekly:
        lines.append("<b>По дням недели:</b>")
        for e in sorted(weekly, key=lambda x: x["time"]):
            lines.append(
                f"  {', '.join(e['days'])} {e['time']} ({e.get('duration', 90)} мин) "
                f"{html.escape(meeting_title(cfg, e['meeting']))} <code>{e['meeting']}</code> #{e.get('sid')}"
            )
    lines.append("")
    lines.append("Удаление: кнопка 🗑 или /del «код»")
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🗑 #{e.get('sid')} {e['meeting']} {e.get('date') or ','.join(e.get('days', []))} {e['time']}",
                callback_data="del:" + str(e.get("sid")),
            )
        ]
        for e in sched
    ]
    buttons.append([InlineKeyboardButton(text="📅 По датам", callback_data="d:" + datetime.date.today().isoformat())])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def status_view(cfg):
    running = sessions.running_info()
    lines = ["📊 <b>Статус</b>", ""]
    if not running:
        lines.append("Активных сессий нет.")
    else:
        for mid, meta in running.items():
            lines.append(
                f"▶️ <code>{mid}</code> — {html.escape(meeting_title(cfg, mid))}\n   в сессии {meta['elapsed']}"
            )
    rows = [[InlineKeyboardButton(text=f"🛑 Стоп {mid}", callback_data="stop:" + mid)] for mid in running]
    rows.append([InlineKeyboardButton(text="🛑 Стоп все", callback_data="stopall")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit(cb, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception as ex:
        log.warning("edit_text не удался (%s), шлю новым сообщением", ex)
        try:
            await cb.message.answer(text, reply_markup=kb)
        except Exception as ex2:
            log.warning("answer тоже не удался: %s", ex2)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    await message.answer(HELP_TEXT, reply_markup=MAIN_KB)


@dp.message(F.text.in_({"📋 Список", "📅 Расписание", "📊 Статус", "🛑 Стоп все", "ℹ️ Помощь"}))
async def kb_buttons(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    cfg = cm.load_config()
    t = message.text
    if t == "ℹ️ Помощь":
        await message.answer(HELP_TEXT)
    elif t == "📋 Список":
        text, kb = list_view(cfg)
        await message.answer(text, reply_markup=kb)
    elif t == "📅 Расписание":
        d = datetime.date.today()
        SELECTED_DATE[message.chat.id] = d.isoformat()
        text, kb = date_view(cfg, d)
        await message.answer(text, reply_markup=kb)
    elif t == "📊 Статус":
        text, kb = status_view(cfg)
        await message.answer(text, reply_markup=kb)
    elif t == "🛑 Стоп все":
        ids = sessions.stop_all()
        await message.answer(f"🛑 Сигнал стопа отправлен: {len(ids)} сессий")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    text, kb = list_view(cm.load_config())
    await message.answer(text, reply_markup=kb)


@dp.message(Command("schedule"))
async def cmd_schedule(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    d = datetime.date.today()
    SELECTED_DATE[message.chat.id] = d.isoformat()
    text, kb = date_view(cm.load_config(), d)
    await message.answer(text, reply_markup=kb)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    text, kb = status_view(cm.load_config())
    await message.answer(text, reply_markup=kb)


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if message.from_user.id != CHAT_ID:
        return
    args = (command.args or "").split()
    if len(args) < 3:
        await message.answer(
            "Формат: /add «id» «дата|дни» «ЧЧ:ММ» [минуты]\n"
            "Пример: /add ukrdil 11.09 10:00 90\n"
            "Пример: /add pdr mon,wed 11:30 45"
        )
        return
    mid, when, tstr = args[0], args[1], args[2]
    try:
        dur = int(args[3]) if len(args) > 3 else 90
    except ValueError:
        await message.answer("Минуты должны быть числом")
        return
    cfg = cm.load_config()
    if not cm.find_meeting(cfg, mid):
        await message.answer(f"Нет встречи с id <code>{html.escape(mid)}</code> — смотри 📋 Список")
        return
    if not re.fullmatch(r"\d{1,2}:\d{2}", tstr):
        await message.answer("Время в формате ЧЧ:ММ, например 10:00")
        return
    entry = {"meeting": mid, "time": tstr, "duration": dur, "sid": new_sid(cfg)}
    d = parse_date_str(when)
    if d:
        entry["date"] = d.isoformat()
    else:
        days = [x for x in when.lower().split(",") if x in DOW_EN]
        if not days:
            await message.answer("Дата: 11.09 / 11.09.2026, или дни: mon,tue,wed,thu,fri,sat,sun")
            return
        entry["days"] = days
    cm.add_schedule_entry(cfg, entry)
    n = scheduler.rebuild_scheduler()
    if d:
        await message.answer(
            f"✅ Добавлено: {html.escape(meeting_title(cfg, mid))} {d.day:02d}.{d.month:02d} {tstr} ({dur} мин), код #{entry['sid']}\n"
            f"Задач в планировщике: {n}"
        )
        text, kb = date_view(cfg, d)
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(
            f"✅ Добавлено: {html.escape(meeting_title(cfg, mid))} каждые {', '.join(entry['days'])} {tstr} ({dur} мин), код #{entry['sid']}\n"
            f"Задач в планировщике: {n}"
        )


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    if message.from_user.id != CHAT_ID:
        return
    sid = (command.args or "").strip().lstrip("#")
    cfg = cm.load_config()
    e = cm.find_entry(cfg, sid)
    if not e:
        await message.answer(f"Запись #{html.escape(sid)} не найдена")
        return
    cm.del_schedule_entry(cfg, sid)
    n = scheduler.rebuild_scheduler()
    await message.answer(f"🗑 Удалено #{html.escape(sid)} ({e['meeting']} {e['time']}). Задач: {n}")


@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    cfg = cm.load_config()
    cm.clear_schedule(cfg)
    n = scheduler.rebuild_scheduler()
    await message.answer(f"🗑 Расписание очищено. Задач: {n}")


@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    n = scheduler.rebuild_scheduler()
    await message.answer(f"🔄 Планировщик пересобран, активных задач: {n}")


@dp.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject):
    if message.from_user.id != CHAT_ID:
        return
    args = (command.args or "").split()
    if not args:
        await message.answer("Формат: /join «id» [минуты]")
        return
    arg0 = args[0]
    try:
        dur = int(args[1]) if len(args) > 1 else 90
    except ValueError:
        await message.answer("Минуты должны быть числом")
        return
    if arg0.startswith("http://") or arg0.startswith("https://"):
        mid = "manual_" + str(int(__import__("time").time()))
        m = {"id": mid, "url": arg0, "title": "Ручной вход"}
    else:
        cfg = cm.load_config()
        m = cm.find_meeting(cfg, arg0)
        if not m:
            await message.answer(f"Нет встречи с id <code>{html.escape(arg0)}</code>")
            return
        mid = arg0
    ok, msg = await sessions.start_session(m, dur, TOKEN, str(CHAT_ID))
    await message.answer(html.escape(msg))


@dp.message(Command("stop"))
async def cmd_stop(message: Message, command: CommandObject):
    if message.from_user.id != CHAT_ID:
        return
    mid = (command.args or "").strip()
    if not mid:
        await message.answer("Формат: /stop «id»")
        return
    sessions.stop_session(mid)
    await message.answer(f"🛑 Сигнал стопа отправлен: <code>{html.escape(mid)}</code>")


@dp.message(Command("stopall"))
async def cmd_stopall(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    ids = sessions.stop_all()
    await message.answer(f"🛑 Сигнал стопа отправлен: {len(ids)} сессий")


@dp.message(F.text.func(lambda t: parse_date_str(t) is not None))
async def text_date(message: Message):
    if message.from_user.id != CHAT_ID:
        return
    d = parse_date_str(message.text)
    SELECTED_DATE[message.chat.id] = d.isoformat()
    text, kb = date_view(cm.load_config(), d)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("d:"))
async def cb_date(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        d = datetime.date.fromisoformat(cb.data[2:])
        SELECTED_DATE[cb.message.chat.id] = d.isoformat()
        text, kb = date_view(cm.load_config(), d)
        await safe_edit(cb, text, kb)
        await cb.answer()
    except Exception:
        log.exception("Ошибка в cb_date")
        await cb.answer("Ошибка внутри cb_date, смотри лог", show_alert=True)


@dp.callback_query(F.data == "all")
async def cb_all(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        text, kb = all_view(cm.load_config())
        await safe_edit(cb, text, kb)
        await cb.answer()
    except Exception:
        log.exception("Ошибка в cb_all")
        await cb.answer("Ошибка внутри cb_all, смотри лог", show_alert=True)


@dp.callback_query(F.data.startswith("join:"))
async def cb_join(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        mid = cb.data[5:]
        cfg = cm.load_config()
        m = cm.find_meeting(cfg, mid)
        if not m:
            await cb.answer("Встреча не найдена", show_alert=True)
            return
        ok, msg = await sessions.start_session(m, 90, TOKEN, str(CHAT_ID))
        await cb.answer(msg, show_alert=True)
    except Exception:
        log.exception("Ошибка в cb_join")
        await cb.answer("Ошибка внутри cb_join, смотри лог", show_alert=True)


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        mid = cb.data[5:]
        sessions.stop_session(mid)
        await cb.answer(f"Стоп отправлен: {mid}")
    except Exception:
        log.exception("Ошибка в cb_stop")
        await cb.answer("Ошибка внутри cb_stop, смотри лог", show_alert=True)


@dp.callback_query(F.data == "stopall")
async def cb_stopall(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        ids = sessions.stop_all()
        await cb.answer(f"Стоп отправлен: {len(ids)}")
    except Exception:
        log.exception("Ошибка в cb_stopall")
        await cb.answer("Ошибка внутри cb_stopall, смотри лог", show_alert=True)


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(cb: CallbackQuery):
    log.info("callback: %s", cb.data)
    try:
        if cb.from_user.id != CHAT_ID:
            await cb.answer("Нет доступа", show_alert=True)
            return
        sid = cb.data[4:]
        cfg = cm.load_config()
        e = cm.find_entry(cfg, sid)
        if not e:
            await cb.answer("Запись уже удалена", show_alert=True)
            return
        cm.del_schedule_entry(cfg, sid)
        scheduler.rebuild_scheduler()
        if e.get("date"):
            d = datetime.date.fromisoformat(e["date"])
            text, kb = date_view(cfg, d)
        else:
            text, kb = all_view(cfg)
        await safe_edit(cb, text, kb)
        await cb.answer(f"Удалено #{sid}")
    except Exception:
        log.exception("Ошибка в cb_del")
        await cb.answer("Ошибка внутри cb_del, смотри лог", show_alert=True)


@dp.callback_query()
async def cb_unknown(cb: CallbackQuery):
    log.warning("Неизвестный callback: %s", cb.data)
    await cb.answer()

