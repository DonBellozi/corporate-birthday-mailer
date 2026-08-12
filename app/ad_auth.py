import uuid

from ldap3 import ALL, Connection, Server, SUBTREE, NTLM
from ldap3.core.exceptions import (
    LDAPException,
    LDAPInvalidCredentialsResult,
)
from ldap3.utils.conv import escape_filter_chars


def _bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _server(cfg: dict[str, str]) -> Server:
    host = cfg.get("ad_server", "").strip()
    if not host:
        raise RuntimeError("Не указан сервер Active Directory")

    use_ssl = _bool(cfg.get("ad_ssl", "false"))
    default_port = "636" if use_ssl else "389"
    port = int(cfg.get("ad_port", default_port) or default_port)

    return Server(
        host,
        port=port,
        use_ssl=use_ssl,
        get_info=ALL,
    )


def _short_login(login: str) -> str:
    """
    Возвращает sAMAccountName независимо от формы:
      DOMAIN\\ivanov
      ivanov@domain.ru
      ivanov
    """
    value = (login or "").strip()
    if "\\" in value:
        value = value.rsplit("\\", 1)[-1]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value.strip()


def _bind_name(login: str, cfg: dict[str, str]) -> str:
    """
    Короткий логин дополняется NetBIOS-доменом.
    Полные DOMAIN\\login и user@domain оставляем как есть.
    """
    value = (login or "").strip()
    if not value:
        raise RuntimeError("Не указан логин Active Directory")

    if "\\" in value or "@" in value:
        return value

    netbios_domain = cfg.get("ad_domain", "").strip()
    if not netbios_domain:
        raise RuntimeError(
            "В настройках Active Directory не указан домен NetBIOS"
        )

    return f"{netbios_domain}\\{value}"


def _guid_text(value) -> str:
    if value is None:
        return ""

    if isinstance(value, bytes):
        try:
            return str(uuid.UUID(bytes_le=value))
        except Exception:
            return value.hex()

    text = str(value).strip().strip("{}")
    try:
        return str(uuid.UUID(text))
    except Exception:
        return text


def _entry_identity(entry) -> dict:
    login_attr = getattr(entry, "sAMAccountName", None)
    login = str(getattr(login_attr, "value", login_attr) or "").strip()

    display_attr = getattr(entry, "displayName", None)
    display_name = str(
        getattr(display_attr, "value", display_attr) or login
    ).strip()

    guid_attr = getattr(entry, "objectGUID", None)
    guid = _guid_text(getattr(guid_attr, "value", guid_attr))

    uac_attr = getattr(entry, "userAccountControl", None)
    uac_value = getattr(uac_attr, "value", uac_attr)
    try:
        disabled = bool(int(uac_value or 0) & 2)
    except Exception:
        disabled = False

    return {
        "ad_guid": guid,
        "login": login,
        "display_name": display_name,
        "distinguished_name": entry.entry_dn,
        "disabled": disabled,
    }


def _escape_login(value: str) -> str:
    return escape_filter_chars(value or "")


def _find_user_by_login(
    login: str,
    cfg: dict[str, str],
    connection: Connection,
) -> dict:
    clean_login = _short_login(login)
    if not clean_login:
        raise RuntimeError("Не указан логин Active Directory")

    base_dn = cfg.get("ad_base_dn", "").strip()
    if not base_dn:
        raise RuntimeError("Не указан Base DN Active Directory")

    template = (
        cfg.get("ad_user_filter", "(sAMAccountName={login})").strip()
        or "(sAMAccountName={login})"
    )
    search_filter = template.replace(
        "{login}",
        _escape_login(clean_login),
    )

    ok = connection.search(
        search_base=base_dn,
        search_filter=search_filter,
        search_scope=SUBTREE,
        attributes=[
            "objectGUID",
            "sAMAccountName",
            "displayName",
            "userAccountControl",
            "distinguishedName",
        ],
        size_limit=2,
    )

    if not ok or len(connection.entries) != 1:
        raise RuntimeError("Пользователь Active Directory не найден")

    identity = _entry_identity(connection.entries[0])

    if identity["disabled"]:
        raise RuntimeError("Учетная запись Active Directory отключена")

    return identity



