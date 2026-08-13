from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Setting
from .security import encrypt_value, decrypt_value

SECRET_KEYS = {"imap_password", "smtp_password", "ad_bind_password"}

DEFAULTS = {
    "auto_send_enabled": "false",
    "send_time": "09:15",
    "mail_subject": "Поздравляем с Днем рождения!",
    "mail_recipient": "",
    "mail_from": "",
    "wishes_enabled": "false",
    "cards_enabled": "true",
    "positions_enabled": "true",

    # Состояния сотрудников, для которых поздравления запрещены.
    # Храним JSON-массив строк. Пустой список = разрешены все состояния.
    "employee_state_blocked": "[]",

    # В невисокосный год у 29 февраля нет календарной даты.
    # "feb28" - поздравлять 28 февраля, "mar1" - поздравлять 1 марта.
    "feb29_policy": "feb28",

    # Разрешенные домены корпоративной почты, по одному на строку.
    # Пустой список отключает проверку домена, но наличие рабочего email
    # остается обязательным.
    "allowed_email_domains": "",

    "imap_host": "", "imap_port": "993", "imap_ssl": "true",
    "imap_login": "", "imap_password": "", "imap_folder": "INBOX",
    "imap_sender_filter": "", "imap_subject_filter": "", "imap_poll_minutes": "15",

    # Хэш последнего импортированного по IMAP файла - используется, чтобы не
    # переимпортировать одно и то же вложение на каждом опросе.
    "imap_last_file_hash": "",

    # Служебное состояние ежедневной кадровой выгрузки.
    # В интерфейсе настроек эти поля не редактируются.
    "snapshot_min_ratio": "80",
    "snapshot_last_success_date": "",
    "snapshot_last_message_key": "",
    "snapshot_status": "",
    "snapshot_status_level": "info",
    "snapshot_status_date": "",

    "smtp_host": "", "smtp_port": "587", "smtp_starttls": "true",
    "smtp_ssl": "false", "smtp_login": "", "smtp_password": "",

    # Схема Active Directory аналогична invite-mailer:
    # обычный LDAP/389 по умолчанию; LDAPS включается отдельно.
    # ad_domain – именно NetBIOS-имя, например DOMAIN.
    "ad_enabled": "false", "ad_server": "", "ad_port": "389", "ad_ssl": "false",
    "ad_domain": "", "ad_base_dn": "", "ad_user_filter": "(sAMAccountName={login})",
    "ad_bind_user": "", "ad_bind_password": "",

    "xlsx_header_row": "2", "xlsx_second_header_row": "3", "xlsx_data_row": "4",
    "xlsx_fio_column": "Сотрудник.Физическое лицо.ФИО",
    "xlsx_birthday_column": "Дата рождения.День, Дата рождения.Название месяца",
    "xlsx_position_column": "Должность",
    "xlsx_hide_column": "Сотрудник.Скрыть день рождения (Сотрудники)",
    "xlsx_id_column": "СНИЛС",
    "xlsx_gender_column": "Физическое лицо.Пол",
    "xlsx_state_column": "Состояние",
    "xlsx_work_email_column": "Физическое лицо.Адрес электронной почты",
}

LEGACY_XLSX_DEFAULTS = {
    "xlsx_fio_column": ("Сотрудник", "Сотрудник.Физическое лицо.ФИО"),
    "xlsx_birthday_column": ("Дата рождения", "Дата рождения.День, Дата рождения.Название месяца"),
    "xlsx_id_column": ("", "СНИЛС"),
    "xlsx_gender_column": ("", "Физическое лицо.Пол"),
}


def ensure_defaults(db: Session):
    """Загружает таблицу настроек одним запросом."""
    objects = list(db.scalars(select(Setting)).all())
    by_key = {obj.key: obj for obj in objects}
    changed = False

    for key, value in DEFAULTS.items():
        obj = by_key.get(key)
        if obj is None:
            obj = Setting(key=key, value=value, encrypted=False)
            db.add(obj)
            by_key[key] = obj
            changed = True
            continue

        legacy = LEGACY_XLSX_DEFAULTS.get(key)
        if legacy and not obj.encrypted and obj.value == legacy[0]:
            obj.value = legacy[1]
            changed = True

    if changed:
        db.commit()

    return by_key


def get_setting(db: Session, key: str, default: str = "") -> str:
    obj = db.get(Setting, key)
    if not obj:
        return default
    return decrypt_value(obj.value) if obj.encrypted else obj.value


def get_all_settings(db: Session) -> dict[str, str]:
    by_key = ensure_defaults(db)
    result = {}

    for key, default in DEFAULTS.items():
        obj = by_key.get(key)
        if not obj:
            result[key] = default
            continue

        result[key] = decrypt_value(obj.value) if obj.encrypted else obj.value

    return result

def set_settings(db: Session, data: dict[str, str]):
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        encrypted = key in SECRET_KEYS and bool(value)
        stored = encrypt_value(value) if encrypted else value
        obj = db.get(Setting, key)
        if obj:
            obj.value, obj.encrypted = stored, encrypted
        else:
            db.add(Setting(key=key, value=stored, encrypted=encrypted))
    db.commit()
