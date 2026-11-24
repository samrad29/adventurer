"""Organization and expedition branding.

Validation and resolution only. Callers own transactions, authorization, and
which columns they update. This module does not read the database and does
not accept a logo or cover path. A later upload service sets those refs.
"""

from __future__ import annotations

import re

DEFAULT_PRIMARY = "#18354C"
DEFAULT_ACCENT = "#5095B6"
WELCOME_LIMIT = 280
COLOR_KEYS = ("primary", "accent")

_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class BrandingError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def normalize_color(value: object) -> str:
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        raise BrandingError("Use a color like #18354C.")
    return value.upper()


def normalize_welcome(value: object) -> str | None:
    """Trim a welcome. Blank becomes None so the caller can inherit or store empty."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BrandingError("Send the welcome as text.")
    text = value.strip()
    if len(text) > WELCOME_LIMIT:
        raise BrandingError("Keep the welcome under 280 characters.")
    return text or None


def normalize_colors(value: object) -> dict | None:
    """None clears the object. A dict stores only valid primary and accent tokens.

    A missing token is omitted. The stored object replaces the previous one.
    It is not merged with the row already in the database.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BrandingError("Send colors as an object or null.")
    unknown = set(value) - set(COLOR_KEYS)
    if unknown:
        raise BrandingError("Colors accept only primary and accent.")
    stored: dict[str, str] = {}
    for key in COLOR_KEYS:
        if key not in value or value[key] is None:
            continue
        stored[key] = normalize_color(value[key])
    return stored


def resolve_branding(
    *,
    kind: str,
    organization: dict | None = None,
    expedition: dict | None = None,
) -> dict:
    """Product defaults, then the organization, then expedition overrides.

    Global expeditions stay on the product defaults. An invalid stored color
    is treated as missing. This does not write the row.
    """
    primary = DEFAULT_PRIMARY
    accent = DEFAULT_ACCENT
    logo = None
    image = None
    welcome = None
    if kind == "organization" and organization is not None:
        colors = organization.get("colors")
        if isinstance(colors, dict):
            primary = _legacy_color(colors.get("primary")) or primary
            accent = _legacy_color(colors.get("accent")) or accent
        logo = _ref(organization.get("logo_ref"))
        image = _ref(organization.get("image_ref"))
        welcome = _text(organization.get("welcome_message"))
    if kind != "global" and expedition is not None:
        override = _legacy_color(expedition.get("accent_color"))
        if override is not None:
            accent = override
        cover = _ref(expedition.get("image_ref"))
        if cover is not None:
            image = cover
        text = _text(expedition.get("welcome_override"))
        if text is not None:
            welcome = text
    return {
        "primary_color": primary,
        "accent_color": accent,
        "logo_ref": logo,
        "image_ref": image,
        "welcome_message": welcome,
    }


def _legacy_color(value: object) -> str | None:
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        return None
    return value.upper()


def _ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