def _dns_domain_from_base_dn(cfg: dict[str, str]) -> str:
    """
    DC=example,DC=local -> example.local
    """
    base_dn = cfg.get("ad_base_dn", "").strip()
    parts = []
    for item in base_dn.split(","):
        item = item.strip()
        if item[:3].lower() == "dc=" and len(item) > 3:
            parts.append(item[3:].strip())
    return ".".join(x for x in parts if x)


def _service_bind_candidates(cfg: dict[str, str]) -> list[tuple[str, str]]:
    """
    Возвращает варианты (имя, тип аутентификации).

    simple:
      - значение как введено;
      - NETBIOS\\login;
      - login@dns-domain.

    ntlm:
      - NETBIOS\\login.
    """
    raw = cfg.get("ad_bind_user", "").strip()
    if not raw:
        return []

    short = _short_login(raw)
    netbios = cfg.get("ad_domain", "").strip()
    dns_domain = _dns_domain_from_base_dn(cfg)

    candidates = []

    def add(user: str, auth: str = "simple"):
        user = (user or "").strip()
        key = (user.casefold(), auth)
        if user and key not in {
            (x[0].casefold(), x[1]) for x in candidates
        }:
            candidates.append((user, auth))

    # 1. Ровно то, что ввел администратор.
    add(raw, "simple")

    # 2. Стандартное down-level имя.
    if short and netbios:
        add(f"{netbios}\\{short}", "simple")

    # 3. UPN, если DNS-домен можно восстановить из Base DN.
    if short and dns_domain:
        add(f"{short}@{dns_domain}", "simple")

    # 4. NTLM для DOMAIN\\user.
    if short and netbios:
        add(f"{netbios}\\{short}", "ntlm")

    return candidates


def _connect_service_account(
    cfg: dict[str, str],
) -> tuple[Connection, str]:
    """
    Пробуем несколько форм имени. Bind выполняем вручную, чтобы при отказе
    сохранить полный connection.result от контроллера домена.
    """
    password = cfg.get("ad_bind_password", "")
    if not password:
        raise RuntimeError(
            "Не указан пароль служебной учетной записи Active Directory"
        )

    candidates = _service_bind_candidates(cfg)
    if not candidates:
        raise RuntimeError(
            "Не указана служебная учетная запись Active Directory"
        )

    errors = []

    for user, auth in candidates:
        conn = None
        try:
            kwargs = {
                "user": user,
                "password": password,
                "auto_bind": False,
                "raise_exceptions": False,
            }
            if auth == "ntlm":
                kwargs["authentication"] = NTLM

            conn = Connection(
                _server(cfg),
                **kwargs,
            )

            if conn.bind():
                label = (
                    f"{user} · NTLM"
                    if auth == "ntlm"
                    else f"{user} · SIMPLE"
                )
                return conn, label

            result = conn.result or {}
            code = result.get("result", "")
            description = result.get("description", "")
            message = str(result.get("message", "") or "").strip()
            dn = str(result.get("dn", "") or "").strip()

            detail = f"result={code} {description}".strip()
            if message:
                detail += f"; message={message}"
            if dn:
                detail += f"; dn={dn}"

            errors.append(
                f"{user} · {auth.upper()}: {detail}"
            )

        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            errors.append(
                f"{user} · {auth.upper()}: "
                f"{exc.__class__.__name__}: {detail}"
            )
        finally:
            if conn is not None and not conn.bound:
                try:
                    conn.unbind()
                except Exception:
                    pass

    raise RuntimeError(
        "Не удалось выполнить bind служебной учетной записи. "
        "Ответы контроллера: " + " | ".join(errors)
    )


