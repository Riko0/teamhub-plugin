"""Проверки клиента хаба: регистрация, живучесть, разбор настроек.

Главный сценарий здесь — хаб перезапустился и забыл агента. Раньше сессия
глохла тихо и навсегда, поэтому он проверяется с обеих сторон: и отправка,
и опрос входящих.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeTransport
from hub_client import (
    HubClient,
    HubConfig,
    HubRejected,
    build_auth_header,
    resolve_agent_id,
)


def test_регистрация_запоминает_секрет(
    client: HubClient, transport: FakeTransport
) -> None:
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
    transport = FakeTransport(
        [{"success": True, "secret": "s"}, {"messages": [{"a": 1}]}]
    )
    assert HubClient(config, transport).poll_events() == [{"a": 1}]


def test_закрытый_клиент_не_переподключается(
    client: HubClient, transport: FakeTransport
) -> None:
    client.connect()
    client.close()
    assert transport.closed is True
    with pytest.raises(HubRejected, match="закрыт"):
        client.send_message("general", "привет")


def test_подключение_происходит_лениво(
    client: HubClient, transport: FakeTransport
) -> None:
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

    def test_префикс_склеивается_с_каталогом(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "superres"
        project.mkdir()
        monkeypatch.chdir(project)
        assert resolve_agent_id({"name_prefix": "ivan"}) == "ivan-superres"

    def test_без_настроек_берётся_каталог(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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


def test_протухший_секрет_считается_потерей_регистрации(config: HubConfig) -> None:
    """Хаб после перезапуска отвечает именно так — проверено на живом сервере."""
    transport = FakeTransport(
        [
            {"success": True, "secret": "старый"},
            {
                "success": False,
                "error_message": "Authentication failed: Invalid or missing secret",
            },
            {"success": True, "secret": "новый"},
            {"success": True, "data": {}},
        ]
    )
    client = HubClient(config, transport)
    client.send_message("general", "привет")
    assert len([c for c in transport.calls if c[1] == "/api/register"]) == 2


def test_любой_отказ_опроса_ведёт_к_переподключению(config: HubConfig) -> None:
    transport = FakeTransport(
        [
            {"success": True, "secret": "s"},
            {"success": False, "error_message": "что угодно"},
            {"success": True, "secret": "новый"},
            {"success": True, "messages": []},
        ]
    )
    client = HubClient(config, transport)
    with pytest.raises(HubRejected):
        client.poll_events()
    assert client.poll_events() == [], "следующий круг должен переподключиться"
    assert len([c for c in transport.calls if c[1] == "/api/register"]) == 2


class TestЗанятоеИмя:
    """Фоновые задачи Claude Code поднимают второй экземпляр в том же каталоге."""

    @staticmethod
    def _занято(config: HubConfig) -> HubClient:
        transport = FakeTransport(
            [
                {
                    "success": False,
                    "message": "Agent goalekseenko-motion already registered with network",
                }
            ]
        )
        return HubClient(config, transport)

    def test_вторичный_экземпляр_не_оспаривает_имя(self, config: HubConfig) -> None:
        from hub_client import HubNameTaken

        client = self._занято(config)
        with pytest.raises(HubNameTaken, match="занято"):
            client.send_message("general", "привет")

    def test_повторных_попыток_не_делает(self, config: HubConfig) -> None:
        from hub_client import HubNameTaken

        transport = FakeTransport(
            [{"success": False, "message": "Agent x already registered with network"}]
        )
        client = HubClient(config, transport)
        for _ in range(3):
            with pytest.raises(HubNameTaken):
                client.list_channels()
        обращений = [c for c in transport.calls if c[1] == "/api/register"]
        assert len(обращений) == 1, "после отказа не должно быть новых попыток"

    def test_занятое_имя_не_путается_с_потерей_регистрации(
        self, config: HubConfig
    ) -> None:
        """«already registered» содержит слово register — легко перепутать."""
        from hub_client import HubNameTaken

        client = self._занято(config)
        with pytest.raises(HubNameTaken):
            client.poll_events()


def test_частые_перерегистрации_считаются_дракой_за_имя(config: HubConfig) -> None:
    """Хаб пускает одноимённого и обесценивает чужой секрет — так возникают качели."""
    from hub_client import HubNameTaken

    ответы = []
    for _ in range(6):
        ответы.append({"success": True, "secret": "s"})
        ответы.append(
            {
                "success": False,
                "error_message": "Authentication failed: Invalid or missing secret",
            }
        )
    client = HubClient(config, FakeTransport(ответы))
    with pytest.raises(HubNameTaken, match="уступаю"):
        for _ in range(6):
            try:
                client.poll_events()
            except HubNameTaken:
                raise
            except HubRejected:
                continue  # обычный отказ — следующий круг переподключится


class TestФоновыеЗадачи:
    """Claude Code поднимает под фоновые задачи процесс в том же каталоге.

    Имя агента выводится из каталога, поэтому без распознавания фоновая
    задача отбирала бы чат у сессии, в которой человек работает.
    """

    def test_фон_узнаётся_по_дереву_процессов(self) -> None:
        from hub_client import is_background_instance

        цепочка = [
            "/home/x/.local/share/claude/versions/2.1.247 --fork-session --resume",
            "claude bg-pty-host --bg-pty-host /tmp/cc-daemon/pty.sock",
            "claude daemon run --origin transient",
        ]
        assert is_background_instance(цепочка) is True

    def test_обычная_сессия_фоном_не_считается(self) -> None:
        from hub_client import is_background_instance

        цепочка = [
            "claude --dangerously-load-development-channels server:telegram-motion --continue",
            "bash -i",
            "tmux new-session -d -s claude",
        ]
        assert is_background_instance(цепочка) is False

    def test_без_доступа_к_дереву_считаем_себя_обычной(self) -> None:
        from hub_client import is_background_instance

        assert is_background_instance([]) is False

    def test_упоминание_слов_в_чужой_команде_не_считается(self) -> None:
        """На этом я попался: строка про фон в аргументах — ещё не фон."""
        from hub_client import is_background_instance

        цепочка = [
            "/bin/bash -c grep bg-pty-host /var/log/syslog",
            "python3 -c print('daemon run')",
            "claude --continue",
        ]
        assert is_background_instance(цепочка) is False

    def test_daemon_run_узнаётся_только_как_подкоманда(self) -> None:
        from hub_client import is_background_instance

        assert (
            is_background_instance(
                ["/home/x/.local/bin/claude daemon run --origin transient"]
            )
            is True
        )
        assert is_background_instance(["/usr/bin/other daemon run"]) is False


def test_уступка_имени_не_вечна(
    config: HubConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Соперник мог закрыться — через время стоит попробовать снова."""
    import hub_client

    transport = FakeTransport(
        [{"success": False, "message": "Agent x already registered"}]
    )
    client = HubClient(config, transport)
    from hub_client import HubNameTaken

    with pytest.raises(HubNameTaken):
        client.list_channels()
    with pytest.raises(HubNameTaken, match="занято"):
        client.list_channels()  # сразу повторять не пытаемся

    время = [hub_client.time.monotonic() + hub_client.RECONNECT_WINDOW_S + 1]
    monkeypatch.setattr(hub_client.time, "monotonic", lambda: время[0])
    transport.responses = [
        {"success": True, "secret": "s"},
        {"success": True, "data": {}},
    ]
    client.list_channels()  # выждали — пробуем снова
    assert len([c for c in transport.calls if c[1] == "/api/register"]) == 2


