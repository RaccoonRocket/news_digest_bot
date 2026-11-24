"""Отбор новостей и формирование HTML-дайджеста для Telegram."""

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from html import escape, unescape
from http.client import HTTPException
from itertools import chain
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from storage import Article


logger = logging.getLogger(__name__)
_MAX_ITEMS = 20
DEFAULT_MAX_ITEMS = _MAX_ITEMS
_MAX_TOP_ITEMS = 5
_MAX_MESSAGE_LENGTH = 4096
_MAX_HTML_BYTES = 32 * 1024
_MAX_ITEM_LENGTH = 180
_MAX_TITLE_LENGTH = 320
_MAX_CATEGORY_LENGTH = 80
_TRANSLATION_BATCH_SIZE = 8
_TRANSLATION_TIMEOUT = 10
_OTHER = "Прочее"
_EMPTY_DIGEST = "За последние 24 часа свежих новостей нет."
_HTML_TAG = re.compile(r"<[^>]+>")
_LATIN = re.compile(r"[a-z]", re.IGNORECASE | re.ASCII)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+(?:[-‐‑][^\W_]+)*")
_LINK_STOP_WORDS = frozenset(
    """
    а без в во для до за и из из-за или к ко на над но о об от по под при про с со у
    как где когда кто что чтобы который которой которого после ранее сегодня уже
    также этот эта это эти сообщает сообщили сообщается рассказали заявили
    a an and at by for from in into is of on or that the this to was with
    """.split()
)
_CATEGORIES = {
    "africa": "Африка",
    "americas": "Северная и Южная Америка",
    "analysis": "Аналитика",
    "asia pacific": "Азиатско-Тихоокеанский регион",
    "business": "Бизнес",
    "china": "Китай",
    "europe": "Европа",
    "explainer": "Разбор",
    "markets": "Рынки",
    "middle east": "Ближний Восток",
    "sport": "Спорт",
    "sports": "Спорт",
    "technology": "Технологии",
    "united states": "США",
    "world": "В мире",
    "прочее": _OTHER,
}
_SENTENCE_END = re.compile(r"""(?P<mark>[.!?…]+)[»”’"')\]]*(?=\s|$)""")
_ABBREVIATION = re.compile(
    r"\b(?:[a-zа-яё]|гг|ул|им|пос|обл|тыс|млн|млрд|руб|коп|mr|mrs|ms|dr|prof|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.$",
    re.IGNORECASE,
)
_CONTEXT_DEPENDENT = re.compile(
    r"^(?:об этом|при этом|после этого|в связи с этим|из-за этого|"
    r"по (?:его|её|ее|их) словам|это(?:т)?|эта|эти|он[аи]?|"
    r"this|that|these|those|he|she|they|it|according to (?:him|her|them))\b",
    re.IGNORECASE,
)


def render_digest(
    articles: Sequence[Article],
    *,
    source_weights: Mapping[str, float],
    max_items: int = DEFAULT_MAX_ITEMS,
    top_items: int = _MAX_TOP_ITEMS,
    now: datetime | None = None,
    translation_url: str | None = None,
    translation_api_key: str | None = None,
) -> list[str]:
    """Сформировать HTML-дайджест, используя часовой пояс now для заголовка."""
    if max_items < 1 or top_items < 0:
        raise ValueError("max_items должен быть положительным, top_items — неотрицательным")

    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Время дайджеста должно содержать часовой пояс")
    header = f"<b>{_digest_title(now.hour)}</b>"
    utc_now = now.astimezone(UTC)
    limit = min(max_items, _MAX_ITEMS)
    ranked = sorted(
        articles,
        key=lambda article: (_importance(article, source_weights, utc_now), article.published),
        reverse=True,
    )
    main = ranked[:min(top_items, _MAX_TOP_ITEMS, limit)]
    recent = sorted(ranked[len(main):], key=lambda article: article.published, reverse=True)
    selected = [_prepare_article(article) for article in main + recent[:limit - len(main)]]
    texts = (text for article in selected for text in (article.title, article.summary))
    categories = (article.category or _OTHER for article in selected[len(main):])
    translations = _translations(chain(texts, categories), translation_url, translation_api_key)

    main_items = []
    groups: dict[str, list[str]] = {}
    for index, article in enumerate(selected):
        title = translations.get(article.title, article.title)
        lead = _lead_sentence(translations.get(article.summary, article.summary))
        text = lead or _short_line(title, _MAX_ITEM_LENGTH)
        item = _format_item(text, title, article.link)
        if item is None:
            continue
        if index < len(main):
            main_items.append(item)
        else:
            category = article.category or _OTHER
            heading = _short_line(translations.get(category, category), _MAX_CATEGORY_LENGTH)
            groups.setdefault(heading, []).append(item)

    other_items = groups.pop(_OTHER, [])
    sections = [("Главное", main_items)] if main_items else []
    sections.extend(groups.items())
    if other_items:
        sections.append((_OTHER, other_items))
    return _pack_sections(header, sections)


