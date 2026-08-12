from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import os, shutil, uuid, json

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select, inspect, text as sql_text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Base, engine, get_db, SessionLocal
from .models import LocalUser, ImportRun, PositionMapping, IntroTemplate, Card, MailLog, AuditLog, EmployeeSnapshot
from .security import hash_password, verify_password
from .settings_service import ensure_defaults, get_all_settings, set_settings
from .ad_auth import authenticate_ad
from .services import import_xlsx, todays_employees, upcoming_birthdays, send_birthday, build_birthday_preview, latest_successful_import, current_employee_states, blocked_employee_states, birthday_send_eligibility, compose_birthday_message
from .mail_service import fetch_latest_xlsx, send_html_mail
from .rendering import validate_template
from .scheduler import start_scheduler
from .position_suggester import suggest_position, split_source_position
from .card_service import save_uploaded_card, delete_card_file, card_file_path, card_meta

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def bootstrap():
    Base.metadata.create_all(bind=engine)

    # create_all() не добавляет новые колонки в уже существующие таблицы.
    # Делаем маленькую безопасную миграцию для поля "Состояние".
    columns = {col["name"] for col in inspect(engine).get_columns("employee_snapshots")}
    if "employee_state" not in columns:
        with engine.begin() as conn:
            conn.execute(sql_text(
                "ALTER TABLE employee_snapshots "
                "ADD COLUMN employee_state VARCHAR(200) NOT NULL DEFAULT ''"
            ))

    if "work_email" not in columns:
        with engine.begin() as conn:
            conn.execute(sql_text(
                "ALTER TABLE employee_snapshots "
                "ADD COLUMN work_email VARCHAR(500) NOT NULL DEFAULT ''"
            ))

    position_columns = {
        col["name"] for col in inspect(engine).get_columns("position_mappings")
    }
    if "congratulate" not in position_columns:
        with engine.begin() as conn:
            conn.execute(sql_text(
                "ALTER TABLE position_mappings "
                "ADD COLUMN congratulate BOOLEAN NOT NULL DEFAULT TRUE"
            ))

    with SessionLocal() as db:
        ensure_defaults(db)
        login = os.getenv("BOOTSTRAP_ADMIN_LOGIN", "admin")
        password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!")
        if not db.scalar(select(LocalUser).where(LocalUser.login == login)):
            db.add(LocalUser(login=login, password_hash=hash_password(password), is_admin=True))
        if not db.scalar(select(IntroTemplate).limit(1)):
            seeds = [
                "Сегодня особенный и праздничный день: свой День рождения отмечает {{ colleague.nom }} – {{ fio }}.",
                "Сегодня у нас замечательный повод для поздравлений: День рождения отмечает {{ colleague.nom }} – {{ fio }}.",
                "Сегодня в нашем коллективе праздничное событие: свой День рождения отмечает {{ colleague.nom }} – {{ fio }}.",
                "Сегодня есть прекрасный повод для теплых поздравлений: свой День рождения отмечает {{ colleague.nom }} – {{ fio }}.",
                "Сегодняшний день начинается с приятного события: День рождения отмечает {{ colleague.nom }} – {{ fio }}.",
            ]
            for i, body in enumerate(seeds, 1):
                db.add(IntroTemplate(name=f"Вариант {i}", body=body))
        db.commit()

@asynccontextmanager
async def lifespan(app):
    bootstrap()
    start_scheduler()
    yield

