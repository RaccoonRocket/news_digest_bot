# Личный дайджест новостей в Telegram

Бот собирает свежие новости Интерфакса и Reuters, убирает дубли и отправляет до 20 пунктов владельцу в личный чат. `/start` показывает справку, `/digest` запускает сбор за последние 24 часа. По умолчанию отправка запланирована на 21:00 Europe/Moscow, очистка SQLite — на 03:00 в том же часовом поясе.

Для запуска нужны Python 3.11 или новее и доступ по HTTPS к Telegram, RSS-лентам и выбранному сервису перевода.

## Локальная установка

В каталоге `news_digest_bot` на macOS или Linux выполните следующие команды. Используйте `python3.11` или команду установленной версии Python 3.11+.

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
chmod 600 .env
```

На Windows в PowerShell можно использовать Python из окружения напрямую. Копирование создаёт `.env` при его отсутствии.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Создайте бота командой `/newbot` и внесите выданный токен в `BOT_TOKEN`. В `OWNER_USER_ID` укажите числовой ID своего аккаунта Telegram. Файл `.env` исключён из Git. [Инструкция Telegram по созданию бота](https://core.telegram.org/bots/tutorial)

Если ID неизвестен, отправьте новому боту `/start` в личном чате, пока процесс бота остановлен. Запустите фрагмент ниже из каталога проекта с помощью Python из `.venv`. В выводе найдите свой аккаунт и скопируйте его ID в `.env`. [Telegram Bot API `getUpdates`](https://core.telegram.org/bots/api#getupdates)

```python
import json
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen

from dotenv import dotenv_values

token = (dotenv_values(".env", interpolate=False).get("BOT_TOKEN") or "").strip()
if not token:
    raise SystemExit("Заполните BOT_TOKEN в .env")

try:
    url = f"https://api.telegram.org/bot{quote(token, safe=':')}/getUpdates"
    with urlopen(url, timeout=15) as response:
        updates = json.load(response)["result"]
except HTTPError as error:
    raise SystemExit(f"Bot API вернул HTTP {error.code}") from None
except OSError:
    raise SystemExit("Не удалось подключиться к Bot API") from None

for update in updates:
    message = update.get("message", {})
    if message.get("chat", {}).get("type") == "private" and message.get("text") == "/start":
        sender = message["from"]
        print(sender.get("username") or sender["first_name"], sender["id"])
```

В `.env` также можно задать `TZ` и `LOG_LEVEL`. По умолчанию используются `Europe/Moscow` и `INFO`. Приложение читает `.env` и `config.yaml` рядом с `bot.py` и создаёт базу `data/news_digest.sqlite3` при первом запуске.

В активированном окружении запустите бота.

```sh
python -m bot
```

На Windows без активации окружения команда выглядит так.

```powershell
.\.venv\Scripts\python.exe -m bot
```

В личном чате с ботом отправьте `/start`, затем `/digest`. Чтобы получать плановые выпуски, оставляйте процесс запущенным на машине с доступом в сеть. Остановить локальный процесс можно через `Ctrl+C`.

## Как собирается дайджест

- `send_time` в `config.yaml` задаётся строкой в кавычках, например `"21:00"`. APScheduler использует часовой пояс `TZ`. В журнале при запуске видны ближайшие события `daily_digest` и `daily_cleanup`. [Документация CronTrigger](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html)
- Интерфакс, Reuters World и резервная лента Google News заданы в `sources`. Проверьте их доступность с машины запуска. При ошибке или отсутствии свежих записей Reuters сборщик пробует резервный URL. Если одна лента недоступна, бот использует данные другой. [Интерфакс](https://www.interfax.ru/rss) · [Reuters World](https://quanwenrss.com/en/site/reuters.html) · [Резервная лента](https://news.google.com/rss/search?q=when%3A24h+site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen)
- SQLite проверяет хэши нормализованных URL и заголовков. Для близких заголовков сборщик использует Jaccard с порогом 0,6. Хэши хранятся 60 дней от первого появления, а новости — 30 дней от публикации. Сбор берёт доступные в RSS записи и отбирает их по дате за последние 24 часа.
- В «Главном» учитываются вес источника, свежесть и совпадения по обоим агентствам. Сравнение заголовков работает по словам исходного текста, поэтому одну историю на разных языках оно может посчитать двумя новостями.
- Выбранные английские заголовки и лиды переводит LibreTranslate по адресу из `translation_url`. Ключ для защищённого экземпляра задаётся в `.env` через `TRANSLATION_API_KEY`. При сбое перевода в выпуск попадёт исходный текст. В Google News часто есть только заголовок, и для таких записей бот показывает «Описание недоступно». [LibreTranslate API](https://docs.libretranslate.com/guides/api_usage/)

## Запуск на VPS с systemd

Пример использует `/opt/news_digest_bot` и пользователя `newsdigest`. Если выберете другую пару, укажите свои значения в `User`, `WorkingDirectory` и `ExecStart` файла `deploy/news-digest.service`. Для того же токена оставьте один процесс polling, остановив локальную копию перед запуском на VPS. [Настройки systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

Выполните команды на VPS из каталога с файлами проекта. Создайте системного пользователя один раз.

```sh
sudo useradd --system --user-group --home-dir /opt/news_digest_bot --shell /usr/sbin/nologin newsdigest
sudo install -d -o newsdigest -g newsdigest -m 0750 /opt/news_digest_bot
sudo install -o newsdigest -g newsdigest -m 0640 \
  bot.py config.py collector.py storage.py digest.py config.yaml requirements.txt .env.example \
  /opt/news_digest_bot/
