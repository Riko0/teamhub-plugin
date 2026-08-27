"""Проверки работы с вики.

Запись сначала выясняет, есть ли страница. Дважды это едва не обернулось
потерей чужого текста: сперва сетевой сбой принимался за «страницы нет»,
потом — любой отказ хаба. Оба случая закреплены здесь.
"""

from __future__ import annotations

import pytest
from conftest import FakeHub, FakeTransport
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


class TestВеткиВики:
    """Вложенности у хаба нет, ветка отбирается на нашей стороне."""

    @staticmethod
    def _со_страницами(config: HubConfig, пути: list[str]) -> WikiClient:
        transport = FakeTransport(
            [
                {"success": True, "secret": "s"},
                {"success": True, "data": {"pages": [{"page_path": p} for p in пути]}},
            ]
        )
        return _wiki(config, transport)

    def test_отбор_по_ветке_оставляет_своё(self, config: HubConfig) -> None:
        wiki = self._со_страницами(
            config, ["motion/vae/обучение", "motion/сплиты", "imagesr/метрики", "motion-другое/х"]
        )
        пути = [p["page_path"] for p in wiki.list_pages("motion")]
        assert пути == ["motion/vae/обучение", "motion/сплиты"]

    def test_соседняя_ветка_с_похожим_началом_не_цепляется(self, config: HubConfig) -> None:
        wiki = self._со_страницами(config, ["motion/a", "motionx/b"])
        assert [p["page_path"] for p in wiki.list_pages("motion")] == ["motion/a"]

    def test_сама_ветка_тоже_попадает(self, config: HubConfig) -> None:
        wiki = self._со_страницами(config, ["motion", "motion/vae"])
        assert len(wiki.list_pages("motion")) == 2

    def test_без_отбора_возвращается_всё_по_порядку(self, config: HubConfig) -> None:
        wiki = self._со_страницами(config, ["б/я", "а/в", "а/б"])
        assert [p["page_path"] for p in wiki.list_pages()] == ["а/б", "а/в", "б/я"]


def test_пустая_ветка_не_выдаётся_за_пустую_вики() -> None:
    assert "ветке motion/vae" in format_pages([], "motion/vae")
    assert "вики пока нет" in format_pages([])


class TestДописывание:
    """Хаб умеет только заменять страницу целиком — склейка на нашей стороне."""

    def test_к_существующей_добавляется_в_конец(self, config: HubConfig) -> None:
        transport = FakeTransport(
            [
                {"success": True, "secret": "s"},
                {"success": True, "data": {"wiki_content": "# Тема\n\nбыло"}},
                {"success": True, "data": {}},
            ]
        )
        assert "дополнена" in _wiki(config, transport).append_to_page("тема", "стало")
        правка = [c for c in transport.calls if c[1] == "/api/send_event"][-1][2]
        assert правка["event_name"] == "wiki.page.edit"
        текст = правка["payload"]["wiki_content"]
        assert текст.startswith("# Тема") and текст.rstrip().endswith("стало")

    def test_несуществующая_страница_создаётся(self, config: HubConfig) -> None:
        transport = FakeTransport(
            [
                {"success": True, "secret": "s"},
                {"success": False, "message": "Page not found: тема"},
                {"success": True, "data": {}},
            ]
        )
        assert "создана" in _wiki(config, transport).append_to_page("тема", "первый раздел")

    def test_обрыв_связи_не_превращается_в_создание(self, config: HubConfig) -> None:
        wiki = _wiki(config, ОтказавшийТранспорт(HubUnreachable("сеть")))
        with pytest.raises(HubUnreachable):
            wiki.append_to_page("тема", "текст")


class TestПравкаКуска:
    """Замена фрагмента вместо пересылки всей страницы."""

    @staticmethod
    def _со_страницей(config: HubConfig, текст: str) -> tuple[WikiClient, FakeTransport]:
        transport = FakeTransport(
            [
                {"success": True, "secret": "s"},
                {"success": True, "data": {"wiki_content": текст}},
                {"success": True, "data": {}},
            ]
        )
        return _wiki(config, transport), transport

    def test_единственное_вхождение_заменяется(self, config: HubConfig) -> None:
        wiki, transport = self._со_страницей(config, "# Тема\n\nстарое значение\n\nхвост")
        assert "поправлена" in wiki.edit_fragment("тема", "старое значение", "новое значение")
        правка = [c for c in transport.calls if c[1] == "/api/send_event"][-1][2]
        assert "новое значение" in правка["payload"]["wiki_content"]
        assert "хвост" in правка["payload"]["wiki_content"], "остальное не должно потеряться"

    def test_неоднозначный_кусок_отвергается(self, config: HubConfig) -> None:
        wiki, transport = self._со_страницей(config, "повтор и ещё раз повтор")
        with pytest.raises(HubRejected, match="встречается 2"):
            wiki.edit_fragment("тема", "повтор", "замена")
        отправки = [c for c in transport.calls if c[1] == "/api/send_event"]
        assert len(отправки) == 1, "при отказе править страницу нельзя"

    def test_отсутствующий_кусок_отвергается(self, config: HubConfig) -> None:
        wiki, _ = self._со_страницей(config, "какой-то текст")
        with pytest.raises(HubRejected, match="нет такого куска"):
            wiki.edit_fragment("тема", "чего тут нет", "замена")


