"""
Plain-text extraction for course material: Canvas page/assignment HTML and PDFs.

Links are kept as ``text (url)`` so a reader (or a model) can still see where the
teacher pointed: videos, worksheets, external sites.
"""
from __future__ import annotations

import io
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
          "section", "article", "header", "footer", "table", "ul", "ol", "blockquote", "pre"}
_SKIP = {"script", "style", "noscript", "head", "template", "svg", "nav"}


_CHROME = {"header", "footer", "aside", "form", "button"}


class _TextParser(HTMLParser):
    def __init__(self, base_url: str = "", skip_chrome: bool = False):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip = _SKIP | (_CHROME if skip_chrome else set())
        self.out: list[str] = []
        self._skip = 0
        self._href: list[str | None] = []
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.skip:
            self._skip += 1
            return
        if self._skip:
            return
        a = dict(attrs)
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self._href.append(a.get("href"))
            self._link_text.append("")
        elif tag == "iframe" and a.get("src"):
            self.out.append(f"\n[embedded: {a.get('title') or 'media'} ({a['src']})]\n")
        elif tag == "img" and a.get("alt"):
            self.out.append(f"[image: {a['alt']}]")

    def handle_endtag(self, tag):
        if tag in self.skip:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a" and self._href:
            href = self._href.pop()
            text = self._link_text.pop().strip()
            if href and self.base_url:
                href = urljoin(self.base_url, href)
            if href and not href.startswith(("#", "javascript:", "mailto:")) and href != text:
                self.out.append(f"{text} ({href})" if text else f"({href})")
            else:
                self.out.append(text)
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._link_text:
            self._link_text[-1] += data
        else:
            self.out.append(data)


_MAIN = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1>", re.I | re.S)


def html_to_text(html: str | None, base_url: str = "", page: bool = False) -> str:
    """Readable text from an HTML fragment or page, links kept as ``text (url)``.
    base_url turns relative links absolute. page=True is for whole web pages: only
    the <main>/<article> content when there is one, without headers, footers and forms."""
    if not html:
        return ""
    if page:
        m = _MAIN.search(html)
        if m and len(m.group(2)) > 500:
            html = m.group(2)
    p = _TextParser(base_url, skip_chrome=page)
    p.feed(html)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_to_text(data: bytes, max_pages: int = 30) -> str:
    """Text of a PDF's first pages; '' if it can't be read (scanned, encrypted, not a PDF)."""
    try:
        from pypdf import PdfReader
    except ImportError:  # optional dependency
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:max_pages]:
            pages.append(page.extract_text() or "")
        return re.sub(r"\n{3,}", "\n\n", "\n\n".join(pages)).strip()
    except Exception:  # pypdf raises a zoo of errors on odd files
        return ""
