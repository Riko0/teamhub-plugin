#!/usr/bin/env python3
"""Первоначальная настройка плагина: спрашивает параметры и сохраняет их.

Запускается один раз, вручную, из терминала:

    python3 teamhub_setup.py

Ответы попадают в ~/.config/teamhub/config.json, откуда их берёт плагин.
Файл создаётся с правами 600, поскольку в нём может лежать пароль.
"""

from __future__ import annotations

import sys
from pathlib import Path

from config_store import load_stored, save_stored
from hub_client import DEFAULT_REMOTE_PORT, HubClient, HubConfig, build_auth_header, resolve_agent_id

CONNECTION_MODES: tuple[str, ...] = ("http", "ssh")


def _ask(question: str, current: str | None = None, required: bool = False) -> str:
    """Спрашивает значение, предлагая текущее как вариант по умолчанию."""
    suffix = f" [{current}]" if current else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip() or (current or "")
        if answer or not required:
            return answer
        print("  нужно указать значение")


def _ask_mode(stored: dict[str, str]) -> str:
    """Спрашивает способ подключения к хабу."""
    current = "ssh" if stored.get("ssh") else "http"
    while True:
        answer = _ask("Способ подключения — http или ssh", current).lower()
        if answer in CONNECTION_MODES:
            return answer
        print("  введите http или ssh")


def _collect(stored: dict[str, str]) -> dict[str, str]:
    """Собирает все настройки в диалоге с пользователем."""
    values: dict[str, str] = {}
    mode = _ask_mode(stored)
    # сохранение сливается с прежним, поэтому неиспользуемый способ гасим явно:
    # иначе оставшийся ssh перебил бы прямой адрес — туннель имеет приоритет
    if mode == "ssh":
        values["ssh"] = _ask("Сервер в виде user@host", stored.get("ssh"), required=True)
        values["ssh_port"] = _ask("Порт хаба на сервере", stored.get("ssh_port") or str(DEFAULT_REMOTE_PORT))
        values["ssh_key"] = _ask("Путь к приватному ключу (пусто — как обычно)", stored.get("ssh_key"))
        values["url"] = ""
    else:
        values["url"] = _ask("Адрес хаба, например https://хост:8443", stored.get("url"), required=True)
        values["ssh"] = values["ssh_port"] = values["ssh_key"] = ""
    values.update(_collect_name(stored))
    values["auth"] = _ask("Логин:пароль, если хаб за авторизацией (пусто — нет)", stored.get("auth"))
    return values


def _collect_name(stored: dict[str, str]) -> dict[str, str]:
    """Спрашивает, как называть агента в чате.

    Сначала само имя, потом — добавлять ли к нему проект. По умолчанию имя
    берётся из каталога проекта: чаще всего это и есть нужное, а плагин один
    на компьютер, и сессий у человека несколько.
    """
    project = Path.cwd().name or "проект"
    print("\nПлагин один на компьютер, а сессий Claude Code у вас несколько.")
    print(f"Если ничего не вводить, именем станет название каталога — сейчас это «{project}».\n")

    current = stored.get("name_prefix") or stored.get("agent_id") or ""
    name = _ask(f"Как называть вас в чате (пусто — «{project}»)", current)
    if not name:
        return {"agent_id": "", "name_prefix": ""}

    per_project = (
        _ask(
            f"Добавлять к имени название проекта? Тогда здесь вы будете «{name}-{project}». да/нет",
            "да",
        )
        .lower()
        .startswith("д")
    )
    if per_project:
        print(f"  в этом проекте вас увидят как: {name}-{project}")
        return {"name_prefix": name, "agent_id": ""}
    print(f"  во всех проектах вас увидят как: {name}")
    return {"agent_id": name, "name_prefix": ""}


def _to_config(values: dict[str, str]) -> HubConfig:
    """Превращает собранные ответы в конфигурацию клиента."""
    return HubConfig(
        agent_id=resolve_agent_id(values),
        auth_header=build_auth_header(values.get("auth")),
        url=(values.get("url") or "").rstrip("/") or None,
        ssh_destination=values.get("ssh") or None,
        ssh_port=int(values.get("ssh_port") or DEFAULT_REMOTE_PORT),
        ssh_key=values.get("ssh_key") or None,
    )


def _verify(values: dict[str, str]) -> bool:
    """Пробует подключиться к хабу и показать каналы."""
    client = HubClient(_to_config(values))
    try:
        client.connect()
        channels = client.list_channels()
    except RuntimeError as exc:
        print(f"\nПроверка не прошла: {exc}")
        return False
    finally:
        client.close()
    names = ", ".join(str(c.get("name")) for c in channels) or "каналов нет"
    print(f"\nПроверка прошла. Доступные каналы: {names}")
    return True


def main() -> None:
    """Ведёт диалог настройки, сохраняет ответы и проверяет подключение."""
    print("Настройка плагина teamhub. Enter — оставить значение в скобках.\n")
    try:
        values = _collect(load_stored())
    except (EOFError, KeyboardInterrupt):
        print("\nОтменено.")
        sys.exit(1)

    path = save_stored(values)
    print(f"\nСохранено: {path}")
    if not _verify(values):
        print("Настройки сохранены, но подключиться не удалось — проверьте адрес и доступ.")
        sys.exit(1)
    prefix, fixed = values.get("name_prefix"), values.get("agent_id")
    if prefix:
        имя = f"{prefix}-<проект>"
    elif fixed:
        имя = fixed
    else:
        имя = "<имя каталога проекта>"
    print(f"Готово. Имя в чате: {имя}. Перезапустите Claude Code.")


if __name__ == "__main__":
    main()
