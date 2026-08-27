"""Транспорт до хаба: HTTP, авторизация и туннель.

Вынесено из клиента ради одного шва: подставив сюда заглушку, логику работы
с хабом можно проверять без живого сервера. Заодно туннель оказался там, где
ему место — рядом с адресом, который он определяет.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any, Final, Protocol

from ssh_tunnel import SshTunnel

REQUEST_TIMEOUT_S: Final[float] = 20.0


class HubUnreachable(RuntimeError):
    """Связи с хабом нет: сеть, таймаут, недоступный порт."""


class HubRejected(RuntimeError):
    """Хаб ответил, но отказал — например, запрошенной страницы не существует."""


def _log(message: str) -> None:
    """Пишет диагностику в stderr: stdout занят протоколом MCP."""
    sys.stderr.write(f"[teamhub] {message}\n")
    sys.stderr.flush()


class Transport(Protocol):
    """Способ доставить запрос до хаба и вернуть разобранный ответ."""

    def get(self, path: str) -> dict[str, Any]:
        """Выполняет GET-запрос."""
        ...

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Выполняет POST-запрос."""
        ...

    def close(self) -> None:
        """Освобождает ресурсы транспорта."""
        ...


class HttpTransport:
    """Обращения к хабу по HTTP, при необходимости через SSH-туннель."""

    def __init__(self, url: str | None, auth_header: str | None, tunnel: SshTunnel | None = None) -> None:
        self._url = (url or "").rstrip("/")
        self._auth_header = auth_header
        self._tunnel = tunnel

    @property
    def base_url(self) -> str:
        """Адрес хаба: через туннель или прямой."""
        if self._tunnel is not None:
            return f"http://127.0.0.1:{self._tunnel.local_port}"
        return self._url

    def open(self) -> None:
        """Поднимает туннель, если он нужен."""
        if self._tunnel is not None:
            self._tunnel.start()

    def close(self) -> None:
        """Закрывает туннель, если он поднимался."""
        if self._tunnel is not None:
            self._tunnel.stop()

    def get(self, path: str) -> dict[str, Any]:
        """Выполняет GET-запрос к хабу."""
        return self._send(urllib.request.Request(self._address(path), method="GET"))

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Выполняет POST-запрос к хабу."""
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self._address(path), data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        return self._send(request)

    def _address(self, path: str) -> str:
        """Собирает полный адрес, предварительно оживив туннель."""
        if self._tunnel is not None:
            self._tunnel.ensure_alive()
        return f"{self.base_url}{path}"

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        """Выполняет запрос и разбирает ответ.

        Raises:
            HubRejected: хаб ответил кодом ошибки.
            HubUnreachable: до хаба не достучались.
        """
        if self._auth_header:
            request.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            path = request.selector.split("?", 1)[0]  # в строке запроса лежит секрет
            raise HubRejected(f"хаб ответил {exc.code} на {path}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise HubUnreachable(f"нет связи с хабом: {type(exc).__name__}") from exc