app = FastAPI(title="Добрый день", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY", "CHANGE_ME"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

def user_name(request):
    return request.session.get("user")

def require_user(request):
    user = user_name(request)
    if not user:
        raise HTTPException(401, "Требуется вход")
    return user

def page(request, template, **context):
    context.update({
        "request": request,
        "user": user_name(request),
    })
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=context,
    )

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    if not user_name(request):
        return RedirectResponse("/login", 303)
    latest = db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at))).first()
    active_snapshot = latest_successful_import(db)
    cfg = get_all_settings(db)
    snapshot_status = cfg.get("snapshot_status", "")
    snapshot_status_level = cfg.get("snapshot_status_level", "info")

    today_birthdays = todays_employees(db)
    today_rows = []
    for emp in today_birthdays:
        can_send, send_reason = birthday_send_eligibility(db, emp)
        today_rows.append({
            "employee": emp,
            "can_send": can_send,
            "send_reason": send_reason,
        })

    next_birthdays = upcoming_birthdays(db, days=30)
    unconfirmed_items = list(db.scalars(
        select(PositionMapping).where(PositionMapping.confirmed == False)
    ).all())
    unconfirmed_count = sum(
        1 for item in unconfirmed_items
        if not suggest_position(item.source_position).auto_use
    )
    return page(
        request,
        "index.html",
        latest=latest,
        upcoming=today_rows,
        next_birthdays=next_birthdays,
        unconfirmed_count=unconfirmed_count,
        active_snapshot=active_snapshot,
        snapshot_status=snapshot_status,
        snapshot_status_level=snapshot_status_level,
    )

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "login.html", error=None)

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, login: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    local = db.scalar(select(LocalUser).where(LocalUser.login == login, LocalUser.active == True))
    if local and verify_password(password, local.password_hash):
        request.session["user"] = login
        request.session["auth_type"] = "local"
        return RedirectResponse("/", 303)

    ok, _ = authenticate_ad(login, password, get_all_settings(db))
    if ok:
        request.session["user"] = login
        request.session["auth_type"] = "ad"
        return RedirectResponse("/", 303)

    return page(request, "login.html", error="Неверный логин/пароль или нет доступа через AD.")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    cfg = get_all_settings(db)
    return page(
        request,
        "settings.html",
        cfg=cfg,
        saved=False,
        employee_states=current_employee_states(db),
        blocked_states=blocked_employee_states(cfg),
        test_message=None,
        test_error=None,
        test_recipient="",
    )

@app.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}

    # Селектор состояний: в форме передаем полный набор показанных
    # состояний и отдельно те, которым разрешена отправка.
    known_states = [
        str(x).strip() for x in form.getlist("employee_state_known")
        if str(x).strip()
    ]
    allowed_state_keys = {
        " ".join(str(x).replace("\xa0", " ").split()).strip().lower().replace("ё", "е")
        for x in form.getlist("employee_state_allowed")
    }
    blocked_states = [
        state for state in known_states
        if " ".join(state.replace("\xa0", " ").split()).strip().lower().replace("ё", "е")
        not in allowed_state_keys
    ]
    data["employee_state_blocked"] = json.dumps(
        blocked_states,
        ensure_ascii=False,
    )

    for checkbox in [
        "auto_send_enabled","wishes_enabled","cards_enabled","positions_enabled",
        "imap_ssl","smtp_starttls","smtp_ssl","ad_enabled","ad_ssl"
    ]:
        data[checkbox] = "true" if checkbox in form else "false"

    old = get_all_settings(db)
    for secret in ["imap_password","smtp_password","ad_bind_password"]:
        if not data.get(secret):
            data[secret] = old.get(secret, "")

    set_settings(db, data)
    db.add(AuditLog(actor=actor, action="settings_changed", details="Изменены настройки"))
    db.commit()
    cfg = get_all_settings(db)
    return page(
        request,
        "settings.html",
        cfg=cfg,
        saved=True,
        employee_states=current_employee_states(db),
        blocked_states=blocked_employee_states(cfg),
        test_message=None,
        test_error=None,
        test_recipient="",
    )