def _search_connection(cfg: dict[str, str]) -> Connection:
    """
    Служебная УЗ нужна только для поиска пользователей.
    Формат имени подбирается автоматически.
    """
    conn, _ = _connect_service_account(cfg)
    return conn


def test_ad_connection(cfg: dict[str, str]) -> tuple[bool, str]:
    """
    Проверяет служебное подключение и показывает, какая форма bind сработала.
    """
    if not _bool(cfg.get("ad_enabled", "false")):
        return False, "Авторизация через Active Directory отключена"

    try:
        conn, bind_label = _connect_service_account(cfg)
        conn.unbind()
        mode = "LDAPS" if _bool(cfg.get("ad_ssl", "false")) else "LDAP"
        return True, (
            "Подключение к Active Directory выполнено успешно. "
            f"{mode}, порт {cfg.get('ad_port', '389')}; "
            f"bind: {bind_label}."
        )
    except Exception as exc:
        return False, str(exc)


def search_ad_users(
    query: str,
    cfg: dict[str, str],
    limit: int = 20,
) -> list[dict]:
    """
    Поиск по фамилии, ФИО, логину или UPN.
    Выполняется под служебной учетной записью.
    """
    if not _bool(cfg.get("ad_enabled", "false")):
        raise RuntimeError("Авторизация через Active Directory отключена")

    base_dn = cfg.get("ad_base_dn", "").strip()
    if not base_dn:
        raise RuntimeError("Не указан Base DN Active Directory")

    query = (query or "").strip()
    if len(query) < 2:
        raise RuntimeError("Введите минимум два символа для поиска")

    safe = escape_filter_chars(query)
    search_filter = (
        "(&(objectCategory=person)(objectClass=user)"
        "(|"
        f"(sAMAccountName=*{safe}*)"
        f"(userPrincipalName=*{safe}*)"
        f"(displayName=*{safe}*)"
        f"(sn=*{safe}*)"
        f"(givenName=*{safe}*)"
        "))"
    )

    conn = _search_connection(cfg)
    try:
        ok = conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=[
                "objectGUID",
                "sAMAccountName",
                "userPrincipalName",
                "displayName",
                "userAccountControl",
                "distinguishedName",
            ],
            size_limit=max(1, min(int(limit or 20), 50)),
        )

        if not ok:
            return []

        result = []
        seen = set()

        for entry in conn.entries:
            item = _entry_identity(entry)
            if not item["ad_guid"] or not item["login"]:
                continue
            if item["ad_guid"] in seen:
                continue

            seen.add(item["ad_guid"])
            result.append(item)

        result.sort(
            key=lambda x: (
                x["display_name"].casefold(),
                x["login"].casefold(),
            )
        )
        return result

    finally:
        conn.unbind()


def authenticate_ad(
    login: str,
    password: str,
    cfg: dict[str, str],
):
    """
    Проверка пароля сделана как в invite-mailer:
    прямой LDAP bind от имени самого входящего пользователя.

    Служебная учетная запись здесь НЕ используется.
    После успешного bind тем же соединением читаем карточку пользователя.
    """
    if not _bool(cfg.get("ad_enabled", "false")):
        return False, "Авторизация через Active Directory отключена"

    if not password:
        return False, "Не указан пароль"

    short_login = _short_login(login)

    try:
        conn = Connection(
            _server(cfg),
            user=_bind_name(login, cfg),
            password=password,
            auto_bind=True,
            raise_exceptions=True,
        )
    except LDAPInvalidCredentialsResult:
        return False, "Неверный логин или пароль"
    except LDAPException as exc:
        return False, f"Ошибка Active Directory: {exc}"
    except Exception as exc:
        return False, f"Ошибка Active Directory: {exc}"

    try:
        identity = _find_user_by_login(
            short_login,
            cfg,
            conn,
        )
        return True, identity
    except Exception as exc:
        return False, str(exc)
    finally:
        conn.unbind()
