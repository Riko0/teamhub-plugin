"""Хранилище настроек плагина в файле пользователя.

Адрес хаба и имя участника задаются один раз при установке и живут в
`~/.config/teamhub/config.json`. Раздел `agents` задаёт персональные надстройки:
один файл обслуживает все сессии, но каждый агент может слушать своё.
Переменные окружения важнее файла — удобно переопределить для одного проекта.

В файле лежит пароль, поэтому он создаётся с правами 600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

FILE_MODE: Final[int] = 0o600
DIR_MODE: Final[int] = 0o700
KNOWN_KEYS: Final[tuple[str, ...]] = (
    "url",
    "agent_id",
    "name_prefix",
    "auth",
    "ssh",
    "ssh_port",
    "ssh_key",
    "notify",
    "notify_channels",
    "notify_max_per_hour",
    "notify_max_chars",
)
AGENTS_KEY: Final[str] = "agents"


def config_path() -> Path:
    """Возвращает путь к файлу настроек с учётом XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "teamhub" / "config.json"


def _raw() -> dict[str, Any]:
    """Читает файл настроек; при отсутствии или порче — пустой словарь."""
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _scalars(source: dict[str, Any]) -> dict[str, str]:
    """Оставляет только известные непустые строковые настройки."""
    return {k: str(v) for k, v in source.items() if k in KNOWN_KEYS and v}


def load_stored() -> dict[str, str]:
    """Общие настройки, без персональных надстроек агентов."""
    return _scalars(_raw())


def load_for_agent(agent_id: str) -> dict[str, str]:
    """Настройки конкретного агента: общие, поверх них — его собственные.

    Раздел ``agents`` позволяет одному файлу обслуживать все сессии: например,
    ``superres`` слушает свои каналы, а ``motion`` — свои.
    """
    raw = _raw()
    merged = _scalars(raw)
    per_agent = raw.get(AGENTS_KEY)
    if isinstance(per_agent, dict):
        own = per_agent.get(agent_id)
        if isinstance(own, dict):
            merged.update(_scalars(own))
    return merged


def save_stored(values: dict[str, str]) -> Path:
    """Дополняет сохранённые настройки, не теряя того, о чём не спрашивали.

    Диалог установки спрашивает не про всё: настройки уведомлений и надстройки
    агентов правят руками. Поэтому здесь именно слияние — иначе повторный
    запуск установки молча стирал бы их. Пустая строка означает «удалить».

    Returns:
        Путь к записанному файлу.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    raw = _raw()
    payload: dict[str, Any] = _scalars(raw)
    for key, value in values.items():
        if key not in KNOWN_KEYS:
            continue
        if value:
            payload[key] = str(value)
        else:
            payload.pop(key, None)  # пустое значение стирает настройку
    existing = raw.get(AGENTS_KEY)
    if isinstance(existing, dict) and existing:
        payload[AGENTS_KEY] = existing
    # создаём пустым с нужными правами: между записью и chmod пароль иначе
    # на мгновение оказался бы доступен всем
    path.touch(mode=FILE_MODE, exist_ok=True)
    path.chmod(FILE_MODE)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
