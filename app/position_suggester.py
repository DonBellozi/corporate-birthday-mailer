"""
Автоматическое определение должности для текста поздравления.

Правила ниже - это "нулевой уровень" (rule engine): они применяются, пока
для конкретной должности нет ни одного подтверждения оператора. Как только
оператор хотя бы раз подтвердил или исправил текст, дальше в дело вступает
обучение (см. position_learning.py) и подставляет более точный вариант -
этот модуль его не знает и не должен, чтобы правила оставались простыми
и проверяемыми независимо от накопленных данных.

Историческая заметка: раньше при должностях вида "Начальник отдела" /
"Главный специалист" в текст подставлялось название ВСЕХ подразделений от
листа до корня. На реальных выгрузках это почти всегда давало слишком
длинную и неестественную фразу. Разбор эталонных данных показал: в 15
случаях из 17 достаточно ближайшего подразделения, глубже нужно нырять
только в исключениях - для них и существует обучение. Поэтому здесь
глубина по умолчанию равна 1 (только ближайшее подразделение).
"""
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


def strip_op_wrapper(name: str) -> str:
    """
    1С помечает обособленные подразделения префиксом "ОП" и заключает
    название в кавычки: 'ОП "Дирекция в городе Саратове"'. Для сотрудника
    это не значит ничего, поэтому снимаем обертку - но только внешнюю: если
    внутри есть еще одни кавычки (название объекта/проекта), их не трогаем.
    'ОП "Дирекция по объектам ССК "Звезда""' -> 'Дирекция по объектам ССК "Звезда"'.
    """
    s = _clean(name)
    for prefix in ("ОП ", "Обособленное подразделение "):
        if s.startswith(prefix):
            rest = s[len(prefix):].strip()
            if rest[:1] == '"' and rest.endswith('"'):
                return rest[1:-1].strip()
            if rest[:1] == "«" and rest.endswith("»"):
                return rest[1:-1].strip()
            return rest
    return s


