import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from openpyxl import load_workbook

MONTHS_RU = {
    "январь":1,"февраль":2,"март":3,"апрель":4,"май":5,"июнь":6,
    "июль":7,"август":8,"сентябрь":9,"октябрь":10,"ноябрь":11,"декабрь":12,
}

def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()

def normalize_key(value: Any) -> str:
    return normalize_text(value).lower().replace("ё", "е")

def parse_birthday(value: Any):
    if value is None or not normalize_text(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.day, value.month

    text = normalize_text(value)
    if "," in text:
        day_text, month_text = text.split(",", 1)
        day = int(day_text.strip())
        month = MONTHS_RU[normalize_key(month_text)]
        date(2000, month, day)
        return day, month

    for fmt in ("%d.%m", "%d.%m.%Y", "%d/%m", "%d/%m/%Y"):
        try:
            d = datetime.strptime(text, fmt)
            return d.day, d.month
        except ValueError:
            pass
    raise ValueError(f"Неизвестный формат даты рождения: {value!r}")

def detect_gender(fio: str, explicit: str = "") -> str:
    x = normalize_key(explicit)
    if x in {"м", "муж", "мужской", "male"}:
        return "male"
    if x in {"ж", "жен", "женский", "female"}:
        return "female"

    parts = normalize_text(fio).split()
    patronymic = parts[2].lower() if len(parts) >= 3 else ""
    if patronymic.endswith(("ович", "евич", "ич")):
        return "male"
    if patronymic.endswith(("овна", "евна", "ична", "инична")):
        return "female"
    return "unknown"

def build_header_map(ws, top_row: int, second_row: int | None):
    result = {}
    for col in range(1, ws.max_column + 1):
        top = normalize_text(ws.cell(top_row, col).value)
        bottom = normalize_text(ws.cell(second_row, col).value) if second_row else ""
        for value in [top, bottom, f"{top} {bottom}".strip()]:
            if value:
                result[normalize_key(value)] = col
    return result

def find_col(headers, expected: str, required=True, aliases=None):
    aliases = aliases or []
    names = [expected, *aliases]

    # Сначала только точные совпадения. Это важно для заголовков вида
    # "Сотрудник....", которых в отчете 1С несколько.
    for name in names:
        target = normalize_key(name)
        if target and target in headers:
            return headers[target]

    # Затем мягкий поиск, но только если получился ровно один кандидат.
    for name in names:
        target = normalize_key(name)
        if not target:
            continue
        candidates = [(header, col) for header, col in headers.items()
                      if target in header or header in target]
        unique_cols = sorted({col for _, col in candidates})
        if len(unique_cols) == 1:
            return unique_cols[0]

    if required:
        raise ValueError(
            f"Не найдена колонка: {expected}. "
            f"Доступные заголовки: {', '.join(sorted(headers.keys()))}"
        )
    return None

def parse_workbook(path: str | Path, cfg: dict[str, str]):
    wb = load_workbook(path, data_only=True)
    ws = wb.active

    h1 = int(cfg.get("xlsx_header_row", "2"))
    h2 = int(cfg.get("xlsx_second_header_row", "3") or 0) or None
    data_row = int(cfg.get("xlsx_data_row", "4"))
    headers = build_header_map(ws, h1, h2)

    fio_col = find_col(
        headers,
        cfg["xlsx_fio_column"],
        aliases=["Сотрудник.Физическое лицо.ФИО", "Физическое лицо.ФИО", "ФИО"],
    )
    birthday_col = find_col(
        headers,
        cfg["xlsx_birthday_column"],
        aliases=[
            "Дата рождения.День, Дата рождения.Название месяца",
            "День месяц года",
        ],
    )
    position_col = find_col(
        headers,
        cfg.get("xlsx_position_column", ""),
        False,
        aliases=["Должность"],
    )
    hide_col = find_col(
        headers,
        cfg.get("xlsx_hide_column", ""),
        False,
        aliases=["Сотрудник.Скрыть день рождения (Сотрудники)"],
    )
    id_col = find_col(
        headers,
        cfg.get("xlsx_id_column", ""),
        False,
        aliases=["СНИЛС"],
    )
    gender_col = find_col(
        headers,
        cfg.get("xlsx_gender_column", ""),
        False,
        aliases=["Физическое лицо.Пол", "Пол"],
    )

    result = []
    for row in range(data_row, ws.max_row + 1):
        fio = normalize_text(ws.cell(row, fio_col).value)
        if not fio:
            continue
        raw_birthday = ws.cell(row, birthday_col).value
        try:
            birthday = parse_birthday(raw_birthday)
        except Exception as exc:
            result.append({"fio": fio, "error": str(exc)})
            continue
        if not birthday:
            continue

        position = normalize_text(ws.cell(row, position_col).value) if position_col else ""
        hidden = normalize_key(ws.cell(row, hide_col).value) == "да" if hide_col else False
        explicit_gender = normalize_text(ws.cell(row, gender_col).value) if gender_col else ""
        gender = detect_gender(fio, explicit_gender)
        employee_key = normalize_text(ws.cell(row, id_col).value) if id_col else ""
        if not employee_key:
            employee_key = normalize_key(f"{fio}|{birthday[0]:02d}.{birthday[1]:02d}")

        result.append({
            "employee_key": employee_key,
            "fio": fio,
            "birthday_day": birthday[0],
            "birthday_month": birthday[1],
            "gender": gender,
            "hide_birthday": hidden,
            "source_position": position,
            "error": None,
        })
    return result