class TestВеткиЦеликом:
    """Путь со слэшами и есть иерархия: операции должны уважать ветку."""

    @staticmethod
    def _wiki_with(config: HubConfig, pages: dict[str, str]) -> tuple[WikiClient, FakeHub]:
        hub = FakeHub(pages)
        return WikiClient(HubClient(config, hub)), hub

    def test_перенос_тащит_вложенные_страницы(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(
            config,
            {"motion/vae": "корень", "motion/vae/обзор": "раз", "motion/vae/кодеки": "два"},
        )
        итог = wiki.rename_page("motion/vae", "motion/автокодировщик")
        assert "3" in итог
        assert set(hub.pages) == {
            "motion/автокодировщик",
            "motion/автокодировщик/обзор",
            "motion/автокодировщик/кодеки",
        }
        assert hub.pages["motion/автокодировщик/обзор"]["wiki_content"] == "раз"

    def test_ветка_без_собственной_страницы_переносится(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(config, {"motion/vae/обзор": "раз"})
        wiki.rename_page("motion/vae", "motion/кодек")
        assert set(hub.pages) == {"motion/кодек/обзор"}

    def test_занятый_путь_не_затирается(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(config, {"старая": "а", "занятая": "б"})
        with pytest.raises(HubRejected, match="уже существует"):
            wiki.rename_page("старая", "занятая")
        assert hub.pages["занятая"]["wiki_content"] == "б", "чужое содержимое цело"
        assert "старая" in hub.pages, "исходная страница на месте"

    def test_ветка_не_сносится_без_явной_просьбы(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(config, {"motion": "корень", "motion/vae": "внутри"})
        with pytest.raises(HubRejected, match="ещё 1 страниц"):
            wiki.delete_page("motion")
        assert len(hub.pages) == 2, "ничего не должно пропасть"

    def test_ветка_сносится_когда_попросили(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(config, {"motion": "корень", "motion/vae": "внутри", "imagesr": "чужое"})
        итог = wiki.delete_page("motion", include_children=True)
        assert "вместе с 1" in итог
        assert set(hub.pages) == {"imagesr"}, "соседняя ветка не тронута"

    def test_одиночная_страница_сносится_без_оговорок(self, config: HubConfig) -> None:
        wiki, hub = self._wiki_with(config, {"тема": "текст"})
        assert "удалена" in wiki.delete_page("тема")
        assert hub.pages == {}


class TestДерево:
    """Отображение должно показывать структуру, а не плоский список."""

    def test_вложенность_видна_отступами(self) -> None:
        вывод = format_pages(
            [
                {"page_path": "motion/vae/обзор", "created_by": "a"},
                {"page_path": "motion/заливка", "created_by": "b"},
                {"page_path": "imagesr/обзор", "created_by": "c"},
            ]
        )
        строки = вывод.splitlines()
        assert строки[0] == "imagesr/"
        assert строки[1].startswith("  обзор")
        assert "    обзор — a" in вывод, "третий уровень отбит вдвое"

    def test_промежуточная_ветка_показана_даже_без_своей_страницы(self) -> None:
        вывод = format_pages([{"page_path": "a/b/c", "created_by": "x"}])
        assert вывод.splitlines()[1] == "  b/", "вложенная ветка должна быть отбита"

    def test_вложенные_ветки_отбиваются_по_глубине(self) -> None:
        """Отступ у самих веток, а не только у страниц: иначе дерево плоское."""
        вывод = format_pages([{"page_path": "motion/vae/слои/первый", "created_by": "x"}])
        assert вывод.splitlines() == [
            "motion/",
            "  vae/",
            "    слои/",
            "      первый — x",
        ]
