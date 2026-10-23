from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from .config import Source, http_url
from .network import tls_context

MAX_FEED_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    source: str
    summary: str
    published: float
    position: int


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    query = [
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "at_medium", "at_campaign"}
    ]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path or "/", urlencode(query), ""))


class NoDoctypeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("DTD в RSS не поддерживается.")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element, *names: str) -> str:
    for name in names:
        for child in element:
            if local_name(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def timestamp(value: str, fallback: float) -> float:
    if not value:
        return fallback
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return min(parsed.timestamp(), fallback)
    except (ValueError, OverflowError, OSError):
        return fallback


def parse_feed(payload: bytes, source: Source, now: float | None = None) -> list[Article]:
    if len(payload) > MAX_FEED_BYTES:
        raise ValueError("RSS превышает лимит 2 МБ.")
    now = time.time() if now is None else now
    root = ET.fromstring(payload, parser=ET.XMLParser(target=NoDoctypeBuilder()))
    if local_name(root.tag) not in {"rss", "RDF", "feed"}:
        raise ValueError("Источник вернул не RSS/Atom.")
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    articles = []
    for position, entry in enumerate(entries[:100]):
        title = plain_text(child_text(entry, "title"))
        link = child_text(entry, "link")
        if not link:
            for node in entry:
                if local_name(node.tag) == "link" and node.get("rel", "alternate") == "alternate":
                    link = node.get("href", "")
                    if link:
                        break
        if not link:
            guid = child_text(entry, "guid")
            link = guid if http_url(guid) else ""
        link = urljoin(source.url, link) if link else ""
        if not title or not http_url(link):
            continue
        summary = plain_text(child_text(entry, "description", "summary", "content", "encoded"))
        publisher = plain_text(child_text(entry, "source")) or source.name
        # Google News appends the publisher to its title; avoid printing it twice.
        title = re.sub(r"\s+-\s+" + re.escape(publisher) + r"$", "", title)
        articles.append(Article(
            title[:500], canonical_url(link), publisher[:80], summary[:1500],
            timestamp(child_text(entry, "pubDate", "published", "date", "updated"), now),
            position,
        ))
    return articles


def fetch_feed(source: Source) -> list[Article]:
    request = Request(source.url, headers={
        "User-Agent": "WorldNewsBot/0.1 (RSS reader)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
        "Accept-Encoding": "identity",
    })
    with urlopen(request, timeout=15, context=tls_context()) as response:
        payload = response.read(MAX_FEED_BYTES + 1)
    return parse_feed(payload, source)

