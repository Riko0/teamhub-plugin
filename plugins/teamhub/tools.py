"""Описания инструментов, которые плагин отдаёт Claude Code.

Вынесено из teamhub_mcp, чтобы протокол и набор инструментов не смешивались
в одном файле.
"""

from __future__ import annotations

from typing import Any, Final

DEFAULT_LIMIT: Final[int] = 20


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Собирает схему объекта для входных параметров инструмента."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def chat_tools(agent_id: str) -> list[dict[str, Any]]:
    """Инструменты командного чата."""
    return [
        {
            "name": "hub_send_message",
            "description": f"Отправить сообщение в канал командного хаба от имени «{agent_id}».",
            "inputSchema": _obj(
                {
                    "channel": {"type": "string", "description": "Имя канала, например general"},
                    "text": {"type": "string", "description": "Текст сообщения"},
                },
                ["channel", "text"],
            ),
        },
        {
            "name": "hub_read_messages",
            "description": "Прочитать последние сообщения канала командного хаба.",
            "inputSchema": _obj(
                {
                    "channel": {"type": "string", "description": "Имя канала"},
                    "limit": {
                        "type": "integer",
                        "description": f"Сколько сообщений (по умолчанию {DEFAULT_LIMIT})",
                    },
                },
                ["channel"],
            ),
        },
        {
            "name": "hub_list_channels",
            "description": "Список каналов командного хаба.",
            "inputSchema": _obj({}),
        },
        {
            "name": "hub_whoami",
            "description": "Под каким именем эта сессия подключена к хабу.",
            "inputSchema": _obj({}),
        },
    ]


def wiki_tools() -> list[dict[str, Any]]:
    """Инструменты общей вики — долговременной памяти команды."""
    return [
        {
            "name": "hub_wiki_list",
            "description": "Список страниц общей вики команды: что уже описано и кем.",
            "inputSchema": _obj({}),
        },
        {
            "name": "hub_wiki_read",
            "description": "Прочитать страницу общей вики команды целиком.",
            "inputSchema": _obj(
                {"page_path": {"type": "string", "description": "Путь страницы, например superres/пайплайн"}},
                ["page_path"],
            ),
        },
        {
            "name": "hub_wiki_search",
            "description": "Найти страницы вики по запросу — прежде чем создавать новую.",
            "inputSchema": _obj(
                {"query": {"type": "string", "description": "Что искать"}},
                ["query"],
            ),
        },
        {
            "name": "hub_wiki_write",
            "description": (
                "Создать страницу вики или переписать существующую. "
                "Сюда идёт то, что переживёт сессию: решения, договорённости, устройство систем."
            ),
            "inputSchema": _obj(
                {
                    "page_path": {
                        "type": "string",
                        "description": "Путь страницы, например superres/пайплайн",
                    },
                    "content": {"type": "string", "description": "Содержимое в разметке markdown"},
                    "title": {"type": "string", "description": "Заголовок (по умолчанию — из пути)"},
                    "category": {"type": "string", "description": "Категория (необязательно)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Метки (необязательно)",
                    },
                },
                ["page_path", "content"],
            ),
        },
    ]


def all_tools(agent_id: str) -> list[dict[str, Any]]:
    """Полный набор инструментов плагина."""
    return chat_tools(agent_id) + wiki_tools()
