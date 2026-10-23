from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...]
    database: Path
    poll_interval_seconds: int = 300
    digest_interval_minutes: int = 30
    max_age_hours: int = 24
    digest_size: int = 5


def http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        return False


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Конфигурация должна быть JSON-объектом.")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
        raise ValueError("В sources нужно указать от 1 до 20 RSS-источников.")
    parsed = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Источник должен содержать name и url.")
        name, url = source.get("name"), source.get("url")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Название источника должно содержать от 1 до 80 символов.")
        if not isinstance(url, str) or not http_url(url):
            raise ValueError(f"У источника {name} должен быть HTTP(S) URL.")
        parsed.append(Source(name.strip(), url))
    if len({source.name for source in parsed}) != len(parsed):
        raise ValueError("Названия источников должны быть уникальными.")
    if len({source.url for source in parsed}) != len(parsed):
        raise ValueError("Адреса источников должны быть уникальными.")
    settings = {}
    for key, default, lower, upper in (
        ("poll_interval_seconds", 300, 30, 86400),
        ("digest_interval_minutes", 30, 1, 1440),
        ("max_age_hours", 24, 1, 168),
        ("digest_size", 5, 1, 5),
    ):
        value = raw.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{key}: нужно целое число от {lower} до {upper}.")
        settings[key] = value
    database = raw.get("database", "data/news.sqlite3")
    if not isinstance(database, str) or not database.strip():
        raise ValueError("database: укажите путь к базе SQLite.")
    return Config(tuple(parsed), path.resolve().parent / database, **settings)

