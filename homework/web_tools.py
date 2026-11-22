"""
Web search and fetch for the tutor. claude-broker gives Claude no web access of its
own, so these run here, in the container, and are served as MCP tools.

fetch_url only goes to public addresses (no loopback, private, link-local...), checked
on every redirect, so a page can't steer it at the host or the home network.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests

from mva_canvas.text import html_to_text, pdf_to_text

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEARCH_URL = "https://html.duckduckgo.com/html/"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"
MAX_RESULTS = 6
MAX_FETCH_BYTES = 2_000_000
FETCH_TEXT_LIMIT = 10_000
FETCH_SECONDS = 12
MAX_REDIRECTS = 5


class WebError(Exception):
    pass


# -- search -----------------------------------------------------------------------

class _DdgParser(HTMLParser):
    """Results from DuckDuckGo's HTML page: a.result__a (title + link) and .result__snippet."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._field: str | None = None
        self._ad_depth = 0
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            self._depth += 1
            if "result--ad" in cls and not self._ad_depth:
                self._ad_depth = self._depth
        if self._ad_depth:
            return
        if tag == "a" and "result__a" in cls:
            self.results.append({"title": "", "url": _unwrap(a.get("href") or ""), "snippet": ""})
            self._field = "title"
        elif "result__snippet" in cls and self.results:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag == "div":
            if self._ad_depth == self._depth:
                self._ad_depth = 0
            self._depth -= 1
        if tag in ("a", "td", "div"):
            self._field = None

    def handle_data(self, data):
        if self._field and self.results and not self._ad_depth:
            self.results[-1][self._field] += data


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<url>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def parse_ddg(html: str, limit: int = MAX_RESULTS) -> list[dict]:
    p = _DdgParser()
    p.feed(html)
    out = []
    for r in p.results:
        url = r["url"]
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue
        out.append({"title": " ".join(r["title"].split()), "url": url,
                    "snippet": " ".join(r["snippet"].split())})
        if len(out) >= limit:
            break
    return out


def _search_ddg(query: str, s) -> list[dict]:
    resp = s.post(SEARCH_URL, data={"q": query, "kl": "uk-en"},
                  headers={"User-Agent": USER_AGENT}, timeout=8)
    if resp.status_code != 200:  # 202 is DuckDuckGo's bot check
        raise WebError(f"DuckDuckGo refused the search (HTTP {resp.status_code})")
    return parse_ddg(resp.text)


def _search_brave(query: str, s, key: str) -> list[dict]:
    resp = s.get(BRAVE_URL, params={"q": query, "count": MAX_RESULTS, "country": "gb"},
                 headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=8)
    if resp.status_code != 200:
        raise WebError(f"Brave refused the search (HTTP {resp.status_code})")
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": html_to_text(r.get("description", ""))}
            for r in (resp.json().get("web") or {}).get("results", [])[:MAX_RESULTS]]


def _search_wikipedia(query: str, s) -> list[dict]:
    resp = s.get(WIKIPEDIA_URL, params={"action": "query", "list": "search", "srsearch": query,
                                        "format": "json", "srlimit": 4, "srprop": "snippet"},
                 headers={"User-Agent": "homework-helper/1.0"}, timeout=8)
    if resp.status_code != 200:
        raise WebError(f"Wikipedia search failed (HTTP {resp.status_code})")
    return [{"title": f"{r['title']} - Wikipedia",
             "url": "https://en.wikipedia.org/wiki/" + quote(r["title"].replace(" ", "_")),
             "snippet": html_to_text(r.get("snippet", ""))}
            for r in resp.json().get("query", {}).get("search", [])]


def web_search(query: str, session: requests.Session | None = None) -> list[dict]:
    """Brave if BRAVE_API_KEY is set, else DuckDuckGo's HTML page. DuckDuckGo sometimes
    answers scripts with a bot check, so Wikipedia's search is the fallback."""
    query = (query or "").strip()[:300]
    if not query:
        raise WebError("Empty search")
    s = session or requests
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    try:
        return _search_brave(query, s, key) if key else _search_ddg(query, s)
    except (WebError, requests.RequestException, ValueError) as e:
        logger.info("Web search fell back to Wikipedia: %s", e)
    try:
        results = _search_wikipedia(query, s)
    except (requests.RequestException, ValueError) as e:
        raise WebError(f"Search failed: {e}")
    if results:
        results.insert(0, {"title": "(Note)", "url": "",
                           "snippet": "General web search is unavailable right now; these are Wikipedia results. "
                                      "You can still fetch_url well-known sites such as BBC Bitesize directly."})
    return results


# -- fetch ------------------------------------------------------------------------

def check_public(url: str) -> None:
    """Raise WebError unless url is http(s) to a host that resolves only to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("Only http(s) links can be fetched")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise WebError(f"Can't find {parsed.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise WebError("That address isn't on the public internet")


_GDOC = re.compile(r"^https://docs\.google\.com/(document|presentation|spreadsheets)/d/([A-Za-z0-9_-]+)")


def readable_url(url: str) -> str:
    """Google Docs/Slides/Sheets links as their plain-text export (works when shared by link)."""
    m = _GDOC.match(url)
    if not m:
        return url
    kind, doc_id = m.groups()
    if kind == "document":
        return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    if kind == "presentation":
        return f"https://docs.google.com/presentation/d/{doc_id}/export/txt"
    return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv"


def fetch_url(url: str, session: requests.Session | None = None,
              check=check_public, limit: int = FETCH_TEXT_LIMIT) -> str:
    """The readable text of a public web page, PDF or Google Doc, truncated to limit."""
    s = session or requests.Session()
    url = readable_url((url or "").strip())
    deadline = time.monotonic() + FETCH_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        check(url)
        resp = s.get(url, headers={"User-Agent": USER_AGENT}, timeout=(5, 8),
                     allow_redirects=False, stream=True)
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise WebError("Redirect without a location")
            url = urljoin(url, location)
            continue
        break
    else:
        resp.close()
        raise WebError("Too many redirects")
    try:
        if resp.status_code >= 400:
            raise WebError(f"HTTP {resp.status_code}")
        buf = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            buf += chunk
            if len(buf) >= MAX_FETCH_BYTES or time.monotonic() > deadline:
                break
        ctype = (resp.headers.get("Content-Type") or "").lower()
        encoding = resp.encoding or "utf-8"
    finally:
        resp.close()
    data = bytes(buf)
    if "pdf" in ctype or data[:5] == b"%PDF-":
        text = pdf_to_text(data) or "(PDF with no readable text)"
    elif "html" in ctype or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        text = html_to_text(data.decode(encoding, errors="replace"), base_url=url, page=True)
    elif ctype.startswith("text/") or "json" in ctype or "csv" in ctype:
        text = data.decode(encoding, errors="replace")
    else:
        raise WebError(f"Can't read this kind of content ({ctype or 'unknown'})")
    text = text.strip() or "(The page had no readable text)"
    return text if len(text) <= limit else text[:limit] + "\n…(truncated)"
