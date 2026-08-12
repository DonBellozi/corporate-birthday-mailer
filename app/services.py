from datetime import date, datetime
from pathlib import Path
import hashlib
import json
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import EmployeeSnapshot, ImportRun, IntroTemplate, PositionMapping, WishTemplate, Card, MailLog, AuditLog
from .settings_service import get_all_settings
from .xlsx_parser import parse_workbook
from .rendering import variable_context, render_text, email_html
from .mail_service import send_html_mail
from .position_suggester import suggest_position
from .card_service import card_file_path, card_meta, display_width

def latest_successful_import(db):
    return db.scalars(
        select(ImportRun).where(ImportRun.success == True).order_by(desc(ImportRun.received_at))
    ).first()


# Базовый набор известных значений текущего отчета 1С.
# Фактический селектор дополнительно пополняется значениями из свежего снимка.
KNOWN_EMPLOYEE_STATES = (
    "Болезнь",
    "Дополнительные выходные дни (оплачиваемые)",
    "Дополнительные выходные дни неоплачиваемые",
    "Дополнительный отпуск",
    "Командировка",
    "Отпуск неоплачиваемый по разрешению работодателя",
    "Отпуск основной",
    "Отпуск по беременности и родам",
    "Отпуск по уходу за ребенком",
    "Отсутствие по невыясненным причинам",
    "Работа",
)


def _state_key(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split()).strip().lower().replace("ё", "е")


def blocked_employee_states(cfg: dict[str, str]) -> list[str]:
    raw = cfg.get("employee_state_blocked", "[]") or "[]"
    try:
        values = json.loads(raw)
    except Exception:
        return []

    if not isinstance(values, list):
        return []

    result = []
    seen = set()
    for value in values:
        value = " ".join(str(value or "").replace("\xa0", " ").split()).strip()
        key = _state_key(value)
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def current_employee_states(db) -> list[str]:
    """
    Список для селектора:
      - известные состояния текущего отчета;
      - состояния из последнего успешного снимка;
      - ранее запрещенные состояния, даже если временно исчезли из выгрузки.
    """
    cfg = get_all_settings(db)
    values = list(KNOWN_EMPLOYEE_STATES)
    values.extend(blocked_employee_states(cfg))

    latest = latest_successful_import(db)
    if latest:
        values.extend(
            x for x in db.scalars(
                select(EmployeeSnapshot.employee_state)
                .where(EmployeeSnapshot.import_id == latest.id)
                .distinct()
            ).all()
            if x
        )

    # Дедупликация без потери нормального написания.
    by_key = {}
    for value in values:
        value = " ".join(str(value or "").replace("\xa0", " ").split()).strip()
        if value:
            by_key.setdefault(_state_key(value), value)

    # "Работа" первой, остальные по алфавиту.
    return sorted(
        by_key.values(),
        key=lambda x: (0 if _state_key(x) == "работа" else 1, _state_key(x)),
    )


def employee_state_is_allowed(emp, cfg: dict[str, str]) -> bool:
    state = " ".join(str(getattr(emp, "employee_state", "") or "").replace("\xa0", " ").split()).strip()

    # Старые снимки, импортированные до появления колонки "Состояние",
    # не блокируем. После следующего импорта значение станет доступно.
    if not state:
        return True

    blocked = {_state_key(x) for x in blocked_employee_states(cfg)}
    return _state_key(state) not in blocked


def birthday_send_eligibility(db, emp) -> tuple[bool, str]:
    if emp.hide_birthday:
        return False, "В 1С установлен запрет «Скрыть день рождения»"

    cfg = get_all_settings(db)
    if not employee_state_is_allowed(emp, cfg):
        state = getattr(emp, "employee_state", "") or "Не указано"
        return False, f"Состояние «{state}» исключено из поздравлений"

    return True, ""


def snapshot_size_is_suspicious(current_count: int, previous_count: int, min_ratio: int = 80):
    """
    Возвращает (is_suspicious, percentage).

    Резкое уменьшение кадровой выгрузки почти всегда означает неполный
    отчет/сбой 1С, а не массовое увольнение. Проверка применяется только
    к автоматической ночной выгрузке.
    """
    if previous_count <= 0:
        return False, 100.0

    percentage = current_count * 100.0 / previous_count
    return percentage < min_ratio, percentage


def import_xlsx(
    db: Session,
    path: Path,
    source="manual",
    enforce_snapshot_sanity: bool = False,
):
    cfg = get_all_settings(db)
    previous = latest_successful_import(db)

    run = ImportRun(filename=path.name, source=source, success=False)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        rows = parse_workbook(path, cfg)
        run.rows_total = len(rows)

        if not rows:
            raise ValueError(
                "Заголовки XLSX найдены, но строки сотрудников отсутствуют. "
                "Проверьте, что из 1С выгружен отчет с детализацией по сотрудникам, "
                "а не только структура подразделений."
            )

        valid_rows = [item for item in rows if not item.get("error")]
        run.rows_valid = len(valid_rows)

        if not valid_rows:
            raise ValueError("В XLSX не найдено ни одной корректной строки сотрудника.")

        if enforce_snapshot_sanity and previous and previous.rows_valid:
            try:
                min_ratio = int(cfg.get("snapshot_min_ratio", "80") or "80")
            except Exception:
                min_ratio = 80
            min_ratio = max(1, min(100, min_ratio))

            suspicious, percentage = snapshot_size_is_suspicious(
                len(valid_rows),
                previous.rows_valid,
                min_ratio,
            )
            if suspicious:
                raise ValueError(
                    "Новая выгрузка выглядит неполной: "
                    f"{len(valid_rows)} работников вместо {previous.rows_valid} "
                    f"({percentage:.1f}% от предыдущего снимка; "
                    f"допустимый минимум {min_ratio}%). "
                    "Рабочим остается предыдущий успешный снимок."
                )

        known_positions = {
            x for x in db.scalars(select(PositionMapping.source_position)).all()
        }

        for item in valid_rows:
            db.add(EmployeeSnapshot(
                import_id=run.id,
                employee_key=item["employee_key"],
                fio=item["fio"],
                birthday_day=item["birthday_day"],
                birthday_month=item["birthday_month"],
                gender=item["gender"],
                employee_state=item.get("employee_state", ""),
                hide_birthday=item["hide_birthday"],
                source_position=item["source_position"],
            ))

            source_position = item.get("source_position") or ""
            if source_position and source_position not in known_positions:
                db.add(PositionMapping(
                    source_position=source_position,
                    display_position="",
                    active=True,
                    confirmed=False,
                ))
                known_positions.add(source_position)

        run.success = True
        db.commit()
        return run

    except Exception as exc:
        # На этапе sanity-check снимки еще не созданы, поэтому неудачная
        # выгрузка не может случайно стать рабочей.
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
        can_send, send_reason = birthday_send_eligibility(db, emp)
        result.append({
            "employee": emp,
            "next_date": next_date,
            "days_left": days_left,
            "can_send": can_send,
            "send_reason": send_reason,
        })

    result.sort(key=lambda item: (item["next_date"], item["employee"].fio))
    return result

