from concurrent.futures import ThreadPoolExecutor
import html
import json
import logging
from threading import Event, Thread
import time

from .config import Config
from .feeds import fetch_feed
from .ranking import digest_text, select_top
from .storage import Store
from .telegram import Telegram, TelegramError

LOG = logging.getLogger(__name__)
HELP = (
    "<b>Главные новости мира</b>\n\n"
    "/news — свежая подборка\n"
    "/sources — источники\n"
    "/keywords — текущий фильтр\n"
    "/keywords космос, экономика — фильтр по любому из слов\n"
    "/keywords off — все темы\n"
    "/status — состояние бота\n"
    "/stop — отключить рассылку\n"
    "/start — включить рассылку\n"
    "/help — помощь\n\n"
    "Приоритет зависит от свежести и позиции в RSS-ленте. "
    "Это редакционная подборка, а не рейтинг просмотров."
)


def refresh(config: Config, store: Store) -> tuple[int, int]:
    def read(source):
        try:
            return source, fetch_feed(source), None
        except Exception as exc:
            # One broken feed must not interrupt the other sources.
            return source, [], type(exc).__name__

    inserted = successful = 0
    health = {}
    with ThreadPoolExecutor(max_workers=min(4, len(config.sources))) as pool:
        for source, articles, error in pool.map(read, config.sources):
            if error:
                LOG.warning("Источник %s недоступен: %s", source.name, error)
                health[source.name] = "недоступен"
            else:
                inserted += store.add(articles)
                successful += 1
                health[source.name] = f"OK, записей: {len(articles)}"
    store.set_state("source_health", json.dumps(health, ensure_ascii=False))
    store.set_state("last_attempt", str(time.time()))
    if successful:
        store.set_state("last_success", str(time.time()))
    LOG.info("Обновление: %s/%s источников, %s новых ссылок", successful, len(config.sources), inserted)
    return successful, inserted


def feed_worker(config: Config, stop: Event):
    store = Store(config.database)
    try:
        while not stop.wait(config.poll_interval_seconds):
            try:
                refresh(config, store)
            except Exception:
                LOG.exception("Ошибка обновления базы новостей")
    finally:
        store.close()


