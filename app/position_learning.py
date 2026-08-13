"""
Обучение на подтверждениях оператора.

Идея: должностей в организации гораздо меньше, чем сотрудников на этих
должностях (в реальной выгрузке - около 150 уникальных названий должности
на несколько сотен человек). Если оператор один раз подтвердил или
исправил текст для должности "Начальник отдела" в одном подразделении, это
почти наверняка подскажет правильный вид для той же должности в ДРУГОМ
подразделении - хотя бы приблизительно, до собственного подтверждения.

Два уровня:
  1. Точное совпадение - для этой же должности в этом же (по названию)
     ближайшем подразделении подтверждение уже было. Используется
     подтвержденная глубина, уверенность высокая.
  2. По аналогии - для этой должности подтверждения были, но в других
     подразделениях. Берется наиболее частая глубина среди них,
     уверенность растет с числом подтверждений, но никогда не достигает
     порога автоматического использования в письмах (см. AUTO_USE_CONFIDENCE
     в position_suggester.py) - предположение по аналогии всегда должно
     пройти через оператора хотя бы один раз для конкретного подразделения.

Ничего не подтверждено -> используется обычное правило из
position_suggester.py (глубина по умолчанию - только ближайшее
подразделение), с более низкой уверенностью, чтобы попасть в очередь
на проверку, а не потеряться среди уже подтвержденных.
"""
from collections import Counter
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PositionPattern
from .position_suggester import (
    PositionSuggestion,
    DEFAULT_SKIP_PREFIXES,
    _key,
    meaningful_chain,
    join_chain,
    split_source_position,
    suggest_position,
)


def _stems(text: str, min_len: int = 4) -> set[str]:
    """
    Грубая нормализация слова для сравнения "было ли это подразделение
    упомянуто в тексте" без полноценной морфологии - обрезаем окончания.
    Длина 5 подобрана так, чтобы "учебный"/"учебного" и "центр"/"центра"
    считались одним и тем же словом, а разные слова не схлопывались.
    """
    return {w[:5] for w in re.findall(rf"[а-яё-]{{{min_len},}}", (text or "").lower().replace("ё", "е"))}


def infer_depth(confirmed_text: str, units: list[str], skip_prefixes, max_depth: int = 3) -> int:
    """
    По итоговому подтвержденному тексту определяет, сколько подразделений
    (считая от ближайшего) оператор в него включил - превращает свободное
    редактирование текста в структурированное правило.
    """
    chain, _ = meaningful_chain(units, skip_prefixes, None, max_depth)
    if not chain:
        return 0

    conf_stems = _stems(confirmed_text)
    depth = 0
    for i, unit in enumerate(chain):
        unit_stems = _stems(unit)
        if not unit_stems:
            continue
        overlap = unit_stems & conf_stems
        if len(overlap) >= max(1, len(unit_stems) // 2):
            depth = i + 1
        else:
            break
    return depth


def _nearest_key(units: list[str], skip_prefixes) -> str:
    chain, _ = meaningful_chain(units, skip_prefixes, None, 1)
    return _key(chain[0]) if chain else ""


def record_confirmation(
    db: Session,
    title: str,
    units: list[str],
    confirmed_text: str,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> None:
    """Сохраняет/обновляет выученный шаблон по итогам подтверждения оператора."""
    confirmed_text = (confirmed_text or "").strip()
    if not confirmed_text:
        return

    title_norm = _key(title)
    nearest_key = _nearest_key(units, skip_prefixes)
    depth = infer_depth(confirmed_text, units, skip_prefixes)

    existing = db.scalar(
        select(PositionPattern).where(
            PositionPattern.title_norm == title_norm,
            PositionPattern.nearest_unit_key == nearest_key,
        )
    )
    if existing:
        existing.depth = depth
        existing.support += 1
    else:
        db.add(PositionPattern(
            title_norm=title_norm,
            nearest_unit_key=nearest_key,
            depth=depth,
            support=1,
        ))


def suggest_with_learning(
    db: Session,
    source_position: str,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> PositionSuggestion:
    """
    Обертка над suggest_position: сначала смотрит, нет ли уже
    подтвержденного шаблона для этой должности (точно для этого
    подразделения или по аналогии с другими), и если есть - подставляет
    его глубину. Правило само по себе (какие слова использовать, нужно ли
    повторять тип подразделения) остается за position_suggester.py -
    обучение управляет только глубиной цепочки.
    """
    units, title = split_source_position(source_position)
    if not title:
        return PositionSuggestion("", 0.0, "Не удалось выделить должность")

    title_norm = _key(title)
    nearest_key = _nearest_key(units, skip_prefixes)

    exact = db.scalar(
        select(PositionPattern).where(
            PositionPattern.title_norm == title_norm,
            PositionPattern.nearest_unit_key == nearest_key,
        )
    )
    siblings = list(db.scalars(
        select(PositionPattern).where(PositionPattern.title_norm == title_norm)
    ).all())

    learned_depth = confidence = reason = None
    if exact is not None:
        learned_depth = exact.depth
        confidence = 0.97
        reason = "Подтверждено ранее для этого подразделения"
    elif siblings:
        depths = [s.depth for s in siblings]
        learned_depth = Counter(depths).most_common(1)[0][0]
        support = sum(s.support for s in siblings)
        # Растет с числом подтверждений, но ниже AUTO_USE_CONFIDENCE (0.90) -
        # предположение по аналогии всегда идет на проверку к оператору.
        confidence = min(0.65 + 0.05 * support, 0.88)
        reason = f"По аналогии с другими подразделениями (подтверждений: {support})"

    base = suggest_position(source_position, skip_prefixes, depth_override=learned_depth)
    if base.text:
        if confidence is not None and confidence > base.confidence:
            return PositionSuggestion(base.text, confidence, reason)
        return base

    # Ни одно правило в position_suggester.py не сработало (незнакомая
    # должность вроде "Делопроизводитель", для которой нет явного правила).
    if learned_depth is not None:
        chain, _ = meaningful_chain(units, skip_prefixes, None, learned_depth)
        if chain:
            return PositionSuggestion(join_chain(title, chain, False), confidence, reason)
        return PositionSuggestion(title, confidence, reason)

    chain, _ = meaningful_chain(units, skip_prefixes, None, 1)
    if chain:
        return PositionSuggestion(
            join_chain(title, chain, False), 0.55, "Новая должность - нужна проверка",
        )
    return PositionSuggestion(title, 0.40, "Новая должность - нужна проверка")
