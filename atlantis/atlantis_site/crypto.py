import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _get_fernet():
    key = getattr(settings, "ADDRESS_ENCRYPTION_KEY", None)
    if not key:
        raise RuntimeError("ADDRESS_ENCRYPTION_KEY is not configured")
    return Fernet(key)


def encrypt_addresses(addresses):
    if not addresses:
        return ""
    payload = json.dumps(addresses, separators=(",", ":")).encode("utf-8")
    return _get_fernet().encrypt(payload).decode("utf-8")


def decrypt_addresses(token):
    if not token:
        return []
    try:
        payload = _get_fernet().decrypt(token.encode("utf-8"))
    except InvalidToken:
        return []
    return json.loads(payload.decode("utf-8"))


def format_address(address):
    if not address:
        return None

    name = " ".join(
        part for part in (
            address.get("first_name", ""),
            address.get("last_name", ""),
        ) if part
    ).strip()

    street_1 = address.get("line_1") or address.get("street_address") or ""
    street_2 = address.get("line_2") or ""
    city = address.get("city") or address.get("locality") or ""
    state = address.get("state") or address.get("region") or ""
    postal = address.get("postal_code") or ""
    country = address.get("country") or ""

    lines = [name, street_1, street_2]

    locality = ", ".join(part for part in (city, state) if part)
    if postal:
        locality = f"{locality} {postal}".strip()
    lines.append(locality)
    lines.append(country)

    lines = [line for line in lines if line]

    if not lines and address.get("formatted"):
        lines = [
            line.strip()
            for line in address["formatted"].splitlines()
            if line.strip()
        ]

    return {
        "id": address.get("id", ""),
        "name": name,
        "lines": lines,
    }
