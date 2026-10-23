import argparse
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import signal
import sqlite3
from threading import Event

from .config import Source, load_config
from .feeds import parse_feed
from .ranking import select_top
from .service import refresh, run_bot
from .storage import Store
from .telegram import TelegramError


def demo():
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    payload = f"""<rss version="2.0"><channel><title>Демо</title>
      <item><title>Учебный пример: международная научная экспедиция</title>
        <link>https://example.com/science?utm_source=rss</link>
        <description>Вымышленная новость для проверки программы.</description><pubDate>{now}</pubDate></item>
      <item><title>Учебный пример: международная научная экспедиция</title>
        <link>https://example.com/science</link><pubDate>{now}</pubDate></item>
      <item><title>Учебный пример: мировой экономический форум</title>
        <link>https://example.com/economy</link><pubDate>{now}</pubDate></item>
    </channel></rss>""".encode()
    store = Store(":memory:")
    try:
        store.add(parse_feed(payload, Source("Демо-источник", "https://example.com/rss")))
        print("ДЕМОНСТРАЦИЯ — вымышленные новости, сеть и Telegram не используются.\n")
        show(select_top(store.candidates(24), [], 5, 24))
        print("\nПовтор одной ссылки удалён: из 3 записей осталось 2.")
    finally:
        store.close()


def show(articles):
    for number, article in enumerate(articles, 1):
        print(f"{number}. {article['title']}\n   {article['source']}\n   {article['url']}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-агрегатор главных мировых новостей")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="Путь к конфигурации JSON")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Демонстрация без сети и токена")
    modes.add_argument("--once", action="store_true", help="Загрузить свежие новости и вывести в терминал без Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()
    try:
        if args.demo:
            demo()
            return 0
        config = load_config(args.config)
        if args.once:
            store = Store(config.database)
            try:
                successful, _ = refresh(config, store)
                articles = select_top(store.candidates(config.max_age_hours), [], config.digest_size,
                                      config.max_age_hours)
                show(articles)
                if not articles:
                    print("В выбранном временном окне нет новостей.")
                return 0 if successful else 1
            finally:
                store.close()
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            parser.error("Задайте TELEGRAM_BOT_TOKEN. Для проверки без токена: --demo или --once.")
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        run_bot(config, token, stop)
        return 0
    except KeyboardInterrupt:
        stop.set()
        return 0
    except (OSError, ValueError, sqlite3.Error, TelegramError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
