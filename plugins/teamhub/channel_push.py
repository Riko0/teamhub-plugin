"""Доставка входящих сообщений хаба прямо в живую сессию Claude Code.

Claude Code умеет принимать от MCP-сервера уведомления `notifications/claude/channel`
и показывать их модели посреди работы — так устроен, например, канал Telegram.
Сервер должен объявить способность `claude/channel`, а сессия — запускаться
с флагом `--channels`.

Здесь фоновый поток опрашивает `/api/poll` хаба и превращает каждое чужое
сообщение в такое уведомление. Опрос идёт в фоне, поэтому агента не нужно
просить «сходи посмотри»: сообщение приходит само.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, Final, NamedTuple

POLL_INTERVAL_S: Final[float] = 3.0
POLL_BACKOFF_S: Final[float] = 15.0
CHANNEL_NOTIFICATION: Final[str] = "notifications/claude/channel"
BROADCAST_TAGS: Final[tuple[str, ...]] = ("@all", "@here", "@все")
NOTIFY_MENTIONS: Final[str] = "mentions"
NOTIFY_ALL: Final[str] = "all"
NOTIFY_OFF: Final[str] = "off"
DEFAULT_MAX_PER_HOUR: Final[int] = 20
DEFAULT_MAX_CHARS: Final[int] = 600
HOUR_S: Final[float] = 3600.0


def _log(message: str) -> None:
    """Пишет диагностику в stderr: stdout занят протоколом MCP."""
    sys.stderr.write(f"[teamhub] {message}\n")
    sys.stderr.flush()


class NotificationWriter:
    """Пишет уведомления в stdout, не сталкиваясь с основным циклом."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def send(self, method: str, params: dict[str, Any]) -> None:
        """Отправляет уведомление JSON-RPC (без идентификатора запроса)."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with self._lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def _extract(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Достаёт из события отправителя, канал и текст; None — если это не сообщение."""
    payload = event.get("payload") or {}
    content = payload.get("content")
    text = content.get("text", "") if isinstance(content, dict) else (payload.get("text") or "")
    text = str(text).strip()
    if not text:
        return None
    author = str(payload.get("source_id") or event.get("source_id") or "?")
    channel = str(payload.get("channel") or "general")
    return author, channel, text


class NotifyPolicy(NamedTuple):
    """Что именно заставляет будить сессию и в каких пределах."""

    mode: str = NOTIFY_MENTIONS
    channels: frozenset[str] = frozenset()
    max_per_hour: int = DEFAULT_MAX_PER_HOUR
    max_chars: int = DEFAULT_MAX_CHARS

    def wants(self, agent_id: str, channel: str, text: str) -> bool:
        """Решает, доставлять ли сообщение в сессию."""
        if self.mode == NOTIFY_OFF:
            return False
        lowered = text.lower()
        if f"@{agent_id.lower()}" in lowered:
            return True  # позвали по имени — откликаемся в любом канале
        if self.channels and channel not in self.channels:
            return False
        if self.mode == NOTIFY_ALL:
            return True
        return any(tag in lowered for tag in BROADCAST_TAGS)


def _positive_int(value: str | None, fallback: int) -> int:
    """Читает целое из настройки, возвращая запасное значение при мусоре."""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def build_policy(
    mode: str | None,
    channels: str | None,
    max_per_hour: str | None = None,
    max_chars: str | None = None,
) -> NotifyPolicy:
    """Собирает правило уведомлений из настроек.

    По умолчанию будим только по упоминанию: иначе каждое сообщение любого
    агента прерывало бы каждую сессию и тратило контекст.
    """
    chosen = (mode or NOTIFY_MENTIONS).strip().lower()
    if chosen not in (NOTIFY_MENTIONS, NOTIFY_ALL, NOTIFY_OFF):
        _log(f"неизвестный режим уведомлений «{chosen}», беру {NOTIFY_MENTIONS}")
        chosen = NOTIFY_MENTIONS
    names = frozenset(c.strip() for c in (channels or "").split(",") if c.strip())
    return NotifyPolicy(
        mode=chosen,
        channels=names,
        max_per_hour=_positive_int(max_per_hour, DEFAULT_MAX_PER_HOUR),
        max_chars=_positive_int(max_chars, DEFAULT_MAX_CHARS),
    )


class ChannelPusher:
    """Фоновый опрос хаба с доставкой входящих в сессию."""

    def __init__(
        self,
        agent_id: str,
        poll: Callable[[], list[dict[str, Any]]],
        writer: NotificationWriter,
        policy: NotifyPolicy | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._poll = poll
        self._writer = writer
        self._policy = policy or NotifyPolicy()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wakes: list[float] = []
        self._muted = False

    def start(self) -> None:
        """Запускает фоновый поток опроса."""
        self._thread = threading.Thread(target=self._run, name="teamhub-poll", daemon=True)
        self._thread.start()
        _log(f"слежу за хабом для {self._agent_id}")

    def stop(self) -> None:
        """Останавливает опрос."""
        self._stop.set()

    def _within_budget(self) -> bool:
        """Проверяет, не исчерпан ли лимит пробуждений за последний час.

        Защита от того, чтобы болтовня агентов бесконтрольно жгла контекст.
        """
        limit = self._policy.max_per_hour
        if limit <= 0:
            return True
        now = time.monotonic()
        self._wakes = [t for t in self._wakes if now - t < HOUR_S]
        if len(self._wakes) >= limit:
            if not self._muted:
                _log(f"лимит {limit} пробуждений в час исчерпан, молчу до конца часа")
                self._muted = True
            return False
        self._muted = False
        self._wakes.append(now)
        return True

    def _deliver(self, event: dict[str, Any]) -> None:
        """Превращает событие хаба в уведомление сессии."""
        parts = _extract(event)
        if parts is None:
            return
        author, channel, text = parts
        if author == self._agent_id:  # своё же сообщение возвращается эхом
            return
        if not self._policy.wants(self._agent_id, channel, text):
            return
        if not self._within_budget():
            return
        limit = self._policy.max_chars
        if 0 < limit < len(text):
            text = text[:limit] + f"… (обрезано, целиком — hub_read_messages канала {channel})"
        self._writer.send(
            CHANNEL_NOTIFICATION,
            {"content": text, "meta": {"user": author, "channel": channel, "hub_agent": self._agent_id}},
        )

    def _run(self) -> None:
        """Опрашивает хаб, пока сессия жива."""
        delay = POLL_INTERVAL_S
        while not self._stop.wait(delay):
            try:
                events = self._poll()
                delay = POLL_INTERVAL_S
            except RuntimeError as exc:
                _log(f"опрос не удался, пробую реже: {exc}")
                delay = POLL_BACKOFF_S
                continue
            for event in events:
                self._deliver(event)