def _digest_title(hour: int) -> str:
    if 6 <= hour < 12:
        return "Утренние новости"
    if 12 <= hour < 18:
        return "Главные новости дня"
    if 18 <= hour < 23:
        return "Новости к вечеру"
    return "Ночной дайджест"


def _importance(article: Article, weights: Mapping[str, float], now: datetime) -> float:
    sources = set(article.sources) | {article.source}
    weight = max(weights.get(source, 1.0) for source in sources)
    age_hours = max(0.0, (now - article.published).total_seconds() / 3600)
    freshness = max(0.0, 1.0 - age_hours / 24)
    return weight + freshness + float(len(sources) > 1)


def _prepare_article(article: Article) -> Article:
    category = _short_line(article.category or "", _MAX_CATEGORY_LENGTH) or _OTHER
    return replace(
        article,
        title=_short_line(article.title, _MAX_TITLE_LENGTH),
        summary=_lead_sentence(article.summary),
        category=_CATEGORIES.get(category.casefold(), category),
    )


def _lead_sentence(summary: str) -> str:
    """Найти первое короткое законченное предложение, понятное без контекста."""
    text = " ".join(summary.split())
    if _CONTEXT_DEPENDENT.match(text.lstrip("«„“\"'([—–- ")):
        return ""
    for end in _SENTENCE_END.finditer(text):
        mark = end.group("mark")
        if mark == "." and end.end() < len(text) and _ABBREVIATION.search(text[:end.end("mark")]):
            continue
        sentence = text[:end.end()]
        if len(sentence) > _MAX_ITEM_LENGTH or "…" in mark or "..." in mark:
            return ""
        return sentence if len(sentence.split()) > 1 else ""
    return ""


def _translations(
    candidates: Iterable[str], url: str | None, api_key: str | None
) -> dict[str, str]:
    if not url:
        return {}
    texts = list(dict.fromkeys(
        value
        for value in candidates
        if _LATIN.search(value) and not _CYRILLIC.search(value)
    ))
    translations = {}
    for start in range(0, len(texts), _TRANSLATION_BATCH_SIZE):
        batch = texts[start:start + _TRANSLATION_BATCH_SIZE]
        try:
            translations.update(_translate_batch(batch, url, api_key))
        except (HTTPException, OSError, ValueError) as error:
            logger.warning("Сбой перевода LibreTranslate. %s", error)
            break
    return translations


def _translate_batch(texts: list[str], url: str, api_key: str | None) -> dict[str, str]:
    payload: dict[str, str | list[str]] = {
        "q": texts, "source": "en", "target": "ru", "format": "text"
    }
    if api_key:
        payload["api_key"] = api_key
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "NewsDigestBot/1.0"},
    )
    with urlopen(request, timeout=_TRANSLATION_TIMEOUT) as response:
        data = json.loads(response.read(128 * 1024))
    translated = data.get("translatedText") if isinstance(data, dict) else None
    if not isinstance(translated, list) or len(translated) != len(texts):
        raise ValueError("Некорректный ответ LibreTranslate")
    return {
        original: translation
        for original, translation in zip(texts, translated, strict=True)
        if isinstance(translation, str) and translation.strip()
    }


def _pack_sections(header: str, sections: Iterable[tuple[str, Sequence[str]]]) -> list[str]:
    messages: list[str] = []
    message = header
    for heading, items in sections:
        label = f"<b>{escape(heading)}</b>"
        start_section = True
        for item in items:
            section_start = f"{label}\n{item}"
            if not _fits_message(f"{header}\n\n{section_start}"):
                continue
            block = section_start if start_section else item
            candidate = f"{message}\n\n{block}"
            if _fits_message(candidate):
                message = candidate
            else:
                messages.append(message)
                message = section_start
            start_section = False
    if message == header:
        return [f"{header}\n\n{_EMPTY_DIGEST}"]
    messages.append(message)
    return messages


def _format_item(text: str, title: str, link: str) -> str | None:
    try:
        url = urlsplit(link)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
        return None
    word = _link_word(text, title)
    if word is None:
        return None
    start, end = word.span()
    anchor = f'<a href="{escape(link)}">{escape(word.group())}</a>'
    return f"▪️ {escape(text[:start])}{anchor}{escape(text[end:])}"


def _link_word(text: str, title: str) -> re.Match[str] | None:
    headline_words = set(_WORD.findall(title.casefold()))

    def priority(match: re.Match[str]) -> tuple[bool, int]:
        word = match.group().casefold()
        content_word = word not in _LINK_STOP_WORDS and any(letter.isalpha() for letter in word)
        headline_bonus = 3 if word in headline_words else 0
        return content_word, len(word) + headline_bonus

    return max(_WORD.finditer(text), key=priority, default=None)


def _fits_message(markup: str) -> bool:
    if len(markup.encode("utf-8")) > _MAX_HTML_BYTES:
        return False
    text = unescape(_HTML_TAG.sub("", markup))
    return len(text.encode("utf-16-le")) // 2 <= _MAX_MESSAGE_LENGTH


def _short_line(text: str, limit: int) -> str:
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    return line[:limit].rpartition(" ")[0] + "…"
