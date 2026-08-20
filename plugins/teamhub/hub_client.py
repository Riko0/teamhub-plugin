"""Клиент командного хаба поверх HTTP-протокола OpenAgents.

Хаб берёт имя отправителя прямо из тела запроса, поэтому каждый участник
команды пишет под своим именем без каких-либо доработок на сервере.

Настройки берутся из переменных окружения, а чего нет там — из файла
`~/.config/teamhub/config.json`, который создаёт teamhub_setup.py:

    TEAMHUB_AGENT_ID     фиксированное имя агента
    TEAMHUB_NAME_PREFIX  личный префикс: имя получится «префикс-проект»
    TEAMHUB_AUTH         basic-авторизация в виде user:password (необязательно)
    TEAMHUB_URL          адрес хаба, например http://127.0.0.1:8700
    TEAMHUB_SSH          user@host — плагин сам поднимет SSH-туннель до хаба
    TEAMHUB_SSH_PORT     порт хаба на сервере (по умолчанию 8700)
    TEAMHUB_SSH_KEY      путь к приватному ключу (необязательно)

Если TEAMHUB_SSH не задан, работаем по HTTP на указанный адрес. Имя агента без
явной настройки берётся из каталога проекта — см. resolve_agent_id.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Final, NamedTuple

from config_store import config_path, load_for_agent, load_stored
from ssh_tunnel import SshTunnel

REQUEST_TIMEOUT_S: Final[float] = 20.0
DEFAULT_REMOTE_PORT: Final[int] = 8700


def _log(message: str) -> None:
    """Пишет диагностику в stderr: stdout занят протоколом MCP."""
    sys.stderr.write(f"[teamhub] {message}\n")
    sys.stderr.flush()


class HubConfig(NamedTuple):
    """Параметры подключения к хабу."""

    agent_id: str
    auth_header: str | None
    url: str | None
    ssh_destination: str | None
    ssh_port: int
    ssh_key: str | None


def build_auth_header(auth: str | None) -> str | None:
    """Собирает заголовок basic-авторизации, если она задана."""
    if not auth:
        return None
    return "Basic " + base64.b64encode(auth.encode("utf-8")).decode("ascii")


def _setting(stored: dict[str, str], variable: str, key: str) -> str | None:
    """Возвращает значение из окружения, иначе из сохранённой настройки.

    Неразвёрнутую подстановку вида ``${VAR}`` игнорируем: конфигурация MCP
    может передать её буквально, если переменная не задана.
    """
    value = os.environ.get(variable, "").strip()
    if value.startswith("${"):
        value = ""
    return value or stored.get(key) or None


def resolve_agent_id(stored: dict[str, str]) -> str:
    """Определяет имя, под которым сессия пишет в чат.

    Плагин ставится один на компьютер, а сессий у человека несколько, поэтому
    имя по умолчанию берётся из каталога проекта — Claude Code запускает сервер
    именно в нём. Порядок: явное имя, затем «префикс-проект», затем проект.
    """
    explicit = _setting(stored, "TEAMHUB_AGENT_ID", "agent_id")
    if explicit:
        return explicit
    project = Path.cwd().name or "unknown"
    prefix = _setting(stored, "TEAMHUB_NAME_PREFIX", "name_prefix")
    return f"{prefix}-{project}" if prefix else project


def notify_settings(agent_id: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Настройки уведомлений с учётом персональных надстроек этого агента."""
    stored = load_for_agent(agent_id)
    return (
        _setting(stored, "TEAMHUB_NOTIFY", "notify"),
        _setting(stored, "TEAMHUB_NOTIFY_CHANNELS", "notify_channels"),
        _setting(stored, "TEAMHUB_NOTIFY_MAX_PER_HOUR", "notify_max_per_hour"),
        _setting(stored, "TEAMHUB_NOTIFY_MAX_CHARS", "notify_max_chars"),
    )


def load_config() -> HubConfig:
    """Собирает конфигурацию: окружение важнее сохранённого файла.

    Raises:
        RuntimeError: если не задан ни адрес хаба, ни SSH-назначение.
    """
    agent_id = resolve_agent_id(load_stored())
    stored = load_for_agent(agent_id)
    url = (_setting(stored, "TEAMHUB_URL", "url") or "").rstrip("/") or None
    ssh_destination = _setting(stored, "TEAMHUB_SSH", "ssh")
    if not url and not ssh_destination:
        raise RuntimeError(f"не задан адрес хаба, файл настроек отсутствует ({config_path()})")
    return HubConfig(
        agent_id=agent_id,
        auth_header=build_auth_header(_setting(stored, "TEAMHUB_AUTH", "auth")),
        url=url,
        ssh_destination=ssh_destination,
        ssh_port=int(_setting(stored, "TEAMHUB_SSH_PORT", "ssh_port") or DEFAULT_REMOTE_PORT),
        ssh_key=_setting(stored, "TEAMHUB_SSH_KEY", "ssh_key"),
    )


