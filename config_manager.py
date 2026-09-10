import os

import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def find_meeting(cfg, mid):
    for m in cfg.get("meetings") or []:
        if m.get("id") == mid:
            return m
    return None


def find_entry(cfg, sid):
    for e in cfg.get("schedule") or []:
        if str(e.get("sid")) == str(sid):
            return e
    return None


def add_schedule_entry(cfg, entry):
    cfg.setdefault("schedule", []).append(entry)
    save_config(cfg)


def del_schedule_entry(cfg, sid):
    sched = cfg.get("schedule") or []
    before = len(sched)
    cfg["schedule"] = [e for e in sched if str(e.get("sid")) != str(sid)]
    save_config(cfg)
    return len(cfg["schedule"]) != before


def clear_schedule(cfg):
    cfg["schedule"] = []
    save_config(cfg)