@app.post("/settings/test-mail", response_class=HTMLResponse)
def settings_test_mail(
    request: Request,
    test_recipient: str = Form(...),
    db: Session = Depends(get_db),
):
    actor = require_user(request)
    cfg = get_all_settings(db)
    recipient = test_recipient.strip()

    if not recipient or "@" not in recipient:
        return page(
            request,
            "settings.html",
            cfg=cfg,
            saved=False,
            employee_states=current_employee_states(db),
            blocked_states=blocked_employee_states(cfg),
            test_message=None,
            test_error="Укажите корректный адрес для тестовой отправки.",
            test_recipient=recipient,
        )

    # Для теста берем реальное поздравление одного из сотрудников,
    # чтобы проверить итоговую сборку письма и открытки.
    sample_emp = None

    for emp in todays_employees(db):
        can_send, _ = birthday_send_eligibility(db, emp)
        if can_send:
            sample_emp = emp
            break

    if sample_emp is None:
        for item in upcoming_birthdays(db, days=365):
            if item.get("can_send"):
                sample_emp = item["employee"]
                break

    if sample_emp is None:
        latest = latest_successful_import(db)
        if latest:
            for emp in db.scalars(
                select(EmployeeSnapshot)
                .where(EmployeeSnapshot.import_id == latest.id)
                .order_by(EmployeeSnapshot.fio)
            ).all():
                can_send, _ = birthday_send_eligibility(db, emp)
                if can_send:
                    sample_emp = emp
                    break

    if sample_emp is None:
        return page(
            request,
            "settings.html",
            cfg=cfg,
            saved=False,
            employee_states=current_employee_states(db),
            blocked_states=blocked_employee_states(cfg),
            test_message=None,
            test_error=(
                "Не найден ни один сотрудник, подходящий для тестовой отправки. "
                "Проверьте, что импортирована кадровая выгрузка и есть хотя бы один "
                "работник, не исключенный из поздравлений."
            ),
            test_recipient=recipient,
        )

    try:
        message = compose_birthday_message(db, sample_emp)

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
            cfg,
            message["subject"],
            recipient,
            message["html"],
            message["rendered"],
            inline_image=inline_image,
        )
        db.add(AuditLog(
            actor=actor,
            action="test_mail_sent",
            details=f"{recipient}; sample={sample_emp.fio}",
        ))
        db.commit()

        return page(
            request,
            "settings.html",
            cfg=cfg,
            saved=False,
            employee_states=current_employee_states(db),
            blocked_states=blocked_employee_states(cfg),
            test_message=(
                f"Тестовое сообщение отправлено на {recipient}. "
                f"Использовано поздравление для: {sample_emp.fio}."
            ),
            test_error=None,
            test_recipient=recipient,
        )
    except Exception as exc:
        db.add(AuditLog(
            actor=actor,
            action="test_mail_failed",
            details=f"{recipient}: {exc}",
        ))
        db.commit()

        return page(
            request,
            "settings.html",
            cfg=cfg,
            saved=False,
            employee_states=current_employee_states(db),
            blocked_states=blocked_employee_states(cfg),
            test_message=None,
            test_error=f"Не удалось отправить тестовое сообщение: {exc}",
            test_recipient=recipient,
        )


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    items = list(db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at)).limit(50)).all())
    return page(request, "imports.html", items=items, message=None)

@app.post("/imports/upload", response_class=HTMLResponse)
async def imports_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    actor = require_user(request)
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Нужен .xlsx")
    directory = Path("/app/data/imports")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4().hex}_{Path(file.filename).name}"
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        run = import_xlsx(db, target, source="manual")
        message = f"Импортировано работников: {run.rows_valid}."
        db.add(AuditLog(actor=actor, action="xlsx_imported", details=file.filename))
        db.commit()
    except Exception as exc:
        message = f"Ошибка импорта: {exc}"
    items = list(db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at)).limit(50)).all())
    return page(request, "imports.html", items=items, message=message)

@app.post("/imports/fetch-mail", response_class=HTMLResponse)
def imports_fetch(request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    try:
        path = fetch_latest_xlsx(get_all_settings(db), Path("/app/data/imports"))
        if not path:
            message = "Подходящее XLSX-вложение не найдено."
        else:
            run = import_xlsx(db, path, source="imap")
            message = f"Получено и импортировано: {run.rows_valid} работников."
            db.add(AuditLog(actor=actor, action="imap_import", details=path.name))
            db.commit()
    except Exception as exc:
        message = f"Ошибка получения: {exc}"
    items = list(db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at)).limit(50)).all())
    return page(request, "imports.html", items=items, message=message)

