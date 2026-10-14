"""RuneScape Wiki (MediaWiki Action API) client: fetch a page's rendered HTML.

Rendered HTML (action=parse) rather than wikitext, because requirement lists on the wiki are often
built by templates or data queries that only exist after rendering.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .http import UpstreamError, get_with_retry


@dataclass(frozen=True)
class WikiPage:
    title: str
    revid: int | None
    html: str


def parse_url(page: str, api_url: str | None = None) -> str:
    params = {"action": "parse", "page": page, "prop": "text|revid", "redirects": "1",
              "disableeditsection": "1", "disabletoc": "1", "format": "json", "formatversion": "2"}
    return f"{api_url or get_settings().wiki_api_url}?{urlencode(params)}"


def fetch_page(client: httpx.Client, page: str, api_url: str | None = None) -> WikiPage:
    resp = get_with_retry(client, parse_url(page, api_url))
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError(f"wiki returned non-JSON for {page!r}") from exc
    if "error" in data:
        err = data["error"]
        raise UpstreamError(f"wiki API error for {page!r}: {err.get('code')}: {err.get('info')}")
    parsed = data.get("parse") or {}
    html = parsed.get("text")
    if isinstance(html, dict):  # formatversion=1 shape
        html = html.get("*")
    if not html:
        raise UpstreamError(f"wiki returned no page text for {page!r}")
    return WikiPage(title=page, revid=parsed.get("revid"), html=html)
