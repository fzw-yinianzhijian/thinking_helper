"""Fetch public page text; page retrieval is not semantic verification of a claim.

Each redirect is checked independently. Connections use a validated, pinned IP
with the original HTTP Host and TLS SNI, avoiding a second DNS lookup at connect.
Tests may inject an httpx transport and a (host, port) -> IP strings resolver.
"""

import asyncio
import hashlib
from html.parser import HTMLParser
import inspect
import ipaddress
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


MAX_BYTES = 1024 * 1024
TOTAL_TIMEOUT = 15
MAX_REDIRECTS = 3
EXCLUDED = {"script", "style", "nav", "aside", "header", "footer", "form", "head",
            "noscript", "svg", "iframe", "template"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "blockquote", "pre"}


class EvidenceError(ValueError):
    pass


def _clean(text):
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[^\S\n]+", " ", text)).strip()


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.ignored = self.main = self.article = self.title = 0
        self.body_text, self.main_text, self.article_text, self.title_text = [], [], [], []

    def boundary(self):
        self.body_text.append("\n")
        if self.main:
            self.main_text.append("\n")
        elif self.article:
            self.article_text.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK:
            self.boundary()
        if tag in VOID:
            return
        if len(self.stack) >= 256:
            raise EvidenceError("html_nesting_too_deep")
        self.stack.append(tag)
        self.ignored += tag in EXCLUDED
        self.main += tag == "main"
        self.article += tag == "article"
        self.title += tag == "title"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in BLOCK:
            self.boundary()
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        for popped in self.stack[index:]:
            self.ignored -= popped in EXCLUDED
            self.main -= popped == "main"
            self.article -= popped == "article"
            self.title -= popped == "title"
        del self.stack[index:]

    def handle_data(self, data):
        if self.title:
            self.title_text.append(data)
        if self.ignored:
            return
        self.body_text.append(data)
        if self.main:
            self.main_text.append(data)
        elif self.article:
            self.article_text.append(data)

    def result(self):
        main = _clean("".join(self.main_text))
        article = _clean("".join(self.article_text))
        return _clean("".join(self.title_text)), main or article or _clean("".join(self.body_text))


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
    except ValueError:
        return False
    return address.is_global and not any((address.is_private, address.is_loopback, address.is_link_local,
                                         address.is_reserved, address.is_multicast, address.is_unspecified))


async def _resolve(host, port):
    records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


async def _target(value, resolver):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise EvidenceError("invalid_url")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            raise EvidenceError("unsupported_scheme")
        if parsed.username is not None or parsed.password is not None:
            raise EvidenceError("userinfo_not_allowed")
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme.lower() == "https" else 80)
        if not host or "%" in host or port not in {80, 443}:
            raise EvidenceError("invalid_host_or_port")
        canonical = str(httpx.URL(urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))))
        url = httpx.URL(canonical)
        host = url.host
    except (ValueError, httpx.InvalidURL) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("invalid_url") from exc
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        result = resolver(host, port)
        addresses = await result if inspect.isawaitable(result) else result
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise EvidenceError("blocked_non_public_address")
    # Pin the public target even if the DNS answer changes immediately afterward.
    pinned = url.copy_with(host=str(addresses[0]))
    return canonical, pinned, url.netloc.decode("ascii"), host


async def _page(client, original, resolver):
    current = original
    for hop in range(MAX_REDIRECTS + 1):
        canonical, pinned, authority, hostname = await _target(current, resolver)
        headers = {"Host": authority, "Accept": "text/html, application/xhtml+xml, text/plain;q=0.8",
                   "Accept-Encoding": "identity", "User-Agent": "ThinkingHelper/0.1 (public research page reader)"}
        async with client.stream("GET", pinned, headers=headers, extensions={"sni_hostname": hostname}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise EvidenceError("redirect_without_location")
                if hop == MAX_REDIRECTS:
                    raise EvidenceError("too_many_redirects")
                current = urljoin(canonical, location)
                continue
            if response.status_code != 200:
                raise EvidenceError("http_status_" + str(response.status_code))
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}:
                raise EvidenceError("unsupported_content_type:" + (media_type or "missing"))
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                raise EvidenceError("compressed_content_not_accepted")
            try:
                declared_size = int(response.headers.get("content-length", "0"))
            except ValueError:
                declared_size = 0
            if declared_size > MAX_BYTES:
                raise EvidenceError("content_too_large")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                if len(body) + len(chunk) > MAX_BYTES:
                    raise EvidenceError("content_too_large")
                body.extend(chunk)
            if bytes(body[:8]).lstrip().startswith(b"%PDF-"):
                raise EvidenceError("unsupported_content_type:application/pdf")
            try:
                text = body.decode(response.charset_encoding or "utf-8", errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            if media_type in {"text/html", "application/xhtml+xml"}:
                parser = PageText()
                parser.feed(text)
                parser.close()
                title, content = parser.result()
            else:
                title, content = "", _clean(text)
            if not content:
                raise EvidenceError("empty_page_text")
            return {"id": "src-" + hashlib.sha256(canonical.encode()).hexdigest()[:12],
                    "url": canonical, "title": (title or hostname)[:500], "excerpt": content[:12000],
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "provenance": "fetched_page", "verified_content": True}
    raise EvidenceError("too_many_redirects")


async def fetch_evidence(urls: list[str], max_sources=3, *, transport=None, resolver=None) -> dict:
    """Return {sources, errors}; verified_content only means the page was read.

    At most three pages are requested concurrently. Total timeout includes DNS,
    redirects and reads. No cookies, environment proxies or ambient credentials
    are used. Compressed responses are skipped to keep the byte limit strict.
    """
    count = max(0, min(int(max_sources), 3))
    selected = list(dict.fromkeys(url for url in urls if isinstance(url, str)))[:count]
    results = [None] * len(selected)
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                               timeout=httpx.Timeout(10, connect=5, read=5, pool=3),
                               limits=httpx.Limits(max_connections=3, max_keepalive_connections=0)) as client:
        async def work(index, url):
            async with semaphore:
                try:
                    results[index] = {"source": await _page(client, url, resolver or _resolve)}
                except asyncio.CancelledError:
                    results[index] = {"error": {"url": url, "reason": "total_timeout"}}
                    raise
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, EvidenceError) else exc.__class__.__name__
                    results[index] = {"error": {"url": url, "reason": reason[:300]}}
        try:
            await asyncio.wait_for(asyncio.gather(*(work(i, url) for i, url in enumerate(selected))), timeout=TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            pass
    sources = {item["source"]["id"]: item["source"] for item in results if item and "source" in item}
    return {"sources": list(sources.values()),
            "errors": [item["error"] for item in results if item and "error" in item]}
