"""Проверки клиента хаба: регистрация, живучесть, разбор настроек.

Главный сценарий здесь — хаб перезапустился и забыл агента. Раньше сессия
глохла тихо и навсегда, поэтому он проверяется с обеих сторон: и отправка,
и опрос входящих.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeTransport
from hub_client import HubClient, HubConfig, HubRejected, build_auth_header, resolve_agent_id


def test_регистрация_запоминает_секрет(client: HubClient, transport: FakeTransport) -> None:
    client.connect()
    client.send_message("general", "привет")
    отправка = [c for c in transport.calls if c[1] == "/api/send_event"][0]
    assert отправка[2]["secret"] == "s3cret"
    assert отправка[2]["source_id"] == "tester"


def test_отказ_регистрации_поднимает_ошибку(config: HubConfig) -> None:
    transport = FakeTransport([{"success": False, "message": "занято"}])
    with pytest.raises(HubRejected, match="занято"):
        HubClient(config, transport).connect()


def test_после_забытой_регистрации_клиент_переподключается(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "первый"},  # первая регистрация
            {"success": False, "message": "agent not registered"},  # хаб забыл
            {"success": True, "secret": "второй"},  # повторная регистрация
            {"success": True, "data": {"ok": True}},  # повторная отправка
        ]
    )
    client = HubClient(config, transport)
    client.send_message("general", "привет")
    регистрации = [c for c in transport.calls if c[1] == "/api/register"]
    assert len(регистрации) == 2, "ожидали повторную регистрацию"


def test_обычный_отказ_не_вызывает_переподключения(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": False, "message": "Page not found: x"},
        ]
    )
    client = HubClient(config, transport)
    with pytest.raises(HubRejected, match="Page not found"):
        client.event("wiki.page.get", {})
    assert len([c for c in transport.calls if c[1] == "/api/register"]) == 1


def test_неуспешный_опрос_не_выдаётся_за_пустой(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": False, "message": "unknown agent"},
        ]
    )
    client = HubClient(config, transport)
    with pytest.raises(HubRejected, match="unknown agent"):
        client.poll_events()


def test_опрос_без_поля_успеха_считается_удачным(config: HubConfig) -> None:
    transport = FakeTransport([{"success": True, "secret": "s"}, {"messages": [{"a": 1}]}])
    assert HubClient(config, transport).poll_events() == [{"a": 1}]


def test_закрытый_клиент_не_переподключается(client: HubClient, transport: FakeTransport) -> None:
    client.connect()
    client.close()
    assert transport.closed is True
    with pytest.raises(HubRejected, match="закрыт"):
        client.send_message("general", "привет")


def test_подключение_происходит_лениво(client: HubClient, transport: FakeTransport) -> None:
    assert transport.calls == [], "создание клиента не должно ходить в сеть"
    client.list_channels()
    assert transport.calls[0][1] == "/api/register"


def test_заголовок_авторизации_собирается_и_отсутствует_без_пароля() -> None:
    assert build_auth_header("team:pass") == "Basic dGVhbTpwYXNz"
    assert build_auth_header(None) is None
    assert build_auth_header("") is None


class TestИмяАгента:
    """Как выбирается имя, под которым сессия видна в чате."""

    def test_явное_имя_побеждает(self) -> None:
        assert resolve_agent_id({"agent_id": "ivan", "name_prefix": "x"}) == "ivan"

    def test_префикс_склеивается_с_каталогом(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = tmp_path / "superres"
        project.mkdir()
        monkeypatch.chdir(project)
        assert resolve_agent_id({"name_prefix": "ivan"}) == "ivan-superres"

    def test_без_настроек_берётся_каталог(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = tmp_path / "motion"
        project.mkdir()
        monkeypatch.chdir(project)
        assert resolve_agent_id({}) == "motion"

    def test_неразвёрнутая_подстановка_игнорируется(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "motion"
        project.mkdir()
        monkeypatch.chdir(project)
        monkeypatch.setenv("TEAMHUB_AGENT_ID", "${TEAMHUB_AGENT_ID:-}")
        assert resolve_agent_id({}) == "motion"