class HubClient:
    """Общение с хабом по HTTP от имени одного агента."""

    def __init__(self, config: HubConfig) -> None:
        self._config = config
        self._secret: str | None = None
        self._connected = False
        self._connect_lock = threading.Lock()
        self._tunnel: SshTunnel | None = None
        if config.ssh_destination:
            self._tunnel = SshTunnel(config.ssh_destination, config.ssh_port, config.ssh_key)

    @property
    def agent_id(self) -> str:
        """Имя, под которым агент пишет в каналы."""
        return self._config.agent_id

    @property
    def _base_url(self) -> str:
        """Текущий адрес хаба: через туннель или прямой."""
        if self._tunnel is not None:
            return f"http://127.0.0.1:{self._tunnel.local_port}"
        return str(self._config.url)

    def connect(self) -> None:
        """Поднимает туннель при необходимости и регистрирует агента."""
        if self._tunnel is not None:
            self._tunnel.start()
        self._register()
        self._connected = True

    def ensure_connected(self) -> None:
        """Подключается, если это ещё не удалось: хаб мог быть недоступен на старте.

        Вызывается и из фонового опроса, поэтому под блокировкой.
        """
        with self._connect_lock:
            if not self._connected:
                self.connect()

    def close(self) -> None:
        """Закрывает туннель, если он поднимался."""
        if self._tunnel is not None:
            self._tunnel.stop()
        self._connected = False

    def _request(self, request: urllib.request.Request) -> dict[str, Any]:
        """Выполняет запрос и возвращает разобранный ответ.

        Raises:
            RuntimeError: если хаб недоступен или вернул ошибку транспорта.
        """
        if self._config.auth_header:
            request.add_header("Authorization", self._config.auth_header)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"хаб ответил {exc.code} на {request.selector}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"нет связи с хабом: {exc}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        """Выполняет GET-запрос к хабу."""
        if self._tunnel is not None:
            self._tunnel.ensure_alive()
        return self._request(urllib.request.Request(f"{self._base_url}{path}", method="GET"))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Выполняет POST и возвращает разобранный ответ.

        Raises:
            RuntimeError: если хаб недоступен или вернул ошибку транспорта.
        """
        if self._tunnel is not None:
            self._tunnel.ensure_alive()
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{self._base_url}{path}", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        return self._request(request)

    def _register(self) -> None:
        """Регистрирует агента в сети, запоминая выданный секрет.

        Raises:
            RuntimeError: если хаб отклонил регистрацию.
        """
        result = self._post("/api/register", {"agent_id": self.agent_id, "metadata": {}})
        if not result.get("success"):
            raise RuntimeError(f"регистрация отклонена: {result.get('error_message')}")
        self._secret = result.get("secret")
        _log(f"агент {self.agent_id} зарегистрирован")

    def event(
        self,
        event_name: str,
        payload: dict[str, Any],
        visibility: str = "network",
        relevant_mod: str | None = None,
    ) -> dict[str, Any]:
        """Отправляет событие хабу и возвращает полезную нагрузку ответа.

        Raises:
            RuntimeError: если хаб отклонил событие.
        """
        self.ensure_connected()
        body: dict[str, Any] = {
            "event_name": event_name,
            "source_id": self.agent_id,
            "event_id": str(uuid.uuid4()),
            "visibility": visibility,
            "payload": payload,
        }
        if relevant_mod:
            body["relevant_mod"] = relevant_mod
        if self._secret:
            body["secret"] = self._secret
        result = self._post("/api/send_event", body)
        if not result.get("success"):
            raise RuntimeError(str(result.get("error_message") or "хаб отклонил событие"))
        return result.get("data") or {}

    def poll_events(self) -> list[dict[str, Any]]:
        """Забирает накопленные для агента события хаба.

        Хаб складывает сюда уведомления о чужих сообщениях — это позволяет
        узнавать о них, не дожидаясь, пока агента попросят посмотреть канал.
        """
        self.ensure_connected()
        query = urllib.parse.urlencode({"agent_id": self.agent_id, "secret": self._secret or ""})
        result = self._get(f"/api/poll?{query}")
        return list(result.get("messages") or [])

    def send_message(self, channel: str, text: str) -> None:
        """Публикует сообщение в канал от имени агента."""
        self.event(
            "thread.channel_message.post",
            {
                "message_type": "channel_message",
                "channel": channel,
                "content": {"text": text},
                "source_id": self.agent_id,
                "relevant_agent_id": self.agent_id,
            },
            visibility="network",
        )

    def read_messages(self, channel: str, limit: int) -> list[dict[str, Any]]:
        """Возвращает последние сообщения канала."""
        data = self.event(
            "thread.channel_messages.retrieve",
            {
                "action": "retrieve_channel_messages",
                "message_type": "message_retrieval",
                "channel": channel,
                "limit": limit,
                "offset": 0,
                "include_threads": True,
            },
            visibility="mod_only",
        )
        return data.get("messages") or []

    def list_channels(self) -> list[dict[str, Any]]:
        """Возвращает список каналов хаба."""
        data = self.event(
            "thread.channels.list",
            {"action": "list_channels", "message_type": "channel_info"},
            visibility="mod_only",
        )
        return data.get("channels") or []
