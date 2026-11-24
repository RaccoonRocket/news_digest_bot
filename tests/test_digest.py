"""Проверки оформления и отбора Telegram-дайджеста."""

import re
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from digest import render_digest
from storage import Article


_MOSCOW = ZoneInfo("Europe/Moscow")
_NOW = datetime(2026, 11, 24, 20, tzinfo=_MOSCOW)
_EVENING = "Новости к вечеру"
_URL = "https://example.com/news"
_BOLD = re.compile(r"<b>([^<]+)</b>")
_ANCHOR = re.compile(r'<a href="([^"]*)">([^<]+)</a>')


def _article(
    title: str,
    summary: str = "",
    *,
    link: str = _URL,
    minutes_ago: int = 0,
    category: str | None = None,
    source: str = "Интерфакс",
    sources: tuple[str, ...] = (),
) -> Article:
    return Article(
        title=title,
        link=link,
        summary=summary,
        source=source,
        published=_NOW - timedelta(minutes=minutes_ago),
        category=category,
        sources=sources,
    )


def _render(*articles: Article, now: datetime = _NOW, **options: Any) -> list[str]:
    return render_digest(
        articles, source_weights={"Интерфакс": 1.0, "Reuters": 1.0}, now=now, **options
    )


class _VisibleText(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.feed(markup)
        self.close()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _items(*messages: str) -> list[str]:
    return [line for message in messages for line in message.splitlines() if line.startswith("▪️ ")]


def _headlines(*messages: str) -> list[str]:
    return ["".join(_VisibleText(item).parts).removeprefix("▪️ ") for item in _items(*messages)]


class DigestTests(TestCase):
    def test_titles_follow_local_time_at_each_boundary(self) -> None:
        news = _article("Станция открыта")
        for hour, minute, title in (
            (0, 0, "Ночной дайджест"),
            (5, 59, "Ночной дайджест"),
            (6, 0, "Утренние новости"),
            (11, 59, "Утренние новости"),
            (12, 0, "Главные новости дня"),
            (17, 59, "Главные новости дня"),
            (18, 0, _EVENING),
            (22, 59, _EVENING),
            (23, 0, "Ночной дайджест"),
        ):
            with self.subTest(hour=hour, minute=minute):
                now = _NOW.replace(hour=hour, minute=minute)
                message, = _render(news, now=now)
                self.assertTrue(message.startswith(f"<b>{title}</b>\n\n"))

    def test_empty_digest_uses_local_title(self) -> None:
        self.assertEqual(
            _render(now=_NOW.replace(hour=8)),
            ["<b>Утренние новости</b>\n\nЗа последние 24 часа свежих новостей нет."],
        )

    def test_short_lead_uses_first_complete_sentence(self) -> None:
        sentence = "Жители г. Рязани снова получают электричество."
        self.assertEqual(
            _headlines(*_render(_article("Электричество в Рязани", sentence + " Работы завершены."))),
            [sentence],
        )

    def test_long_lead_uses_title_even_when_next_sentence_is_short(self) -> None:
        lead = "Комиссия изучает " + "новые подробности " * 12 + "законопроекта. Решение принято."
        self.assertEqual(
            _headlines(*_render(_article("Депутаты рассмотрели законопроект", lead))),
            ["Депутаты рассмотрели законопроект"],
        )

    def test_missing_incomplete_or_context_dependent_lead_uses_title(self) -> None:
        for lead in ("", "Движение на участке дороги", "Движение на дороге…", "Об этом сообщили власти."):
            with self.subTest(lead=lead):
                self.assertEqual(
                    _headlines(*_render(_article("Движение открыто", lead))),
                    ["Движение открыто"],
                )

    def test_long_title_ends_at_word_boundary_within_180_characters(self) -> None:
        prefix = " ".join(["город"] * 30)
        self.assertEqual(len(prefix + "…"), 180)
        self.assertEqual(
            _headlines(*_render(_article(prefix + " округ"))),
            [prefix + "…"],
        )

    def test_only_significant_word_is_linked(self) -> None:
        news = _article(
            "Событие в Индийском океане",
            "В Индийском океане произошло землетрясение магнитудой 5,7.",
        )
        self.assertEqual(
            _items(*_render(news)),
            [f'▪️ В Индийском океане произошло <a href="{_URL}">землетрясение</a> магнитудой 5,7.'],
        )

    def test_link_word_prefers_the_topic_shared_with_the_title(self) -> None:
        news = _article("Снижение платежей", "По данным ведомства, утверждено снижение платы.")
        item, = _items(*_render(news))
        self.assertEqual(_ANCHOR.findall(item), [(_URL, "снижение")])

    def test_escaping_and_list_spacing(self) -> None:
        url = _URL + '?a="test"&b=2'
        news = _article('Тариф < 20 & "без изменений"', link=url)
        message, = _render(news, _article("Вторая новость"))
        item, second = _items(message)
        self.assertIn(f"{item}\n\n{second}", message)
        self.assertEqual(_headlines(message), [news.title, "Вторая новость"])
        self.assertIn("Тариф &lt; 20 &amp; &quot;без ", item)
        self.assertIn(f'href="{_URL}?a=&quot;test&quot;&amp;b=2"', item)
        self.assertEqual(
            [(unescape(href), text) for href, text in _ANCHOR.findall(item)],
            [(url, "изменений")],
        )

    def test_translated_lead_and_title_are_used_with_length_check(self) -> None:
        first = _article(
            "River levels rise", "The river rose overnight. Further details follow.", source="Reuters"
        )
        second = _article(
            "Council approves funds",
            "Officials agreed on funding.",
            source="Reuters",
            category="Energy policy",
        )
        translations = {
            "The river rose overnight.": "За ночь уровень реки поднялся.",
            "Officials agreed on funding.": "Члены совета изучили " + "сведения о расходах " * 12 + "и одобрили их.",
            "Council approves funds": "Совет одобрил выделение средств",
            "Energy policy": "Энергетика",
        }
        with patch("digest._translate_batch", return_value=translations) as translate:
            message, = _render(first, second, top_items=1, translation_url="https://translate.example.com/translate")

        self.assertEqual(
            _headlines(message),
            ["За ночь уровень реки поднялся.", "Совет одобрил выделение средств"],
        )
        self.assertEqual(_BOLD.findall(message), [_EVENING, "Главное", "Энергетика"])
        self.assertIn("The river rose overnight.", translate.call_args.args[0])
        self.assertNotIn(first.summary, translate.call_args.args[0])

    def test_sections_follow_latest_article_and_other_comes_last(self) -> None:
        articles = [
            _article("Более ранние новости спорта", minutes_ago=30, category="Спорт"),
            _article("Новости дня за рубежом", minutes_ago=10, category="World"),
            _article("Общая новость", minutes_ago=900, sources=("Интерфакс", "Reuters")),
            _article("Событие без рубрики", minutes_ago=1),
            _article("Вчерашние новости зарубежья", minutes_ago=60, category="В мире"),
            _article("Последние новости спорта", minutes_ago=20, category="Спорт"),
            _article("Более раннее событие без рубрики", minutes_ago=70),
        ]
        message, = _render(*articles, top_items=1)
        self.assertEqual(_BOLD.findall(message), [_EVENING, "Главное", "В мире", "Спорт", "Прочее"])
        self.assertEqual(_headlines(message), [articles[index].title for index in (2, 1, 4, 5, 0, 3, 6)])

    def test_default_limit_and_caps_preserve_ranking_and_recency(self) -> None:
        recent = [
            _article(f"Новость {index}", minutes_ago=index, link=f"{_URL}/{index}", category="Экономика")
            for index in range(25)
        ]
        important = _article("Общая новость", minutes_ago=900, sources=("Интерфакс", "Reuters"))
        for config in ({}, {"max_items": 25, "top_items": 10}):
            with self.subTest(config=config):
                message, = _render(*recent, important, **config)
                first_section, _, _ = message.partition("<b>Экономика</b>")
                self.assertEqual(len(_items(first_section)), 5)
                self.assertEqual(
                    _headlines(message), [important.title, *(article.title for article in recent[:19])]
                )
                for item in _items(message):
                    self.assertEqual(len(_ANCHOR.findall(item)), 1)

    def test_split_keeps_items_whole_and_header_appears_once(self) -> None:
        articles = [
            _article(
                "🚀" * 168 + f" Сводка {index:02d}",
                minutes_ago=index,
                link=f"{_URL}/{index}",
                category="Экономика",
            )
            for index in range(20)
        ]
        first, second = _render(*articles, top_items=0)
        self.assertEqual(_BOLD.findall(first), [_EVENING, "Экономика"])
        self.assertEqual(_BOLD.findall(second), ["Экономика"])
        self.assertEqual(_headlines(first, second), [article.title for article in articles])
        self.assertEqual(
            [_ANCHOR.findall(item) for item in _items(first, second)],
            [[(article.link, "Сводка")] for article in articles],
        )
        for message in (first, second):
            text = "".join(_VisibleText(message).parts)
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
