"""Хранение новостей и истории дедупликации в SQLite."""

import logging
import sqlite3
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path


logger = logging.getLogger(__name__)
TitleMatcher = Callable[[frozenset[str], frozenset[str]], bool]

_SCHEMA = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    published REAL NOT NULL,
    category TEXT
);

CREATE TABLE IF NOT EXISTS article_sources (
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    PRIMARY KEY (article_id, source)
);

CREATE TABLE IF NOT EXISTS fingerprints (
    url_hash TEXT NOT NULL,
    title_hash TEXT NOT NULL,
    title_words TEXT NOT NULL,
    first_seen REAL NOT NULL,
    article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    PRIMARY KEY (url_hash, title_hash)
);

CREATE INDEX IF NOT EXISTS articles_published_idx ON articles (published);
CREATE INDEX IF NOT EXISTS fingerprints_url_idx ON fingerprints (url_hash, first_seen);
CREATE INDEX IF NOT EXISTS fingerprints_title_idx ON fingerprints (title_hash, first_seen);
CREATE INDEX IF NOT EXISTS fingerprints_seen_idx ON fingerprints (first_seen);
CREATE INDEX IF NOT EXISTS fingerprints_article_idx ON fingerprints (article_id);
"""


@dataclass(frozen=True, slots=True)
class Article:
    """Нормализованная новость с датой публикации и списком источников."""

    title: str
    link: str
    summary: str
    source: str
    published: datetime
    category: str | None = None
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedArticle:
    """Новость с хэшами нормализованных URL и заголовка и словами заголовка."""

    article: Article
    url_hash: str
    title_hash: str
    title_words: frozenset[str]


class Storage:
    """Хранилище с отдельным соединением SQLite для каждой операции."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Создать отсутствующие таблицы и индексы и включить WAL."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise RuntimeError(f"SQLite вернул режим {mode!r} при включении WAL")
            connection.executescript(_SCHEMA)
        logger.debug("Хранилище SQLite готово")

    def save_articles(
        self,
        articles: Sequence[PreparedArticle],
        *,
        dedup_days: int,
        is_near_duplicate: TitleMatcher,
        now: datetime | None = None,
    ) -> int:
        """Сохранить новости и упоминания, вернуть число новых записей.

        Срок дедупликации отсчитывается от первого появления пары хэшей.
        """
        seen_at = _timestamp(now or datetime.now(UTC))
        cutoff = _cutoff(seen_at, dedup_days)
        if not articles:
            return 0

        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            history = deque(
                (row["article_id"], frozenset(row["title_words"].split()))
                for row in connection.execute(
                    """SELECT article_id, title_words FROM fingerprints
                       WHERE first_seen >= ?
                       ORDER BY first_seen DESC, url_hash, title_hash""",
                    (cutoff,),
                )
            )

            for entry in articles:
                duplicate, article_id = _find_duplicate(
                    connection, entry, cutoff, history, is_near_duplicate
                )
                if not duplicate:
                    article_id = _insert_article(connection, entry.article)
                    inserted += 1
                if article_id is not None:
                    connection.execute(
                        """INSERT INTO article_sources (article_id, source) VALUES (?, ?)
                           ON CONFLICT (article_id, source) DO NOTHING""",
                        (article_id, entry.article.source),
                    )

                fingerprint = connection.execute(
                    """INSERT INTO fingerprints
                           (url_hash, title_hash, title_words, first_seen, article_id)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (url_hash, title_hash) DO UPDATE SET
                           title_words = excluded.title_words,
                           first_seen = excluded.first_seen,
                           article_id = excluded.article_id
                       WHERE fingerprints.first_seen < ?""",
                    (
                        entry.url_hash,
                        entry.title_hash,
                        " ".join(sorted(entry.title_words)),
                        seen_at,
                        article_id,
                        cutoff,
                    ),
                )
                if fingerprint.rowcount:
                    history.appendleft((article_id, entry.title_words))

        return inserted

    def list_articles(self, *, since: datetime, until: datetime) -> list[Article]:
        """Вернуть новости за указанный интервал включительно, от свежих к старым."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.*, s.source AS mentioned_source
                   FROM articles AS a
                   JOIN article_sources AS s ON s.article_id = a.id
                   WHERE a.published BETWEEN ? AND ?
                   ORDER BY a.published DESC, a.id DESC, s.source""",
                (_timestamp(since), _timestamp(until)),
            )
            articles = []
            for _, mentions in groupby(rows, key=lambda row: row["id"]):
                group = list(mentions)
                row = group[0]
                articles.append(
                    Article(
                        title=row["title"],
                        link=row["link"],
                        summary=row["summary"],
                        source=row["source"],
                        published=datetime.fromtimestamp(row["published"], UTC),
                        category=row["category"],
                        sources=tuple(mention["mentioned_source"] for mention in group),
                    )
                )
        return articles

    def cleanup(
        self, *, retention_days: int, dedup_days: int, now: datetime | None = None
    ) -> tuple[int, int]:
        """Удалить старые новости и хэши, вернуть два счётчика в этом порядке."""
        current = _timestamp(now or datetime.now(UTC))
        published_cutoff = _cutoff(current, retention_days)
        fingerprint_cutoff = _cutoff(current, dedup_days)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fingerprints = connection.execute(
                "DELETE FROM fingerprints WHERE first_seen < ?", (fingerprint_cutoff,)
            ).rowcount
            articles = connection.execute(
                "DELETE FROM articles WHERE published < ?", (published_cutoff,)
            ).rowcount
        logger.info("Очистка SQLite завершена (%d новостей, %d записей дедупликации)", articles, fingerprints)
        return articles, fingerprints

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection


def _find_duplicate(
    connection: sqlite3.Connection,
    entry: PreparedArticle,
    cutoff: float,
    history: deque[tuple[int | None, frozenset[str]]],
    is_near_duplicate: TitleMatcher,
) -> tuple[bool, int | None]:
    """Найти совпадение, включая хэши уже удалённых новостей."""
    exact = connection.execute(
        """SELECT article_id FROM fingerprints
           WHERE first_seen >= ? AND (url_hash = ? OR title_hash = ?)
           ORDER BY (url_hash = ?) DESC, first_seen DESC, url_hash, title_hash
           LIMIT 1""",
        (cutoff, entry.url_hash, entry.title_hash, entry.url_hash),
    ).fetchone()
    if exact is not None:
        return True, exact["article_id"]

    for article_id, words in history:
        if is_near_duplicate(entry.title_words, words):
            return True, article_id
    return False, None


def _insert_article(connection: sqlite3.Connection, article: Article) -> int:
    cursor = connection.execute(
        """INSERT INTO articles (title, link, summary, source, published, category)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            article.title,
            article.link,
            article.summary,
            article.source,
            _timestamp(article.published),
            article.category,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _timestamp(value: datetime) -> float:
    if value.utcoffset() is None:
        raise ValueError("Дата должна содержать часовой пояс")
    return value.timestamp()


def _cutoff(timestamp: float, days: int) -> float:
    if days < 1:
        raise ValueError("Срок хранения должен быть положительным")
    return timestamp - timedelta(days=days).total_seconds()
