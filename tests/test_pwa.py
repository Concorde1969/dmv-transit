"""The site installs as an app: manifest, icons, service worker and offline page."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_the_site_is_installable(isolated_settings, monkeypatch):
    from transit_mcp.chat import app as app_mod

    settings = isolated_settings.model_copy(update={"anthropic_api_key": "k"})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def nothing(self):
        self._tools = []

    monkeypatch.setattr(app_mod.McpBridge, "start", nothing)
    monkeypatch.setattr(app_mod.McpBridge, "stop", nothing)
    with TestClient(app_mod.app) as c:
        page = c.get("/").text
        assert '<link rel="manifest" href="/static/manifest.webmanifest"' in page
        sw = c.get("/sw.js")                     # at the root, so it covers the whole site
        assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
        assert sw.headers["cache-control"] == "no-cache" and "/static/offline.html" in sw.text
        manifest = c.get("/static/manifest.webmanifest")
        assert manifest.headers["content-type"].startswith("application/manifest+json")
        m = manifest.json()
        assert m["display"] == "standalone" and m["start_url"].startswith("/")
        purposes = {i["purpose"] for i in m["icons"]}
        assert purposes == {"any", "maskable"}
        for icon in m["icons"]:
            assert c.get(icon["src"]).status_code == 200, icon["src"]
        assert "You're offline" in c.get("/static/offline.html").text
