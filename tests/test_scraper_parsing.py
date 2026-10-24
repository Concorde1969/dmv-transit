from __future__ import annotations

from transit_mcp.ingest.web_scraper import _child_links, _extract_markdown, _extract_title


def _wmata_rules(isolated_settings):
    """Crawl rules straight from the built-in wmata.toml — proves config drives the crawl."""
    from transit_mcp.agencies import get_agency
    from transit_mcp.ingest.sources.web_pages import WebPagesOptions

    src = get_agency("wmata", isolated_settings).sources_of("web_pages")[0]
    return WebPagesOptions.model_validate(src.options()).rules()


def _html(fixtures_dir):
    return (fixtures_dir / "html" / "sample_fares_page.html").read_text("utf-8")


def test_extract_title_prefers_h1(fixtures_dir):
    assert _extract_title(_html(fixtures_dir), "https://www.wmata.com/pay.html") == "Pay for Metro"


def test_extract_markdown_keeps_body_drops_nav(fixtures_dir):
    md = _extract_markdown(_html(fixtures_dir))
    assert "distance based" in md
    assert "flat $2.25" in md
    assert "children under age five" in md
    # Navigation / footer boilerplate should be gone.
    assert "Espanol" not in md
    assert "© WMATA" not in md


def test_child_links_filtered_to_allowlist(fixtures_dir, isolated_settings):
    links = _child_links(
        _html(fixtures_dir), "https://www.wmata.com/pay.html", _wmata_rules(isolated_settings)
    )
    assert "https://www.wmata.com/pay/smart-trip.html" in links
    assert "https://www.wmata.com/pay/fare-programs.html" in links
    assert "https://www.wmata.com/ride/guides/parking.html" in links
    # External, localized, and out-of-section links are excluded.
    assert not any("twitter.com" in x for x in links)
    assert not any("/am/" in x for x in links)