class Bot:
    def __init__(self, config: Config, store: Store, telegram: Telegram, username: str = ""):
        self.config, self.store, self.telegram = config, store, telegram
        self.username = username.casefold()
        self.interval = config.digest_interval_minutes * 60

    def top(self, keywords: list[str], after: int = 0, through: int | None = None):
        candidates = self.store.candidates(self.config.max_age_hours, after, through)
        return select_top(candidates, keywords, self.config.digest_size, self.config.max_age_hours)

    def handle(self, update: dict):
        message = update.get("message", {})
        chat = message.get("chat", {})
        # Personal subscriptions only: a group member cannot change another user's filters.
        if chat.get("type") != "private" or not isinstance(chat.get("id"), int):
            return
        text = message.get("text", "").strip()
        if not text:
            return
        command, _, argument = text.partition(" ")
        command, _, mention = command.partition("@")
        if mention and mention.casefold() != self.username:
            return
        chat_id = chat["id"]
        subscriber = self.store.subscriber(chat_id)
        keywords = json.loads(subscriber["keywords"]) if subscriber else []
        argument = argument.strip()

        if command == "/start":
            if subscriber is None or not subscriber["active"]:
                self.store.subscribe(chat_id, self.interval)
            response = (
                f"Рассылка включена: до {self.config.digest_size} новых заголовков каждые "
                f"{self.config.digest_interval_minutes} мин., если есть подходящие новости.\n"
                "Первую подборку можно получить командой /news.\n\n" + HELP
            )
        elif command == "/stop":
            self.store.unsubscribe(chat_id)
            response = "Рассылка отключена. Для возобновления: /start."
        elif command in {"/news", "/top"}:
            through = self.store.newest_id()
            articles = self.top(keywords, through=through)
            response = digest_text(articles)
            if not articles and not self.store.get_state("last_success"):
                response = "Пока не удалось загрузить источники. Бот повторит попытку автоматически."
            self.telegram.send(chat_id, response)
            if subscriber and subscriber["active"]:
                self.store.advance(chat_id, through, time.time() + self.interval)
            return
        elif command == "/sources":
            health = json.loads(self.store.get_state("source_health", "{}"))
            response = "<b>Источники</b>\n" + "\n".join(
                f"• {html.escape(source.name)} — {html.escape(health.get(source.name, 'ещё не проверен'))}"
                for source in self.config.sources
            )
        elif command == "/keywords":
            if subscriber is None:
                response = "Сначала включите бота командой /start."
            elif not argument:
                response = "Фильтр: " + html.escape(", ".join(keywords) or "все темы")
            else:
                values = [] if argument.casefold() in {"off", "все"} else list(dict.fromkeys(
                    word.strip().casefold() for word in argument.split(",") if word.strip()
                ))
                if len(values) > 10 or any(len(word) > 60 for word in values):
                    response = "Укажите до 10 слов или фраз через запятую, до 60 символов каждое."
                else:
                    self.store.keywords(chat_id, values)
                    response = "Фильтр сохранён: " + html.escape(", ".join(values) or "все темы")
        elif command == "/status":
            active = bool(subscriber and subscriber["active"])
            last = self.store.get_state("last_success")
            freshness = (f"{max(0, int((time.time() - float(last)) / 60))} мин. назад"
                         if last else "пока не было")
            response = (
                f"Рассылка: {'включена' if active else 'выключена'}\n"
                f"Интервал: {self.config.digest_interval_minutes} мин.\n"
                f"Фильтр: {html.escape(', '.join(keywords) or 'все темы')}\n"
                f"Успешное обновление: {freshness}\n"
                "Доступность отдельных лент: /sources"
            )
        else:
            response = HELP
        self.telegram.send(chat_id, response)

    def broadcast(self, stop: Event):
        for subscriber in self.store.due(time.time()):
            if stop.is_set():
                break
            chat_id = subscriber["chat_id"]
            through = self.store.newest_id()
            articles = self.top(json.loads(subscriber["keywords"]), subscriber["cursor"], through)
            try:
                if articles:
                    self.telegram.send(chat_id, digest_text(articles, "Новая подборка мировых новостей"))
            except TelegramError as exc:
                if exc.code in {401, 409}:
                    raise
                if exc.code in {400, 403}:
                    self.store.unsubscribe(chat_id)
                    LOG.warning("Подписка отключена: Telegram %s", exc.code)
                else:
                    self.store.advance(chat_id, subscriber["cursor"], time.time() + max(60, exc.retry_after))
                    if exc.code == 429:
                        stop.wait(exc.retry_after)
                        break
                continue
            self.store.advance(chat_id, through, time.time() + self.interval)
            if articles:
                stop.wait(0.1)


def run_bot(config: Config, token: str, stop: Event):
    telegram = Telegram(token)
    identity = telegram.call("getMe")
    if telegram.call("getWebhookInfo").get("url"):
        raise ValueError("У бота включён webhook. Используйте отдельного бота или предварительно отключите webhook.")
    store = Store(config.database)
    worker = None
    try:
        refresh(config, store)
        worker = Thread(target=feed_worker, args=(config, stop), daemon=True)
        worker.start()
        bot = Bot(config, store, telegram, identity.get("username", ""))
        offset = int(store.get_state("telegram_offset", "0"))
        LOG.info("Бот @%s запущен. Остановить: Ctrl+C", identity.get("username", ""))
        while not stop.is_set():
            try:
                updates = telegram.updates(offset)
                for update in updates:
                    if stop.is_set():
                        break
                    try:
                        bot.handle(update)
                    except TelegramError as exc:
                        if exc.code not in {400, 403}:
                            raise
                        LOG.warning("Ответ не доставлен: Telegram %s", exc.code)
                    offset = update["update_id"] + 1
                    store.set_state("telegram_offset", str(offset))
                bot.broadcast(stop)
            except TelegramError as exc:
                if exc.code in {401, 409}:
                    raise
                LOG.warning("%s Повтор через %s с.", exc, exc.retry_after)
                stop.wait(exc.retry_after)
    finally:
        stop.set()
        if worker:
            worker.join(timeout=20)
        store.close()

