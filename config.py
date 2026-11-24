"""Загрузка настроек бота из .env и config.yaml."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values

from collector import SourceConfig
from digest import DEFAULT_MAX_ITEMS


_PROJECT_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """Настройки отсутствуют или содержат некорректное значение."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Проверенные настройки запуска, доставки и хранения."""

    bot_token: str = field(repr=False)
    owner_user_id: int
    timezone: ZoneInfo
    log_level: int
    send_time: time
    sources: tuple[SourceConfig, ...]
    max_items: int
    top_items: int
    retention_days: int
    dedup_days: int
    database_path: Path
    translation_url: str | None
    translation_api_key: str | None = field(repr=False)


def load_settings(project_dir: Path = _PROJECT_DIR) -> Settings:
    """Загрузить настройки из каталога проекта и проверить значения."""
    root = project_dir.resolve()
    try:
        with (root / ".env").open(encoding="utf-8") as stream:
            env = dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        raise ConfigError("Создайте .env по образцу .env.example") from None
    except (OSError, UnicodeError):
        raise ConfigError("Не удалось прочитать .env") from None

    token = (env.get("BOT_TOKEN") or "").strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ConfigError("Укажите действительный BOT_TOKEN в .env") from None

    try:
        owner_id = int(env.get("OWNER_USER_ID") or "")
    except ValueError:
        owner_id = 0
    if owner_id <= 0:
        raise ConfigError("OWNER_USER_ID должен быть положительным числовым ID Telegram")

    timezone_name = (env.get("TZ") or "").strip() or "Europe/Moscow"
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("Укажите часовой пояс IANA в TZ, например Europe/Moscow") from None

    log_name = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    log_level = logging.getLevelNamesMapping().get(log_name)
    if log_level is None:
        raise ConfigError("LOG_LEVEL должен быть уровнем logging, например INFO")

    try:
        values = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError("Не удалось прочитать config.yaml. Проверьте файл и синтаксис YAML") from None
    if not isinstance(values, dict):
        raise ConfigError("config.yaml должен содержать словарь настроек")
    if values.get("language", "ru") != "ru":
        raise ConfigError("Для русского дайджеста укажите language: ru")

    return Settings(
        bot_token=token,
        owner_user_id=owner_id,
        timezone=timezone,
        log_level=log_level,
        send_time=_parse_time(values.get("send_time", "21:00")),
        sources=_parse_sources(values.get("sources")),
        max_items=_integer(values, "max_items", default=DEFAULT_MAX_ITEMS, minimum=1),
        top_items=_integer(values, "top_items", default=5, minimum=0),
        retention_days=_integer(values, "retention_days", default=30, minimum=1),
        dedup_days=_integer(values, "dedup_days", default=60, minimum=1),
        database_path=root / "data" / "news_digest.sqlite3",
        translation_url=_optional_url(values.get("translation_url"), "translation_url"),
        translation_api_key=(env.get("TRANSLATION_API_KEY") or "").strip() or None,
    )


def _parse_time(value: object) -> time:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        try:
            return time.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError('send_time задаётся в формате "HH:MM", например "21:00"')


def _integer(values: Mapping[str, object], key: str, *, default: int, minimum: int) -> int:
    value = values.get(key, default)
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{key} должен быть целым числом от {minimum}")
    return value


def _parse_sources(values: object) -> tuple[SourceConfig, ...]:
    if not isinstance(values, list) or not values:
        raise ConfigError("Укажите хотя бы один источник в sources")

    sources = []
    for value in values:
        if not isinstance(value, dict):
            raise ConfigError("Каждый элемент sources должен содержать name и rss_url")
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("У каждого источника должно быть непустое поле name")
        weight = value.get("weight", 1.0)
        try:
            valid_weight = type(weight) in (int, float) and isfinite(weight) and weight >= 0
        except OverflowError:
            valid_weight = False
        if not valid_weight:
            raise ConfigError(f"Вес источника {name} должен быть конечным неотрицательным числом")

        sources.append(
            SourceConfig(
                name=name.strip(),
                rss_url=_http_url(value.get("rss_url"), f"rss_url ({name})"),
                weight=float(weight),
                fallback_rss_url=_optional_url(value.get("fallback_rss_url"), f"fallback_rss_url ({name})"),
            )
        )
    if len({source.name.casefold() for source in sources}) != len(sources):
        raise ConfigError("Имена источников в sources должны быть уникальными")
    return tuple(sources)


def _http_url(value: object, key: str) -> str:
    if isinstance(value, str):
        address = value.strip()
        try:
            url = urlsplit(address)
            valid = url.scheme in ("http", "https") and url.hostname and url.username is None
            if valid and not any(character.isspace() for character in address):
                return address
        except ValueError:
            pass
    raise ConfigError(f"{key} должен содержать URL HTTP или HTTPS")


def _optional_url(value: object, key: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _http_url(value, key)
