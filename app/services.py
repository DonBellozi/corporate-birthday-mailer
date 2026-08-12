from datetime import date, datetime
from pathlib import Path
import random
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import EmployeeSnapshot, ImportRun, IntroTemplate, PositionMapping, WishTemplate, MailLog, AuditLog
from .settings_service import get_all_settings
from .xlsx_parser import parse_workbook
from .rendering import variable_context, render_text, email_html
from .mail_service import send_html_mail
from .position_suggester import suggest_position

def latest_successful_import(db):
    return db.scalars(
        select(ImportRun).where(ImportRun.success == True).order_by(desc(ImportRun.received_at))
    ).first()

def import_xlsx(db: Session, path: Path, source="manual"):
    cfg = get_all_settings(db)
    run = ImportRun(filename=path.name, source=source, success=False)
    db.add(run); db.commit(); db.refresh(run)
    try:
        rows = parse_workbook(path, cfg)
        run.rows_total = len(rows)

        if not rows:
            raise ValueError(
                "Заголовки XLSX найдены, но строки сотрудников отсутствуют. "
                "Проверьте, что из 1С выгружен отчет с детализацией по сотрудникам, "
                "а не только структура подразделений."
            )

        valid = 0
        known_positions = {x for x in db.scalars(select(PositionMapping.source_position)).all()}
        for item in rows:
            if item.get("error"):
                continue
            db.add(EmployeeSnapshot(
                import_id=run.id,
                employee_key=item["employee_key"],
                fio=item["fio"],
                birthday_day=item["birthday_day"],
                birthday_month=item["birthday_month"],
                gender=item["gender"],
                hide_birthday=item["hide_birthday"],
                source_position=item["source_position"],
            ))
            valid += 1
            source_position = item.get("source_position") or ""
            if source_position and source_position not in known_positions:
                db.add(PositionMapping(source_position=source_position, display_position="", active=True, confirmed=False))
                known_positions.add(source_position)
        run.rows_valid = valid
        run.success = True
        db.commit()
        return run
    except Exception as exc:
        run.error_text = str(exc)
        run.success = False
        db.commit()
        raise

def todays_employees(db):
    latest = latest_successful_import(db)
    if not latest:
        return []
    today = date.today()
    return list(db.scalars(
        select(EmployeeSnapshot).where(
            EmployeeSnapshot.import_id == latest.id,
            EmployeeSnapshot.birthday_day == today.day,
            EmployeeSnapshot.birthday_month == today.month,
        ).order_by(EmployeeSnapshot.fio)
    ).all())


def upcoming_birthdays(db, days=30):
    """
    Возвращает дни рождения после сегодняшней даты на следующие `days` дней.
    Работает по дню/месяцу, поэтому корректно проходит через границу года.
    Сегодняшние именинники сюда не попадают.
    """
    latest = latest_successful_import(db)
    if not latest:
        return []

    from datetime import timedelta

    today = date.today()
    employees = list(db.scalars(
        select(EmployeeSnapshot).where(
            EmployeeSnapshot.import_id == latest.id,
        )
    ).all())

    date_map = {}
    for offset in range(1, days + 1):
        target = today + timedelta(days=offset)
        date_map[(target.day, target.month)] = (target, offset)

    result = []
    for emp in employees:
        key = (emp.birthday_day, emp.birthday_month)
        match = date_map.get(key)
        if not match:
            continue

        next_date, days_left = match
        result.append({
            "employee": emp,
            "next_date": next_date,
            "days_left": days_left,
        })

    result.sort(key=lambda item: (item["next_date"], item["employee"].fio))
    return result

def choose_least_used(items):
    if not items:
        return None
    minimum = min(x.usage_count for x in items)
    return random.choice([x for x in items if x.usage_count == minimum])

def choose_intro(db):
    return choose_least_used(list(db.scalars(
        select(IntroTemplate).where(IntroTemplate.active == True)
    ).all()))

def choose_wish(db, gender):
    return choose_least_used(list(db.scalars(
        select(WishTemplate).where(
            WishTemplate.active == True,
            WishTemplate.gender.in_([gender, "universal"]),
        )
    ).all()))

