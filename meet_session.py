#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
meet_session.py — участие бота в одной встрече Google Meet.

Запуск как subprocess:
    --id --url --duration (мин) --token --chat-id [--sid] [--max-overrun]

Особенности:
  * Один общий профиль chrome_profile => одновременно только ОДНА встреча.
    За это отвечает sessions.py, он просто не запускает вторую.
  * Вход ТОЛЬКО через «Подключиться также на этом устройстве».
  * Если на экране предвхода «Пока никого нет» — не входим, код 99.
  * Выход: истекло время И организатора нет в звонке.

Коды возврата:
    0  — штатно отработали (пишется маркер done, повторно не заходим)
    1  — ошибка входа (планировщик повторит)
    99 — в комнате никого нет (планировщик повторит)
"""

import argparse
import html
import logging
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "chrome_profile")
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
DEBUG_DIR = os.path.join(STORAGE_DIR, "debug")
CHROME_BIN = "/usr/bin/google-chrome"

TZ = ZoneInfo("Europe/Kiev")

RC_OK = 0
RC_FAIL = 1
RC_SKIP_NOBODY = 99

JOIN_WAIT_SEC = 90
JOIN_CONFIRM_SEC = 45
QUEUED_WAIT_SEC = 300
LOOP_SLEEP_SEC = 20
SUBS_DIR = "storage/subs"
CAP_FLUSH_SEC = 10

ROSTER_TG_SEC = 300


# ---------- Логирование с киевским временем ----------

class KievFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(KievFormatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))
logging.root.handlers[:] = [_handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("meet")


def log(prefix, msg):
    logger.info("[%s] %s", prefix, msg)


def now_kiev():
    return datetime.now(TZ).strftime("%H:%M:%S")


def send_tg(token, chat_id, text):
    try:
        requests.post(
            "https://api.telegram.org/bot" + str(token) + "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        logger.warning("TG send error: %s", e)


# ---------- Маркеры текста страницы ----------

NOBODY_MARKERS = [
    "пока никого нет", "пока никого здесь нет", "здесь пока никого",
    "пока нет никого", "никого нет",
    "поки нікого немає", "поки никого немає", "нікого ще немає",
    "no one else is here", "nobody else is here", "no one is here yet",
]

ENDED_MARKERS = [
    "звонок заверш", "видеовстреча заверш", "встреча заверш", "зустріч заверш",
    "организатор завершил", "організатор завершив",
    "call ended", "meeting ended", "the call has ended",
    "вы покинули", "ви покинули", "вас удалено", "вас видалено",
    "участники были удалены",
]

LOGIN_MARKERS = [
    "выполните вход", "увійдіть у свій", "увійдіть в свій",
    "sign in to continue", "введите адрес электронной почты",
    "choose an account", "выберите аккаунт", "choosing an account",
]

QUEUED_MARKERS = [
    "попросить присоединиться", "попросити приєднатися",
    "ждет, пока", "ждём, пока", "очекування", "очередь",
    "организатор решит", "організатор вирішить",
    "waiting to join", "ask to join",
]

ORG_MARKERS = ["организатор", "організатор", "organizer"]

JOIN_ALSO_LABELS = [
    "Подключиться также на этом устройстве",
    "Підключитися також на цьому пристрої",
    "Также на этом устройстве",
    "Also join on this device",
    "Join on this device",
]

JOIN_PRIMARY_LABELS = [
    "Присоединиться сейчас", "Приєднатися зараз",
    "Присоединиться", "Приєднатися",
    "Join now",
    "Попросить присоединиться", "Попросити приєднатися", "Ask to join",
]

SWITCH_DEVICE_LABELS = [
    "Присоединиться также на этом устройстве",
    "Приєднатися також на цьому пристрої",
    "Подключиться также на этом устройстве",
    "Join also on this device",
    "Use this device also",
    "Присоединиться как ещё один участник",
    "Join as an additional participant",
]

IN_CALL_SELECTORS = [
    "button[jsname='CQylAd']",
    "button[aria-label*='покинуть' i]",
    "button[aria-label*='завершить вызов' i]",
    "button[aria-label*='вийти' i]",
    "button[aria-label*='leave' i]",
    "button[aria-label*='hang up' i]",
]

LEAVE_SELECTORS = IN_CALL_SELECTORS + [
    "button[aria-label*='завершить' i]",
]

LEAVE_CONFIRM_LABELS = [
    "Выйти", "Покинуть", "Завершить", "Вийти", "Покинути",
    "Leave", "Leave call", "End call",
]


def _match(text, markers):
    low = (text or "").lower()
    for m in markers:
        if m in low:
            return m
    return None


# ---------- Очистка имён ----------

SKIP_WORDS = [
    "more_vert", "keep_off", "keep_outline", "mic_off", "mic_none",
    "videocam_off", "videocam", "push_pin", "volume_up", "volume_off",
    "delete_outline", "person_remove", "open_in_new",
    "еще варианты", "ещё варианты",
    "дополнительные действия", "невозможно отключить",
    "disable_other_mics",
    "организатор", "organizer", "організатор",
    "закрепить", "открепить", "удалить",
    "изображение", "презентацию", "картинку",
    "главном экране", "экрана",
    "пользователя",
]

EXACT_SKIP = {
    "организатор", "організатор", "organizer", "host", "ведущий",
    "вы", "ви", "участники", "люди", "все", "в эфире", "чат",
}

BLOCKED_SUBSTRINGS = [
    "закрепить изображение",
    "открепить презентацию",
    "удалить эту картинку",
    "на главном экране",
    "с главного экрана",
]


def _is_icon(s):
    s = (s or "").strip()
    if not s:
        return True
    low = s.lower()
    for blocked in BLOCKED_SUBSTRINGS:
        if blocked in low:
            return True
    if low in EXACT_SKIP:
        return True
    if len(s) > 40 and any(w.lower() in low for w in SKIP_WORDS):
        return True
    if re.fullmatch(r"[a-z_]+", s):
        return True
    return False


def _clean_name(s):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = s.split(":", 1)[0].strip()
    s = s.replace("(Вы)", "").replace("(Ви)", "").strip()
    return s


def _extract_participant_info(inner_text, el=None):
    """Возвращает dict {'name': str} или None."""
    if el is not None:
        try:
            aria = _clean_name(el.get_attribute("aria-label") or "")
            if aria and not _is_icon(aria) and len(aria) >= 3:
                if re.search(r"[a-zа-яіїєґ]", aria.lower()):
                    return {"name": aria}
        except Exception:
            pass
    candidates = []
    for line in (inner_text or "").split("\n"):
        nm = _clean_name(line)
        if not nm or _is_icon(nm):
            continue
        if len(nm) < 3 or len(nm) > 45:
            continue
        if not re.search(r"[a-zа-яіїєґ]", nm.lower()):
            continue
        candidates.append(nm)
    if not candidates:
        return None
    return {"name": max(candidates, key=len)}


# ---------- Страница: текст, состояние ----------

def _page_text(page):
    try:
        return page.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:
        try:
            return page.inner_text("body", timeout=2000) or ""
        except Exception:
            return ""


def _in_call(page):
    for sel in IN_CALL_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _click_label(page, labels):
    """Клик по aria-label / role=button / text. Порядок важен."""
    for lab in labels:
        # 1) exact aria-label (Google Meet диалоги)
        try:
            loc = page.locator(f'[aria-label="{lab}"]').first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=4000)
                log("join", "Нажато (aria): " + lab)
                return lab
        except Exception:
            pass
        # 2) partial aria-label
        try:
            loc = page.locator(f'[aria-label*="{lab}"]').first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=4000)
                log("join", "Нажато (aria*): " + lab)
                return lab
        except Exception:
            pass
        # 3) role=button accessible name
        try:
            loc = page.get_by_role("button", name=lab, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=4000)
                log("join", "Нажато (role): " + lab)
                return lab
        except Exception:
            pass
        # 3.5) JS click directly on button by textContent (bypasses pointer-events:none inner spans)
        try:
            js = """(t) => {
                for (const el of document.querySelectorAll('button, [role="button"]')) {
                    if (el.textContent.trim().indexOf(t) !== -1) { el.click(); return true; }
                }
                return false;
            }"""
            if page.evaluate(js, lab):
                log("join", "Нажато (js): " + lab)
                return lab
        except Exception:
            pass
        # 4) locator text= (div/button без role)
        try:
            loc = page.locator(f'text={lab}').first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=4000)
                log("join", "Нажато (text): " + lab)
                return lab
        except Exception:
            pass
    return None

def _mute_mic_cam(page, stage):
    for sel, name, js_kw in [
        ("[aria-label*='микрофон' i], [aria-label*='microphone' i], [aria-label*='мікрофон' i]", "mic", "микрофон"),
        ("[aria-label*='камера' i], [aria-label*='camera' i], [aria-label*='камеру' i]", "cam", "камера"),
    ]:
        done = False
        # 1) Playwright aria-label click
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                pressed = el.get_attribute("aria-pressed")
                if pressed == "true" or pressed is None:
                    el.click(timeout=2500)
                done = True
                log(stage, name + " выключен")
                continue
        except Exception:
            pass
        # 2) JS fallback (handles dynamic toolbar)
        try:
            js = """(kw) => {
                const a = Array.from(document.querySelectorAll('button, [role="button"]'));
                const el = a.find(b => b.getAttribute('aria-label') && b.getAttribute('aria-label').toLowerCase().includes(kw));
                if (el) { el.click(); return true; }
                return false;
            }"""
            if page.evaluate(js, js_kw):
                log(stage, name + " выключен (js)")
                done = True
                continue
        except Exception:
            pass
        # 3) Keyboard fallback
        if not done:
            try:
                if name == "mic":
                    page.keyboard.press("Control+d")
                else:
                    page.keyboard.press("Control+e")
                log(stage, name + " выключен (hotkey)")
            except Exception:
                pass


def _dump_debug(page, mid, tag):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        base = os.path.join(DEBUG_DIR, mid + "_" + stamp + "_" + tag)
        page.screenshot(path=base + ".png")
        with open(base + ".html", "w", encoding="utf-8") as fh:
            fh.write(page.content())
        log("debug", "Скриншот и HTML сохранены: " + base + ".png")
        return base + ".png"
    except Exception as e:
        logger.warning("debug dump error: %s", e)
        return None


# ---------- Ростер ----------

def _people_button(page):
    try:
        btns = page.locator("button, div[role='button']")
        n = btns.count()
        for i in range(n):
            el = btns.nth(i)
            try:
                txt = (el.inner_text(timeout=700) or "").strip()
            except Exception:
                continue
            if re.fullmatch(r"\d{1,3}", txt):
                return el, int(txt)
    except Exception as e:
        logger.warning("people button error: %s", e)
    return None, 0


def _open_participant_panel(page):
    el, _ = _people_button(page)
    if el is None:
        return False
    try:
        el.click(timeout=2500)
        time.sleep(1.5)
        return True
    except Exception:
        return False


def _count_participants(page):
    best = 0
    for _ in range(2):
        _, num = _people_button(page)
        best = max(best, num)
        time.sleep(0.4)
    try:
        best = max(best, page.locator("[data-participant-id]").count())
    except Exception:
        pass
    return best


def _scan_roster(page, text=None):
    """Один проход: (имена, организатор_в_звонке, имя_организатора)."""
    names = set()
    org_present = False
    org_name = None

    if text is None:
        text = _page_text(page)
    low = text.lower()
    for m in ORG_MARKERS:
        if m in low:
            org_present = True
            break

    try:
        rows = page.locator("[data-participant-id]")
        if rows.count() == 0:
            _open_participant_panel(page)
            rows = page.locator("[data-participant-id]")
        n = rows.count()
        for i in range(n):
            el = rows.nth(i)
            try:
                txt = el.inner_text(timeout=1200) or ""
            except Exception:
                continue
            info = _extract_participant_info(txt, el)
            if info and info.get("name"):
                names.add(info["name"])
                if "орган" in txt.lower():
                    org_present = True
                    if not org_name:
                        org_name = info["name"]
    except Exception as e:
        logger.warning("roster scan error: %s", e)

    # Очистка UI-артефактов
    _skip_ui = ("Показать", "Вы", "Ви", "К началу", "До початку", "Присоединиться", "Подключиться")
    names = {n for n in names if n and n.strip() and not any(s in n for s in _skip_ui)}
    return names, org_present, org_name


# ---------- Вход ----------

def _enable_captions(page, mid):
    labels = ["Turn on captions", "Включить субтитры", "Увімкнути субтитри"]
    off_labels = ["Turn off captions", "Выключить субтитры", "Вимкнути субтитри"]
    for label in off_labels:
        try:
            if page.locator(f'button[aria-label*="{label}"]').count() > 0:
                log(mid, "Субтитры уже включены")
                return True
        except Exception:
            pass
    for label in labels:
        try:
            btn = page.locator(f'button[aria-label*="{label}"]').first
            if btn.is_visible(timeout=3000):
                btn.click(timeout=2000)
                log(mid, "Включил субтитры")
                return True
        except Exception:
            pass
    return False


def _inject_caption_collector(page):
    return page.evaluate(r"""
    () => {
        if (window.__capObs) { window.__capObs.disconnect(); window.__capObs = null; }
        window.__captions = [];
        const cap = document.querySelector('[jsname="dSyhDe"]') || document.querySelector('[aria-label*="aption"]');
        if (!cap) return "NO_CONTAINER";
        const seen = new Set();
        const collect = (root) => {
            const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
            let n;
            while (n = tw.nextNode()) {
                const t = n.textContent.trim();
                if (t.length >= 3 && t.length <= 300 && !seen.has(t)) {
                    seen.add(t);
                    window.__captions.push(t);
                }
            }
        };
        collect(cap);
        const obs = new MutationObserver((muts) => {
            for (const m of muts) {
                if (m.type === 'characterData') {
                    const t = m.target.textContent.trim();
                    if (t.length >= 3 && t.length <= 300 && !seen.has(t)) {
                        seen.add(t);
                        window.__captions.push(t);
                    }
                } else if (m.type === 'childList') {
                    for (const node of m.addedNodes) {
                        if (node.nodeType === Node.ELEMENT_NODE) collect(node);
                    }
                }
            }
        });
        obs.observe(cap, { childList: true, subtree: true, characterData: true });
        window.__capObs = obs;
        return "OK";
    }
    """)
def _flush_captions(page, mid, token=None, chat_id=None):
    try:
        arr = page.evaluate("window.__captions || []")
        if arr:
            page.evaluate("window.__captions = []")
            os.makedirs(SUBS_DIR, exist_ok=True)
            path = os.path.join(SUBS_DIR, mid + ".txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(arr) + "\n")
            if token and chat_id:
                send_tg(token, chat_id, "\u270f\ufe0f \u0421\u0443\u0431\u0442\u0438\u0442\u0440\u044b +" + str(len(arr)) + " \u0441\u0442\u0440\u043e\u043a, \u0444\u0430\u0439\u043b: " + path)
            return len(arr)
    except Exception as e:
        log("meet", "Caption flush error: " + str(e))
    return 0

def _ensure_media_off(page, mid):
    prefixes = ("Выключить ", "Вимкнути ", "Turn off ", "Отключить ")
    devices = ("камеру", "микрофон", "camera", "microphone", "мікрофон")
    found = False
    for p in prefixes:
        for d in devices:
            label = p + d
            try:
                btn = page.locator(f'button[aria-label*="{label}"]').first
                if btn.is_visible(timeout=3000):
                    btn.click(timeout=2000)
                    log(mid, f"Отключил: {label}")
                    found = True
            except Exception:
                pass
            try:
                btn = page.locator(f'div[role="button"][aria-label*="{label}"]').first
                if btn.is_visible(timeout=3000):
                    btn.click(timeout=2000)
                    log(mid, f"Отключил (div): {label}")
                    found = True
            except Exception:
                pass
    if not found:
        try:
            page.keyboard.press("Control+e")
            page.keyboard.press("Control+d")
            log(mid, "Отправлены Ctrl+E и Ctrl+D (fallback)")
        except Exception as e:
            log(mid, "Fallback не сработал: " + str(e))


def _wait_and_join(page, mid):
    _ensure_media_off(page, mid)
    """Терпеливый вход. Возвращает (rc, причина)."""
    started = time.time()
    clicked = None
    switch_clicked = None
    confirm_deadline = None

    while True:
        now = time.time()
        text = _page_text(page)

        login_hit = _match(text, LOGIN_MARKERS)
        if login_hit:
            _dump_debug(page, mid, "login")
            return RC_FAIL, "Профиль разлогинен (найдено: " + login_hit + ")"

        if _match(text, ENDED_MARKERS):
            return RC_OK, "Встреча уже завершена"

        if _in_call(page):
            if clicked:
                return RC_OK, "Вошёл через «" + clicked + "»"
            return RC_OK, "Уже в звонке"

        nobody = _match(text, NOBODY_MARKERS)
        if nobody and clicked is None:
            return RC_SKIP_NOBODY, "На экране предвхода «Пока никого нет»"

        if clicked is None:
            if now - started > JOIN_WAIT_SEC:
                _dump_debug(page, mid, "no_join_button")
                return RC_FAIL, "Кнопка входа не найдена за " + str(JOIN_WAIT_SEC) + " с"
            clicked = _click_label(page, JOIN_ALSO_LABELS)
            if clicked is None:
                clicked = _click_label(page, JOIN_PRIMARY_LABELS)
                if clicked:
                    page.wait_for_timeout(3000)
                    clicked2 = _click_label(page, JOIN_ALSO_LABELS)
                    if not clicked2:
                        clicked2 = _click_label(page, SWITCH_DEVICE_LABELS)
                    if clicked2:
                        clicked = clicked2
            if clicked:
                confirm_deadline = time.time() + JOIN_CONFIRM_SEC
        else:
            if _match(text, QUEUED_MARKERS):
                confirm_deadline = max(confirm_deadline, time.time() + QUEUED_WAIT_SEC)
                log("join", "Мы в очереди на допуск организатором — жду")
            if not switch_clicked:
                switch_clicked = _click_label(page, JOIN_ALSO_LABELS + SWITCH_DEVICE_LABELS)
                if switch_clicked:
                    confirm_deadline = time.time() + JOIN_CONFIRM_SEC
                    log("join", "Dialog pressed: " + switch_clicked)
            if time.time() > confirm_deadline:
                if _match(text, NOBODY_MARKERS):
                    return RC_SKIP_NOBODY, "После клика комната оказалась пустой"
                _dump_debug(page, mid, "join_failed")
                return RC_FAIL, "Кнопка «" + clicked + "» нажата, но вход не подтвердился"

        time.sleep(2)


# ---------- Выход ----------

def _leave_call(page):
    for sel in LEAVE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(timeout=2500)
            log("leave", "Кнопка выхода нажата: " + sel)
            break
        except Exception:
            continue
    time.sleep(1.5)
    _click_label(page, LEAVE_CONFIRM_LABELS)
    time.sleep(1.5)


# ---------- Основная сессия ----------

def run_session(meet_id, url, duration_min, token, chat_id, sid=None, max_overrun_min=20):
    prefix = meet_id or "meet"
    os.makedirs(STORAGE_DIR, exist_ok=True)
    stop_flag = os.path.join(STORAGE_DIR, "stop_" + str(meet_id) + ".flag")
    if os.path.exists(stop_flag):
        os.remove(stop_flag)

    log(prefix, "Открываю " + url + " (sid=" + str(sid) + ", duration=" + str(duration_min) + " мин)")
    send_tg(token, chat_id, "🚀 Вхожу во встречу\n<code>" + html.escape(url) + "</code>")

    reason = "неизвестно"
    rc = RC_OK
    total = 0
    organizer = None
    names = set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            executable_path=CHROME_BIN,
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--window-size=1280,800",
            ],
        )
        page = None
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=60000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            time.sleep(4)

            _mute_mic_cam(page, "prep")

            rc, reason = _wait_and_join(page, prefix)
            log(prefix, "Вход: rc=" + str(rc) + " — " + reason)

            if rc != RC_OK:
                ctx.close()
                return rc

            _mute_mic_cam(page, "post")
            time.sleep(5)
            log(prefix, "Вошёл в звонок в " + now_kiev())

            _open_participant_panel(page)
            names, org_present, org_name = _scan_roster(page)
            if not org_name:
                for attempt in range(8):
                    time.sleep(2)
                    names2, org_present2, org_name = _scan_roster(page)
                    names |= names2
                    org_present = org_present or org_present2
                    if org_name:
                        break
                    log(prefix, "Организатор ещё не виден, попытка " + str(attempt + 1) + "/8")

            organizer = org_name
            total = len(names) if names else _count_participants(page)
            names_list = sorted(names)
            log(prefix, "Всего: " + str(total) + ", уникальных имён: " + str(len(names)))
            log(prefix, "Организатор: " + str(organizer or "не найден"))
            log(prefix, "Имена: " + ", ".join(names_list))
            cap_ok = _enable_captions(page, prefix)

            log(prefix, 'Captions enabled: ' + str(cap_ok))

            for attempt in range(5):

                res = _inject_caption_collector(page)

                log(prefix, 'Caption collector attempt ' + str(attempt + 1) + ': ' + str(res))

                if res == 'OK':

                    break

                time.sleep(3)



            msg = "✅ Вошёл во встречу в " + now_kiev() + "\n👥 Участников: " + str(total)
            if organizer:
                msg += "\n👑 Организатор: <b>" + html.escape(organizer) + "</b>"
            else:
                msg += "\n👑 Организатор: не определён"
            if names_list:
                msg += "\n📋 Список:\n" + "\n".join("• " + html.escape(n) for n in names_list)
            msg += "\n⏱ Буду сидеть " + str(int(duration_min)) + " мин, выйду когда время истечёт и орга не будет"
            send_tg(token, chat_id, msg)

            # ---- Основной цикл ----
            deadline = time.time() + duration_min * 60
            hard_cap = deadline + max_overrun_min * 60
            reported_names = set(names)
            last_roster_tg = time.time()

            while True:
                _cap_counter = 0
                now = time.time()

                if os.path.exists(stop_flag):
                    reason = "стоп по команде из Telegram"
                    send_tg(token, chat_id, "🛑 Остановлено по команде из Telegram")
                    break

                text = _page_text(page)
                ended = _match(text, ENDED_MARKERS)
                if ended:
                    reason = "звонок закрыт (" + ended + ")"
                    break

                names_now, org_present, org_name = _scan_roster(page, text)
                if org_name:
                    organizer = org_name
                if names_now:
                    names |= names_now
                total = len(names_now) if names_now else _count_participants(page)

                left_sec = int(max(0, deadline - now))
                log("meet", "Участников: " + str(total) +
                    " | орг: " + ("в звонке" if org_present else "НЕТ") +
                    " | до конца времени: " + str(left_sec) + " с")

                if now >= deadline:
                    if not org_present:
                        reason = "истекло время и организатора нет"
                        break
                    if now >= hard_cap and not org_present:
                        reason = ("истекло время, орг ещё в звонке, "
                                  "достигнут лимит +" + str(int(max_overrun_min)) + " мин")
                        break
                    log("meet", "Время вышло, но организатор в звонке — жду его выхода")
                else:
                    if names_now != reported_names and (now - last_roster_tg) >= ROSTER_TG_SEC:
                        came = sorted(names_now - reported_names)
                        gone = sorted(reported_names - names_now)
                        parts = []
                        if came:
                            parts.append("➕ пришли: " + ", ".join(came))
                        if gone:
                            parts.append("➖ вышли: " + ", ".join(gone))
                        if parts:
                            send_tg(token, chat_id,
                                    "🔄 " + prefix + " | 👥 " + str(total) + "\n" +
                                    "\n".join(html.escape(x) for x in parts))
                        reported_names = set(names_now)
                        last_roster_tg = now

                _cap_counter += LOOP_SLEEP_SEC
                if _cap_counter >= CAP_FLUSH_SEC:
                    n = _flush_captions(page, prefix)
                    if n:
                        log(prefix, "Субтитры: +" + str(n) + " строк")
                    _cap_counter = 0

                time.sleep(LOOP_SLEEP_SEC)

            _leave_call(page)
        except Exception as e:
            logger.exception("session error")
            if page is not None:
                _dump_debug(page, prefix, "exception")
            send_tg(token, chat_id, "❌ Сбой сессии " + html.escape(prefix) + ": " + html.escape(str(e))[:300])
            try:
                ctx.close()
            except Exception:
                pass
            return RC_FAIL
        try:
            ctx.close()
        except Exception:
            pass

    if os.path.exists(stop_flag):
        try:
            os.remove(stop_flag)
        except Exception:
            pass

    log(prefix, "Выход: " + reason + " в " + now_kiev())
    send_tg(token, chat_id,
            "📴 Вышел из встречи в " + now_kiev() +
            "\nПричина: " + html.escape(reason) +
            "\n👥 На выходе: " + str(total) +
            "\n👑 Организатор: " + html.escape(organizer or "не определён"))
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--token", required=True)
    ap.add_argument("--chat-id", required=True)
    ap.add_argument("--sid", default=None)
    ap.add_argument("--max-overrun", type=float, default=20,
                    help="сколько минут максимум досиживаем после истечения времени, если орг ещё в звонке")
    args = ap.parse_args()

    try:
        rc = run_session(args.id, args.url, args.duration, args.token,
                         args.chat_id, sid=args.sid, max_overrun_min=args.max_overrun)
    except Exception as e:
        logger.exception("fatal")
        try:
            send_tg(args.token, args.chat_id,
                    "❌ Критическая ошибка " + html.escape(str(args.id)) + ": " + html.escape(str(e))[:300])
        except Exception:
            pass
        rc = RC_FAIL
    sys.exit(int(rc))


if __name__ == "__main__":
    main()

