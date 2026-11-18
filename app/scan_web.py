"""Read official web pages as text: httpx + BeautifulSoup, headless Chromium when the page is built by JavaScript."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, UnicodeDammit

from .retry import TRANSIENT_STATUS, transient


def retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers.get("retry-after", ""))
    except ValueError:
        return None

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}
MAX_CHARS = 120_000
MIN_TEXT = 400  # less visible text than this usually means the page is built by JavaScript
JS_HINTS = re.compile(r"enable javascript|javascriptを有効|javascript is (disabled|required)|"
                      r"<div id=\"(app|root|__next|__nuxt)\"></div>", re.I)
DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "head")
BLOCKS = ("p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "dd", "dt",
          "table", "ul", "ol", "header", "footer", "time")


class FetchError(Exception):
    """transient: worth trying again (timeout, 5xx, 429, DNS); a 404 or a non-page is not."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass
class Page:
    url: str
    text: str
    rendered: bool

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def html_to_text(html: str, base_url: str) -> str:
    """Visible text with links kept as `text <url>` so ticket links survive."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(DROP_TAGS):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href)
        label = a.get_text(" ", strip=True)
        a.replace_with(f"{label} <{full}>" if label else f"<{full}>")
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        img.replace_with(f"[image: {alt}]" if alt else "")
    for tag in soup.find_all(("td", "th")):
        tag.insert_after(" | ")
    for tag in soup.find_all(BLOCKS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = soup.get_text("")
    lines = [re.sub(r"[ \t　\xa0]+", " ", line).strip() for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if not line:
            blank = True
            continue
        if blank and out:
            out.append("")
        out.append(line)
        blank = False
    return "\n".join(out)


def visible_length(text: str) -> int:
    return len(re.sub(r"<https?://[^>]+>|\s", "", text))


def needs_browser(html: str, text: str) -> bool:
    return visible_length(text) < MIN_TEXT or bool(JS_HINTS.search(html[:20000]) and visible_length(text) < 3000)


def fetch_plain(url: str, client: httpx.Client | None = None) -> tuple[str, str]:
    """(final url, html)."""
    own = client is None
    client = client or httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30)
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}"[:300], transient(e)) from e
    finally:
        if own:
            client.close()
    if r.status_code >= 400:
        err = FetchError(f"HTTP {r.status_code}", r.status_code in TRANSIENT_STATUS)
        err.retry_after = retry_after(r)
        raise err
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and ctype:
        raise FetchError(f"Not a web page ({ctype.split(';')[0]})")
    if "charset=" in ctype.lower():
        return str(r.url), r.text
    # No charset header: let the page's <meta charset> decide (Shift_JIS sites are common in Japan).
    return str(r.url), UnicodeDammit(r.content, is_html=True).unicode_markup or r.text


class Browser:
    """One headless Chromium for the whole scan, started on first use."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._broken = ""          # why Chromium couldn't start; then every page fails fast with that reason

    def _start(self) -> None:
        if self._broken:
            raise FetchError(self._broken)
        from playwright.sync_api import sync_playwright
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        except Exception as e:
            # Stop Playwright, or the next start in this thread fails with "Sync API inside the asyncio loop".
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._pw = self._browser = None
            first_line = (str(e).splitlines() or [""])[0]
            self._broken = (f"Browser: headless Chromium won't start ({type(e).__name__}: {first_line[:150]}). "
                            "On the server run: .venv/bin/python -m playwright install chromium")
            raise FetchError(self._broken) from e

    def html(self, url: str) -> tuple[str, str]:
        if self._browser is None:
            self._start()
        ctx = self._browser.new_context(user_agent=UA, locale="ja-JP")
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # pages with endless polling never go idle; take what's there
                pass
            return page.url, page.content()
        except Exception as e:
            first_line = (str(e).splitlines() or [""])[0]
            raise FetchError(f"Browser: {type(e).__name__}: {first_line}"[:300], transient(e)) from e
        finally:
            ctx.close()

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        self._browser = self._pw = None


def fetch_page(url: str, *, rendered_before: bool = False, browser: Browser | None = None,
               client: httpx.Client | None = None, probe: bool = False) -> Page:
    """Fetch a page as text. Uses the browser if the page needed it before or looks JavaScript-built.

    probe (first time a page is read): also render it once, and keep the rendered text if JavaScript
    adds real content (pages where only part is filled in by scripts, like a tour schedule).
    """
    if not (rendered_before and browser):
        final, html = fetch_plain(url, client)
        text = html_to_text(html, final)
        if browser is None:
            return Page(url, cap(text, url), False)
        if not needs_browser(html, text):
            if not probe or "<script" not in html.lower():
                return Page(url, cap(text, url), False)
            try:
                rfinal, rhtml = browser.html(url)
            except FetchError as e:
                log.info("%s: render check failed (%s); using the plain page", url, e)
                return Page(url, cap(text, url), False)
            rtext = html_to_text(rhtml, rfinal)
            plain_n, rendered_n = visible_length(text), visible_length(rtext)
            if rendered_n > plain_n * 1.15 + 150:
                log.info("%s: JavaScript adds content (%d -> %d chars); reading it with the browser from now on",
                         url, plain_n, rendered_n)
                return Page(url, cap(rtext, url), True)
            return Page(url, cap(text, url), False)
        log.info("%s looks JavaScript-built (%d chars); using the browser", url, visible_length(text))
    final, html = browser.html(url)
    return Page(url, cap(html_to_text(html, final), url), True)


def cap(text: str, url: str) -> str:
    if len(text) > MAX_CHARS:
        log.warning("%s: page text is %d chars; reading the first %d", url, len(text), MAX_CHARS)
        return text[:MAX_CHARS]
    return text


def same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).hostname or "", urlsplit(b).hostname or ""
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb) or strip(ha).endswith("." + strip(hb)) or strip(hb).endswith("." + strip(ha))
