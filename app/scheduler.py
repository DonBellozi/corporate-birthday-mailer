import hashlib
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from .db import SessionLocal
from .models import AuditLog
from .settings_service import get_all_settings, set_settings
from .services import todays_employees, send_birthday, import_xlsx
from .mail_service import fetch_latest_xlsx_if_new

scheduler = BackgroundScheduler()
_last_send_day = None
_last_imap_check = None

def _check_mail(db, cfg):
    """
    Сначала проверяет только UID подходящих IMAP-писем.

    Уже обработанный UID не скачивается повторно. Для нового письма XLSX
    скачивается один раз, после чего дополнительно проверяется SHA-256.
    Хэш остается вторым уровнем защиты на случай, если новое письмо содержит
    то же самое вложение.
    """
    try:
        result = fetch_latest_xlsx_if_new(
            cfg,
            Path("/app/data/imports"),
            last_uid=cfg.get("imap_last_message_uid", ""),
            last_uidvalidity=cfg.get("imap_uidvalidity", ""),
        )

        if not result["new_message"]:
            return

        checked_state = {
            "imap_last_message_uid": result.get("uid", ""),
            "imap_uidvalidity": result.get("uidvalidity", ""),
        }

        path = result.get("path")
        if not path:
            # Письмо подходит под фильтр, но XLSX в нем нет. Запоминаем UID,
            # чтобы не скачивать это же письмо снова при следующем опросе.
            set_settings(db, checked_state)
            return

        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if file_hash == cfg.get("imap_last_file_hash", ""):
            path.unlink(missing_ok=True)
            set_settings(db, {
                **checked_state,
                "imap_last_file_hash": file_hash,
            })
            return

        # UID фиксируем только после успешного импорта. Если импорт упал
        # из-за временной ошибки, это же письмо будет безопасно повторено
        # на следующем опросе вместо того, чтобы потеряться.
        import_xlsx(db, path, source="imap")
        set_settings(db, {
            **checked_state,
            "imap_last_file_hash": file_hash,
        })
    except Exception as exc:
        db.add(AuditLog(actor="scheduler", action="imap_fetch_failed", details=str(exc)))
        db.commit()

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
            _check_mail(db, cfg)
            cfg = get_all_settings(db)

        if cfg.get("auto_send_enabled", "false").lower() != "true":
            return

        try:
            hh, mm = map(int, cfg.get("send_time", "09:15").split(":"))
        except Exception:
            return

        day_key = now.date().isoformat()
        target_minutes = hh * 60 + mm
        now_minutes = now.hour * 60 + now.minute

        # Сравниваем "не раньше назначенного времени", а не "ровно в эту
        # минуту": если тик пропустит точную минуту (перезапуск контейнера,
        # долгий IMAP-запрос на этом же тике), рассылка все равно уйдет при
        # следующем тике в тот же день, а не пропустится до завтра.
        if now_minutes >= target_minutes and _last_send_day != day_key:
            _last_send_day = day_key
            for emp in todays_employees(db, cfg=cfg):
                send_birthday(db, emp, actor="scheduler")

def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(tick, "interval", minutes=1, id="main_tick", replace_existing=True)
        scheduler.start()
