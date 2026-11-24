"""Отбор новостей и формирование HTML-дайджеста для Telegram."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from html import escape, unescape
from http.client import HTTPException
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from storage import Article


logger = logging.getLogger(__name__)
_MAX_ITEMS = 20
_MAX_MESSAGE_LENGTH = 4096
_MAX_HTML_BYTES = 32 * 1024
_MAX_TITLE_LENGTH = 320
_MAX_LEAD_LENGTH = 240
_MAX_CATEGORY_LENGTH = 80
_TRANSLATION_BATCH_SIZE = 8
_TRANSLATION_TIMEOUT = 10
_HEADER = "<b>Дайджест за 24 часа</b>"
_OTHER = "Прочее"
_HTML_TAG = re.compile(r"<[^>]+>")
_LATIN = re.compile(r"[a-z]", re.IGNORECASE | re.ASCII)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
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
}


def render_digest(
    articles: Sequence[Article],
    *,
    source_weights: Mapping[str, float],
    max_items: int = _MAX_ITEMS,
    top_items: int = 5,
    now: datetime | None = None,
    translation_url: str | None = None,
    translation_api_key: str | None = None,
) -> list[str]:
    """Вернуть готовые HTML-сообщения для отправки в Telegram."""
    if max_items < 1 or top_items < 0:
        raise ValueError("max_items должен быть положительным, top_items — неотрицательным")
    if not articles:
        return ["За последние 24 часа свежих новостей нет."]

    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("Время дайджеста должно содержать часовой пояс")
    now = now.astimezone(UTC)
    limit = min(max_items, _MAX_ITEMS)
    ranked = sorted(
        articles,
        key=lambda article: (_importance(article, source_weights, now), article.published),
        reverse=True,
    )
    main = [_display_article(article) for article in ranked[:min(top_items, limit)]]
    recent = sorted(ranked[len(main):], key=lambda article: article.published, reverse=True)
    remaining = [_display_article(article) for article in recent[:limit - len(main)]]

    translations = _translations(main + remaining, translation_url, translation_api_key)
    grouped: dict[str, list[Article]] = {}
    for article in remaining:
        category = article.category or _OTHER
        heading = _short_line(translations.get(category, category), _MAX_CATEGORY_LENGTH)
        grouped.setdefault(heading, []).append(article)

    sections = [("Главное", main)] if main else []
    sections.extend(grouped.items())
    return _pack_sections(sections, translations)


def _importance(article: Article, weights: Mapping[str, float], now: datetime) -> float:
    sources = set(article.sources) | {article.source}
    weight = max(weights.get(source, 1.0) for source in sources)
    age_hours = max(0.0, (now - article.published).total_seconds() / 3600)
    freshness = max(0.0, 1.0 - age_hours / 24)
    return weight + freshness + float(len(sources) > 1)


def _display_article(article: Article) -> Article:
    category = _short_line(article.category or "", _MAX_CATEGORY_LENGTH) or _OTHER
    return replace(
        article,
        title=_short_line(article.title, _MAX_TITLE_LENGTH),
        summary=_short_line(article.summary, _MAX_LEAD_LENGTH),
        source=_short_line(article.source, 64),
        category=_CATEGORIES.get(category.casefold(), category),
    )


def _translations(
    articles: Sequence[Article], url: str | None, api_key: str | None
) -> dict[str, str]:
    if not url:
        return {}
    texts = list(dict.fromkeys(
        value
        for article in articles
        for value in (article.title, article.summary, article.category or "")
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


def _pack_sections(
    sections: Sequence[tuple[str, list[Article]]], translations: Mapping[str, str]
) -> list[str]:
    messages: list[str] = []
    message = _HEADER
    for heading, articles in sections:
        section = f"\n\n<b>{escape(heading)}</b>"
        for index, article in enumerate(articles):
            item = _format_article(article, translations)
            row = f"\n{item} — {_format_link(article.link)}"
            if not _fits_message(_HEADER + section + row):
                row = f"\n{item} — Ссылка недоступна"
            candidate = message + (section if index == 0 else "") + row
            if _fits_message(candidate):
                message = candidate
            else:
                messages.append(message)
                message = _HEADER + section + row
    messages.append(message)
    return messages


def _format_article(article: Article, translations: Mapping[str, str]) -> str:
    title = _short_line(translations.get(article.title, article.title), _MAX_TITLE_LENGTH)
    summary = _short_line(translations.get(article.summary, article.summary), _MAX_LEAD_LENGTH)
    summary = summary or "Описание недоступно"
    return f"<b>{escape(title)}</b> — {escape(summary)} — [{escape(article.source)}]"


def _format_link(link: str) -> str:
    try:
        url = urlsplit(link)
    except ValueError:
        return "Ссылка недоступна"
    if url.scheme in ("http", "https") and url.hostname and url.username is None:
        return f'<a href="{escape(link)}">Читать</a>'
    return "Ссылка недоступна"


def _fits_message(markup: str) -> bool:
    if len(markup.encode("utf-8")) > _MAX_HTML_BYTES:
        return False
    text = unescape(_HTML_TAG.sub("", markup))
    return len(text.encode("utf-16-le")) // 2 <= _MAX_MESSAGE_LENGTH


def _short_line(text: str, limit: int) -> str:
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    prefix = line[:limit - 1].rstrip()
    return (prefix.rpartition(" ")[0] or prefix) + "…"
