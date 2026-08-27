"""Проверки доставки входящих: кого будить, как часто и что отправлять.

Разбор упоминаний ломался дважды — сначала совпадением по подстроке, потом
слишком строгими границами, — поэтому случаев здесь много и они конкретные.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from channel_push import ChannelPusher, NotificationWriter, _safe_name, build_policy


class RecordingWriter(NotificationWriter):
    """Писатель, который вместо stdout копит кадры."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []

    def write(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)


def _event(text: str, author: str = "кто-то", channel: str = "general") -> dict[str, Any]:
    """Собирает событие хаба так, как его отдаёт опрос."""
    return {"payload": {"source_id": author, "channel": channel, "content": {"text": text}}}


@pytest.mark.parametrize(
    ("текст", "разбудит"),
    [
        ("@grisha глянешь?", True),
        ("спроси у @grisha.", True),  # точка в конце предложения
        ("@grisha, срочно", True),
        ("@grisha!", True),
        ("(@grisha)", True),
        ("@GRISHA большими", True),
        ("@grisha-superres упал", False),  # другое имя целиком
        ("@grishanya привет", False),
        ("обсуждаем grisha", False),  # без собаки не обращение
        ("пиши на user@grisha", False),  # похоже на почту
    ],
)
def test_упоминание_срабатывает_только_на_целое_имя(текст: str, разбудит: bool) -> None:
    policy = build_policy("mentions", None)
    assert policy.wants("grisha", "general", текст) is разбудит


@pytest.mark.parametrize("тег", ["@all", "@here", "@все"])
def test_обращение_ко_всем_будит_кого_угодно(тег: str) -> None:
    policy = build_policy("mentions", None)
    assert policy.wants("кто-угодно", "general", f"{тег} собрание") is True
    assert policy.wants("кто-угодно", "general", f"{тег}.") is True


def test_ложное_срабатывание_на_похожий_тег_не_проходит() -> None:
    policy = build_policy("mentions", None)
    assert policy.wants("bob", "general", "поехали в @hereford") is False


def test_режим_all_будит_на_всё_в_своих_каналах() -> None:
    policy = build_policy("all", "imagesr")
    assert policy.wants("bob", "imagesr", "просто болтовня") is True
    assert policy.wants("bob", "motion", "просто болтовня") is False


def test_зов_по_имени_проходит_мимо_ограничения_каналов() -> None:
    policy = build_policy("all", "imagesr")
    assert policy.wants("bob", "videosr", "@bob глянешь?") is True


def test_режим_off_молчит_всегда() -> None:
    policy = build_policy("off", None)
    assert policy.wants("bob", "general", "@bob срочно") is False


def test_свои_сообщения_не_возвращаются_эхом() -> None:
    writer = RecordingWriter()
    pusher = ChannelPusher("bob", lambda: [], writer, build_policy("all", None))
    pusher._deliver(_event("@bob сам себе", author="bob"))
    assert writer.frames == []


def test_лимит_пробуждений_за_час_соблюдается() -> None:
    writer = RecordingWriter()
    policy = build_policy("all", None, max_per_hour="3")
    pusher = ChannelPusher("bob", lambda: [], writer, policy)
    for _ in range(5):
        pusher._deliver(_event("сообщение"))
    assert len(writer.frames) == 3


def test_длинное_сообщение_обрезается_с_подсказкой() -> None:
    writer = RecordingWriter()
    policy = build_policy("all", None, max_chars="20")
    pusher = ChannelPusher("bob", lambda: [], writer, policy)
    pusher._deliver(_event("х" * 100))
    content = writer.frames[0]["params"]["content"]
    assert content.startswith("х" * 20)
    assert "hub_read_messages" in content


def test_имена_очищаются_перед_вставкой_в_поля() -> None:
    writer = RecordingWriter()
    pusher = ChannelPusher("bob", lambda: [], writer, build_policy("all", None))
    pusher._deliver(_event("привет", author='зло" onclick="', channel="ка\nнал"))
    meta = writer.frames[0]["params"]["meta"]
    assert '"' not in meta["user"] and "<" not in meta["user"]
    assert "\n" not in meta["channel"]


def test_пустое_имя_не_превращается_в_пустую_строку() -> None:
    assert _safe_name("!!!") == "?"


def test_кадр_уведомления_соответствует_протоколу() -> None:
    writer = RecordingWriter()
    pusher = ChannelPusher("bob", lambda: [], writer, build_policy("all", None))
    pusher._deliver(_event("привет"))
    frame = writer.frames[0]
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == "notifications/claude/channel"
    assert "id" not in frame  # уведомление, а не запрос


def test_запись_переживает_одиночный_суррогат(capsys: pytest.CaptureFixture[str]) -> None:
    NotificationWriter().write({"jsonrpc": "2.0", "method": "x", "params": {"t": "\ud800"}})
    out = capsys.readouterr().out
    assert json.loads(out)["method"] == "x"


def test_запись_из_двух_потоков_не_рвёт_кадры(capsys: pytest.CaptureFixture[str]) -> None:
    writer = NotificationWriter()
    payload = {"jsonrpc": "2.0", "method": "m", "params": {"t": "я" * 3000}}

    threads = [threading.Thread(target=writer.write, args=(payload,)) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 12
    for line in lines:  # каждая строка — целый разбираемый кадр
        assert json.loads(line)["method"] == "m"
