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
from .models import LocalUser, ADAuthorizedUser, ImportRun, PositionMapping, EmployeePositionChoice, IntroTemplate, Card, MailLog, AuditLog, EmployeeSnapshot
from .security import hash_password, verify_password
from .settings_service import ensure_defaults, get_all_settings, set_settings
from .ad_auth import authenticate_ad, search_ad_users, test_ad_connection
from .services import import_xlsx, todays_employees, upcoming_birthdays, send_birthday, build_birthday_preview, latest_successful_import, current_employee_states, blocked_employee_states, birthday_send_eligibility, compose_birthday_message, employee_position_conflicts, resolved_latest_employees
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


def user_role(request):
    role = request.session.get("role")
    return role if role in {"admin", "operator"} else None


def user_display_name(request):
    return request.session.get("display_name") or user_name(request)


def _refresh_ad_session(request):
    """
    Для AD-пользователя роль проверяется на каждом защищенном запросе.
    Поэтому отзыв доступа и смена роли начинают действовать сразу.
    """
    if request.session.get("auth_type") != "ad":
        return

    ad_guid = request.session.get("ad_guid", "")
    if not ad_guid:
        request.session.clear()
        return

    with SessionLocal() as db:
        access = db.scalar(select(ADAuthorizedUser).where(
            ADAuthorizedUser.ad_guid == ad_guid,
            ADAuthorizedUser.active == True,
        ))

        if not access or access.role not in {"admin", "operator"}:
            request.session.clear()
            return

        request.session["user"] = access.login
        request.session["display_name"] = access.display_name or access.login
        request.session["role"] = access.role


def require_user(request):
    _refresh_ad_session(request)

    user = user_name(request)
    role = user_role(request)
    if not user or not role:
        raise HTTPException(401, "Требуется вход")
    return user


def require_admin(request):
    user = require_user(request)
    if user_role(request) != "admin":
        raise HTTPException(403, "Доступ только для администратора")
    return user


def page(request, template, **context):
    context.update({
        "request": request,
        "user": user_name(request),
        "display_name": user_display_name(request),
        "role": user_role(request),
        "is_admin": user_role(request) == "admin",
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
    try:
        require_user(request)
    except HTTPException:
        return RedirectResponse("/login", 303)

    latest = db.scalars(
        select(ImportRun).order_by(desc(ImportRun.received_at))
    ).first()
    active_snapshot = latest_successful_import(db)
    cfg = get_all_settings(db)

    return page(
        request,
        "index.html",
        latest=latest,
        active_snapshot=active_snapshot,
        snapshot_status=cfg.get("snapshot_status", ""),
        snapshot_status_level=cfg.get("snapshot_status_level", "info"),
        today_date=date.today(),
    )


@app.get("/dashboard-fragment", response_class=HTMLResponse)
def dashboard_fragment(request: Request, db: Session = Depends(get_db)):
    require_user(request)

    employees = resolved_latest_employees(db)
    cfg = get_all_settings(db)

    today_rows = []
    for emp in todays_employees(db, employees=employees, cfg=cfg):
        can_send, send_reason = birthday_send_eligibility(db, emp, cfg=cfg)
        today_rows.append({
            "employee": emp,
            "can_send": can_send,
            "send_reason": send_reason,
        })

    next_birthdays = upcoming_birthdays(
        db,
        days=30,
        employees=employees,
        cfg=cfg,
    )

    unconfirmed_items = list(db.scalars(
        select(PositionMapping).where(PositionMapping.confirmed == False)
    ).all())
    unconfirmed_count = sum(
        1 for item in unconfirmed_items
        if not suggest_position(item.source_position).auto_use
    )
    unconfirmed_count += sum(
        1 for item in employee_position_conflicts(db)
        if not item["resolved"]
    )

    return templates.TemplateResponse(
        request=request,
        name="dashboard_fragment.html",
        context={
            "request": request,
            "today_date": date.today(),
            "upcoming": today_rows,
            "next_birthdays": next_birthdays,
            "unconfirmed_count": unconfirmed_count,
        },
    )

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return page(request, "login.html", error=None)

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, login: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    local = db.scalar(select(LocalUser).where(LocalUser.login == login, LocalUser.active == True))
    if local and verify_password(password, local.password_hash):
        request.session.clear()
        request.session["user"] = login
        request.session["display_name"] = login
        request.session["auth_type"] = "local"
        request.session["role"] = "admin" if local.is_admin else "operator"
        return RedirectResponse("/", 303)

    ok, result = authenticate_ad(login, password, get_all_settings(db))
    if ok:
        access = db.scalar(select(ADAuthorizedUser).where(
            ADAuthorizedUser.ad_guid == result["ad_guid"],
            ADAuthorizedUser.active == True,
        ))

        if not access or access.role not in {"admin", "operator"}:
            return page(
                request,
                "login.html",
                error="Учетная запись AD не имеет доступа к системе.",
            )

        # AD мог изменить ФИО или логин. Обновляем локальную карточку,
        # не меняя назначенную администратором роль.
        access.login = result["login"]
        access.display_name = result.get("display_name") or result["login"]
        access.distinguished_name = result.get("distinguished_name", "")
        db.commit()

        request.session.clear()
        request.session["user"] = access.login
        request.session["display_name"] = access.display_name or access.login
        request.session["auth_type"] = "ad"
        request.session["ad_guid"] = access.ad_guid
        request.session["role"] = access.role
        return RedirectResponse("/", 303)

    return page(
        request,
        "login.html",
        error="Неверный логин/пароль или нет доступа к системе.",
    )

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)

def _authorized_users(db):
    return list(db.scalars(
        select(ADAuthorizedUser).order_by(
            ADAuthorizedUser.active.desc(),
            ADAuthorizedUser.display_name,
            ADAuthorizedUser.login,
        )
    ).all())


@app.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
):
    require_admin(request)

    search_results = []
    search_error = None
    query = (q or "").strip()

    if query:
        try:
            search_results = search_ad_users(
                query,
                get_all_settings(db),
                limit=20,
            )
        except Exception as exc:
            search_error = str(exc)

    assigned_by_guid = {
        item.ad_guid: item
        for item in _authorized_users(db)
    }

    for item in search_results:
        assigned = assigned_by_guid.get(item["ad_guid"])
        item["assigned_role"] = assigned.role if assigned else ""
        item["assigned_active"] = assigned.active if assigned else False

    return page(
        request,
        "users.html",
        items=_authorized_users(db),
        search_results=search_results,
        search_error=search_error,
        query=query,
        message=None,
    )