class ПадающийНаОтключении(FakeTransport):
    """Заглушка, у которой снятие регистрации срывается."""

    def post(self, path: str, body: dict) -> dict:
        if path == "/api/unregister":
            raise RuntimeError("связи нет")
        return super().post(path, body)


def test_закрытие_снимает_регистрацию(
    client: HubClient, transport: FakeTransport
) -> None:
    """Хаб держит регистрацию вечно, поэтому уходя за собой нужно убрать.

    Иначе имя остаётся в списке участников навсегда — так после каждой
    проверки связи при настройке на хабе оседал лишний агент.
    """
    client.connect()
    client.close()
    снятие = [c for c in transport.calls if c[1] == "/api/unregister"]
    assert len(снятие) == 1
    assert снятие[0][2] == {"agent_id": "tester", "secret": "s3cret"}


def test_закрытие_без_подключения_не_ходит_на_хаб(config: HubConfig) -> None:
    """Если подключения не было, снимать нечего — лишний запрос ни к чему."""
    transport = FakeTransport()
    HubClient(config, transport).close()
    assert not [c for c in transport.calls if c[1] == "/api/unregister"]


def test_повторное_закрытие_не_снимает_дважды(
    client: HubClient, transport: FakeTransport
) -> None:
    """Второй close не должен слать ещё один запрос."""
    client.connect()
    client.close()
    client.close()
    assert len([c for c in transport.calls if c[1] == "/api/unregister"]) == 1


def test_сорвавшееся_снятие_не_мешает_закрыться(config: HubConfig) -> None:
    """На выходе важнее закрыть транспорт, чем доложить об ошибке.

    Сессия завершается, жаловаться уже некому, а незакрытый SSH-туннель
    остался бы висеть процессом.
    """
    transport = ПадающийНаОтключении([{"success": True, "secret": "s"}])
    клиент = HubClient(config, transport)
    клиент.connect()
    клиент.close()
    assert transport.closed
