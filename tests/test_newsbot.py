from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from newsbot.config import Config, Source, load_config
from newsbot.feeds import Article, canonical_url, parse_feed
from newsbot.ranking import digest_text, select_top
from newsbot.service import Bot, refresh
from newsbot.storage import Store
from newsbot.telegram import Telegram, TelegramError


SOURCE = Source("Test News", "https://example.com/feed")


def article(title="Мировая экономика", url="https://example.com/1", position=0, age=0, source="Test News"):
    return Article(title, url, source, "Описание новости", time.time() - age * 3600, position)


class ParserTests(unittest.TestCase):
    def test_rss_html_dates_tracking_and_invalid_entries(self):
        feed = b'''<rss><channel>
          <item><title>&lt;b&gt;Title &amp;amp; text&lt;/b&gt;</title>
            <link>https://example.com/story?id=7&amp;utm_source=feed#section</link>
            <description>&lt;script&gt;bad()&lt;/script&gt;Good</description>
            <pubDate>Wed, 01 Jan 2025 10:00:00 GMT</pubDate></item>
          <item><title>Unsafe</title><link>javascript:alert(1)</link></item>
          <item><title>No link</title><guid>opaque-id</guid></item>
          <item><link>https://example.com/no-title</link></item>
        </channel></rss>'''
        items = parse_feed(feed, SOURCE)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Title & text")
        self.assertEqual(items[0].summary, "Good")
        self.assertEqual(items[0].url, "https://example.com/story?id=7")
        self.assertEqual(items[0].published, 1735725600)

    def test_atom_alternate_link_date_and_relative_url(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Atom</title><link rel="self" href="/api/entry"/>
          <link href="/article"/><published>2025-01-01T12:00:00+02:00</published>
          <summary type="html">&lt;p&gt;Hello&lt;/p&gt;</summary>
        </entry></feed>'''
        item = parse_feed(feed, SOURCE)[0]
        self.assertEqual(item.url, "https://example.com/article")
        self.assertEqual(item.published, 1735725600)
        self.assertEqual(item.summary, "Hello")

    def test_rdf_and_unknown_date(self):
        feed = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/"><item><title>RDF</title>
          <link>https://example.com/rdf</link><pubDate>not a date</pubDate></item></rdf:RDF>'''
        self.assertEqual(parse_feed(feed, SOURCE, now=1234)[0].published, 1234)

    def test_dtd_and_html_response_rejected(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss/>', SOURCE)
        with self.assertRaises(ValueError):
            parse_feed(b'<html><body>Access denied</body></html>', SOURCE)

    def test_canonicalization_preserves_article_identity(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com/a?id=2&utm_medium=rss#x"),
                         "https://example.com/a?id=2")
        self.assertNotEqual(canonical_url("https://example.com/a?id=2"),
                            canonical_url("https://example.com/a?id=3"))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "news.sqlite3"
        self.config = Config((SOURCE,), self.path)
        self.store = Store(self.path)
        self.sent = []
        self.failure = None
        parent = self

        class FakeTelegram:
            def send(self, chat_id, text):
                if parent.failure:
                    raise parent.failure
                parent.sent.append((chat_id, text))

        self.bot = Bot(self.config, self.store, FakeTelegram(), "worldbot")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def command(self, text, chat_id=1, chat_type="private"):
        self.bot.handle({"message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}})

    def due(self, chat_id=1):
        subscriber = self.store.subscriber(chat_id)
        self.store.advance(chat_id, subscriber["cursor"], 0)

    def test_deduplication_and_history_survive_restart(self):
        self.assertEqual(self.store.add([article()]), 1)
        self.assertEqual(self.store.add([article(title="Updated")]), 0)
        self.command("/start")
        self.command("/keywords Космос, Экономика")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.store.candidates(24)), 1)
        self.assertEqual(json.loads(self.store.subscriber(1)["keywords"]), ["космос", "экономика"])

    def test_subscription_ignores_archive_and_sends_new_once(self):
        self.store.add([article()])
        self.command("/start")
        self.store.add([article("Новая новость", "https://example.com/2")])
        self.due()
        self.bot.broadcast(Event())
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Новая новость", self.sent[-1][1])
        self.assertNotIn("Мировая экономика", self.sent[-1][1])
        self.due()
        self.bot.broadcast(Event())
        self.assertEqual(len(self.sent), 2)

    def test_filters_are_independent_and_stop_prevents_delivery(self):
        self.command("/start", 1)
        self.command("/keywords космос", 1)
        self.command("/start", 2)
        self.command("/stop", 2)
        self.store.add([article(), article("Космос сегодня", "https://example.com/space")])
        self.due(1)
        self.due(2)
        self.bot.broadcast(Event())
        self.assertEqual(self.sent[-1][0], 1)
        self.assertIn("Космос сегодня", self.sent[-1][1])
        self.assertNotIn("Мировая экономика", self.sent[-1][1])
        self.assertFalse(self.store.subscriber(2)["active"])

    def test_delivery_failure_does_not_lose_pending_news(self):
        self.command("/start")
        self.store.add([article()])
        self.due()
        self.failure = TelegramError(503)
        self.bot.broadcast(Event())
        self.assertEqual(self.store.subscriber(1)["cursor"], 0)
        self.failure = None
        self.due()
        self.bot.broadcast(Event())
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.subscriber(1)["cursor"], self.store.newest_id())

    def test_blocked_user_is_deactivated(self):
        self.command("/start")
        self.store.add([article()])
        self.due()
        self.failure = TelegramError(403)
        self.bot.broadcast(Event())
        self.assertFalse(self.store.subscriber(1)["active"])

    def test_rate_limit_keeps_news_and_observes_retry_after(self):
        self.command("/start")
        self.store.add([article()])
        self.due()
        self.failure = TelegramError(429, 17)
        with patch.object(Event, "wait", return_value=False) as wait:
            self.bot.broadcast(Event())
            wait.assert_called_once_with(17)
        self.assertEqual(self.store.subscriber(1)["cursor"], 0)

    def test_manual_digest_is_not_repeated_in_auto_digest(self):
        self.command("/start")
        self.store.add([article()])
        self.command("/news")
        self.due()
        self.bot.broadcast(Event())
        self.assertEqual(len(self.sent), 2)

    def test_group_messages_and_other_bot_commands_ignored(self):
        self.command("/start", chat_type="group")
        self.command("/start@anotherbot")
        self.assertFalse(self.sent)
        self.assertIsNone(self.store.subscriber(1))

    def test_one_failed_source_does_not_block_healthy_source(self):
        other = Source("Other", "https://example.org/rss")
        config = replace(self.config, sources=(SOURCE, other))

        def fake_fetch(source):
            if source == SOURCE:
                raise URLError("offline")
            return [article()]

        with patch("newsbot.service.fetch_feed", side_effect=fake_fetch):
            self.assertEqual(refresh(config, self.store), (1, 1))
        self.assertTrue(self.store.get_state("last_success"))

    def test_ranking_prefers_prominent_fresh_articles_and_respects_filter(self):
        self.store.add([
            article("Космос: свежая главная", "https://example.com/1", position=0),
            article("Космос: старая", "https://example.com/2", position=0, age=20),
            article("Космос: далеко в ленте", "https://example.com/3", position=50),
            article("Экономика", "https://example.com/4"),
            article("Космос: архив", "https://example.com/5", age=48),
        ])
        top = self.bot.top(["космос"])
        self.assertEqual(top[0]["title"], "Космос: свежая главная")
        self.assertEqual(len(top), 3)

    def test_html_is_escaped_and_visible_text_stays_below_telegram_limit(self):
        self.store.add([article('<b>Headline</b> & "quote"')])
        text = digest_text(self.store.candidates(24))
        self.assertIn("&lt;b&gt;Headline&lt;/b&gt;", text)
        rows = [dict(title="😀" * 500, summary="😀" * 1500, source="😀" * 80,
                     url="https://example.com") for _ in range(5)]
        from newsbot.feeds import plain_text
        visible = plain_text(digest_text(rows))
        self.assertLess(len(visible.encode("utf-16-le")) // 2, 4096)


class ConfigurationAndAPITests(unittest.TestCase):
    def test_configuration_rejects_unsafe_or_invalid_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for change in ({"digest_size": True}, {"poll_interval_seconds": 0},
                           {"sources": [{"name": "Test", "url": "file:///etc/passwd"}]}):
                config = {"sources": [{"name": "Test", "url": "https://example.com/rss"}]}
                config.update(change)
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_telegram_retry_after_and_no_token_in_errors(self):
        error = HTTPError("https://api.telegram.org/botsecret/sendMessage", 429, "Too many", {},
                          BytesIO(b'{"ok":false,"error_code":429,"parameters":{"retry_after":19}}'))
        with patch("newsbot.telegram.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as caught:
                Telegram("secret").send(1, "test")
        self.assertEqual(caught.exception.retry_after, 19)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
