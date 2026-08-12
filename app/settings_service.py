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

    "imap_host": "", "imap_port": "993", "imap_ssl": "true",
    "imap_login": "", "imap_password": "", "imap_folder": "INBOX",
    "imap_sender_filter": "", "imap_subject_filter": "", "imap_poll_minutes": "15",

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

    "ad_enabled": "false", "ad_server": "", "ad_port": "636", "ad_ssl": "true",
    "ad_domain": "", "ad_base_dn": "", "ad_user_filter": "(sAMAccountName={login})",
    "ad_allowed_group_dn": "", "ad_bind_user": "", "ad_bind_password": "",

    "xlsx_header_row": "2", "xlsx_second_header_row": "3", "xlsx_data_row": "4",
    "xlsx_fio_column": "Сотрудник.Физическое лицо.ФИО",
    "xlsx_birthday_column": "Дата рождения.День, Дата рождения.Название месяца",
    "xlsx_position_column": "Должность",
    "xlsx_hide_column": "Сотрудник.Скрыть день рождения (Сотрудники)",
    "xlsx_id_column": "СНИЛС",
    "xlsx_gender_column": "Физическое лицо.Пол",
    "xlsx_state_column": "Состояние",
}

LEGACY_XLSX_DEFAULTS = {
    "xlsx_fio_column": ("Сотрудник", "Сотрудник.Физическое лицо.ФИО"),
    "xlsx_birthday_column": ("Дата рождения", "Дата рождения.День, Дата рождения.Название месяца"),
    "xlsx_id_column": ("", "СНИЛС"),
    "xlsx_gender_column": ("", "Физическое лицо.Пол"),
}


def ensure_defaults(db: Session):
    changed = False
    for key, value in DEFAULTS.items():
        obj = db.get(Setting, key)
        if obj is None:
            db.add(Setting(key=key, value=value, encrypted=False))
            changed = True
            continue

        # Одноразово обновляем старые значения первого MVP на реальные
        # заголовки текущей выгрузки 1С. Пользовательские значения не трогаем.
        legacy = LEGACY_XLSX_DEFAULTS.get(key)
        if legacy and not obj.encrypted and obj.value == legacy[0]:
            obj.value = legacy[1]
            changed = True

    if changed:
        db.commit()

def get_setting(db: Session, key: str, default: str = "") -> str:
    obj = db.get(Setting, key)
    if not obj:
        return default
    return decrypt_value(obj.value) if obj.encrypted else obj.value

def get_all_settings(db: Session) -> dict[str, str]:
    ensure_defaults(db)
    return {key: get_setting(db, key, value) for key, value in DEFAULTS.items()}

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
