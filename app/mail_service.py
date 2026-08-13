import email
import imaplib
import smtplib
import ssl
import uuid
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path

def decode_header_value(value):
    if not value:
        return ""
    chunks = []
    for part, enc in decode_header(value):
        chunks.append(part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part)
    return "".join(chunks)

def fetch_latest_xlsx(cfg: dict[str, str], target_dir: Path):
    host = cfg.get("imap_host", "").strip()
    if not host:
        raise ValueError("IMAP не настроен")
    port = int(cfg.get("imap_port", "993"))
    use_ssl = cfg.get("imap_ssl", "true").lower() == "true"

    client = (
        imaplib.IMAP4_SSL(host, port, timeout=30) if use_ssl
        else imaplib.IMAP4(host, port, timeout=30)
    )
    try:
        client.login(cfg.get("imap_login", ""), cfg.get("imap_password", ""))
        client.select(cfg.get("imap_folder", "INBOX") or "INBOX")

        criteria = ["ALL"]
        sender = cfg.get("imap_sender_filter", "").strip()
        subject = cfg.get("imap_subject_filter", "").strip()
        if sender:
            criteria += ["FROM", f'"{sender}"']
        if subject:
            criteria += ["SUBJECT", f'"{subject}"']

        status, data = client.search(None, *criteria)
        if status != "OK":
            raise RuntimeError("Ошибка IMAP SEARCH")

        for msg_id in reversed(data[0].split()):
            status, msg_data = client.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            for part in msg.walk():
                filename = decode_header_value(part.get_filename())
                if filename.lower().endswith(".xlsx"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        target_dir.mkdir(parents=True, exist_ok=True)
                        target = target_dir / f"{uuid.uuid4().hex}_{Path(filename).name}"
                        target.write_bytes(payload)
                        return target
        return None
    finally:
        try:
            client.logout()
        except Exception:
            pass

def _imap_search_criteria(cfg: dict[str, str]) -> list[str]:
    criteria = ["ALL"]
    sender = cfg.get("imap_sender_filter", "").strip()
    subject = cfg.get("imap_subject_filter", "").strip()

    if sender:
        criteria += ["FROM", f'"{sender}"']
    if subject:
        criteria += ["SUBJECT", f'"{subject}"']

    return criteria


def _imap_uidvalidity(client) -> str:
    """
    UID действителен только внутри конкретной версии IMAP-папки.
    UIDVALIDITY меняется, если папка была пересоздана/сброшена.
    """
    try:
        _, data = client.response("UIDVALIDITY")
        if not data:
            return ""
        value = data[-1]
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        return str(value or "").strip()
    except Exception:
        return ""


def _message_bytes_from_fetch(msg_data):
    for item in msg_data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _save_xlsx_from_message(raw_message: bytes, target_dir: Path):
    msg = email.message_from_bytes(raw_message)

    for part in msg.walk():
        filename = decode_header_value(part.get_filename())
        if not filename.lower().endswith(".xlsx"):
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid.uuid4().hex}_{Path(filename).name}"
        target.write_bytes(payload)
        return target

    return None


