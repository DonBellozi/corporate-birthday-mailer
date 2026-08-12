from ldap3 import ALL, Connection, Server, SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars


AD_MATCHING_RULE_IN_CHAIN = "1.2.840.113556.1.4.1941"


def _norm_dn(value: str) -> str:
    return (value or "").strip().lower()


def _role_for_user(
    conn,
    base_dn: str,
    user_filter: str,
    direct_groups: list[str],
    admin_group_dn: str,
    operator_group_dn: str,
) -> str | None:
    """
    Роль определяется по членству в группах AD.
    Администратор имеет приоритет, если пользователь состоит в обеих группах.

    Сначала проверяем обычный memberOf, затем AD matching-rule-in-chain,
    чтобы вложенные группы тоже работали.
    """
    direct = {_norm_dn(x) for x in direct_groups if x}

    def member_of(group_dn: str) -> bool:
        group_dn = (group_dn or "").strip()
        if not group_dn:
            return False

        if _norm_dn(group_dn) in direct:
            return True

        # Рекурсивное членство AD: пользователь может входить в группу
        # через одну или несколько вложенных групп.
        recursive_filter = (
            f"(&{user_filter}"
            f"(memberOf:{AD_MATCHING_RULE_IN_CHAIN}:="
            f"{escape_filter_chars(group_dn)}))"
        )
        conn.search(
            search_base=base_dn,
            search_filter=recursive_filter,
            search_scope=SUBTREE,
            attributes=["distinguishedName"],
            size_limit=1,
        )
        return bool(conn.entries)

    if member_of(admin_group_dn):
        return "admin"
    if member_of(operator_group_dn):
        return "operator"
    return None


def authenticate_ad(login: str, password: str, cfg: dict[str, str]):
    """
    Возвращает:
      (True, {"login": ..., "display_name": ..., "role": ...})
    или
      (False, "причина")

    Пароль нигде не сохраняется.
    """
    if cfg.get("ad_enabled", "false").lower() != "true":
        return False, "AD отключен"

    login = (login or "").strip()
    if not login or not password:
        return False, "Не указан логин или пароль"

    host = cfg.get("ad_server", "").strip()
    if not host:
        return False, "AD не настроен"

    use_ssl = cfg.get("ad_ssl", "true").lower() == "true"
    port = int(cfg.get("ad_port", "636" if use_ssl else "389"))
    base_dn = cfg.get("ad_base_dn", "").strip()
    domain = cfg.get("ad_domain", "").strip()

    if not base_dn:
        return False, "Не настроен Base DN"

    raw_user_filter = cfg.get(
        "ad_user_filter",
        "(sAMAccountName={login})",
    )
    user_filter = raw_user_filter.replace(
        "{login}",
        escape_filter_chars(login),
    )

    admin_group_dn = cfg.get("ad_admin_group_dn", "").strip()
    operator_group_dn = cfg.get("ad_operator_group_dn", "").strip()

    # Обратная совместимость. Раньше любой пользователь из
    # ad_allowed_group_dn имел полный доступ. Если новые ролевые группы
    # еще не настроены, старая группа продолжает работать как admin.
    if not admin_group_dn and not operator_group_dn:
        admin_group_dn = cfg.get("ad_allowed_group_dn", "").strip()

    if not admin_group_dn and not operator_group_dn:
        return False, "Не настроены группы доступа AD"

    try:
        server = Server(
            host,
            port=port,
            use_ssl=use_ssl,
            get_info=ALL,
        )

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
            # Если служебной УЗ нет, сначала аутентифицируем самого
            # пользователя и используем его подключение для чтения AD.
            user_bind = f"{domain}\\{login}" if domain else login
            search_conn = Connection(
                server,
                user=user_bind,
                password=password,
                auto_bind=True,
            )

        search_conn.search(
            search_base=base_dn,
            search_filter=user_filter,
            search_scope=SUBTREE,
            attributes=[
                "memberOf",
                "displayName",
                "sAMAccountName",
                "distinguishedName",
            ],
            size_limit=2,
        )

        if len(search_conn.entries) != 1:
            search_conn.unbind()
            return False, "Пользователь AD не найден"

        entry = search_conn.entries[0]
        user_dn = entry.entry_dn
        groups = [str(x) for x in getattr(entry, "memberOf", [])]
        display_name = str(getattr(entry, "displayName", "") or login)

        # При служебной УЗ пароль пользователя еще не проверялся.
        if bind_user:
            Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=True,
            ).unbind()

        role = _role_for_user(
            search_conn,
            base_dn,
            user_filter,
            groups,
            admin_group_dn,
            operator_group_dn,
        )
        search_conn.unbind()

        if not role:
            return False, "Пользователь не входит в разрешенные группы AD"

        return True, {
            "login": login,
            "display_name": display_name,
            "role": role,
        }

    except LDAPException as exc:
        return False, f"Ошибка AD: {exc}"
    except Exception as exc:
        return False, f"Ошибка AD: {exc}"
