import json
from pathlib import Path
import sqlite3
import time

from .feeds import Article


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                summary TEXT NOT NULL,
                published REAL NOT NULL,
                position INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS articles_published ON articles(published);
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                keywords TEXT NOT NULL DEFAULT '[]',
                cursor INTEGER NOT NULL DEFAULT 0,
                next_digest REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def add(self, articles: list[Article]) -> int:
        count = 0
        with self.db:
            for article in articles:
                cursor = self.db.execute("""
                    INSERT INTO articles(url,title,source,summary,published,position)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING
                """, (article.url, article.title, article.source, article.summary,
                      article.published, article.position))
                count += cursor.rowcount
                if cursor.rowcount == 0:
                    # Refresh editorial rank without changing the delivery ID or publication date.
                    self.db.execute("UPDATE articles SET position=? WHERE url=?",
                                    (article.position, article.url))
        return count

    def newest_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM articles").fetchone()[0]

    def subscribe(self, chat_id: int, interval: int):
        with self.db:
            self.db.execute("""
                INSERT INTO subscribers(chat_id,cursor,next_digest) VALUES(?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET active=1,
                    cursor=excluded.cursor, next_digest=excluded.next_digest
            """, (chat_id, self.newest_id(), time.time() + interval))

    def unsubscribe(self, chat_id: int):
        with self.db:
            self.db.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (chat_id,))

    def subscriber(self, chat_id: int):
        return self.db.execute("SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()

    def keywords(self, chat_id: int, values: list[str]):
        with self.db:
            self.db.execute("UPDATE subscribers SET keywords=? WHERE chat_id=?",
                            (json.dumps(values, ensure_ascii=False), chat_id))

    def due(self, now: float):
        return self.db.execute("SELECT * FROM subscribers WHERE active=1 AND next_digest<=?",
                               (now,)).fetchall()

    def advance(self, chat_id: int, cursor: int, next_digest: float):
        with self.db:
            self.db.execute("UPDATE subscribers SET cursor=?,next_digest=? WHERE chat_id=?",
                            (cursor, next_digest, chat_id))

    def candidates(self, max_age_hours: int, after: int = 0, through: int | None = None):
        through = self.newest_id() if through is None else through
        return self.db.execute("""
            SELECT * FROM articles WHERE id>? AND id<=? AND published>=?
            ORDER BY published DESC LIMIT 2000
        """, (after, through, time.time() - max_age_hours * 3600)).fetchall()

    def get_state(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value))