sudo -u newsdigest python3.11 -m venv /opt/news_digest_bot/.venv
sudo -u newsdigest /opt/news_digest_bot/.venv/bin/python -m pip install -r /opt/news_digest_bot/requirements.txt
sudo -u newsdigest cp -n /opt/news_digest_bot/.env.example /opt/news_digest_bot/.env
sudo chmod 600 /opt/news_digest_bot/.env
sudo -u newsdigest vi /opt/news_digest_bot/.env
```

Заполните `.env` на VPS, установите unit и запустите службу.

```sh
sudo install -m 0644 deploy/news-digest.service /etc/systemd/system/news-digest.service
sudo systemd-analyze verify /etc/systemd/system/news-digest.service
sudo systemctl daemon-reload
sudo systemctl enable --now news-digest.service
sudo systemctl status news-digest.service --no-pager
```

Эти команды показывают последние записи журнала и продолжают вывод по мере поступления новых. После изменения `.env` или `config.yaml` перезапустите службу через `sudo systemctl restart news-digest.service`. Если изменился сам unit, сначала выполните `sudo systemctl daemon-reload`. [Документация journalctl](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html)

```sh
sudo journalctl -u news-digest.service -n 50 --no-pager
sudo journalctl -u news-digest.service -f
```

## Чек-лист проверки

- [ ] Бот запустился, а журнал показывает ближайший `daily_digest` на `21:00:00+03:00` и `daily_cleanup` на `03:00:00+03:00` при настройках по умолчанию.
- [ ] Владелец получил справку по `/start` и новости по `/digest`. В выпуске до 20 пунктов, в «Главном» до пяти. Ссылки открываются. Команда с другого аккаунта или в группе остаётся без ответа.
- [ ] Плановая отправка сработала. Поставьте `send_time` на 2–3 минуты вперёд по `TZ`, перезапустите бота и сверьте ближайший запуск с журналом. После получения выпуска верните желаемое время и перезапустите бота ещё раз.
- [ ] Резерв Reuters работает. Для проверки временно задайте `rss_url` Reuters как `"https://rss-test.invalid/feed"` и перезапустите бота. После `/digest` проверьте в журнале `Сбой RSS Reuters` и результат загрузки резервной ленты. Верните исходный URL и снова перезапустите бота.
- [ ] Проверка ниже сохранила одну новость с двумя источниками. При повторном `/digest` на той же ленте журнал показывает `новых 0`, пока RSS не изменится. Ручной дайджест выводит новости из текущего окна.
- [ ] На VPS `systemctl status` показывает `active (running)`, а в `journalctl` видны запуски и успешная отправка `Дайджест отправлен`.

### Пример дедупликации

Запустите в окружении проекта. Пример использует временную базу SQLite и подставляет две RSS-записи. У заголовков «Совет утвердил план развития» и «Совет утвердил план работы» три общих слова из пяти, поэтому в результат попадёт одна новость.

```python
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from feedparser import FeedParserDict

from collector import SourceConfig, collect_articles
from storage import Storage

sources = [
    SourceConfig("Интерфакс", "https://interfax.example/feed"),
    SourceConfig("Reuters", "https://reuters.example/feed"),
]
titles = ["Совет утвердил план развития", "Совет утвердил план работы"]
feeds = {
    source.rss_url: FeedParserDict(entries=[
        FeedParserDict(title=title, link=source.rss_url + "/news", summary="Краткое описание")
    ])
    for source, title in zip(sources, titles, strict=True)
}
with TemporaryDirectory() as directory:
    storage = Storage(Path(directory) / "news.sqlite3")
    storage.initialize()
    with patch("collector._download_feed", side_effect=lambda url: (feeds[url], url)):
        now = datetime.now(UTC)
        articles = collect_articles(storage, sources, dedup_days=60, now=now)
        assert len(articles) == 1
        assert set(articles[0].sources) == {"Интерфакс", "Reuters"}
        assert collect_articles(storage, sources, dedup_days=60, now=now) == articles
        print(articles[0].title, "—", ", ".join(articles[0].sources))
```
