"""
Fetch site information from the website using SEO meta tags (no LLM).
Used to record business/industry/target-user context in component_assessment.json.
"""
import re
import urllib.request
from html.parser import HTMLParser
from typing import Any


class _SEOMetaParser(HTMLParser):
    """Extract title, meta description, og:* and first h1 from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str = ""
        self.description: str = ""
        self.og_title: str = ""
        self.og_description: str = ""
        self.og_type: str = ""
        self.keywords: str = ""
        self.h1: str = ""
        self._in_title = False
        self._in_h1 = False
        self._meta_attrs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict((k, v or "") for k, v in attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (d.get("name") or d.get("property") or "").strip().lower()
            content = (d.get("content") or "").strip()
            if not content:
                return
            if name == "description":
                self.description = content
            elif name == "og:title":
                self.og_title = content
            elif name == "og:description":
                self.og_description = content
            elif name == "og:type":
                self.og_type = content
            elif name == "keywords":
                self.keywords = content
        elif tag == "h1" and not self.h1:
            self._in_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        s = data.strip()
        if not s:
            return
        if self._in_title:
            self.title = (self.title + " " + s).strip()
        if self._in_h1:
            self.h1 = (self.h1 + " " + s).strip()


def fetch_site_seo_meta(url: str, timeout: int = 10) -> dict[str, Any]:
    """
    Fetch URL and extract SEO meta (title, description, og:*, keywords, first h1).
    No LLM; pure HTTP + HTML parsing. Returns a dict suitable for component_assessment.site_info.
    On error returns dict with url and error key.
    """
    if not url or not url.strip():
        return {"url": "", "error": "No URL provided"}
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    result: dict[str, Any] = {
        "url": url,
        "title": "",
        "description": "",
        "og_title": "",
        "og_description": "",
        "og_type": "",
        "keywords": "",
        "h1": "",
    }
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "AIRTA-diagnostics/1.0 (site info; no LLM)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                result["error"] = f"HTTP {resp.status}"
                return result
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
    except Exception as e:
        result["error"] = str(e)
        return result

    parser = _SEOMetaParser()
    try:
        parser.feed(html)
    except Exception as e:
        result["error"] = f"Parse: {e}"
        return result

    result["title"] = parser.title or parser.og_title
    result["description"] = parser.description or parser.og_description
    result["og_title"] = parser.og_title
    result["og_description"] = parser.og_description
    result["og_type"] = parser.og_type
    result["keywords"] = parser.keywords
    result["h1"] = parser.h1
    return result
