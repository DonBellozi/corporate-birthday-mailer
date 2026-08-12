from dataclasses import dataclass
import re


AUTO_USE_CONFIDENCE = 0.90


@dataclass(frozen=True)
class PositionSuggestion:
    text: str
    confidence: float
    reason: str = ""

    @property
    def auto_use(self) -> bool:
        return bool(self.text) and self.confidence >= AUTO_USE_CONFIDENCE


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def _key(value: str) -> str:
    return _clean(value).lower().replace("ё", "е")


UNIT_TYPES = {
    "департамент": {
        "genitive": "департамента",
        "instrumental": "департаментом",
    },
    "управление": {
        "genitive": "управления",
        "instrumental": "управлением",
    },
    "отдел": {
        "genitive": "отдела",
        "instrumental": "отделом",
    },
    "группа": {
        "genitive": "группы",
        "instrumental": "группой",
    },
    "сектор": {
        "genitive": "сектора",
        "instrumental": "сектором",
    },
    "дирекция": {
        "genitive": "дирекции",
        "instrumental": "дирекцией",
    },
    "центр": {
        "genitive": "центра",
        "instrumental": "центром",
    },
    "служба": {
        "genitive": "службы",
        "instrumental": "службой",
    },
}


def split_source_position(source_position: str):
    """
    Текущий формат:
      Подразделение / Подразделение / ...  Должность
    Между путем и должностью два пробела.
    """
    source = (source_position or "").strip()
    if not source:
        return [], ""

    if "\t" in source:
        path, title = source.rsplit("\t", 1)
    elif "  " in source:
        path, title = source.rsplit("  ", 1)
    else:
        # Старые записи без подразделений.
        return [], _clean(source)

    units = [_clean(x) for x in path.split(" / ") if _clean(x)]
    title = _clean(title)

    # Технический верхний уровень в поздравлении сам по себе не нужен.
    units = [x for x in units if _key(x) != "центральный аппарат"]
    return units, title


def detect_unit_type(unit_name: str) -> str | None:
    key = _key(unit_name)
    words = re.findall(r"[а-яa-z]+", key)
    for unit_type in UNIT_TYPES:
        if unit_type in words:
            return unit_type
    return None


def _inflect_adjective_token(token: str) -> str:
    """
    Минимальная морфология для названий подразделений.
    Нужна прежде всего для форм:
      Финансово-ревизионный отдел -> финансово-ревизионного отдела
      Контрольно-ревизионное управление -> контрольно-ревизионного управления
      Учебный центр -> Учебного центра
    """
    original = token
    lower = token.lower()

    endings = [
        ("ый", "ого"),
        ("ой", "ого"),
        ("ий", "его"),
        ("ое", "ого"),
        ("ее", "его"),
        ("ая", "ой"),
        ("яя", "ей"),
    ]
    for old, new in endings:
        if lower.endswith(old) and len(lower) > len(old):
            result = original[:-len(old)] + new
            if original[:1].isupper():
                result = result[:1].upper() + result[1:]
            return result
    return original


def unit_to_genitive(unit_name: str) -> str:
    unit_name = _clean(unit_name)
    unit_type = detect_unit_type(unit_name)
    if not unit_type:
        return unit_name

    words = unit_name.split()
    type_index = None
    for i, token in enumerate(words):
        plain = re.sub(r"[^А-Яа-яA-Za-z]", "", token).lower().replace("ё", "е")
        if plain == unit_type:
            type_index = i
            break

    if type_index is None:
        return unit_name

    # Прилагательные перед типом подразделения согласуем в родительном.
    for i in range(type_index):
        words[i] = _inflect_adjective_token(words[i])

    replacement = UNIT_TYPES[unit_type]["genitive"]
    if words[type_index][:1].isupper() and type_index > 0:
        replacement = replacement[:1].upper() + replacement[1:]
    words[type_index] = replacement
    result = " ".join(words)

    # Форма подразделения вставляется после названия должности,
    # поэтому начинаем ее со строчной буквы.
    if result:
        result = result[:1].lower() + result[1:]
    return result


def _unit_tail(unit_name: str, unit_type: str) -> str:
    """
    Для "Отдел информационных технологий" -> "информационных технологий".
    Для "Финансово-ревизионный отдел" возвращаем пусто: там нужно использовать
    полную форму "финансово-ревизионного отдела".
    """
    unit_name = _clean(unit_name)
    words = unit_name.split()
    for i, token in enumerate(words):
        plain = re.sub(r"[^А-Яа-яA-Za-z]", "", token).lower().replace("ё", "е")
        if plain == unit_type:
            if i == 0:
                return " ".join(words[i + 1:]).strip()
            return ""
    return ""


def _find_nearest_unit(units: list[str], wanted_type: str | None = None):
    for unit in reversed(units):
        unit_type = detect_unit_type(unit)
        if not unit_type:
            continue
        if wanted_type is None or unit_type == wanted_type:
            return unit, unit_type
    return None, None


