from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from .db import SessionLocal
from .settings_service import get_all_settings
from .services import todays_employees, send_birthday, import_xlsx
from .mail_service import fetch_latest_xlsx

scheduler = BackgroundScheduler()
_last_send_day = None
_last_imap_check = None

def tick():
    global _last_send_day, _last_imap_check
    now = datetime.now()
    with SessionLocal() as db:
        cfg = get_all_settings(db)

        poll = max(1, int(cfg.get("imap_poll_minutes", "15") or "15"))
        if cfg.get("imap_host") and (
            _last_imap_check is None or (now - _last_imap_check).total_seconds() >= poll * 60
        ):
            _last_imap_check = now
            try:
                path = fetch_latest_xlsx(cfg, Path("/app/data/imports"))
                if path:
                    import_xlsx(db, path, source="imap")
            except Exception:
                pass

        if cfg.get("auto_send_enabled", "false").lower() != "true":
            return

        try:
            hh, mm = map(int, cfg.get("send_time", "09:15").split(":"))
        except Exception:
            return

        day_key = now.date().isoformat()
        if now.hour == hh and now.minute == mm and _last_send_day != day_key:
            _last_send_day = day_key
            for emp in todays_employees(db):
                send_birthday(db, emp, actor="scheduler")

def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(tick, "interval", minutes=1, id="main_tick", replace_existing=True)
        scheduler.start()
