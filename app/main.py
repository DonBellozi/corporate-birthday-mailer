from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
import os, shutil, uuid

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .db import Base, engine, get_db, SessionLocal
from .models import LocalUser, ImportRun, PositionMapping, IntroTemplate, MailLog, AuditLog, EmployeeSnapshot
from .security import hash_password, verify_password
from .settings_service import ensure_defaults, get_all_settings, set_settings
from .ad_auth import authenticate_ad
from .services import import_xlsx, todays_employees, send_birthday
from .mail_service import fetch_latest_xlsx
from .rendering import validate_template
from .scheduler import start_scheduler

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def bootstrap():
    Base.metadata.create_all(bind=engine)
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
    context.update({"request": request, "user": user_name(request)})
    return templates.TemplateResponse(template, context)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    if not user_name(request):
        return RedirectResponse("/login", 303)
    latest = db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at))).first()
    upcoming = todays_employees(db)
    unconfirmed_count = len(list(db.scalars(
        select(PositionMapping).where(PositionMapping.confirmed == False)
    ).all()))
    return page(request, "index.html", latest=latest, upcoming=upcoming, unconfirmed_count=unconfirmed_count)

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
    return page(request, "settings.html", cfg=get_all_settings(db), saved=False)

@app.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    form = await request.form()
    data = {k: str(v) for k, v in form.items()}
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
    return page(request, "settings.html", cfg=get_all_settings(db), saved=True)

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
    items = list(db.scalars(select(PositionMapping).order_by(PositionMapping.confirmed, PositionMapping.source_position)).all())
    return page(request, "positions.html", items=items)

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
    db.add(AuditLog(actor=actor, action="position_changed", details=f"{item_id}: {item.display_position}"))
    db.commit()
    return RedirectResponse("/positions", 303)

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
    return page(request, "today.html", items=todays_employees(db), date=date.today())

@app.post("/today/{employee_id}/send")
def today_send(employee_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    emp = db.get(EmployeeSnapshot, employee_id)
    if not emp:
        raise HTTPException(404)
    send_birthday(db, emp, actor=actor)
    return RedirectResponse("/today", 303)

@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db)):
    require_user(request)
    mails = list(db.scalars(select(MailLog).order_by(desc(MailLog.id)).limit(200)).all())
    audits = list(db.scalars(select(AuditLog).order_by(desc(AuditLog.id)).limit(200)).all())
    return page(request, "logs.html", mails=mails, audits=audits)