def fetch_latest_xlsx_if_new(
    cfg: dict[str, str],
    target_dir: Path,
    last_uid: str = "",
    last_uidvalidity: str = "",
):
    """
    Легкая проверка IMAP для планировщика.

    Сначала выполняется UID SEARCH: это не скачивает тело писем и вложения.
    Если после last_uid новых подходящих писем нет, возвращаемся сразу.

    Полное тело BODY.PEEK[] скачивается только для новых UID. Вложение XLSX
    сохраняется только из нового письма. UIDVALIDITY защищает от ситуации,
    когда IMAP-папка была пересоздана и счетчик UID начал новую историю.

    Возвращает словарь:
      new_message   - появились ли новые подходящие UID;
      path          - путь к XLSX или None;
      uid           - максимальный проверенный UID;
      uidvalidity   - текущий UIDVALIDITY папки;
      message_uid   - UID письма, из которого взят XLSX (если найден).
    """
    host = cfg.get("imap_host", "").strip()
    if not host:
        raise ValueError("IMAP не настроен")

    port = int(cfg.get("imap_port", "993"))
    use_ssl = cfg.get("imap_ssl", "true").lower() == "true"
    # Таймаут обязателен: без него зависший коннект к недоступному серверу
    # блокирует поток планировщика навсегда. APScheduler по умолчанию не
    # запускает следующий тик, пока не завершился предыдущий (max_instances=1
    # для задачи "main_tick"), поэтому одно такое зависание тихо
    # останавливает не только проверку почты, но и рассылку дней рождения -
    # они выполняются в одном и том же тике.
    client = (
        imaplib.IMAP4_SSL(host, port, timeout=30) if use_ssl
        else imaplib.IMAP4(host, port, timeout=30)
    )

    try:
        client.login(cfg.get("imap_login", ""), cfg.get("imap_password", ""))
        status, _ = client.select(cfg.get("imap_folder", "INBOX") or "INBOX")
        if status != "OK":
            raise RuntimeError("Ошибка IMAP SELECT")

        uidvalidity = _imap_uidvalidity(client)
        criteria = _imap_search_criteria(cfg)

        status, data = client.uid("SEARCH", None, *criteria)
        if status != "OK":
            raise RuntimeError("Ошибка IMAP UID SEARCH")

        uids = data[0].split() if data and data[0] else []
        if not uids:
            return {
                "new_message": False,
                "path": None,
                "uid": "",
                "uidvalidity": uidvalidity,
                "message_uid": "",
            }

        # Если UIDVALIDITY изменился, старый UID относится уже к другой
        # версии папки и сравнивать с ним нельзя.
        same_mailbox_generation = (
            not last_uidvalidity
            or not uidvalidity
            or last_uidvalidity == uidvalidity
        )

        try:
            last_uid_number = int(last_uid) if same_mailbox_generation and last_uid else 0
        except (TypeError, ValueError):
            last_uid_number = 0

        parsed_uids = []
        for raw_uid in uids:
            try:
                parsed_uids.append((int(raw_uid), raw_uid))
            except (TypeError, ValueError):
                continue

        if not parsed_uids:
            return {
                "new_message": False,
                "path": None,
                "uid": "",
                "uidvalidity": uidvalidity,
                "message_uid": "",
            }

        parsed_uids.sort(key=lambda item: item[0])
        latest_uid_number = parsed_uids[-1][0]
        latest_uid = str(latest_uid_number)

        new_uids = [
            raw_uid
            for uid_number, raw_uid in parsed_uids
            if uid_number > last_uid_number
        ]

        if not new_uids:
            return {
                "new_message": False,
                "path": None,
                "uid": latest_uid,
                "uidvalidity": uidvalidity,
                "message_uid": "",
            }

        # Смотрим только новые письма, начиная с самого свежего. BODY.PEEK[]
        # не меняет флаг Seen в почтовом ящике.
        for raw_uid in reversed(new_uids):
            status, msg_data = client.uid("FETCH", raw_uid, "(BODY.PEEK[])")
            if status != "OK":
                continue

            raw_message = _message_bytes_from_fetch(msg_data)
            if not raw_message:
                continue

            path = _save_xlsx_from_message(raw_message, target_dir)
            if path:
                message_uid = (
                    raw_uid.decode("ascii", errors="ignore")
                    if isinstance(raw_uid, bytes)
                    else str(raw_uid)
                )
                return {
                    "new_message": True,
                    "path": path,
                    "uid": latest_uid,
                    "uidvalidity": uidvalidity,
                    "message_uid": message_uid,
                }

        # Новые подходящие письма были, но XLSX в них нет. Их все равно
        # считаем проверенными, чтобы не скачивать их заново каждые 15 минут.
        return {
            "new_message": True,
            "path": None,
            "uid": latest_uid,
            "uidvalidity": uidvalidity,
            "message_uid": "",
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass

def send_html_mail(cfg, subject, recipient, html_body, text_body="", inline_image=None):
    host = cfg.get("smtp_host", "").strip()
    if not host:
        raise ValueError("SMTP не настроен")

    port = int(cfg.get("smtp_port", "587"))
    use_ssl = cfg.get("smtp_ssl", "false").lower() == "true"
    starttls = cfg.get("smtp_starttls", "true").lower() == "true"
    login = cfg.get("smtp_login", "")
    sender = cfg.get("mail_from", "").strip() or login

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(text_body or "Поздравление с Днем рождения")
    msg.add_alternative(html_body, subtype="html")

    if inline_image:
        path = Path(inline_image["path"])
        data = path.read_bytes()
        mime = inline_image.get("mime", "image/jpeg")
        maintype, subtype = mime.split("/", 1)
        cid = inline_image.get("cid", "birthday-card")
        html_part = msg.get_payload()[-1]
        html_part.add_related(
            data,
            maintype=maintype,
            subtype=subtype,
            cid=f"<{cid}>",
            filename=inline_image.get("filename") or path.name,
            disposition="inline",
        )

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
            if login:
                smtp.login(login, cfg.get("smtp_password", ""))
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if starttls:
                smtp.starttls(context=context)
            if login:
                smtp.login(login, cfg.get("smtp_password", ""))
            smtp.send_message(msg)
