"""Клиент командного хаба: регистрация агента и обмен событиями.

Хаб берёт имя отправителя прямо из тела запроса, поэтому каждый участник
команды пишет под своим именем без каких-либо доработок на сервере.

Как доставляются запросы, клиент не знает — этим занимается transport.
Благодаря этому логику ниже можно проверять без живого сервера.

Настройки берутся из переменных окружения, а чего нет там — из файла
`~/.config/teamhub/config.json`, который создаёт teamhub_setup.py:

    TEAMHUB_AGENT_ID     фиксированное имя агента
    TEAMHUB_NAME_PREFIX  личный префикс: имя получится «префикс-проект»
    TEAMHUB_AUTH         basic-авторизация в виде user:password (необязательно)
    TEAMHUB_URL          адрес хаба, например http://127.0.0.1:8700
    TEAMHUB_SSH          user@host — плагин сам поднимет SSH-туннель до хаба
    TEAMHUB_SSH_PORT     порт хаба на сервере (по умолчанию 8700)
    TEAMHUB_SSH_KEY      путь к приватному ключу (необязательно)
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Final, NamedTuple

from config_store import config_path, load_for_agent, load_stored
from ssh_tunnel import SshTunnel
from transport import HttpTransport, HubRejected, HubUnreachable, Transport

__all__ = [
    "DEFAULT_REMOTE_PORT",
    "HubClient",
    "HubConfig",
    "HubRejected",
    "HubUnreachable",
    "build_auth_header",
    "load_config",
    "notify_settings",
    "resolve_agent_id",
]

DEFAULT_REMOTE_PORT: Final[int] = 8700
# по этим словам в отказе понимаем, что хаб забыл нашу регистрацию.
# «authentication failed» сюда входит не для красоты: именно так отвечает
# хаб после перезапуска, когда наш секрет уже недействителен
LOST_REGISTRATION: Final[tuple[str, ...]] = (
    "not registered",
    "unknown agent",
    "register",
    "authentication failed",
    "invalid or missing secret",
)


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
        ValueError: если порт в настройках не число.
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


def build_transport(config: HubConfig) -> HttpTransport:
    """Собирает транспорт под заданную конфигурацию."""
    tunnel = None
    if config.ssh_destination:
        tunnel = SshTunnel(config.ssh_destination, config.ssh_port, config.ssh_key)
    return HttpTransport(config.url, config.auth_header, tunnel)


class HubClient:
    """Общение с хабом от имени одного агента."""

    def __init__(self, config: HubConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = transport if transport is not None else build_transport(config)
        self._secret: str | None = None
        self._connected = False
        self._closed = False
        self._connect_lock = threading.Lock()

    @property
    def agent_id(self) -> str:
        """Имя, под которым агент пишет в каналы."""
        return self._config.agent_id

    def connect(self) -> None:
        """Открывает транспорт и регистрирует агента."""
        opener = getattr(self._transport, "open", None)
        if callable(opener):
            opener()
        self._register()
        self._connected = True

    def ensure_connected(self) -> None:
        """Подключается, если это ещё не удалось или регистрация потерялась.

        Raises:
            HubRejected: если клиент уже закрыт и переподключаться нельзя.
        """
        if self._closed:
            raise HubRejected("клиент закрыт")
        with self._connect_lock:
            if not self._connected:
                self.connect()

    def close(self) -> None:
        """Закрывает клиента навсегда: переподключений больше не будет."""
        self._closed = True
        self._connected = False
        self._transport.close()

    def _invalidate(self) -> None:
        """Помечает регистрацию потерянной — следующий вызов переподключится."""
        with self._connect_lock:
            self._connected = False
            self._secret = None

    @staticmethod
    def _lost_registration(message: str) -> bool:
        """Похож ли отказ хаба на забытую регистрацию."""
        lowered = message.lower()
        return any(marker in lowered for marker in LOST_REGISTRATION)

    @staticmethod
    def _reason(result: dict[str, Any], default: str) -> str:
        """Достаёт причину отказа: хаб кладёт её то в одно поле, то в другое."""
        return str(result.get("error_message") or result.get("message") or default)

    def _register(self) -> None:
        """Регистрирует агента в сети, запоминая выданный секрет.

        Raises:
            HubRejected: если хаб отклонил регистрацию.
        """
        result = self._transport.post("/api/register", {"agent_id": self.agent_id, "metadata": {}})
        if not result.get("success"):
            raise HubRejected(f"регистрация отклонена: {self._reason(result, 'без объяснения')}")
        self._secret = result.get("secret")
        _log(f"агент {self.agent_id} зарегистрирован")

    def event(
        self,
        event_name: str,
        payload: dict[str, Any],
        visibility: str = "network",
        relevant_mod: str | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Отправляет событие хабу и возвращает полезную нагрузку ответа.

        Raises:
            HubRejected: если хаб отклонил событие.
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
        result = self._transport.post("/api/send_event", body)
        if result.get("success"):
            return result.get("data") or {}
        message = self._reason(result, "хаб отклонил событие")
        if not retry or not self._lost_registration(message):
            raise HubRejected(message)
        _log("хаб забыл регистрацию, подключаюсь заново")
        self._invalidate()
        return self.event(event_name, payload, visibility, relevant_mod, retry=False)

    def poll_events(self) -> list[dict[str, Any]]:
        """Забирает накопленные для агента события хаба.

        Raises:
            HubRejected: если хаб отказал — иначе потеря регистрации осталась
                бы незамеченной и сессия оглохла бы навсегда.
        """
        self.ensure_connected()
        query = urllib.parse.urlencode({"agent_id": self.agent_id, "secret": self._secret or ""})
        result = self._transport.get(f"/api/poll?{query}")
        if not result.get("success", True):
            # любой отказ опроса после удачного подключения означает, что нас
            # разлюбили: секрет протух или хаб перезапустился. Сбрасываем
            # подключение — следующий круг зарегистрируется заново
            message = self._reason(result, "опрос отклонён")
            _log(f"опрос отклонён ({message}), переподключусь на следующем круге")
            self._invalidate()
            raise HubRejected(message)
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