# Должности, которые сами по себе уже достаточно информативны.
SELF_CONTAINED_PREFIXES = (
    "советник ",
    "директор ",
    "генеральный директор",
    "первый заместитель директора",
    "заместитель директора",
    "помощник директора",
)


# Явная связь должности с типом подразделения.
ROLE_UNIT_RULES = (
    (r"(заместитель\s+начальника\s+отдела|начальник\s+отдела)$", "отдел", "genitive", 0.99),
    (r"(заместитель\s+начальника\s+управления|начальник\s+управления)$", "управление", "genitive", 0.99),
    (r"(руководитель\s+департамента)$", "департамент", "genitive", 0.99),
    (r"(руководитель\s+группы)$", "группа", "genitive", 0.99),
    (r"(заведующий\s+сектором|заведующая\s+сектором)$", "сектор", "instrumental", 0.99),
)


GENERAL_ATTACH_PREFIXES = (
    "главный специалист",
    "ведущий специалист",
    "старший специалист",
    "специалист",
    "главный эксперт",
    "ведущий эксперт",
    "эксперт",
    "ведущий инженер",
    "инженер",
    "ведущий инженер-",
    "главный консультант",
    "ведущий консультант",
    "консультант",
    "менеджер проекта",
    "менеджер",
)


AMBIGUOUS_TITLES = (
    "главный инженер проекта",
    "руководитель проекта",
)


def _join_explicit_role(title: str, unit_name: str, unit_type: str, role_case: str) -> str:
    genitive = unit_to_genitive(unit_name)
    tail = _unit_tail(unit_name, unit_type)

    # Если название подразделения начинается с его типа:
    # "Отдел информационных технологий" + "Начальник отдела"
    # -> "Начальник отдела информационных технологий".
    if tail:
        return _clean(f"{title} {tail}")

    # Если тип подразделения стоит после определения:
    # "Финансово-ревизионный отдел" + "Начальник отдела"
    # -> "Начальник финансово-ревизионного отдела".
    if role_case == "instrumental":
        # Редкий случай. Для сектора обычно исходное имя начинается с "Сектор".
        return _clean(f"{title} {genitive}")

    genitive_type = UNIT_TYPES[unit_type]["genitive"]
    pattern = re.compile(rf"\b{re.escape(genitive_type)}\b", re.IGNORECASE)
    if pattern.search(title):
        return _clean(pattern.sub(genitive, title, count=1))

    return _clean(f"{title} {genitive}")


def suggest_position(source_position: str) -> PositionSuggestion:
    units, title = split_source_position(source_position)
    if not title:
        return PositionSuggestion("", 0.0, "Не удалось выделить должность")

    title_key = _key(title)

    if title_key.startswith("заведующая сектором"):
        title = re.sub(
            r"^заведующая\s+сектором",
            "Заведующий сектором",
            title,
            count=1,
            flags=re.IGNORECASE,
        )
        title_key = _key(title)

    # Уже самодостаточная должность.
    if any(title_key.startswith(prefix) for prefix in SELF_CONTAINED_PREFIXES):
        return PositionSuggestion(title, 0.98, "Должность самодостаточна")

    # Не угадываем спорные роли.
    if any(title_key.startswith(prefix) for prefix in AMBIGUOUS_TITLES):
        return PositionSuggestion(title, 0.35, "Должность определена, но нужен выбор уровня подразделения")

    # Явные роли: начальник отдела, руководитель департамента и т.д.
    for pattern, wanted_type, role_case, confidence in ROLE_UNIT_RULES:
        if re.search(pattern, title_key):
            unit, unit_type = _find_nearest_unit(units, wanted_type)
            if unit:
                text = _join_explicit_role(title, unit, unit_type, role_case)
                return PositionSuggestion(
                    text,
                    confidence,
                    f"Должность однозначно связана с подразделением «{unit}»",
                )

    # "Заместитель руководителя" обычно относится к ближайшему подразделению.
    if title_key.startswith("заместитель руководителя"):
        unit, _ = _find_nearest_unit(units)
        if unit:
            return PositionSuggestion(
                _clean(f"{title} {unit_to_genitive(unit)}"),
                0.90,
                f"Использовано ближайшее подразделение «{unit}»",
            )

    # Специалисты / инженеры / эксперты: добавляем ближайшее рабочее подразделение.
    if any(title_key.startswith(prefix) for prefix in GENERAL_ATTACH_PREFIXES):
        unit, _ = _find_nearest_unit(units)
        if unit:
            return PositionSuggestion(
                _clean(f"{title} {unit_to_genitive(unit)}"),
                0.92,
                f"Использовано ближайшее подразделение «{unit}»",
            )

    # Общий "руководитель" — предложение показать можно, но автоматически
    # использовать пока не будем.
    if title_key == "руководитель":
        unit, _ = _find_nearest_unit(units)
        if unit:
            return PositionSuggestion(
                _clean(f"{title} {unit_to_genitive(unit)}"),
                0.72,
                "Неоднозначная должность «Руководитель»",
            )

    return PositionSuggestion("", 0.0, "Автоматическое правило пока не найдено")
