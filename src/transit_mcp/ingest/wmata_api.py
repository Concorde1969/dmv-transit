"""WMATA Developer API client and normalizers.

Client methods return ``(RawResponse, parsed_json)``. Normalizer functions are
pure: they take parsed JSON and return lists of ``models`` records, so they can be
unit-tested against saved fixtures with no network.

API reference: https://developer.wmata.com/docs/services/
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from transit_mcp.ingest.http import HttpClient
from transit_mcp.models import Alert, Fare, Line, Station, UnitStatus

API_BASE = "https://api.wmata.com"

ENDPOINTS = {
    "rail_incidents": "/Incidents.svc/json/Incidents",
    "elevator_incidents": "/Incidents.svc/json/ElevatorIncidents",
    # Bus incidents live under the Incidents service, not Bus.svc.
    "bus_incidents": "/Incidents.svc/json/BusIncidents",
    "lines": "/Rail.svc/json/jLines",
    "stations": "/Rail.svc/json/jStations",
    "station_info": "/Rail.svc/json/jStationInfo",
    "station_parking": "/Rail.svc/json/jStationParking",
    "station_entrances": "/Rail.svc/json/jStationEntrances",
    "station_to_station": "/Rail.svc/json/jSrcStationToDstStationInfo",
}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _split_lines(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(";") if p.strip()]


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class WmataApiClient:
    def __init__(self, http: HttpClient, api_key: str):
        self._http = http
        self._key = api_key

    def _get(self, endpoint_key: str, params: dict[str, str] | None = None):
        url = API_BASE + ENDPOINTS[endpoint_key]
        # api_key travels as a header, never in the URL/query string.
        self._http._client.headers["api_key"] = self._key
        raw = self._http.get(url, params=params, conditional=False)
        return raw, json.loads(raw.text)

    # -- raw endpoint calls -------------------------------------------------
    def rail_incidents(self):
        return self._get("rail_incidents")

    def elevator_incidents(self):
        return self._get("elevator_incidents")

    def bus_incidents(self):
        return self._get("bus_incidents")

    def lines(self):
        return self._get("lines")

    def stations(self):
        return self._get("stations")

    def station_parking(self, station_code: str | None = None):
        params = {"StationCode": station_code} if station_code else None
        return self._get("station_parking", params)

    def station_entrances(self):
        return self._get("station_entrances")

    def station_to_station(self, from_code: str, to_code: str | None = None):
        params = {"FromStationCode": from_code}
        if to_code:
            params["ToStationCode"] = to_code
        return self._get("station_to_station", params)


# ---------------------------------------------------------------------------
# Normalizers (pure functions over parsed JSON)
# ---------------------------------------------------------------------------
def normalize_rail_incidents(data: dict) -> list[Alert]:
    out: list[Alert] = []
    for item in data.get("Incidents", []) or []:
        out.append(
            Alert(
                provider_alert_id=f"rail:{item.get('IncidentID')}",
                mode="rail",
                headline=item.get("IncidentType") or "Rail incident",
                description=item.get("Description"),
                affected_lines=_split_lines(item.get("LinesAffected")),
                severity=item.get("DelaySeverity") or None,
                updated_at=_parse_dt(item.get("DateUpdated")),
                raw_json=_canonical_json(item),
            )
        )
    return out


def normalize_bus_incidents(data: dict) -> list[Alert]:
    out: list[Alert] = []
    for item in data.get("BusIncidents", []) or []:
        routes = item.get("RoutesAffected") or []
        out.append(
            Alert(
                provider_alert_id=f"bus:{item.get('IncidentID')}",
                mode="bus",
                headline=item.get("IncidentType") or "Bus incident",
                description=item.get("Description"),
                affected_lines=[str(r) for r in routes],
                updated_at=_parse_dt(item.get("DateUpdated")),
                raw_json=_canonical_json(item),
            )
        )
    return out


def normalize_elevator_incidents(
    data: dict,
) -> tuple[list[UnitStatus], list[Alert]]:
    """Return unit-status rows plus one roll-up alert per affected station."""
    units: list[UnitStatus] = []
    for item in data.get("ElevatorIncidents", []) or []:
        unit_type = (item.get("UnitType") or "ELEVATOR").strip().lower()
        unit_type = "escalator" if unit_type.startswith("esc") else "elevator"
        units.append(
            UnitStatus(
                station_code=item.get("StationCode") or "",
                unit_type=unit_type,  # type: ignore[arg-type]
                unit_name=item.get("UnitName") or "",
                location_description=item.get("LocationDescription"),
                symptom=item.get("SymptomDescription") or item.get("SymptomCode"),
                out_of_service=True,
                date_out_of_service=_parse_dt(item.get("DateOutOfServ")),
                estimated_return=item.get("EstimatedReturnToService") or None,
                updated_at=_parse_dt(item.get("DateUpdated")),
                raw_json=_canonical_json(item),
            )
        )

    alerts: list[Alert] = []
    by_station: dict[str, list[UnitStatus]] = {}
    for u in units:
        by_station.setdefault(u.station_code, []).append(u)
    for code, station_units in by_station.items():
        esc = sum(1 for u in station_units if u.unit_type == "escalator")
        elv = sum(1 for u in station_units if u.unit_type == "elevator")
        parts = []
        if elv:
            parts.append(f"{elv} elevator{'s' if elv != 1 else ''}")
        if esc:
            parts.append(f"{esc} escalator{'s' if esc != 1 else ''}")
        alerts.append(
            Alert(
                provider_alert_id=f"unit:{code}",
                mode="elevator_escalator",
                headline=f"{' and '.join(parts)} out of service",
                description="; ".join(
                    f"{u.unit_name} ({u.unit_type}): {u.location_description or ''} "
                    f"- {u.symptom or 'out of service'}".strip()
                    for u in station_units
                ),
                affected_stations=[code],
                updated_at=max(
                    (u.updated_at for u in station_units if u.updated_at),
                    default=None,
                ),
                raw_json=_canonical_json([json.loads(u.raw_json) for u in station_units]),
            )
        )
    return units, alerts


def normalize_lines(data: dict) -> list[Line]:
    palette = {
        "RD": "#BF0D3E", "BL": "#009CDE", "YL": "#FFD100", "OR": "#ED8B00",
        "GR": "#00B140", "SV": "#919D9D",
    }
    out: list[Line] = []
    for item in data.get("Lines", []) or []:
        code = item.get("LineCode") or ""
        out.append(
            Line(
                code=code,
                display_name=f"{item.get('DisplayName', code)} Line",
                color_hex=palette.get(code),
                start_station_code=item.get("StartStationCode") or None,
                end_station_code=item.get("EndStationCode") or None,
                raw_json=_canonical_json(item),
            )
        )
    return out


def _station_line_codes(item: dict) -> list[str]:
    codes = [item.get(f"LineCode{i}") for i in range(1, 5)]
    return [c for c in codes if c]


def _station_together(item: dict) -> list[str]:
    return [
        item[k]
        for k in ("StationTogether1", "StationTogether2")
        if item.get(k)
    ]


def normalize_stations(
    data: dict, parking_codes: set[str] | None = None
) -> list[Station]:
    parking_codes = parking_codes or set()
    out: list[Station] = []
    for item in data.get("Stations", []) or []:
        code = item.get("Code") or ""
        addr = item.get("Address") or {}
        out.append(
            Station(
                code=code,
                name=item.get("Name") or code,
                lat=item.get("Lat"),
                lon=item.get("Lon"),
                line_codes=_station_line_codes(item),
                address={k: str(v) for k, v in addr.items() if v is not None},
                has_parking=code in parking_codes,
                together_codes=_station_together(item),
                raw_json=_canonical_json(item),
            )
        )
    return out


def parking_station_codes(data: dict) -> set[str]:
    """Codes of stations that actually have parking capacity.

    ``jStationParking`` returns an entry for many stations with a zero
    ``TotalCount``; those have no parking and must not be flagged.
    """
    codes: set[str] = set()
    for item in data.get("StationsParking", []) or []:
        code = item.get("Code")
        if not code:
            continue
        all_day = (item.get("AllDayParking") or {}).get("TotalCount") or 0
        short = (item.get("ShortTermParking") or {}).get("TotalCount") or 0
        if all_day > 0 or short > 0:
            codes.add(code)
    return codes


def normalize_station_to_station(data: dict) -> list[Fare]:
    out: list[Fare] = []
    for item in data.get("StationToStationInfos", []) or []:
        fare = item.get("RailFare") or {}
        src = item.get("SourceStation") or ""
        dst = item.get("DestinationStation") or ""
        if not src or not dst or src == dst:
            continue
        out.append(
            Fare(
                origin_code=src,
                dest_code=dst,
                peak_fare=fare.get("PeakTime"),
                offpeak_fare=fare.get("OffPeakTime"),
                senior_disabled_fare=fare.get("SeniorDisabled"),
                rail_time_min=item.get("RailTime"),
                raw_json=_canonical_json(item),
            )
        )
    return out


def normalize_entrances(data: dict) -> dict[str, list[dict]]:
    """Group station entrances by station code (for synthesized 'station' docs)."""
    by_station: dict[str, list[dict]] = {}
    for item in data.get("Entrances", []) or []:
        for key in ("StationCode1", "StationCode2"):
            code = item.get(key)
            if code:
                by_station.setdefault(code, []).append(
                    {
                        "name": item.get("Name") or "",
                        "description": item.get("Description") or "",
                        "lat": item.get("Lat"),
                        "lon": item.get("Lon"),
                    }
                )
    return by_station
