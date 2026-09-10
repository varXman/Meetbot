# -*- coding: utf-8 -*-
"""Планировщик: сканирует окна расписания и запускает сессии.

Правила:
  * каждые 30 сек проверяем все записи расписания;
  * окно = [время, время + duration];
  * заходим в ЛЮБОЙ момент внутри окна, если сессия ещё не запущена;
  * код 0 пишет маркер done — повторно в это окно не заходим;
  * ОДНА сессия за раз: если что-то уже идёт, второе окно НЕ стартуем,
    а один раз (не чаще 10 мин) пишем в Telegram «занято, решайте».
"""
import asyncio
import glob
import logging
import os
import time
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

import config_manager
import sessions
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

TZ = ZoneInfo("Europe/Kiev")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE = os.path.join(BASE_DIR, "storage")
DOW_EN = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

SCAN_SECONDS = 30      # как часто проверяем окна
MIN_GAP_SEC = 40       # минимальный интервал между попытками по одной записи
BUSY_NOTICE_GAP = 600  # не чаще раза в 10 мин напоминаем «занято»
END_MIN_SEC = 60       # если окну осталось < минуты — про «занято» не пишем

scheduler = AsyncIOScheduler(timezone=TZ)
_last_attempt = {}
_last_busy = {}


class _QuietFilter(logging.Filter):
    """Убираем служебный шум APScheduler из лога."""
    def filter(self, record):
        try:
            m = record.getMessage()
        except Exception:
            return True
        bad = ("Executing", "Execution", "Adding job", "Added job",
               "Scheduler started", "next run time")
        return not any(m.startswith(b) or b in m for b in bad)


for _name in ("apscheduler.executors.default", "apscheduler.core", "apscheduler.scheduler"):
    logging.getLogger(_name).addFilter(_QuietFilter())


def _log(msg):
    logging.info("Scheduler | %s", msg)


def _done_marker(sid, d):
    return os.path.join(STORAGE, "done_" + str(sid) + "_" + d.strftime("%Y-%m-%d") + ".flag")


async def _run_session(meeting_id, duration, sid):
    cfg = config_manager.load_config()
    meeting = config_manager.find_meeting(cfg, meeting_id)
    if not meeting:
        _log("meeting not found: " + str(meeting_id))
        return
    token = cfg["telegram"]["token"]
    chat_id = cfg["telegram"]["chat_id"]
    res = await sessions.start_session(meeting, duration, token, chat_id, sid=sid)
    _log("старт " + str(meeting_id) + ": " + str(res))


def _window(e, now):
    try:
        hh, mm = map(int, str(e["time"]).split(":")[:2])
    except Exception:
        return None
    t = dtime(hh, mm)
    d = now.date()
    try:
        dur = int(e.get("duration", 90))
    except Exception:
        dur = 90
    if "date" in e:
        try:
            d = datetime.strptime(str(e["date"]), "%Y-%m-%d").date()
        except Exception:
            return None
        if d != now.date():
            return None
    elif "days" in e:
        wd = DOW_EN[now.weekday()]
        days = [str(x).strip().lower() for x in e["days"]]
        if wd not in days:
            return None
    else:
        return None
    start = datetime.combine(d, t, tzinfo=TZ)
    return start, start + timedelta(minutes=dur)


def _cleanup_markers():
    try:
        cutoff = datetime.now(TZ) - timedelta(days=3)
        for f in glob.glob(os.path.join(STORAGE, "done_*.flag")):
            try:
                if datetime.fromtimestamp(os.path.getmtime(f), TZ) < cutoff:
                    os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


async def _scan():
    now = datetime.now(TZ)
    mono = time.monotonic()
    cfg = config_manager.load_config()
    running = sessions.running_ids()

    for e in cfg.get("schedule", []):
        try:
            mid = str(e["meeting"])
            sid = str(e.get("sid") or mid)
            w = _window(e, now)
            if not w:
                continue
            start, end = w
            if not (start <= now <= end):
                continue
            if mid in running:
                continue
            if os.path.exists(_done_marker(sid, now)):
                continue

            # --- Одна сессия за раз: второе окно только сообщаем ---
            if running:
                if (end - now).total_seconds() < END_MIN_SEC:
                    continue
                if mono - _last_busy.get(sid, 0) >= BUSY_NOTICE_GAP:
                    _last_busy[sid] = mono
                    other = ", ".join(running)
                    msg = (
                        "⏸ Занято — решайте.\n\n"
                        "Окно активно: " + mid + " (" + start.strftime("%H:%M") + "–" +
                        end.strftime("%H:%M") + " по Киеву)\n"
                        "Сейчас идёт: " + other + "\n\n"
                        "В " + mid + " НЕ захожу: одновременно работает только одна встреча.\n"
                        "Если нужно войти в " + mid + " — остановите текущую (🛑 Стоп). "
                        "После остановки я зайду в " + mid + " автоматически в течение 30 секунд.\n"
                        "Если не трогать — текущая досидит, а " + mid + " будет пропущена."
                    )
                    _log("занято: " + mid + " ждёт (идёт " + other + ")")
                    asyncio.create_task(sessions.notify(msg))
                continue

            prev = _last_attempt.get(sid)
            if prev is not None and (mono - prev) < MIN_GAP_SEC:
                continue
            _last_attempt[sid] = mono
            _log("Окно активно: " + mid + " (" + start.strftime("%H:%M") + "-" +
                 end.strftime("%H:%M") + " по Киеву), запускаю сессию")
            asyncio.create_task(_run_session(mid, e.get("duration", 90), sid))
        except Exception as ex:
            _log("scan error: " + repr(ex))

    _cleanup_markers()


def rebuild_scheduler():
    for job in scheduler.get_jobs():
        job.remove()
    scheduler.add_job(_scan, IntervalTrigger(seconds=SCAN_SECONDS, timezone=TZ),
                      id="scan", replace_existing=True,
                      max_instances=1, coalesce=True)
    if not scheduler.running:
        scheduler.start()
    cfg = config_manager.load_config()
    _log("пересобран (Киев): записей расписания " + str(len(cfg.get("schedule", []))) +
         ", проверка каждые " + str(SCAN_SECONDS) + " с, одна сессия за раз")

