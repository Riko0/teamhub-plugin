"""Проверки создания и удаления каналов.

Что проверяется. Клиент должен слать хабу правильно названные события с
правильной нагрузкой, а слой инструментов — превращать ответ хаба в понятную
человеку строку. Отдельно проверяется, что отказ хаба (имя занято настройками
сервера, канала нет) доходит до вызывающего исключением, а не теряется.

Как. Живой сервер не нужен: под клиентом стоит транспорт-заглушка из conftest,
он отвечает заранее заданным и запоминает запросы. Это модульные проверки —
ни сеть, ни туннель, ни сам хаб не участвуют.

Крайние случаи. Канал, который уже существует (хаб отвечает created=false);
отказ хаба; отсутствующее в ответе поле messages_removed — вывод не должен
на нём ломаться.
"""

from __future__ import annotations

import pytest
from conftest import FakeTransport
from hub_client import HubClient, HubConfig, HubRejected
from teamhub_mcp import BridgeState, _call_tool


def _клиент(config: HubConfig, ответы: list[dict]) -> HubClient:
    """Собирает клиента поверх заглушки с заданными ответами."""
    return HubClient(config, FakeTransport(ответы))


def test_создание_канала_шлёт_нужное_событие(config: HubConfig) -> None:
    """Клиент отправляет thread.channel.create с именем и описанием."""
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": True, "data": {"channel": "новый", "created": True}},
        ]
    )
    данные = HubClient(config, transport).create_channel("новый", "про новый проект")
    тело = [c for c in transport.calls if c[1] == "/api/send_event"][0][2]
    assert тело["event_name"] == "thread.channel.create"
    assert тело["payload"] == {"channel": "новый", "description": "про новый проект"}
    assert данные["created"] is True


def test_удаление_канала_шлёт_нужное_событие(config: HubConfig) -> None:
    """Клиент отправляет thread.channel.delete с именем канала."""
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": True, "data": {"messages_removed": 4}},
        ]
    )
    данные = HubClient(config, transport).delete_channel("лишний")
    тело = [c for c in transport.calls if c[1] == "/api/send_event"][0][2]
    assert тело["event_name"] == "thread.channel.delete"
    assert тело["payload"] == {"channel": "лишний"}
    assert данные["messages_removed"] == 4


def test_отказ_хаба_доходит_исключением(config: HubConfig) -> None:
    """Канал из настроек сервера удалить нельзя — отказ не должен теряться."""
    клиент = _клиент(
        config,
        [
            {"success": True, "secret": "s"},
            {
                "success": False,
                "error_message": "Channel comes from server configuration: general",
            },
        ],
    )
    with pytest.raises(HubRejected, match="server configuration"):
        клиент.delete_channel("general")


def test_ответ_про_созданный_канал(config: HubConfig) -> None:
    """Инструмент сообщает о создании человеческим языком."""
    клиент = _клиент(
        config,
        [
            {"success": True, "secret": "s"},
            {"success": True, "data": {"channel": "новый", "created": True}},
        ],
    )
    состояние = BridgeState(клиент, None)
    assert (
        _call_tool(состояние, "hub_create_channel", {"channel": "новый"})
        == "канал новый создан"
    )


def test_ответ_про_уже_существующий_канал(config: HubConfig) -> None:
    """Повторное создание не выдаётся за создание."""
    клиент = _клиент(
        config,
        [
            {"success": True, "secret": "s"},
            {"success": True, "data": {"channel": "general", "created": False}},
        ],
    )
    состояние = BridgeState(клиент, None)
    assert (
        _call_tool(состояние, "hub_create_channel", {"channel": "general"})
        == "канал general уже был"
    )


@pytest.mark.parametrize(("ответ", "ожидание"), [({"messages_removed": 3}, 3), ({}, 0)])
def test_ответ_про_удаление(config: HubConfig, ответ: dict, ожидание: int) -> None:
    """Число убранных сообщений показывается, а его отсутствие не ломает вывод."""
    клиент = _клиент(
        config, [{"success": True, "secret": "s"}, {"success": True, "data": ответ}]
    )
    состояние = BridgeState(клиент, None)
    строка = _call_tool(состояние, "hub_delete_channel", {"channel": "лишний"})
    assert строка == f"канал лишний удалён, сообщений убрано: {ожидание}"


def test_инструменты_объявлены() -> None:
    """Оба инструмента попадают в список, который видит модель."""
    from tools import all_tools

    имена = {t["name"] for t in all_tools("tester")}
    assert {"hub_create_channel", "hub_delete_channel"} <= имена
