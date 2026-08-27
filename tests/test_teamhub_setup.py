"""Проверки диалога первоначальной настройки.

Диалог задаёт вопросы по очереди, поэтому в тестах подменяем ввод списком
ответов — так видно, что именно человек напечатал и что из этого вышло.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import teamhub_setup


@pytest.fixture
def отвечает(monkeypatch: pytest.MonkeyPatch):
    """Подсовывает диалогу заранее заготовленные ответы."""

    def _настроить(*ответы: str):
        очередь = list(ответы)
        monkeypatch.setattr("builtins.input", lambda _: очередь.pop(0) if очередь else "")

    return _настроить


@pytest.fixture(autouse=True)
def _в_каталоге_проекта(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Диалог показывает имя каталога, поэтому каталог должен быть предсказуем."""
    project = tmp_path / "motion"
    project.mkdir()
    monkeypatch.chdir(project)


def test_пустой_ответ_оставляет_имя_каталога(отвечает) -> None:
    отвечает("")
    assert teamhub_setup._collect_name({}) == {"agent_id": "", "name_prefix": ""}


def test_имя_с_проектом_становится_префиксом(отвечает) -> None:
    отвечает("ivan", "да")
    assert teamhub_setup._collect_name({}) == {"name_prefix": "ivan", "agent_id": ""}


def test_имя_без_проекта_становится_постоянным(отвечает) -> None:
    отвечает("ivan", "нет")
    assert teamhub_setup._collect_name({}) == {"agent_id": "ivan", "name_prefix": ""}


def test_прежний_выбор_предлагается_по_умолчанию(отвечает) -> None:
    отвечает("", "да")  # Enter на вопросе об имени
    assert teamhub_setup._collect_name({"name_prefix": "ivan"})["name_prefix"] == "ivan"


def test_смена_способа_гасит_прежний_ключ(отвечает) -> None:
    """Иначе при слиянии настроек остался бы старый ключ и перебил новый."""
    отвечает("ivan", "нет")
    выбор = teamhub_setup._collect_name({"name_prefix": "прежний"})
    assert выбор["name_prefix"] == "", "префикс должен быть стёрт явно"


def test_переход_на_http_гасит_настройки_ssh(отвечает) -> None:
    отвечает("http", "https://хаб:8443", "", "")
    значения = teamhub_setup._collect({"ssh": "root@старый", "ssh_port": "8700"})
    assert значения["url"] == "https://хаб:8443"
    assert значения["ssh"] == "" and значения["ssh_port"] == ""
