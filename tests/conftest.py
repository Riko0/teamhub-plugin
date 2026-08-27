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


class FakeHub:
    """Поддельный хаб с настоящим поведением вики.

    Очередь заранее заготовленных ответов оказалась хрупкой: стоило изменить
    порядок вызовов внутри клиента, и тесты падали, хотя поведение оставалось
    верным. Здесь вместо очереди простая модель хранилища.
    """

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages: dict[str, dict[str, Any]] = {
            path: {"page_path": path, "wiki_content": body, "created_by": "кто-то"}
            for path, body in (pages or {}).items()
        }
        self.events: list[str] = []
        self.closed = False

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def get(self, path: str) -> dict[str, Any]:
        return {"success": True, "messages": []}

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/register":
            return {"success": True, "secret": "s3cret"}
        name = body.get("event_name", "")
        payload = body.get("payload") or {}
        self.events.append(name)
        page_path = payload.get("page_path")
        if name == "wiki.pages.list":
            return {"success": True, "data": {"pages": list(self.pages.values())}}
        if name == "wiki.page.get":
            page = self.pages.get(page_path)
            if page is None:
                return {"success": False, "message": f"Page not found: {page_path}"}
            return {"success": True, "data": dict(page)}
        if name == "wiki.page.create":
            self.pages[page_path] = {
                "page_path": page_path,
                "wiki_content": payload.get("wiki_content", ""),
                "title": payload.get("title"),
                "created_by": body.get("source_id"),
            }
            return {"success": True, "data": {}}
        if name == "wiki.page.edit":
            if page_path not in self.pages:
                return {"success": False, "message": f"Page not found: {page_path}"}
            self.pages[page_path]["wiki_content"] = payload.get("wiki_content", "")
            return {"success": True, "data": {}}
        if name == "wiki.page.delete":
            if self.pages.pop(page_path, None) is None:
                return {"success": False, "message": f"Page not found: {page_path}"}
            return {"success": True, "data": {}}
        return {"success": True, "data": {}}


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