def smart_title(name: str) -> str:
    """
    В части филиалов название подразделения приходит из 1С ЗАГЛАВНЫМИ
    БУКВАМИ ЦЕЛИКОМ - без этой нормализации оно в таком же виде попадает
    в личное письмо сотруднику. Если написано ЗАГЛАВНЫМИ не целиком
    (аббревиатура внутри обычного названия) - не трогаем.
    """
    letters = [c for c in name if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        low = name.lower()
        return low[:1].upper() + low[1:]
    return name


def normalize_unit(name: str) -> str:
    return smart_title(strip_op_wrapper(_clean(name)))


# Родительный падеж для типовых слов подразделений. Список собран по
# реальной оргструктуре - если появится новый тип подразделения, которого
# здесь нет, название останется в исходном виде (без падежного окончания),
# а не сломается. Новый тип достаточно дописать сюда одной строкой.
UNIT_TYPES = {
    "департамент": "департамента", "управление": "управления", "отдел": "отдела",
    "отделение": "отделения", "группа": "группы", "сектор": "сектора",
    "дирекция": "дирекции", "центр": "центра", "служба": "службы",
    "филиал": "филиала", "представительство": "представительства",
    "подразделение": "подразделения", "офис": "офиса", "канцелярия": "канцелярии",
    "лаборатория": "лаборатории", "участок": "участка", "институт": "института",
    "комитет": "комитета", "комплекс": "комплекса", "штаб": "штаба", "бюро": "бюро",
    "общежитие": "общежития",
}


def detect_unit_type(unit_name: str) -> str | None:
    key = _key(unit_name)
    words = re.findall(r"[а-яa-z]+", key)
    for unit_type in UNIT_TYPES:
        if unit_type in words:
            return unit_type
    return None


def deduplicate_units(units: list[str]) -> list[str]:
    """Нормализует, убирает одинаковые уровни дерева и технический корень."""
    result = []
    seen = set()
    for unit in units:
        unit = normalize_unit(unit)
        key = _key(unit)
        if not unit or not key or key == "центральный аппарат":
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(unit)
    return result


def split_source_position(source_position: str):
    """
    Текущий формат:
      Подразделение / Подразделение / ...  Должность
    Между путем и должностью два пробела (или таб - используется в тестах
    и при ручном формировании строки).
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
    return deduplicate_units(units), title


# Названия, которые сами по себе не говорят, где именно работает человек
# ("Администрация" есть почти в каждом филиале). При поиске ближайшего
# подразделения такие названия пропускаются, но остаются видны в дереве на
# странице "Должности" - оператор по-прежнему видит полный путь.
# Список редактируется в настройках (Setting "position_skip_units"); это
# лишь запасной вариант на случай, если настройка еще не сохранялась.
DEFAULT_SKIP_PREFIXES = ("администрация", "руководство")


def parse_skip_prefixes(raw: str) -> tuple[str, ...]:
    """
    Разбирает настройку position_skip_units (по названию на строку).
    Настройка всегда заполнена значением по умолчанию через ensure_defaults,
    поэтому здесь не подставляется свое значение по умолчанию - если
    администратор осознанно очистит список, пропускать перестанет
    ничего, и это ожидаемо.
    """
    lines = [_key(line) for line in (raw or "").splitlines()]
    return tuple(line for line in lines if line)


def _is_skip_unit(unit_name: str, skip_prefixes) -> bool:
    key = _key(unit_name)
    return any(key.startswith(p) for p in skip_prefixes if p)


def _inflect_adjective_token(token: str) -> str:
    """
    Минимальная морфология для прилагательных перед типом подразделения:
      Финансово-ревизионный отдел -> финансово-ревизионного отдела
      Контрольно-ревизионное управление -> контрольно-ревизионного управления
      Учебный центр -> учебного центра
    """
    original = token
    lower = token.lower()
    endings = [
        ("ский", "ского"), ("цкий", "цкого"), ("кий", "кого"), ("ый", "ого"),
        ("ой", "ого"), ("ий", "его"), ("ое", "ого"), ("ее", "его"),
        ("ая", "ой"), ("яя", "ей"),
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

    for i in range(type_index):
        words[i] = _inflect_adjective_token(words[i])
    words[type_index] = UNIT_TYPES[unit_type]
    result = " ".join(words)

    # Подставляется после названия должности - начинаем со строчной буквы.
    return result[:1].lower() + result[1:] if result else result


def _unit_tail(unit_name: str, unit_type: str) -> str:
    """
    "Отдел информационных технологий" -> "информационных технологий"
    (используется, когда тип подразделения совпадает со словом в самой
    должности - "Начальник отдела" - и повторять его не нужно).
    """
    words = _clean(unit_name).split()
    for i, token in enumerate(words):
        plain = re.sub(r"[^А-Яа-яA-Za-z]", "", token).lower().replace("ё", "е")
        if plain == unit_type:
            return " ".join(words[i + 1:]).strip() if i == 0 else ""
    return ""


def _unit_to_context_genitive(unit_name: str) -> str:
    return unit_to_genitive(unit_name) if detect_unit_type(unit_name) else _clean(unit_name)


def meaningful_chain(units, skip_prefixes=DEFAULT_SKIP_PREFIXES, wanted_type=None, depth=1):
    """
    Идет от ближайшего подразделения к корню, пропуская служебные названия
    (skip_prefixes) - уже без технического корня, его убирает
    deduplicate_units. Если задан wanted_type, точкой отсчета становится
    ближайшее подразделение именно этого типа (для явных правил вроде
    "начальник отдела"). Глубина считается от точки отсчета дальше, уже
    без ограничения по типу - на этом шаге предполагается, что оператор
    (или обучение) уже выбрали, сколько уровней нужно.

    Возвращает (единицы от ближней к дальней, индекс первой в units)
    или ([], None), если подходящих единиц нет.
    """
    filtered = [(i, u) for i, u in enumerate(units) if not _is_skip_unit(u, skip_prefixes)]
    if not filtered:
        return [], None

    start_pos = None
    for pos in range(len(filtered) - 1, -1, -1):
        _, u = filtered[pos]
        if wanted_type is None or detect_unit_type(u) == wanted_type:
            start_pos = pos
            break
    if start_pos is None or depth <= 0:
        return [], None

    positions = list(range(start_pos, max(start_pos - depth, -1), -1))
    chosen = [filtered[p] for p in positions]
    return [u for _, u in chosen], chosen[0][0]


def join_chain(title: str, chain: list[str], strip_repeated_type: bool = False) -> str:
    """
    Склеивает должность с цепочкой подразделений (ближайшее -> дальние).

    strip_repeated_type=True - только когда должность уже называет тип
    подразделения ("Начальник ОТДЕЛА", "Заведующий СЕКТОРОМ"): тогда тип
    в приклеенном названии не повторяем - "Начальник отдела" + "Отдел
    кадров" -> "Начальник отдела кадров", а не "... отдела отдела кадров".
    Для остальных должностей ("Главный специалист") тип нужен целиком:
    "Главный специалист отдела кадров", а не "...специалист кадров".
    """
    if not chain:
        return _clean(title)

    nearest = chain[0]
    unit_type = detect_unit_type(nearest)
    if unit_type and strip_repeated_type:
        tail = _unit_tail(nearest, unit_type)
        genitive_type = UNIT_TYPES[unit_type]
        pattern = re.compile(rf"\b{re.escape(genitive_type)}\b", re.IGNORECASE)
        if tail:
            head = _clean(f"{title} {tail}")
        elif pattern.search(title):
            head = _clean(pattern.sub(unit_to_genitive(nearest), title, count=1))
        else:
            head = _clean(f"{title} {unit_to_genitive(nearest)}")
    else:
        head = _clean(f"{title} {_unit_to_context_genitive(nearest)}")

    parents = " ".join(_unit_to_context_genitive(u) for u in chain[1:])
    return _clean(f"{head} {parents}")


# Должности, которые сами по себе уже достаточно информативны.
SELF_CONTAINED_PREFIXES = (
    "советник ", "директор ", "генеральный директор",
    "первый заместитель директора", "заместитель директора", "помощник директора",
)

# Явная связь должности с типом подразделения - тип уже назван в самой
# должности, поэтому strip_repeated_type=True (см. join_chain).
ROLE_UNIT_RULES = (
    (r"(заместитель\s+начальника\s+отдела|начальник\s+отдела)$", "отдел", 0.99),
    (r"(заместитель\s+начальника\s+управления|начальник\s+управления)$", "управление", 0.99),
    (r"(руководитель\s+департамента)$", "департамент", 0.99),
    (r"(руководитель\s+группы)$", "группа", 0.99),
    (r"(заведующий\s+сектором|заведующая\s+сектором)$", "сектор", 0.99),
)

GENERAL_ATTACH_PREFIXES = (
    "главный специалист", "ведущий специалист", "старший специалист", "специалист",
    "главный эксперт", "ведущий эксперт", "эксперт", "ведущий инженер", "инженер",
    "ведущий инженер-", "главный консультант", "ведущий консультант", "консультант",
    "менеджер проекта", "менеджер",
)

AMBIGUOUS_TITLES = ("главный инженер проекта", "руководитель проекта")


def suggest_position(
    source_position: str,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
    depth_override: int | None = None,
) -> PositionSuggestion:
    """
    skip_prefixes и depth_override пробрасываются слоем обучения
    (position_learning.py): первый - из настроек, второй - когда для этой
    должности уже есть подтвержденный образец с другой глубиной цепочки,
    чем 1 по умолчанию. Без слоя обучения оба аргумента можно не передавать.
    """
    units, title = split_source_position(source_position)
    if not title:
        return PositionSuggestion("", 0.0, "Не удалось выделить должность")

    title_key = _key(title)

    if title_key.startswith("заведующая сектором"):
        title = re.sub(
            r"^заведующая\s+сектором", "Заведующий сектором", title, count=1, flags=re.IGNORECASE,
        )
        title_key = _key(title)

    depth = depth_override if depth_override is not None else 1

    if title_key == "директор":
        chain, _ = meaningful_chain(units, skip_prefixes, None, depth)
        if chain:
            return PositionSuggestion(join_chain(title, chain), 0.98, "Ближайшее подразделение")
        return PositionSuggestion(title, 0.98, "Должность самодостаточна")

    if any(title_key.startswith(p) for p in SELF_CONTAINED_PREFIXES):
        return PositionSuggestion(title, 0.98, "Должность самодостаточна")

    if any(title_key.startswith(p) for p in AMBIGUOUS_TITLES):
        return PositionSuggestion(title, 0.35, "Нужен выбор уровня подразделения")

    for pattern, wanted_type, confidence in ROLE_UNIT_RULES:
        if re.search(pattern, title_key):
            chain, _ = meaningful_chain(units, skip_prefixes, wanted_type, depth)
            if chain:
                return PositionSuggestion(
                    join_chain(title, chain, strip_repeated_type=True),
                    confidence,
                    "Ближайшее подразделение",
                )

    if title_key.startswith("заместитель руководителя"):
        chain, _ = meaningful_chain(units, skip_prefixes, None, depth)
        if chain:
            return PositionSuggestion(join_chain(title, chain), 0.90, "Ближайшее подразделение")

    if any(title_key.startswith(p) for p in GENERAL_ATTACH_PREFIXES):
        chain, _ = meaningful_chain(units, skip_prefixes, None, depth)
        if chain:
            return PositionSuggestion(join_chain(title, chain), 0.85, "Ближайшее подразделение")

    if title_key == "руководитель":
        chain, _ = meaningful_chain(units, skip_prefixes, None, depth)
        if chain:
            return PositionSuggestion(
                join_chain(title, chain), 0.60, "Неоднозначная должность «Руководитель»",
            )

    return PositionSuggestion("", 0.0, "Автоматическое правило пока не найдено")