@app.post("/users/add", response_class=HTMLResponse)
async def users_add(
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_admin(request)
    form = await request.form()

    ad_guid = str(form.get("ad_guid", "")).strip()
    login = str(form.get("login", "")).strip()
    display_name = str(form.get("display_name", "")).strip()
    distinguished_name = str(form.get("distinguished_name", "")).strip()
    role = str(form.get("role", "operator")).strip()

    if role not in {"admin", "operator"}:
        role = "operator"

    if not ad_guid or not login:
        raise HTTPException(400, "Некорректная учетная запись AD")

    item = db.scalar(select(ADAuthorizedUser).where(
        ADAuthorizedUser.ad_guid == ad_guid,
    ))

    if item:
        item.login = login
        item.display_name = display_name or login
        item.distinguished_name = distinguished_name
        item.role = role
        item.active = True
    else:
        item = ADAuthorizedUser(
            ad_guid=ad_guid,
            login=login,
            display_name=display_name or login,
            distinguished_name=distinguished_name,
            role=role,
            active=True,
            added_by=actor,
        )
        db.add(item)

    db.add(AuditLog(
        actor=actor,
        action="ad_user_access_granted",
        details=f"{login}; role={role}",
    ))
    db.commit()
    return RedirectResponse("/users", 303)


@app.post("/users/{item_id}", response_class=HTMLResponse)
async def users_update(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_admin(request)
    item = db.get(ADAuthorizedUser, item_id)
    if not item:
        raise HTTPException(404)

    form = await request.form()
    role = str(form.get("role", item.role)).strip()
    if role not in {"admin", "operator"}:
        role = "operator"

    item.role = role
    item.active = "active" in form

    db.add(AuditLog(
        actor=actor,
        action="ad_user_access_changed",
        details=(
            f"{item.login}; role={item.role}; "
            f"active={'yes' if item.active else 'no'}"
        ),
    ))
    db.commit()
    return RedirectResponse("/users", 303)


@app.post("/users/{item_id}/delete")
def users_delete(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_admin(request)
    item = db.get(ADAuthorizedUser, item_id)
    if not item:
        raise HTTPException(404)

    details = f"{item.login}; role={item.role}"
    db.delete(item)
    db.add(AuditLog(
        actor=actor,
        action="ad_user_access_removed",
        details=details,
    ))
    db.commit()
    return RedirectResponse("/users", 303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    # Страницу видят и оператор, и администратор - каждому показывается
    # своя часть в settings.html (is_admin передается через page()).
    # Полная форма настроек сохраняется только через POST /settings,
    # который по-прежнему доступен лишь администратору.
    require_user(request)
    cfg = get_all_settings(db)
    return page(
        request,
        "settings.html",
        cfg=cfg,
        saved=False,
        state_saved=request.query_params.get("state_saved") == "1",
        employee_states=current_employee_states(db),
        blocked_states=blocked_employee_states(cfg),
        test_message=None,
        test_error=None,
        test_recipient="",
        ad_test_message=None,
        ad_test_error=None,
    )


@app.post("/settings/feb29-policy", response_class=HTMLResponse)
async def settings_feb29_policy(request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    form = await request.form()
    policy = str(form.get("feb29_policy", "feb28"))
    if policy not in {"feb28", "mar1"}:
        policy = "feb28"

    set_settings(db, {"feb29_policy": policy})
    db.add(AuditLog(
        actor=actor,
        action="feb29_policy_changed",
        details="28 февраля" if policy == "feb28" else "1 марта",
    ))
    db.commit()
    return RedirectResponse("/settings?state_saved=1", 303)

@app.post("/settings", response_class=HTMLResponse)
async def settings_post(request: Request, db: Session = Depends(get_db)):
    actor = require_admin(request)
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
        ad_test_message=None,
        ad_test_error=None,
    )


@app.post("/settings/test-ad")
async def settings_test_ad(
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_admin(request)

    form = await request.form()
    saved_cfg = get_all_settings(db)
    cfg = dict(saved_cfg)

    for key in [
        "ad_server",
        "ad_port",
        "ad_domain",
        "ad_base_dn",
        "ad_user_filter",
        "ad_bind_user",
    ]:
        if key in form:
            cfg[key] = str(form.get(key, "")).strip()

    cfg["ad_enabled"] = "true" if "ad_enabled" in form else "false"
    cfg["ad_ssl"] = "true" if "ad_ssl" in form else "false"

    # Если пароль введен в форме – проверяем именно его.
    # Если поле пустое – используем сохраненный пароль.
    entered_password = str(form.get("ad_bind_password", "") or "")
    if entered_password:
        cfg["ad_bind_password"] = entered_password

    ok, message = test_ad_connection(cfg)

    if ok:
        # Успешно проверенные параметры AD сразу сохраняем.
        # Поэтому поиск пользователей и следующая загрузка страницы
        # используют ровно тот пароль, который только что прошел bind.
        set_settings(db, {
            "ad_enabled": cfg.get("ad_enabled", "false"),
            "ad_server": cfg.get("ad_server", ""),
            "ad_port": cfg.get("ad_port", "389"),
            "ad_ssl": cfg.get("ad_ssl", "false"),
            "ad_domain": cfg.get("ad_domain", ""),
            "ad_base_dn": cfg.get("ad_base_dn", ""),
            "ad_user_filter": cfg.get(
                "ad_user_filter",
                "(sAMAccountName={login})",
            ),
            "ad_bind_user": cfg.get("ad_bind_user", ""),
            "ad_bind_password": cfg.get("ad_bind_password", ""),
        })
        db.add(AuditLog(
            actor=actor,
            action="ad_settings_verified_saved",
            details=(
                f"{cfg.get('ad_server', '')}:"
                f"{cfg.get('ad_port', '389')}; "
                f"{cfg.get('ad_bind_user', '')}"
            ),
        ))
        db.commit()
        message += " Настройки AD сохранены."

    return JSONResponse(
        {
            "ok": ok,
            "message": message,
            "saved": bool(ok),
        },
        status_code=200 if ok else 400,
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
            ad_test_message=None,
            ad_test_error=None,
        )

    # Для теста берем реальное поздравление одного из сотрудников,
    # чтобы проверить итоговую сборку письма и открытки.
    sample_emp = None

    for emp in todays_employees(db, cfg=cfg):
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
            ad_test_message=None,
            ad_test_error=None,
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
            ad_test_message=None,
            ad_test_error=None,
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
            ad_test_message=None,
            ad_test_error=None,
        )


@app.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    items = list(db.scalars(select(ImportRun).order_by(desc(ImportRun.received_at)).limit(50)).all())
    return page(request, "imports.html", items=items, message=None)

@app.post("/imports/upload", response_class=HTMLResponse)
async def imports_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    actor = require_admin(request)
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
    actor = require_admin(request)
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
    mapping_by_source = {item.source_position: item for item in items}

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

    multi_position_rows = []
    for conflict in employee_position_conflicts(db):
        options = []
        for source_position in conflict["positions"]:
            mapping = mapping_by_source.get(source_position)
            suggestion = suggest_position(source_position)
            source_units, source_title = split_source_position(source_position)

            display_position = ""
            if mapping and mapping.confirmed and mapping.display_position.strip():
                display_position = mapping.display_position.strip()
            elif suggestion.text:
                display_position = suggestion.text
            else:
                display_position = source_title

            options.append({
                "source_position": source_position,
                "source_units": source_units,
                "source_title": source_title,
                "display_position": display_position,
                "congratulate": mapping.congratulate if mapping else True,
            })

        multi_position_rows.append({
            **conflict,
            "options": options,
        })

    return page(
        request,
        "positions.html",
        rows=rows,
        multi_position_rows=multi_position_rows,
    )


@app.post("/positions/states")
async def positions_states_save(
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_user(request)
    form = await request.form()

    known_states = [
        str(x).strip()
        for x in form.getlist("employee_state_known")
        if str(x).strip()
    ]
    allowed_state_keys = {
        " ".join(str(x).replace("\xa0", " ").split())
        .strip().lower().replace("ё", "е")
        for x in form.getlist("employee_state_allowed")
    }

    blocked_states = [
        state
        for state in known_states
        if " ".join(state.replace("\xa0", " ").split())
        .strip().lower().replace("ё", "е")
        not in allowed_state_keys
    ]

    set_settings(db, {
        "employee_state_blocked": json.dumps(
            blocked_states,
            ensure_ascii=False,
        ),
    })
    db.add(AuditLog(
        actor=actor,
        action="employee_states_changed",
        details=(
            "Исключены из поздравлений: "
            + (", ".join(blocked_states) if blocked_states else "нет")
        ),
    ))
    db.commit()
    return RedirectResponse("/settings?state_saved=1", 303)


@app.post("/employee-position-choice")
async def employee_position_choice_save(
    request: Request,
    db: Session = Depends(get_db),
):
    actor = require_user(request)
    form = await request.form()
    employee_key = str(form.get("employee_key", "")).strip()
    source_position = str(form.get("source_position", "")).strip()

    latest = latest_successful_import(db)
    if not latest or not employee_key or not source_position:
        raise HTTPException(400, "Не выбран работник или должность")

    valid = db.scalar(select(EmployeeSnapshot).where(
        EmployeeSnapshot.import_id == latest.id,
        EmployeeSnapshot.employee_key == employee_key,
        EmployeeSnapshot.source_position == source_position,
    ))
    if not valid:
        raise HTTPException(400, "Выбранная должность отсутствует в текущей выгрузке")

    item = db.scalar(select(EmployeePositionChoice).where(
        EmployeePositionChoice.employee_key == employee_key,
    ))
    if item:
        item.source_position = source_position
    else:
        item = EmployeePositionChoice(
            employee_key=employee_key,
            source_position=source_position,
        )
        db.add(item)

    db.add(AuditLog(
        actor=actor,
        action="employee_position_selected",
        details=f"{valid.fio}: {source_position}",
    ))
    db.commit()
    return RedirectResponse("/positions", 303)


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
    db.add(AuditLog(
        actor=actor,
        action="position_changed",
        details=(
            f"{item_id}: {item.display_position}; "
            f"использовать должность={'да' if item.active else 'нет'}"
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

@app.get("/today")
def today_page(request: Request):
    require_user(request)
    return RedirectResponse("/", 303)

@app.post("/today/{employee_id}/send")
def today_send(employee_id: int, request: Request, db: Session = Depends(get_db)):
    actor = require_user(request)
    emp = db.get(EmployeeSnapshot, employee_id)
    if not emp:
        raise HTTPException(404)
    send_birthday(db, emp, actor=actor)
    return RedirectResponse("/", 303)



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