@app.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    items = list(db.scalars(
        select(PositionMapping).order_by(PositionMapping.confirmed, PositionMapping.source_position)
    ).all())

    rows = []
    for item in items:
        suggestion = suggest_position(item.source_position)
        source_units, source_title = split_source_position(item.source_position)
        rows.append({
            "item": item,
            "suggestion": suggestion,
            "input_value": item.display_position if item.display_position else suggestion.text,
            "source_units": source_units,
            "source_title": source_title,
        })

    return page(request, "positions.html", rows=rows)

@app.post("/positions/{item_id}")
async def positions_save(item_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    item = db.get(PositionMapping, item_id)
    if not item:
        raise HTTPException(404)
    form = await request.form()
    item.display_position = str(form.get("display_position", "")).strip()
    item.confirmed = bool(item.display_position)
    item.active = "active" in form
    item.congratulate = "congratulate" in form
    db.add(AuditLog(
        actor=actor,
        action="position_changed",
        details=(
            f"{item_id}: {item.display_position}; "
            f"использовать должность={'да' if item.active else 'нет'}; "
            f"поздравлять={'да' if item.congratulate else 'нет'}"
        ),
    ))
    db.commit()
    return RedirectResponse("/positions", 303)


def _card_rows(db):
    items = list(db.scalars(
        select(Card).order_by(Card.active.desc(), Card.created_at.desc(), Card.id.desc())
    ).all())
    return [
        {
            "item": item,
            "meta": card_meta(item.filename),
        }
        for item in items
    ]


@app.get("/cards", response_class=HTMLResponse)
def cards_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    cfg = get_all_settings(db)
    return page(
        request,
        "cards.html",
        rows=_card_rows(db),
        cards_enabled=cfg.get("cards_enabled") == "true",
        error=None,
        message=None,
    )


@app.post("/cards/upload", response_class=HTMLResponse)
async def cards_upload(
    request: Request,
    name: str = Form(""),
    gender: str = Form("universal"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    actor = require_user(request)
    cfg = get_all_settings(db)

    if gender not in {"male", "female", "universal"}:
        gender = "universal"

    try:
        data = await file.read()
        filename, meta = save_uploaded_card(data)
        display_name = name.strip() or Path(file.filename or "Открытка").stem

        item = Card(
            name=display_name[:200],
            gender=gender,
            filename=filename,
            active=True,
            uploaded_by=actor,
        )
        db.add(item)
        db.add(AuditLog(
            actor=actor,
            action="card_uploaded",
            details=f"{display_name}; {meta['width']}x{meta['height']}",
        ))
        db.commit()

        return page(
            request,
            "cards.html",
            rows=_card_rows(db),
            cards_enabled=cfg.get("cards_enabled") == "true",
            error=None,
            message="Изображение добавлено в фотобанк.",
        )
    except Exception as exc:
        return page(
            request,
            "cards.html",
            rows=_card_rows(db),
            cards_enabled=cfg.get("cards_enabled") == "true",
            error=str(exc),
            message=None,
        )


@app.post("/cards/{item_id}")
async def cards_save(item_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    item = db.get(Card, item_id)
    if not item:
        raise HTTPException(404)

    form = await request.form()
    gender = str(form.get("gender", "universal"))
    if gender not in {"male", "female", "universal"}:
        gender = "universal"

    item.name = str(form.get("name", item.name)).strip()[:200] or item.name
    item.gender = gender
    item.active = "active" in form

    db.add(AuditLog(
        actor=actor,
        action="card_changed",
        details=f"{item.id}: {item.name}",
    ))
    db.commit()
    return RedirectResponse("/cards", 303)


@app.post("/cards/{item_id}/delete")
def cards_delete(item_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    item = db.get(Card, item_id)
    if not item:
        raise HTTPException(404)

    filename = item.filename
    name = item.name
    db.delete(item)
    db.add(AuditLog(
        actor=actor,
        action="card_deleted",
        details=name,
    ))
    db.commit()
    delete_card_file(filename)
    return RedirectResponse("/cards", 303)


@app.get("/cards/{item_id}/image")
def card_image(item_id: int, request: Request, db: Session = Depends(get_db)):
    require_user(request)
    item = db.get(Card, item_id)
    if not item:
        raise HTTPException(404)

    path = card_file_path(item.filename)
    meta = card_meta(item.filename)
    if not path.exists() or not meta.get("exists"):
        raise HTTPException(404, "Файл изображения отсутствует")

    return FileResponse(
        path,
        media_type=meta.get("mime") or "application/octet-stream",
        filename=None,
    )



@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    items = list(db.scalars(select(IntroTemplate).order_by(IntroTemplate.id)).all())
    return page(request, "templates.html", items=items, error=None)

@app.post("/templates/new")
def template_new(request: Request, name: str = Form(...), body: str = Form(...), db: Session = Depends(get_db)):
    actor = require_user(request)
    unknown = validate_template(body)
    if unknown:
        items = list(db.scalars(select(IntroTemplate).order_by(IntroTemplate.id)).all())
        return page(request, "templates.html", items=items, error="Неизвестные переменные: " + ", ".join(unknown))
    db.add(IntroTemplate(name=name.strip(), body=body.strip(), active=True))
    db.add(AuditLog(actor=actor, action="intro_created", details=name))
    db.commit()
    return RedirectResponse("/templates", 303)

@app.post("/templates/{item_id}")
async def template_save(item_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    item = db.get(IntroTemplate, item_id)
    if not item:
        raise HTTPException(404)
    form = await request.form()
    body = str(form.get("body", "")).strip()
    unknown = validate_template(body)
    if unknown:
        raise HTTPException(400, "Неизвестные переменные: " + ", ".join(unknown))
    item.name = str(form.get("name", item.name)).strip()
    item.body = body
    item.active = "active" in form
    db.add(AuditLog(actor=actor, action="intro_changed", details=item.name))
    db.commit()
    return RedirectResponse("/templates", 303)

@app.post("/templates/{item_id}/delete")
def template_delete(item_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    item = db.get(IntroTemplate, item_id)
    if item:
        db.add(AuditLog(actor=actor, action="intro_deleted", details=item.name))
        db.delete(item)
        db.commit()
    return RedirectResponse("/templates", 303)

@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    rows = []
    for emp in todays_employees(db):
        can_send, send_reason = birthday_send_eligibility(db, emp)
        rows.append({
            "employee": emp,
            "can_send": can_send,
            "send_reason": send_reason,
        })
    return page(request, "today.html", rows=rows, date=date.today())

@app.post("/today/{employee_id}/send")
def today_send(employee_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    emp = db.get(EmployeeSnapshot, employee_id)
    if not emp:
        raise HTTPException(404)
    send_birthday(db, emp, actor=actor)
    return RedirectResponse("/today", 303)



@app.get("/preview/{employee_id}")
def preview_birthday(employee_id: int, request: Request, db: Session = Depends(get_db)):
    require_user(request)

    emp = db.get(EmployeeSnapshot, employee_id)
    if not emp:
        raise HTTPException(404, "Сотрудник не найден")

    try:
        data = build_birthday_preview(db, emp)
        return JSONResponse(data)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=400,
        )

@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    mails = list(db.scalars(select(MailLog).order_by(desc(MailLog.id)).limit(200)).all())
    audits = list(db.scalars(select(AuditLog).order_by(desc(AuditLog.id)).limit(200)).all())
    return page(request, "logs.html", mails=mails, audits=audits)
