"""``type = "gtfs_rt_alerts"`` — an agency's GTFS-realtime service alerts (detours,
closed stops, delays, cancelled trips), refreshed with the other real-time sources.

TOML options::

    [[sources]]
    type = "gtfs_rt_alerts"
    transitland_id = "f-fairfaxconnector~rt"   # alerts URL taken from Transitland
    url = "https://agency.example/gtfsrt/alerts"  # fallback / agencies not listed
    # transitland_atlas = "fairfaxcounty.gov.dmfr.json"  # if not the url's domain
    # auth_env = "SOME_API_KEY"   # secret sent where the Transitland record asks

Alerts are stored in the ``alerts`` table under the agency's slug, with the GTFS
route and stop ids they affect, so trip answers can flag them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from transit_mcp.agencies.models import AgencyConfig, SourceConfig
from transit_mcp.ingest.sources import register_source
from transit_mcp.ingest.sources.base import IngestContext
from transit_mcp.ingest.transitland import _secret, atlas_file_for, feed_record
from transit_mcp.models import Alert

# GTFS-realtime Alert.Effect enum -> plain words
_EFFECTS = {
    1: "no service", 2: "reduced service", 3: "significant delays", 4: "detour",
    5: "additional service", 6: "modified service", 7: "other effect",
    8: "unknown effect", 9: "stop moved", 10: "no effect", 11: "accessibility issue",
}


class GtfsRtAlertsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transitland_id: str | None = None
    transitland_atlas: str | None = None
    url: str | None = None
    auth_env: str | None = None

    @model_validator(mode="after")
    def _has_a_source(self) -> GtfsRtAlertsOptions:
        if not (self.url or self.transitland_id):
            raise ValueError("a gtfs_rt_alerts source needs `transitland_id` or `url`")
        return self


def _text(ts) -> str | None:
    """First English (or first) translation of a GTFS-rt TranslatedString."""
    if not ts.translation:
        return None
    for t in ts.translation:
        if (t.language or "en").lower().startswith("en"):
            return t.text.strip() or None
    return ts.translation[0].text.strip() or None


def _ts(value: int) -> datetime | None:
    return datetime.fromtimestamp(value) if value else None


def parse_alerts(content: bytes, *, feed_id: str, mode: str) -> list[Alert]:
    from google.transit import gtfs_realtime_pb2

    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(content)
    now = datetime.now()
    out: list[Alert] = []
    for ent in msg.entity:
        if not ent.HasField("alert") or ent.is_deleted:
            continue
        a = ent.alert
        periods = [(_ts(p.start), _ts(p.end)) for p in a.active_period]
        if periods and not any((s is None or s <= now) and (e is None or now <= e)
                               for s, e in periods):
            continue  # scheduled for later, or already over
        headline = _text(a.header_text) or _EFFECTS.get(a.effect, "Service alert")
        informed = list(a.informed_entity)
        out.append(Alert(
            provider_alert_id=f"gtfsrt:{feed_id}:{ent.id}",
            feed_id=feed_id,
            mode=mode,
            headline=headline,
            description=_text(a.description_text),
            affected_routes=sorted({i.route_id for i in informed if i.route_id}),
            affected_stops=sorted({i.stop_id for i in informed if i.stop_id}),
            url=_text(a.url),
            severity=_EFFECTS.get(a.effect),
            starts_at=periods[0][0] if periods else None,
            ends_at=periods[0][1] if periods else None,
            updated_at=_ts(msg.header.timestamp),
            raw_json=json.dumps({"id": ent.id, "effect": a.effect, "cause": a.cause}),
        ))
    return out


@register_source
class GtfsRtAlertsSource:
    type = "gtfs_rt_alerts"
    scope = "api"           # refreshed with the other real-time sources
    updates_network = False

    def run(self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig) -> dict:
        opts = GtfsRtAlertsOptions.model_validate(source.options())
        url, headers = opts.url, {}
        if opts.transitland_id:
            rec = feed_record(opts.transitland_id, settings=ctx.settings, http=ctx.api_http,
                              atlas_file=opts.transitland_atlas or atlas_file_for(opts.url))
            if rec and rec.realtime_alerts:
                url = rec.realtime_alerts
                if rec.auth_type == "header" and rec.auth_param:
                    secret = _secret(ctx.settings, opts.auth_env)
                    if not secret:
                        return {"skipped": f"{url} needs {opts.auth_env or 'an API key'}"}
                    headers = {rec.auth_param: secret}
        if not url:
            return {"skipped": "no alerts URL listed for this feed"}

        _status, content, _ = ctx.api_http.get_bytes(url, headers=headers)
        mode = "rail" if agency.mode == "rail" else "bus"
        alerts = parse_alerts(content, feed_id=agency.slug, mode=mode)
        sid, _ = ctx.db.upsert_source(
            url=url, source_type="api", category="alerts", feed_id=agency.slug,
            http_status=200, content_hash=hashlib.sha256(content).hexdigest(),
        )
        for a in alerts:
            ctx.db.upsert_alert(source_id=sid, alert=a)
        resolved = ctx.db.reconcile_alerts(
            mode=mode, feed_id=agency.slug, seen_ids=[a.provider_alert_id for a in alerts]
        )
        ctx.db.commit()
        return {"alerts": len(alerts), "resolved": resolved, "url": url}
