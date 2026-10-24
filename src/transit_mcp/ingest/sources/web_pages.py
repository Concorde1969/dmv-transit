"""``type = "web_pages"`` — scrape an agency's curated rider-info / fare pages.

TOML options::

    [[sources]]
    type = "web_pages"
    base_url = "https://www.example-transit.org"   # lets page urls start with "/"
    max_pages = 60                                # cap incl. discovered children
    pages = [
      { url = "/fares", category = "fares" },
      { url = "/how-to-ride", category = "payment", follow_children = true },
    ]
    # Only needed when a page has follow_children = true:
    child_prefixes = ["/riders/"]
    child_suffix = ".html"
    child_blocklist = ["/riders/jobs.html"]
    skip_path_segments = ["es", "fr"]            # e.g. translated mirrors
    child_categories = [{ prefix = "/riders/fares", category = "fares" }]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from transit_mcp.agencies.models import AgencyConfig, SourceConfig
from transit_mcp.ingest.sources import register_source
from transit_mcp.ingest.sources.base import IngestContext
from transit_mcp.ingest.web_scraper import CrawlRules, PageSpec, scrape_pages
from transit_mcp.models import Category


class _Page(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    category: Category
    follow_children: bool = False


class _ChildCategory(BaseModel):
    prefix: str
    category: Category


class WebPagesOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[_Page]
    base_url: str | None = None
    max_pages: int = 60
    child_prefixes: list[str] = Field(default_factory=list)
    child_suffix: str | None = None
    child_blocklist: list[str] = Field(default_factory=list)
    skip_path_segments: list[str] = Field(default_factory=list)
    child_categories: list[_ChildCategory] = Field(default_factory=list)

    def rules(self) -> CrawlRules:
        return CrawlRules(
            base_url=self.base_url,
            child_prefixes=tuple(self.child_prefixes),
            child_suffix=self.child_suffix,
            child_blocklist=frozenset(self.child_blocklist),
            skip_path_segments=frozenset(self.skip_path_segments),
            child_categories=tuple((c.prefix, c.category) for c in self.child_categories),
        )


@register_source
class WebPagesSource:
    type = "web_pages"
    scope = "web"
    updates_network = False

    def run(self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig) -> dict:
        opts = WebPagesOptions.model_validate(source.options())
        return scrape_pages(
            ctx.db,
            ctx.web_http,
            [PageSpec(p.url, p.category, p.follow_children) for p in opts.pages],
            rules=opts.rules(),
            feed_id=agency.slug,
            max_pages=ctx.options.max_pages or opts.max_pages,
            force=ctx.options.force,
        )
