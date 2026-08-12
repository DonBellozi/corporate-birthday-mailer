from io import BytesIO
from pathlib import Path
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError


CARD_DIR = Path("/app/data/cards")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 1600

FORMAT_INFO = {
    "JPEG": {"ext": ".jpg", "mime": "image/jpeg"},
    "PNG": {"ext": ".png", "mime": "image/png"},
}


def orientation_from_size(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "unknown"
    ratio = width / height
    if ratio >= 1.15:
        return "horizontal"
    if ratio <= 0.87:
        return "vertical"
    return "square"


def orientation_label(value: str) -> str:
    return {
        "horizontal": "Горизонтальная",
        "vertical": "Вертикальная",
        "square": "Квадратная",
    }.get(value, "Не определена")


def _safe_card_path(filename: str) -> Path:
    name = Path(filename or "").name
    if not name:
        raise ValueError("Не задан файл изображения")
    return CARD_DIR / name


def card_file_path(filename: str) -> Path:
    return _safe_card_path(filename)


def card_meta(filename: str) -> dict:
    path = _safe_card_path(filename)
    if not path.exists():
        return {
            "exists": False,
            "width": 0,
            "height": 0,
            "orientation": "unknown",
            "orientation_label": "Файл отсутствует",
            "mime": "",
            "size_bytes": 0,
        }

    try:
        with Image.open(path) as img:
            width, height = img.size
            fmt = (img.format or "").upper()
    except Exception:
        return {
            "exists": False,
            "width": 0,
            "height": 0,
            "orientation": "unknown",
            "orientation_label": "Ошибка файла",
            "mime": "",
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

    orientation = orientation_from_size(width, height)
    return {
        "exists": True,
        "width": width,
        "height": height,
        "orientation": orientation,
        "orientation_label": orientation_label(orientation),
        "mime": FORMAT_INFO.get(fmt, {}).get("mime", ""),
        "size_bytes": path.stat().st_size,
    }


def _open_validated_image(data: bytes):
    if not data:
        raise ValueError("Файл пустой")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Изображение слишком большое. Максимальный размер исходного файла – 15 МБ.")

    try:
        probe = Image.open(BytesIO(data))
        fmt = (probe.format or "").upper()
        probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ValueError("Файл не является корректным PNG или JPEG изображением")

    if fmt not in FORMAT_INFO:
        raise ValueError("Поддерживаются только PNG и JPEG")

    try:
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image.load()
    except Exception:
        raise ValueError("Не удалось прочитать изображение")

    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("Некорректный размер изображения")
    if width * height > MAX_PIXELS:
        raise ValueError("Изображение имеет слишком большое разрешение")

    return image, fmt


def save_uploaded_card(data: bytes) -> tuple[str, dict]:
    """
    Проверяет и сохраняет изображение в /app/data/cards.

    Большие изображения уменьшаются до 1600 px по длинной стороне:
    для письма этого достаточно, а размер вложения остается разумным.
    """
    image, fmt = _open_validated_image(data)
    CARD_DIR.mkdir(parents=True, exist_ok=True)

    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)

    info = FORMAT_INFO[fmt]
    filename = f"{uuid.uuid4().hex}{info['ext']}"
    path = CARD_DIR / filename

    if fmt == "JPEG":
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        elif image.mode == "L":
            image = image.convert("RGB")
        image.save(path, "JPEG", quality=88, optimize=True)
    else:
        image.save(path, "PNG", optimize=True)

    meta = card_meta(filename)
    return filename, meta


def delete_card_file(filename: str) -> None:
    path = _safe_card_path(filename)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def display_width(meta: dict) -> int:
    """
    Ограничение ширины изображения в письме по ориентации.
    Не увеличиваем маленькие изображения выше их фактической ширины.
    """
    width = int(meta.get("width") or 0)
    orientation = meta.get("orientation")
    limit = {
        "horizontal": 840,
        "square": 720,
        "vertical": 570,
    }.get(orientation, 780)

    if width <= 0:
        return limit
    return min(width, limit)
