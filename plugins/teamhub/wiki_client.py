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

    def children_of(self, page_path: str) -> list[str]:
        """Пути страниц, лежащих внутри ветки."""
        branch = page_path.strip("/")
        return [
            str(page.get("page_path"))
            for page in self.list_pages()
            if str(page.get("page_path", "")).startswith(f"{branch}/")
        ]

    def delete_page(self, page_path: str, include_children: bool = False) -> str:
        """Удаляет страницу, при желании вместе со всей веткой.

        Ветка не удаляется молча: снести десяток страниц одним словом слишком
        легко, поэтому нужно попросить об этом отдельно.

        Raises:
            HubRejected: если внутри есть страницы, а сносить их не просили.
        """
        kids = self.children_of(page_path)
        if kids and not include_children:
            listed = ", ".join(kids[:3]) + ("…" if len(kids) > 3 else "")
            raise HubRejected(
                f"внутри {page_path} ещё {len(kids)} страниц ({listed}). "
                "Чтобы снести ветку целиком, попросите об этом явно"
            )
        for child in kids:
            self._event("wiki.page.delete", {"page_path": child})
        self._event("wiki.page.delete", {"page_path": page_path})
        if kids:
            return f"ветка {page_path} удалена вместе с {len(kids)} страницами внутри"
        return f"страница {page_path} удалена"

    def page_history(self, page_path: str) -> list[dict[str, Any]]:
        """Возвращает историю правок страницы."""
        data = self._event("wiki.page.history", {"page_path": page_path})
        return list(data.get("versions") or data.get("history") or [])

    def _move_one(self, page_path: str, new_path: str) -> None:
        """Переносит одну страницу; вызывать после проверки занятости.

        Raises:
            HubRejected: если исходной страницы нет.
        """
        content = self.read_page(page_path).get("wiki_content")
        if content is None:
            raise HubRejected(f"страницы {page_path} нет")
        self.write_page(new_path, content, title=new_path.rsplit("/", 1)[-1])
        self._event("wiki.page.delete", {"page_path": page_path})

    def rename_page(self, page_path: str, new_path: str) -> str:
        """Переносит страницу или всю ветку на новый путь.

        Переименования в хабе нет, поэтому переносим сами: пишем по новому
        пути и убираем старую. Вложенные страницы едут следом — иначе ветка
        распалась бы на части.

        Raises:
            HubRejected: если целевой путь занят или переносить нечего.
        """
        old, new = page_path.strip("/"), new_path.strip("/")
        kids = self.children_of(old)
        itself = self.read_page(old).get("wiki_content") is not None
        if not kids and not itself:
            raise HubRejected(f"страницы {old} нет")

        targets = {old: new, **{kid: f"{new}/{kid[len(old) + 1 :]}" for kid in kids}}
        for source, target in targets.items():
            if source == old and not itself:
                continue
            if self.read_page(target).get("wiki_content") is not None:
                raise HubRejected(f"страница {target} уже существует")

        moved = 0
        for source, target in targets.items():
            if source == old and not itself:
                continue
            self._move_one(source, target)
            moved += 1
        if moved > 1:
            return f"перенесено страниц: {moved} — ветка {old} теперь {new}"
        return f"страница перенесена: {old} → {new}"

    def edit_fragment(self, page_path: str, old_text: str, new_text: str) -> str:
        """Заменяет кусок текста страницы, не пересылая её целиком.

        Кусок должен встречаться ровно один раз: иначе непонятно, какой из них
        имелся в виду, и молча испортить чужой текст хуже, чем отказать.

        Raises:
            HubRejected: если страницы нет или кусок не найден либо неоднозначен.
        """
        page = self.read_page(page_path)
        body = page.get("wiki_content")
        if body is None:
            raise HubRejected(f"страницы {page_path} нет")
        found = body.count(old_text)
        if found == 0:
            raise HubRejected(f"в странице {page_path} нет такого куска текста")
        if found > 1:
            raise HubRejected(f"кусок встречается {found} раза — уточните, добавив окружение")
        self._event(
            "wiki.page.edit",
            {"page_path": page_path, "wiki_content": body.replace(old_text, new_text, 1)},
        )
        return f"страница {page_path} поправлена"


def _author(page: dict[str, Any]) -> str:
    """Кто завёл страницу."""
    return str(page.get("created_by") or page.get("creator_id") or "?")


def _tree(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Раскладывает плоские пути в дерево по слэшам."""
    root: dict[str, Any] = {}
    for page in pages:
        node = root
        parts = str(page.get("page_path", "")).split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {}).setdefault("дети", {})
        node.setdefault(parts[-1], {})["страница"] = page
    return root


def _render(node: dict[str, Any], depth: int = 0) -> list[str]:
    """Обходит дерево, отбивая уровни отступом."""
    lines = []
    for name in sorted(node):
        item = node[name]
        page = item.get("страница")
        kids = item.get("дети") or {}
        pad = "  " * depth
        if page is not None:
            lines.append(f"{pad}{name} — {_author(page)}")
        else:
            lines.append(f"{pad}{name}/")
        if kids:
            lines.extend(_render(kids, depth + 1))
    return lines


def format_pages(pages: list[dict[str, Any]], prefix: str | None = None) -> str:
    """Показывает страницы деревом: путь со слэшами и есть иерархия."""
    if not pages:
        return f"в ветке {prefix} страниц нет" if prefix else "в вики пока нет страниц"
    return "\n".join(_render(_tree(pages)))


def format_history(versions: list[dict[str, Any]], page_path: str) -> str:
    """Показывает историю правок страницы."""
    if not versions:
        return f"история страницы {page_path} пуста"
    lines = []
    for item in sorted(versions, key=lambda x: x.get("version_number") or 0, reverse=True):
        number = item.get("version_number", "?")
        who = item.get("author_id") or item.get("created_by") or item.get("source_id") or "?"
        when = str(item.get("created_timestamp") or item.get("timestamp") or "")[:19]
        note = item.get("change_summary") or item.get("rationale") or ""
        lines.append(f"версия {number} — {who} {when} {note}".rstrip())
    return f"история {page_path}:\n" + "\n".join(lines)


def format_page(page: dict[str, Any], page_path: str) -> str:
    """Собирает страницу в текст для модели."""
    content = page.get("wiki_content")
    if content is None:
        return f"страницы {page_path} нет"
    title = page.get("title") or page_path
    author = page.get("created_by") or page.get("creator_id") or "?"
    return f"# {title}\n(страница {page_path}, автор {author})\n\n{content}"