def get_position(db, source_position):
    """
    Приоритет:
      1. подтвержденная оператором должность;
      2. автоматическое предложение с высокой уверенностью;
      3. иначе должность не вставляется.
    """
    if not source_position:
        return ""

    item = db.scalar(select(PositionMapping).where(
        PositionMapping.source_position == source_position,
    ))

    if item and not item.active:
        return ""

    if item and item.confirmed and item.display_position.strip():
        return item.display_position.strip()

    suggestion = suggest_position(source_position)
    return suggestion.text if suggestion.auto_use else ""

def compose_birthday_message(db, emp):
    """
    Собирает письмо без отправки и без изменения счетчиков использования.

    Эту же функцию используют предпросмотр и реальная отправка, чтобы
    оператор видел тот же результат, который формирует почтовая логика.
    """
    cfg = get_all_settings(db)

    intro = choose_intro(db)
    if not intro:
        raise ValueError("Нет активного текста поздравления")

    position = (
        get_position(db, emp.source_position)
        if cfg.get("positions_enabled") == "true"
        else ""
    )

    ctx = variable_context({
        "fio": emp.fio,
        "birthday_day": emp.birthday_day,
        "birthday_month": emp.birthday_month,
        "gender": emp.gender,
    }, position)

    intro_text = render_text(intro.body, ctx)

    wish = None
    wish_text = ""
    if cfg.get("wishes_enabled") == "true":
        wish = choose_wish(db, emp.gender)
        if wish:
            wish_text = render_text(wish.body, ctx)

    recipient = cfg.get("mail_recipient", "").strip()
    subject = cfg.get("mail_subject", "Поздравляем с Днем рождения!").strip()

    rendered = (
        intro_text
        + ("\n" + position if position else "")
        + ("\n" + wish_text if wish_text else "")
    )

    html_body = email_html(
        intro_text,
        wish_text,
        position,
        emp.gender,
    )

    return {
        "cfg": cfg,
        "intro": intro,
        "wish": wish,
        "intro_text": intro_text,
        "wish_text": wish_text,
        "position": position,
        "recipient": recipient,
        "subject": subject,
        "rendered": rendered,
        "html": html_body,
    }


def build_birthday_preview(db, emp):
    message = compose_birthday_message(db, emp)
    return {
        "fio": emp.fio,
        "subject": message["subject"],
        "recipient": message["recipient"],
        "position": message["position"],
        "intro_name": message["intro"].name if message["intro"] else "",
        "wish_name": message["wish"].name if message["wish"] else "",
        "html": message["html"],
        "excluded": bool(emp.hide_birthday),
        "warning": (
            "Сотрудник исключен из поздравлений признаком «Скрыть день рождения»."
            if emp.hide_birthday
            else ""
        ),
    }


def send_birthday(db, emp, actor="scheduler"):
    if emp.hide_birthday:
        return False, "Работник исключен из поздравлений"

    year = date.today().year
    old = db.scalar(select(MailLog).where(
        MailLog.employee_key == emp.employee_key,
        MailLog.birthday_year == year,
        MailLog.status == "sent",
    ))
    if old:
        return False, "Поздравление уже отправлено"

    try:
        message = compose_birthday_message(db, emp)
    except ValueError as exc:
        return False, str(exc)

    recipient = message["recipient"]
    subject = message["subject"]
    if not recipient:
        return False, "Не настроена группа рассылки"

    log = db.scalar(select(MailLog).where(
        MailLog.employee_key == emp.employee_key,
        MailLog.birthday_year == year,
    ))
    if not log:
        log = MailLog(
            employee_key=emp.employee_key,
            fio=emp.fio,
            birthday_year=year,
            recipient=recipient,
            subject=subject,
        )
        db.add(log)

    log.rendered_text = message["rendered"]
    log.status = "sending"
    db.commit()

    try:
        send_html_mail(
            message["cfg"],
            subject,
            recipient,
            message["html"],
            message["rendered"],
        )
        log.status = "sent"
        log.sent_at = datetime.utcnow()

        intro = message["intro"]
        intro.usage_count += 1
        intro.last_used_at = datetime.utcnow()

        wish = message["wish"]
        if wish:
            wish.usage_count += 1
            wish.last_used_at = datetime.utcnow()

        db.add(AuditLog(
            actor=actor,
            action="birthday_sent",
            details=emp.fio,
        ))
        db.commit()
        return True, "Отправлено"
    except Exception as exc:
        log.status = "failed"
        log.error_text = str(exc)
        db.commit()
        return False, str(exc)
