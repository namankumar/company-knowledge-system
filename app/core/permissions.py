import json
from email.utils import parseaddr

COMPANY_VIEW_ROLES = {"founder", "operator"}
USER_ROLES = {"founder", "operator", "member"}


def normalize_email(email: str) -> str:
    parsed = parseaddr(email)[1] or email
    return parsed.strip().lower()


def email_domain(email: str) -> str:
    clean = normalize_email(email)
    if "@" not in clean:
        return ""
    return clean.rsplit("@", 1)[1]


def normalize_domains(domains: list[str] | None) -> list[str]:
    seen = set()
    result = []
    for domain in domains or []:
        clean = domain.strip().lower().lstrip("@")
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def settings_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(value)


def role_can_view_company(role: str | None) -> bool:
    return role in COMPANY_VIEW_ROLES


def validate_role(role: str) -> str:
    if role not in USER_ROLES:
        raise ValueError(f"role must be one of {sorted(USER_ROLES)}")
    return role
