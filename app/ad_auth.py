import uuid

from ldap3 import ALL, Connection, Server, SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars


def _server(cfg: dict[str, str]):
    host = cfg.get("ad_server", "").strip()
    if not host:
        raise ValueError("AD не настроен")

    use_ssl = cfg.get("ad_ssl", "true").lower() == "true"
    port = int(cfg.get("ad_port", "636" if use_ssl else "389"))

    return Server(
        host,
        port=port,
        use_ssl=use_ssl,
        get_info=ALL,
    )


def _short_login(login: str) -> str:
    value = (login or "").strip()
    if "\\" in value:
        value = value.rsplit("\\", 1)[-1]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value.strip()


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
    login = str(getattr(entry, "sAMAccountName", "") or "").strip()
    display_name = str(getattr(entry, "displayName", "") or login).strip()
    guid_value = getattr(entry, "objectGUID", None)
    guid = _guid_text(getattr(guid_value, "value", guid_value))

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


def _search_connection(cfg: dict[str, str]):
    bind_user = cfg.get("ad_bind_user", "").strip()
    bind_password = cfg.get("ad_bind_password", "")

    if not bind_user:
        raise ValueError(
            "Для поиска пользователей AD укажите служебную учетную запись"
        )

    return Connection(
        _server(cfg),
        user=bind_user,
        password=bind_password,
        auto_bind=True,
    )


def search_ad_users(
    query: str,
    cfg: dict[str, str],
    limit: int = 20,
) -> list[dict]:
    """
    Поиск пользователя по фамилии, ФИО, логину или UPN.
    Используется администратором приложения при выдаче доступа.
    """
    if cfg.get("ad_enabled", "false").lower() != "true":
        raise ValueError("AD отключен")

    base_dn = cfg.get("ad_base_dn", "").strip()
    if not base_dn:
        raise ValueError("Не настроен Base DN")

    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("Введите минимум два символа для поиска")

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
        conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=[
                "objectGUID",
                "sAMAccountName",
                "userPrincipalName",
                "displayName",
                "userAccountControl",
            ],
            size_limit=max(1, min(int(limit or 20), 50)),
        )

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

        result.sort(key=lambda x: (x["display_name"].lower(), x["login"].lower()))
        return result
    finally:
        conn.unbind()


def authenticate_ad(
    login: str,
    password: str,
    cfg: dict[str, str],
):
    """
    AD только удостоверяет личность.
    Роль и право доступа определяются локальной таблицей приложения.
    """
    if cfg.get("ad_enabled", "false").lower() != "true":
        return False, "AD отключен"

    if not login or not password:
        return False, "Не указан логин или пароль"

    base_dn = cfg.get("ad_base_dn", "").strip()
    if not base_dn:
        return False, "Не настроен Base DN"

    short_login = _short_login(login)
    raw_filter = cfg.get(
        "ad_user_filter",
        "(sAMAccountName={login})",
    )
    user_filter = raw_filter.replace(
        "{login}",
        escape_filter_chars(short_login),
    )

    try:
        server = _server(cfg)
        bind_user = cfg.get("ad_bind_user", "").strip()
        bind_password = cfg.get("ad_bind_password", "")

        if bind_user:
            search_conn = Connection(
                server,
                user=bind_user,
                password=bind_password,
                auto_bind=True,
            )
        else:
            domain = cfg.get("ad_domain", "").strip()
            user_bind = (
                login
                if ("\\" in login or "@" in login)
                else (f"{domain}\\{login}" if domain else login)
            )
            search_conn = Connection(
                server,
                user=user_bind,
                password=password,
                auto_bind=True,
            )

        try:
            search_conn.search(
                search_base=base_dn,
                search_filter=user_filter,
                search_scope=SUBTREE,
                attributes=[
                    "objectGUID",
                    "sAMAccountName",
                    "displayName",
                    "userAccountControl",
                ],
                size_limit=2,
            )

            if len(search_conn.entries) != 1:
                return False, "Пользователь AD не найден"

            entry = search_conn.entries[0]
            identity = _entry_identity(entry)

            if identity["disabled"]:
                return False, "Учетная запись AD отключена"

            # При поиске через служебную УЗ пароль пользователя еще
            # не проверен. Проверяем отдельным bind по DN.
            if bind_user:
                Connection(
                    server,
                    user=entry.entry_dn,
                    password=password,
                    auto_bind=True,
                ).unbind()

            return True, identity
        finally:
            search_conn.unbind()

    except LDAPException as exc:
        return False, f"Ошибка AD: {exc}"
    except Exception as exc:
        return False, f"Ошибка AD: {exc}"
