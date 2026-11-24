"""Сбор RSS, очистка полей и подготовка новостей к дедупликации."""

import logging
import re
import unicodedata
from calendar import timegm
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import feedparser
from feedparser import FeedParserDict

from storage import Article, PreparedArticle, Storage


logger = logging.getLogger(__name__)
_HTTP_TIMEOUT = 12
_MAX_FEED_BYTES = 4 * 1024 * 1024
_MAX_LEAD_LENGTH = 240
_TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "msclkid", "yclid"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WORD = re.compile(r"[^\W_]+")
_STOP_WORDS = frozenset(
    """
    а без бы был была были было в вам вас во вот все вы да для до его ее если еще
    же за здесь и из или им их к как ко когда кто ли либо мне мы на над него нее
    ни но о об обо он она они от по под при про с со так там те тем то того тоже
    только тот у уже что чтобы это этот я
    a an and are as at be been by for from has have in into is it its of on or
    that the their them this to was were will with
    """.split()
)
_BLOCK_TAGS = {"blockquote", "br", "div", "h1", "h2", "h3", "hr", "li", "p", "td", "tr"}
_HIDDEN_TAGS = {"script", "style", "template"}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Имя, адрес основной и резервной RSS и вес источника в дайджесте."""

    name: str
    rss_url: str
    weight: float = 1.0
    fallback_rss_url: str | None = None


def collect_articles(
    storage: Storage,
    sources: Sequence[SourceConfig],
    *,
    dedup_days: int,
    now: datetime | None = None,
) -> list[Article]:
    """Собрать RSS, сохранить результат и вернуть уникальные новости за сутки."""
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Время сбора должно содержать часовой пояс")
    now = now.astimezone(UTC)
    since = now - timedelta(hours=24)
    weights = {source.name: source.weight for source in sources}

    candidates = []
    for source in sources:
        candidates.extend(_read_source(source, since, now))
    candidates.sort(
        key=lambda item: (item.article.published, weights[item.article.source]),
        reverse=True,
    )

    inserted = storage.save_articles(
        candidates, dedup_days=dedup_days, is_near_duplicate=is_near_duplicate, now=now
    )
    articles = storage.list_articles(since=since, until=now)
    logger.info(
        "Сбор RSS завершён (кандидатов %d, новых %d, за сутки %d)",
        len(candidates), inserted, len(articles),
    )
    return articles


def is_near_duplicate(left: frozenset[str], right: frozenset[str]) -> bool:
    """Сравнить множества значимых слов по Jaccard с порогом 0.6."""
    union = left | right
    return bool(union) and len(left & right) / len(union) >= 0.6


def _read_source(source: SourceConfig, since: datetime, now: datetime) -> list[PreparedArticle]:
    for label, url in (("основная", source.rss_url), ("резервная", source.fallback_rss_url)):
        if not url:
            continue
        try:
            feed, base_url = _download_feed(url)
            candidates = []
            for entry in feed.entries:
                try:
                    candidate = _prepare_entry(entry, source, base_url, since, now)
                except (TypeError, ValueError) as error:
                    logger.debug("Пропущена запись %s (%s)", source.name, error)
                    continue
                if candidate is not None:
                    candidates.append(candidate)
            if candidates:
                logger.info("%s, %s лента (записей %d)", source.name, label, len(candidates))
                return candidates
            logger.info("%s, %s лента (свежих записей нет)", source.name, label)
        except Exception as error:
            logger.warning("Сбой RSS %s (%s лента). %s", source.name, label, error)
    return []


def _download_feed(url: str) -> tuple[FeedParserDict, str]:
    request = Request(url, headers={"User-Agent": "NewsDigestBot/1.0"})
    with urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        payload = response.read(_MAX_FEED_BYTES + 1)
        base_url = response.geturl()
        headers = {name.lower(): value for name, value in response.headers.items()}
    if len(payload) > _MAX_FEED_BYTES:
        raise ValueError("RSS превышает допустимый размер")

    headers["content-location"] = base_url
    feed = feedparser.parse(payload, response_headers=headers)
    if not feed.version or (feed.bozo and not feed.entries):
        reason = feed.get("bozo_exception", "формат RSS или Atom не распознан")
        raise ValueError(f"Некорректная лента ({reason})")
    if feed.bozo:
        logger.debug("RSS прочитан с предупреждением парсера (%s)", feed.bozo_exception)
    return feed, base_url


def _prepare_entry(
    entry: FeedParserDict,
    source: SourceConfig,
    base_url: str,
    since: datetime,
    now: datetime,
) -> PreparedArticle | None:
    published = _published_at(entry, now)
    if not since <= published <= now:
        return None

    raw_link = (entry.get("link") or "").strip()
    if not raw_link:
        return None
    link = urljoin(base_url, raw_link)
    url_key = _url_key(link)

    title = _plain_text(entry.get("title") or "")
    title = title.removesuffix(f" - {source.name}").strip()
    title_key = _title_key(title)
    if not url_key or not title_key:
        return None

    summary = _plain_text(entry.get("summary") or entry.get("description") or "")
    article = Article(
        title=title,
        link=link,
        summary=_lead(summary, title_key, source.name),
        source=source.name,
        published=published,
        category=_category(entry),
    )
    words = frozenset(
        word for word in _WORD.findall(_fold(title))
        if word not in _STOP_WORDS and (len(word) > 1 or word.isdecimal())
    )
    return PreparedArticle(
        article=article,
        url_hash=sha256(url_key.encode("utf-8")).hexdigest(),
        title_hash=sha256(title_key.encode("utf-8")).hexdigest(),
        title_words=words,
    )


def _published_at(entry: FeedParserDict, now: datetime) -> datetime:
    parsed = entry.get("published_parsed")
    if parsed is None:
        return now
    try:
        return datetime.fromtimestamp(timegm(parsed), UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        return now


def _url_key(link: str) -> str | None:
    url = urlsplit(link)
    if url.scheme not in _DEFAULT_PORTS or not url.hostname or url.username is not None:
        return None
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    port = url.port
    netloc = f"{host}:{port}" if port is not None and port != _DEFAULT_PORTS[url.scheme] else host
    params = []
    for name, value in parse_qsl(url.query, keep_blank_values=True):
        key = name.casefold()
        if not key.startswith("utm_") and key not in _TRACKING_PARAMS:
            params.append((name, value))
    params.sort(key=lambda param: param[0])
    return urlunsplit((url.scheme, netloc, url.path.rstrip("/") or "/", urlencode(params), ""))


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _title_key(text: str) -> str:
    return "".join(character for character in _fold(text) if character.isalnum())


def _category(entry: FeedParserDict) -> str | None:
    for tag in entry.get("tags", ()):
        term = _plain_text(tag.get("term") or "")
        if term:
            return term
    return None


def _lead(summary: str, title_key: str, source_name: str) -> str:
    source_key = _title_key(source_name)
    if _title_key(summary) in {title_key, title_key + source_key, title_key + source_key * 2}:
        return ""
    if len(summary) <= _MAX_LEAD_LENGTH:
        return summary
    prefix = summary[:_MAX_LEAD_LENGTH - 1]
    return (prefix.rpartition(" ")[0] or prefix) + "…"


def _plain_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


class _HTMLText(HTMLParser):
    """Извлечь видимый текст и сохранить пробелы между блоками HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self.hidden_depth += 1
        elif tag in _BLOCK_TAGS and not self.hidden_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in _BLOCK_TAGS and not self.hidden_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)
