"""Проверки работы с вики.

Запись сначала выясняет, есть ли страница. Дважды это едва не обернулось
потерей чужого текста: сперва сетевой сбой принимался за «страницы нет»,
потом — любой отказ хаба. Оба случая закреплены здесь.
"""

from __future__ import annotations

import pytest
from conftest import FakeTransport
from hub_client import HubClient, HubConfig, HubRejected
from transport import HubUnreachable
from wiki_client import WikiClient, format_page, format_pages


class ОтказавшийТранспорт(FakeTransport):
    """Транспорт, который после регистрации всегда падает заданной ошибкой."""

    def __init__(self, ошибка: Exception) -> None:
        super().__init__([{"success": True, "secret": "s"}])
        self._ошибка = ошибка

    def post(self, path: str, body):
        if path == "/api/register":
            return super().post(path, body)
        raise self._ошибка


def _wiki(config: HubConfig, transport: FakeTransport) -> WikiClient:
    return WikiClient(HubClient(config, transport))


def test_отсутствующая_страница_даёт_пустой_ответ(config: HubConfig) -> None:
    transport = FakeTransport(
        [{"success": True, "secret": "s"}, {"success": False, "message": "Page not found: тема"}]
    )
    assert _wiki(config, transport).read_page("тема") == {}


def test_обрыв_связи_не_выдаётся_за_отсутствие_страницы(config: HubConfig) -> None:
    wiki = _wiki(config, ОтказавшийТранспорт(HubUnreachable("сеть упала")))
    with pytest.raises(HubUnreachable):
        wiki.read_page("тема")


def test_посторонний_отказ_не_выдаётся_за_отсутствие_страницы(config: HubConfig) -> None:
    transport = FakeTransport(
        [{"success": True, "secret": "s"}, {"success": False, "message": "доступ запрещён"}]
    )
    with pytest.raises(HubRejected, match="доступ запрещён"):
        _wiki(config, transport).read_page("тема")


def test_несуществующая_страница_создаётся(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": False, "message": "Page not found: тема"},
            {"success": True, "data": {}},
        ]
    )
    assert "создана" in _wiki(config, transport).write_page("тема", "текст")
    события = [c[2]["event_name"] for c in transport.calls if c[1] == "/api/send_event"]
    assert события[-1] == "wiki.page.create"


def test_существующая_страница_правится_а_не_создаётся_заново(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": True, "data": {"wiki_content": "прежний текст"}},
            {"success": True, "data": {}},
        ]
    )
    assert "обновлена" in _wiki(config, transport).write_page("тема", "новый текст")
    события = [c[2]["event_name"] for c in transport.calls if c[1] == "/api/send_event"]
    assert события[-1] == "wiki.page.edit", "правка не должна превращаться в создание"


def test_список_пустой_вики_читается_понятно() -> None:
    assert "нет страниц" in format_pages([])


def test_страница_показывается_с_автором() -> None:
    вывод = format_page({"wiki_content": "тело", "title": "Заголовок", "created_by": "bob"}, "путь")
    assert "Заголовок" in вывод and "bob" in вывод and "тело" in вывод


def test_отсутствующая_страница_описывается_словами() -> None:
    assert "нет" in format_page({}, "путь/тема")
