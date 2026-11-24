"""Точка входа Telegram-бота с ежедневным дайджестом."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.methods import SendMessage
from aiogram.types import LinkPreviewOptions, Message
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from collector import collect_articles
from config import ConfigError, Settings, load_settings
from digest import render_digest
from storage import Storage


logger = logging.getLogger(__name__)


class DigestRunner:
    """Координировать сбор, доставку и очистку базы во время работы бота."""

    def __init__(self, bot: Bot, settings: Settings, storage: Storage) -> None:
        self.bot = bot
        self.settings = settings
        self.storage = storage
        self._digest_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[tuple[int, int]] | None = None
        self._stopping = False

    async def send_digest(self, *, manual: bool = False) -> None:
        """Собрать и отправить дайджест, если сбор уже не запущен."""
        if self._stopping:
            return
        if self._digest_task is not None and not self._digest_task.done():
            if manual:
                await self._notify("Дайджест уже собирается.")
            else:
                logger.info("Плановая отправка совпала с текущим сбором дайджеста")
            return

        self._digest_task = asyncio.create_task(self._deliver(manual))
        await asyncio.shield(self._digest_task)

    async def cleanup(self) -> None:
        """Запустить ежедневную очистку SQLite в рабочем потоке."""
        if self._stopping:
            return
        self._cleanup_task = asyncio.create_task(
            asyncio.to_thread(
                self.storage.cleanup,
                retention_days=self.settings.retention_days,
                dedup_days=self.settings.dedup_days,
            )
        )
        await asyncio.shield(self._cleanup_task)

    async def drain(self) -> None:
        """Остановить приём новых заданий и дождаться текущих операций."""
        self._stopping = True
        tasks = [task for task in (self._digest_task, self._cleanup_task) if task is not None]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _deliver(self, manual: bool) -> None:
        try:
            messages = await asyncio.to_thread(self._build_messages)
            for text in messages:
                await self._send_html(text)
        except TelegramForbiddenError:
            logger.warning("Telegram отклонил отправку. Владелец должен открыть чат и отправить /start")
            return
        except TelegramAPIError as error:
            logger.error("Ошибка Telegram при отправке дайджеста (%s)", type(error).__name__)
        except Exception:
            logger.exception("Не удалось собрать или отправить дайджест")
        else:
            logger.info("Дайджест отправлен (%d сообщений)", len(messages))
            return
        if manual:
            await self._notify("Не удалось отправить дайджест. Подробности в журнале.")

    def _build_messages(self) -> list[str]:
        articles = collect_articles(
            self.storage,
            self.settings.sources,
            dedup_days=self.settings.dedup_days,
            now=datetime.now(UTC),
        )
        return render_digest(
            articles,
            source_weights={source.name: source.weight for source in self.settings.sources},
            max_items=self.settings.max_items,
            top_items=self.settings.top_items,
            now=datetime.now(self.settings.timezone),
            translation_url=self.settings.translation_url,
            translation_api_key=self.settings.translation_api_key,
        )

    async def _send_html(self, text: str) -> None:
        method = SendMessage(
            chat_id=self.settings.owner_user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        try:
            await self.bot(method)
        except TelegramRetryAfter as error:
            logger.info("Telegram попросил повторить отправку через %d с", error.retry_after)
            await asyncio.sleep(error.retry_after)
            await self.bot(method)

    async def _notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.settings.owner_user_id, text)
        except TelegramAPIError as error:
            logger.warning("Telegram отклонил уведомление владельцу (%s)", type(error).__name__)


def create_router(runner: DigestRunner) -> Router:
    """Зарегистрировать команды для владельца в личном чате."""
    settings = runner.settings
    router = Router()
    router.message.filter(
        F.chat.type == ChatType.PRIVATE,
        F.chat.id == settings.owner_user_id,
        F.from_user.id == settings.owner_user_id,
    )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "/digest — собрать новости за последние 24 часа.\n"
            f"Ежедневная отправка в {settings.send_time:%H:%M} ({settings.timezone.key})."
        )

    @router.message(Command("digest"))
    async def digest_command(_message: Message) -> None:
        await runner.send_digest(manual=True)

    return router


def create_scheduler(runner: DigestRunner) -> AsyncIOScheduler:
    """Настроить ежедневную доставку и очистку в часовом поясе бота."""
    settings = runner.settings
    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
    )
    scheduler.add_job(
        runner.send_digest,
        CronTrigger(
            hour=settings.send_time.hour, minute=settings.send_time.minute, timezone=settings.timezone
        ),
        id="daily_digest",
    )
    scheduler.add_job(
        runner.cleanup,
        CronTrigger(hour=3, minute=0, timezone=settings.timezone),
        id="daily_cleanup",
    )
    return scheduler


async def run_bot(settings: Settings) -> None:
    """Запустить polling и завершить планировщик перед закрытием сессии Telegram."""
    storage = Storage(settings.database_path)
    await asyncio.to_thread(storage.initialize)
    async with Bot(settings.bot_token, session=AiohttpSession(timeout=20)) as bot:
        runner = DigestRunner(bot, settings, storage)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(runner))

        scheduler = create_scheduler(runner)
        stopped = asyncio.Event()
        scheduler.add_listener(lambda _: stopped.set(), EVENT_SCHEDULER_SHUTDOWN)
        scheduler.start()
        try:
            for job in scheduler.get_jobs():
                logger.info("Следующий запуск %s в %s", job.id, job.next_run_time.isoformat())
            await dispatcher.start_polling(bot, close_bot_session=False)
        finally:
            scheduler.pause()
            try:
                await runner.drain()
            finally:
                scheduler.shutdown()
                await stopped.wait()


def main() -> int:
    """Прочитать конфиг, настроить журнал и запустить бота."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        logger.error("%s", error)
        return 1

    logging.getLogger().setLevel(settings.log_level)
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except TelegramUnauthorizedError:
        logger.error("Telegram отклонил токен. Проверьте BOT_TOKEN в .env")
        return 1
    except TelegramAPIError as error:
        logger.error("Ошибка Telegram при запуске (%s)", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
