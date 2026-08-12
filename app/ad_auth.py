from ldap3 import ALL, Connection, Server, SUBTREE
from ldap3.core.exceptions import LDAPException

def authenticate_ad(login: str, password: str, cfg: dict[str, str]):
    if cfg.get("ad_enabled", "false").lower() != "true":
        return False, "AD отключен"

    host = cfg.get("ad_server", "").strip()
    if not host:
        return False, "AD не настроен"

    use_ssl = cfg.get("ad_ssl", "true").lower() == "true"
    port = int(cfg.get("ad_port", "636" if use_ssl else "389"))
    base_dn = cfg.get("ad_base_dn", "").strip()
    domain = cfg.get("ad_domain", "").strip()

    try:
        server = Server(host, port=port, use_ssl=use_ssl, get_info=ALL)
        bind_user = cfg.get("ad_bind_user", "").strip()
        bind_password = cfg.get("ad_bind_password", "")
        groups = []

        if bind_user:
            search_conn = Connection(server, user=bind_user, password=bind_password, auto_bind=True)
            user_filter = cfg.get("ad_user_filter", "(sAMAccountName={login})").replace("{login}", login)
            search_conn.search(
                search_base=base_dn,
                search_filter=user_filter,
                search_scope=SUBTREE,
                attributes=["memberOf", "displayName", "sAMAccountName"],
            )
            if len(search_conn.entries) != 1:
                return False, "Пользователь AD не найден"
            entry = search_conn.entries[0]
            user_dn = entry.entry_dn
            groups = [str(x) for x in getattr(entry, "memberOf", [])]
        else:
            user_dn = f"{domain}\\{login}" if domain else login

        Connection(server, user=user_dn, password=password, auto_bind=True).unbind()

        allowed_group = cfg.get("ad_allowed_group_dn", "").strip()
        if allowed_group:
            if not bind_user:
                return False, "Для проверки группы нужна служебная УЗ AD"
            if allowed_group.lower() not in {x.lower() for x in groups}:
                return False, "Нет членства в разрешенной группе"

        return True, login
    except LDAPException as exc:
        return False, f"Ошибка AD: {exc}"
    except Exception as exc:
        return False, f"Ошибка AD: {exc}"
