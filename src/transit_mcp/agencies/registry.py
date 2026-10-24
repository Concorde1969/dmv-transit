"""Discover and validate agency definition files.

Two places are read, in order:

1. Built-in definitions shipped with the package
   (``src/transit_mcp/agencies/definitions/*.toml``).
2. The local agencies folder (``AGENCIES_DIR``, default ``./agencies``). A file there
   with a new slug adds an agency; a file with an existing slug replaces the built-in
   definition entirely.

Files whose name starts with ``_`` (e.g. ``_template.toml``) are ignored.
"""

from __future__ import annotations

import json
import tomllib
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from transit_mcp.agencies.models import AgencyConfig

if TYPE_CHECKING:
    from transit_mcp.config import Settings
    from transit_mcp.db.database import Database

_BUILTIN_PACKAGE = "transit_mcp.agencies"
_BUILTIN_DIR = "definitions"


class AgencyConfigError(ValueError):
    """An agency file could not be parsed or failed validation."""


def _parse(text: str, origin: str) -> AgencyConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgencyConfigError(f"{origin}: not valid TOML — {exc}") from exc
    try:
        cfg = AgencyConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(file)'}: {e['msg']}"
            for e in exc.errors()
        )
        raise AgencyConfigError(f"{origin}: {problems}") from exc
    cfg.origin = origin
    return cfg


def _builtin_files() -> list[tuple[str, str]]:
    root = resources.files(_BUILTIN_PACKAGE).joinpath(_BUILTIN_DIR)
    out = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".toml") and not entry.name.startswith("_"):
            out.append((f"built-in:{entry.name}", entry.read_text("utf-8")))
    return out


def _local_files(directory: Path) -> list[tuple[str, str]]:
    if not directory or not directory.is_dir():
        return []
    return [
        (str(p), p.read_text("utf-8"))
        for p in sorted(directory.glob("*.toml"))
        if not p.name.startswith("_")
    ]


def discover(
    settings: Settings | None = None, *, include_disabled: bool = False
) -> tuple[list[AgencyConfig], list[str]]:
    """Return ``(agencies, errors)``. Never raises for a single bad file."""
    if settings is None:
        from transit_mcp.config import get_settings

        settings = get_settings()

    by_slug: dict[str, AgencyConfig] = {}
    errors: list[str] = []
    for origin, text in _builtin_files() + _local_files(settings.agencies_dir):
        try:
            cfg = _parse(text, origin)
        except AgencyConfigError as exc:
            errors.append(str(exc))
            continue
        by_slug[cfg.slug] = cfg  # local files come last, so they override

    agencies = sorted(by_slug.values(), key=lambda a: a.slug)
    if not include_disabled:
        agencies = [a for a in agencies if a.enabled]
    return agencies, errors


def load_agencies(
    settings: Settings | None = None, *, include_disabled: bool = False
) -> list[AgencyConfig]:
    """All agency definitions. Raises :class:`AgencyConfigError` if any file is bad."""
    agencies, errors = discover(settings, include_disabled=include_disabled)
    if errors:
        raise AgencyConfigError(
            "Problem(s) in agency definition files:\n  " + "\n  ".join(errors)
        )
    return agencies


def get_agency(slug: str, settings: Settings | None = None) -> AgencyConfig | None:
    return next((a for a in load_agencies(settings) if a.slug == slug), None)


def seed_curated_tables(db: Database, agencies: list[AgencyConfig]) -> None:
    """Write each agency's profile and fare-vending info into the database, and drop
    rows for agencies that are no longer defined (or are disabled)."""
    slugs = [a.slug for a in agencies]
    for a in agencies:
        p, f = a.profile, a.fares
        db.conn.execute(
            """
            INSERT INTO agency_profiles
                (feed_id, display_name, mode, role, strengths, weaknesses,
                 service_area, connects_to, frequency_tier, park_and_ride, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(feed_id) DO UPDATE SET
                display_name = excluded.display_name, mode = excluded.mode,
                role = excluded.role, strengths = excluded.strengths,
                weaknesses = excluded.weaknesses, service_area = excluded.service_area,
                connects_to = excluded.connects_to,
                frequency_tier = excluded.frequency_tier,
                park_and_ride = excluded.park_and_ride, updated_at = datetime('now')
            """,
            (
                a.slug, a.display_name, a.mode, p.role, p.strengths, p.weaknesses,
                p.service_area, json.dumps(p.connects_to), p.frequency_tier,
                int(p.park_and_ride),
            ),
        )
        db.conn.execute(
            """
            INSERT INTO fare_vending
                (feed_id, base_fare, reduced_fare, currency, payment_methods,
                 where_to_buy, mobile_app_name, app_ios_url, app_android_url,
                 accepts_smartrip, accepts_contactless, cash_onboard,
                 exact_change_required, transfer_policy, fare_page_url, notes,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(feed_id) DO UPDATE SET
                base_fare = excluded.base_fare, reduced_fare = excluded.reduced_fare,
                currency = excluded.currency,
                payment_methods = excluded.payment_methods,
                where_to_buy = excluded.where_to_buy,
                mobile_app_name = excluded.mobile_app_name,
                app_ios_url = excluded.app_ios_url,
                app_android_url = excluded.app_android_url,
                accepts_smartrip = excluded.accepts_smartrip,
                accepts_contactless = excluded.accepts_contactless,
                cash_onboard = excluded.cash_onboard,
                exact_change_required = excluded.exact_change_required,
                transfer_policy = excluded.transfer_policy,
                fare_page_url = excluded.fare_page_url, notes = excluded.notes,
                updated_at = datetime('now')
            """,
            (
                a.slug, f.base_fare, f.reduced_fare, f.currency,
                json.dumps(f.payment_methods), json.dumps(f.where_to_buy),
                f.mobile_app_name, f.app_ios_url, f.app_android_url,
                int(f.accepts_smartrip), int(f.accepts_contactless),
                int(f.cash_onboard), int(f.exact_change_required),
                f.transfer_policy, f.fare_page_url, f.notes,
            ),
        )
    if slugs:
        marks = ",".join("?" for _ in slugs)
        db.conn.execute(f"DELETE FROM agency_profiles WHERE feed_id NOT IN ({marks})", slugs)
        db.conn.execute(f"DELETE FROM fare_vending WHERE feed_id NOT IN ({marks})", slugs)
    db.commit()