def choose_least_used(items, seed=""):
    if not items:
        return None

    minimum = min(x.usage_count for x in items)
    candidates = sorted(
        [x for x in items if x.usage_count == minimum],
        key=lambda x: x.id,
    )

    if not seed or len(candidates) == 1:
        return candidates[0]

    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % len(candidates)
    return candidates[index]


def choose_intro(db, seed=""):
    return choose_least_used(list(db.scalars(
        select(IntroTemplate).where(IntroTemplate.active == True)
    ).all()), seed=seed)


def choose_wish(db, gender, seed=""):
    return choose_least_used(list(db.scalars(
        select(WishTemplate).where(
            WishTemplate.active == True,
            WishTemplate.gender.in_([gender, "universal"]),
        )
    ).all()), seed=seed)


def choose_card(db, gender, seed=""):
    items = list(db.scalars(
        select(Card).where(
            Card.active == True,
            Card.gender.in_([gender, "universal"]),
        )
    ).all())

    # Не выбираем запись, если физический файл уже отсутствует.
    items = [x for x in items if card_file_path(x.filename).exists()]
    return choose_least_used(items, seed=seed)


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

    Выбор текста, пожелания и открытки детерминирован для конкретного
    сотрудника среди наименее использованных вариантов. Поэтому повторный
    предпросмотр показывает тот же вариант, пока не изменился фотобанк
    или счетчики реальных отправок.
    """
    cfg = get_all_settings(db)
    seed = emp.employee_key or str(emp.id)

    intro = choose_intro(db, seed=f"{seed}:intro")
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
        wish = choose_wish(db, emp.gender, seed=f"{seed}:wish")
        if wish:
            wish_text = render_text(wish.body, ctx)

    card = None
    card_path = None
    card_info = None
    if cfg.get("cards_enabled") == "true":
        card = choose_card(db, emp.gender, seed=f"{seed}:card")
        if card:
            card_path = card_file_path(card.filename)
            card_info = card_meta(card.filename)
            if not card_info.get("exists"):
                card = None
                card_path = None
                card_info = None

    recipient = cfg.get("mail_recipient", "").strip()
    subject = "Поздравляем С Днем Рождения!"

    rendered = (
        intro_text
        + ("\n" + wish_text if wish_text else "")
        + (f"\n[Открытка: {card.name}]" if card else "")
    )

    cid = "birthday-card"
    html_body = email_html(
        intro_text,
        wish_text,
        position,
        emp.gender,
        card_src=f"cid:{cid}" if card else "",
        card_width=display_width(card_info or {}),
    )

    return {
        "cfg": cfg,
        "intro": intro,
        "wish": wish,
        "card": card,
        "card_path": card_path,
        "card_info": card_info,
        "card_cid": cid,
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

    preview_html = message["html"]
    card = message["card"]
    if card:
        preview_html = email_html(
            message["intro_text"],
            message["wish_text"],
            message["position"],
            emp.gender,
            card_src=f"/cards/{card.id}/image",
            card_width=display_width(message["card_info"] or {}),
        )

    return {
        "fio": emp.fio,
        "subject": message["subject"],
        "recipient": message["recipient"],
        "position": message["position"],
        "intro_name": message["intro"].name if message["intro"] else "",
        "wish_name": message["wish"].name if message["wish"] else "",
        "card_name": card.name if card else "",
        "card_orientation": (
            (message["card_info"] or {}).get("orientation_label", "")
            if card else ""
        ),
        "html": preview_html,
        "excluded": not birthday_send_eligibility(db, emp)[0],
        "warning": birthday_send_eligibility(db, emp)[1],
    }


def send_birthday(db, emp, actor="scheduler"):
    can_send, reason = birthday_send_eligibility(db, emp)
    if not can_send:
        return False, reason

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
        inline_image = None
        card = message["card"]
        if card and message["card_path"]:
            inline_image = {
                "path": str(message["card_path"]),
                "mime": (message["card_info"] or {}).get("mime") or "image/jpeg",
                "cid": message["card_cid"],
                "filename": card.filename,
            }

        send_html_mail(
            message["cfg"],
            subject,
            recipient,
            message["html"],
            message["rendered"],
            inline_image=inline_image,
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

        card = message["card"]
        if card:
            card.usage_count += 1
            card.last_used_at = datetime.utcnow()

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
