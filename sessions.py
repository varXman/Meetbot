# -*- coding: utf-8 -*-
"""Управление сессиями встреч: запуск/остановка/статус.

ЖЁСТКОЕ ПРАВИЛО: одновременно живёт ровно ОДНА сессия.
Профиль Chrome один (chrome_profile), вторая встреча не запускается,
пока первая работает. Решение «стопать или ждать» принимает человек
(через Telegram), бот сам ничего не убивает.
"""
import asyncio
import glob
import os
import signal
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kiev")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
PROFILE_DIR = os.path.join(BASE_DIR, "chrome_profile")
PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python")
SCRIPT = os.path.join(BASE_DIR, "meet_session.py")

RC_SKIP_NOBODY = 99

_running = {}
_meta = {}
_notified = set()
import telegram_bot  # noqa: E402


def _flag_path(mid):
    return os.path.join(STORAGE_DIR, "stop_" + str(mid) + ".flag")


def _done_path(sid):
    return os.path.join(STORAGE_DIR, "done_" + str(sid) + "_" +
                        datetime.now(TZ).strftime("%Y-%m-%d") + ".flag")


async def notify(text):
    """Публичная отправка сообщения в Telegram (использует планировщик)."""
    await _notify(text)


async def _notify(text):
    try:
        await telegram_bot.bot.send_message(telegram_bot.CHAT_ID, text)
    except Exception:
        pass


def _kill_leftover_chrome():
    """Перед стартом добиваем зависший Chrome и чистим локи профиля."""
    if _running:
        return
    try:
        subprocess.run(["pkill", "-9", "-f", "chrome_profile"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass
    for f in glob.glob(os.path.join(PROFILE_DIR, "Singleton*")):
        try:
            os.remove(f)
        except Exception:
            pass


async def start_session(meeting, duration, token, chat_id, sid=None):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    mid = str(meeting["id"])

    # --- ЗАМОК: одна сессия за раз ---
    if mid in _running:
        return "Уже запущено: " + mid
    if _running:
        busy = ", ".join(_running.keys())
        return "Занято (идёт " + busy + ") — " + mid + " не запускаю"

    _kill_leftover_chrome()

    url = meeting["url"]
    log_path = "/tmp/meet_" + mid + ".log"
    f = open(log_path, "a", encoding="utf-8")
    f.write("\n=== старт " + datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S") +
            " (Киев), sid=" + str(sid) + " ===\n")
    f.flush()

    args = [PYTHON, "-u", SCRIPT,
            "--id", mid, "--url", url,
            "--duration", str(int(duration)),
            "--token", str(token), "--chat-id", str(chat_id)]
    if sid:
        args += ["--sid", str(sid)]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=f, stderr=asyncio.subprocess.STDOUT, cwd=BASE_DIR)
    f.close()

    _running[mid] = proc
    _meta[mid] = {"url": url, "duration": int(duration), "sid": sid,
                  "started": datetime.now(TZ).strftime("%H:%M:%S")}
    asyncio.create_task(_watch(mid, proc, sid))
    return "Запущено: " + mid


async def _watch(mid, proc, sid):
    try:
        rc = await proc.wait()
    except Exception:
        rc = -1
    finally:
        _running.pop(mid, None)
        _meta.pop(mid, None)

    if rc == 0:
        if sid:
            try:
                open(_done_path(sid), "w").close()
            except Exception:
                pass
        return

    if rc == RC_SKIP_NOBODY:
        key = "skip_" + str(sid or mid)
        if key not in _notified:
            _notified.add(key)
            asyncio.create_task(_notify(
                "⏳ " + mid + ": в комнате пока никого нет — не вхожу, повторю позже"))
        return

    key = "fail_" + str(sid or mid) + "_" + str(rc)
    if key not in _notified:
        _notified.add(key)
        asyncio.create_task(_notify(
            "⚠️ " + mid + ": вход не удался (код " + str(rc) +
            "), повторю попытку. Скриншот — в storage/debug/"))


async def stop_session(mid):
    mid = str(mid)
    proc = _running.get(mid)
    if not proc:
        return "Не запущено: " + mid
    try:
        open(_flag_path(mid), "w").close()
    except Exception:
        pass
    for _ in range(15):
        await asyncio.sleep(1)
        if proc.returncode is not None:
            break
    if proc.returncode is None:
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        await asyncio.sleep(3)
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
    _running.pop(mid, None)
    _meta.pop(mid, None)
    return "Остановлено: " + mid


async def stop_all():
    res = []
    for mid in list(_running.keys()):
        res.append(await stop_session(mid))
    return "; ".join(res) if res else "Нет активных сессий"


def running_ids():
    return list(_running.keys())


def running_info():
    return [{"id": k, **v} for k, v in _meta.items()]

