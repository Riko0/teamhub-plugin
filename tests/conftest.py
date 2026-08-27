"""Общие приспособления для тестов плагина.

Модули плагина лежат рядом друг с другом и импортируются по коротким именам —
так их запускает Claude Code. Поэтому каталог плагина добавляем в путь поиска.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "teamhub"
sys.path.insert(0, str(PLUGIN_DIR))

from hub_client import HubClient, HubConfig  # noqa: E402


class FakeTransport:
    """Транспорт-заглушка: отвечает заранее заданным и помнит запросы."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.closed = False
        self.opened = False

    def _next(self, default: dict[str, Any]) -> dict[str, Any]:
        return self.responses.pop(0) if self.responses else default

    def open(self) -> None:
        self.opened = True

    def get(self, path: str) -> dict[str, Any]:
        self.calls.append(("GET", path, None))
        return self._next({"success": True, "messages": []})

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        if path == "/api/register":
            return self._next({"success": True, "secret": "s3cret"})
        return self._next({"success": True, "data": {}})

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def config() -> HubConfig:
    """Конфигурация без туннеля — транспорт всё равно подменяется."""
    return HubConfig(
        agent_id="tester",
        auth_header=None,
        url="http://hub.invalid",
        ssh_destination=None,
        ssh_port=8700,
        ssh_key=None,
    )


@pytest.fixture
def transport() -> FakeTransport:
    """Свежая заглушка транспорта."""
    return FakeTransport()


@pytest.fixture
def client(config: HubConfig, transport: FakeTransport) -> HubClient:
    """Клиент хаба поверх заглушки."""
    return HubClient(config, transport)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный каталог настроек, чтобы не трогать настоящий."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for name in list(sys.modules):
        if name in {"config_store"}:
            del sys.modules[name]
    return tmp_path / "teamhub"
