"""Проверки хранилища настроек.

Повторный запуск установки однажды стёр настройки уведомлений — диалог о них
не спрашивает, а сохранение переписывало файл начисто. Здесь это закреплено.
"""

from __future__ import annotations

import importlib
import json
import stat
from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Хранилище, указывающее в изолированный каталог."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    import config_store

    return importlib.reload(config_store)


def test_сохранение_не_теряет_то_о_чём_не_спрашивали(store) -> None:
    store.save_stored({"url": "http://a", "notify": "all", "notify_max_per_hour": "5"})
    store.save_stored({"url": "http://b"})  # повторная установка спросила только адрес
    осталось = store.load_stored()
    assert осталось["url"] == "http://b"
    assert осталось["notify"] == "all"
    assert осталось["notify_max_per_hour"] == "5"


def test_пустое_значение_стирает_настройку(store) -> None:
    store.save_stored({"url": "http://a", "ssh": "root@old"})
    store.save_stored({"url": "http://a", "ssh": ""})
    assert "ssh" not in store.load_stored()


def test_надстройки_агентов_переживают_повторную_установку(store) -> None:
    path = store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": "http://a", "agents": {"bob": {"notify": "off"}}}), encoding="utf-8")
    store.save_stored({"url": "http://b"})
    assert json.loads(path.read_text())["agents"] == {"bob": {"notify": "off"}}


def test_настройки_агента_накладываются_поверх_общих(store) -> None:
    path = store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"notify": "mentions", "url": "http://a", "agents": {"bob": {"notify": "all"}}}),
        encoding="utf-8",
    )
    assert store.load_for_agent("bob")["notify"] == "all"
    assert store.load_for_agent("bob")["url"] == "http://a", "общие настройки должны остаться"
    assert store.load_for_agent("другой")["notify"] == "mentions"


def test_файл_с_паролем_недоступен_посторонним(store) -> None:
    path = store.save_stored({"url": "http://a", "auth": "team:секрет"})
    режим = stat.S_IMODE(path.stat().st_mode)
    assert режим == 0o600, f"ожидали 600, получили {режим:o}"


def test_испорченный_файл_не_роняет_чтение(store) -> None:
    path = store.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{это не json", encoding="utf-8")
    assert store.load_stored() == {}


def test_неизвестные_ключи_не_сохраняются(store) -> None:
    store.save_stored({"url": "http://a", "постороннее": "значение"})
    assert "постороннее" not in store.load_stored()
