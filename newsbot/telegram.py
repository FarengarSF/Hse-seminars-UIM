import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .network import tls_context


class TelegramError(Exception):
    def __init__(self, code: int = 0, retry_after: int = 5):
        self.code = code
        self.retry_after = retry_after
        # Deliberately exclude request URLs: they contain the bot token.
        super().__init__(f"Ошибка Telegram API (код {code or 'сеть'}).")


class Telegram:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._context = tls_context()

    def call(self, method: str, **params):
        request = Request(self._base + method,
                          data=json.dumps(params).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        code = 0
        try:
            with urlopen(request, timeout=35, context=self._context) as response:
                body = response.read(4 * 1024 * 1024)
        except HTTPError as exc:
            code = exc.code
            body = exc.read(4 * 1024 * 1024)
            exc.close()
        except (URLError, TimeoutError, OSError):
            raise TelegramError() from None
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise TelegramError(code) from None
        if not isinstance(result, dict):
            raise TelegramError(code)
        if not result.get("ok"):
            delay = result.get("parameters", {}).get("retry_after", 5)
            raise TelegramError(result.get("error_code", code), max(1, int(delay)))
        return result["result"]

    def updates(self, offset: int):
        return self.call("getUpdates", offset=offset, timeout=5, allowed_updates=["message"])

    def send(self, chat_id: int, text: str):
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         parse_mode="HTML", link_preview_options={"is_disabled": True})

