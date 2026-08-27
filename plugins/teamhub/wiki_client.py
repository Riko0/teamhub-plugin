"""Работа с общей вики хаба: страницы, которые агенты ведут сами.

Вики — это память команды между сессиями: решения, договорённости, устройство
систем. Чат живёт минутами, вики — месяцами.

Мод вики отвечает на события синхронно, поэтому здесь то же, что и в чате:
отправляем событие и разбираем ответ.
"""

from __future__ import annotations

from typing import Any, Final

from hub_client import HubClient, HubRejected

WIKI_MOD: Final[str] = "openagents.mods.workspace.wiki"
MOD_ONLY: Final[str] = "mod_only"
PREVIEW_CHARS: Final[int] = 200
NOT_FOUND: Final[tuple[str, ...]] = ("not found", "does not exist", "не найдена")


class WikiClient:
    """Чтение и правка страниц вики от имени агента."""

    def __init__(self, hub: HubClient) -> None:
        self._hub = hub

    def _event(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Отправляет событие вики, подставляя имя агента."""
        payload = {"event_name": name, "source_id": self._hub.agent_id, **payload}
        return self._hub.event(name, payload, visibility=MOD_ONLY, relevant_mod=WIKI_MOD)

    def list_pages(self, prefix: str | None = None) -> list[dict[str, Any]]:
        """Возвращает страницы вики, при желании только одну ветку.

        Вложенности у хаба нет: путь со слэшами — просто имя. Поэтому ветка
        отбирается здесь, по началу пути.
        """
        pages = list(self._event("wiki.pages.list", {}).get("pages") or [])
        if prefix:
            branch = prefix.strip("/")
            pages = [
                page
                for page in pages
                if str(page.get("page_path", "")).startswith(f"{branch}/") or page.get("page_path") == branch
            ]
        return sorted(pages, key=lambda page: str(page.get("page_path", "")))

    def read_page(self, page_path: str) -> dict[str, Any]:
        """Возвращает страницу целиком; для отсутствующей — пустой словарь.

        Отсутствие страницы хаб считает неуспехом, но для нас это обычный
        ответ: именно так проверяется, создавать страницу или править.
        """
        try:
            return self._event("wiki.page.get", {"page_path": page_path})
        except HubRejected as exc:
            if not any(marker in str(exc).lower() for marker in NOT_FOUND):
                raise  # прочие отказы — не повод считать страницу несуществующей
            return {}

    def search_pages(self, query: str) -> list[dict[str, Any]]:
        """Ищет страницы по запросу."""
        data = self._event("wiki.pages.search", {"query": query, "search_query": query})
        return list(data.get("pages") or data.get("results") or [])

    def write_page(
        self,
        page_path: str,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Создаёт страницу или переписывает существующую.

        Returns:
            Короткое описание того, что произошло.
        """
        existing = self.read_page(page_path)
        if existing.get("wiki_content") is not None:
            self._event("wiki.page.edit", {"page_path": page_path, "wiki_content": content})
            return f"страница {page_path} обновлена"
        self._event(
            "wiki.page.create",
            {
                "page_path": page_path,
                "title": title or page_path.rsplit("/", 1)[-1],
                "wiki_content": content,
                # category хаб хранит, но в списке не отдаёт и не отбирает по ней —
                # бесполезное поле, не занимаем им внимание модели
                "tags": tags or [],
            },
        )
        return f"страница {page_path} создана"

    def append_to_page(self, page_path: str, text: str, title: str | None = None) -> str:
        """Дописывает раздел в конец страницы, не пересылая её целиком.

        Хаб умеет только заменять страницу, поэтому склейка происходит здесь.
        Так агенту не приходится тащить через контекст весь прежний текст.

        Returns:
            Короткое описание того, что произошло.
        """
        existing = self.read_page(page_path)
        body = existing.get("wiki_content")
        if body is None:
            return self.write_page(page_path, text, title=title)
        joined = f"{body.rstrip()}\n\n{text.strip()}\n"
        self._event("wiki.page.edit", {"page_path": page_path, "wiki_content": joined})
        return f"страница {page_path} дополнена"


def format_pages(pages: list[dict[str, Any]], prefix: str | None = None) -> str:
    """Собирает список страниц в текст для модели."""
    if not pages:
        return f"в ветке {prefix} страниц нет" if prefix else "в вики пока нет страниц"
    lines = []
    for page in pages:
        path = page.get("page_path", "?")
        title = page.get("title") or path
        author = page.get("created_by") or page.get("creator_id") or "?"
        lines.append(f"{path} — {title} (автор {author})")
    return "\n".join(lines)


def format_page(page: dict[str, Any], page_path: str) -> str:
    """Собирает страницу в текст для модели."""
    content = page.get("wiki_content")
    if content is None:
        return f"страницы {page_path} нет"
    title = page.get("title") or page_path
    author = page.get("created_by") or page.get("creator_id") or "?"
    return f"# {title}\n(страница {page_path}, автор {author})\n\n{content}"
