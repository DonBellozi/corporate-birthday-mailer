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

    client = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
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
