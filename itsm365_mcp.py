#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2"]
# ///
"""
MCP-сервер ITSM365 (платформа NSMP, JSON-RPC). Только чтение; вложения заявок сохраняются на диск.

Настройки: переменные окружения ITSM365_SERVER и ITSM365_ACCESS_KEY,
иначе - config.ini рядом со скриптом, секция [itsm365] (server, access_key).

Запуск (stdio):  uv run itsm365_mcp.py      либо   pip install "mcp>=2" && python3 itsm365_mcp.py
"""

import configparser
import difflib
import html
import json
import os
import re
import shutil
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

SERVER_CAP = 1000  # сервер игнорирует limit/offset и отдаёт не больше 1000 записей на запрос
PERMISSION_ERROR_RE = re.compile(r"нет прав на чтение атрибута\s+'([^']+)'", re.IGNORECASE)

# Поля заявки для готовых инструментов: код -> (название в интерфейсе, элемент view).
# Название нужно, чтобы опознать поле в ошибке "нет прав на чтение атрибута '...'".
TICKET_FIELDS = {
    "UUID":            (None,                      "UUID"),
    "number":          ("Номер",                   "number"),
    "title":           ("Название",                "title"),
    "state":           ("Статус",                  "state"),
    "shortDescr":      ("Тема",                    "shortDescr"),
    "clientEmployee":  ("Контрагент (сотрудник)",  {"clientEmployee": ["title"]}),
    "office":          ("Объект обслуживания",     {"office": ["UUID", "title"]}),
    "responsibleTeam": ("Ответственный (команда)", {"responsibleTeam": ["title"]}),
    "service":         ("Услуга",                  {"service": ["title"]}),
    "creationDate":    ("Дата создания",           "creationDate"),
}

mcp = MCPServer("itsm365", instructions=(
    "ITSM365 (NSMP JSON-RPC), только чтение. Особенности сервера: limit/offset игнорируются, "
    "за запрос отдаётся не больше 1000 объектов; операторы gt/lt/between не поддерживаются; "
    "like чувствителен к регистру; даты приходят как {'delegate': 'Oct 15, 2026, 11:04:31 AM'} в UTC; "
    "при ошибке 'нет прав на чтение атрибута' уберите этот атрибут из view."
))


def _config() -> dict:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"), encoding="utf-8")
    cfg = dict(cp["itsm365"]) if cp.has_section("itsm365") else {}
    server = os.environ.get("ITSM365_SERVER") or cfg.get("server")
    key = os.environ.get("ITSM365_ACCESS_KEY") or cfg.get("access_key")
    if not (server and key):
        raise ToolError("Не заданы server/access_key (ITSM365_SERVER, ITSM365_ACCESS_KEY или config.ini)")
    return {**cfg, "server": server.rstrip("/"), "access_key": key}


def _url() -> str:
    cfg = _config()
    return (f"{cfg['server']}/sd/services/rest/exec-post?accessKey={cfg['access_key']}"
            "&func=modules.jsonRpc.process&params=request,response,user&raw=true")


def rpc(method: str, params: dict):
    """Вызов JSON-RPC. Возвращает result; ошибки - ToolError (без URL, чтобы не светить ключ)."""
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 0}).encode("utf-8")
    req = urllib.request.Request(_url(), data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:500]}") from None
    except urllib.error.URLError as e:
        raise ToolError(f"Нет соединения с сервером: {e.reason}") from None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ToolError(f"Сервер вернул не-JSON: {raw[:500]}") from None
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    if parsed.get("error"):
        raise ToolError(f"Ошибка JSON-RPC: {json.dumps(parsed['error'], ensure_ascii=False)}")
    return parsed.get("result")  # пустой ответ без result = ничего не найдено


def _text(html_text) -> str:
    """HTML из ITSM365 -> простой текст."""
    s = re.sub(r"<(br|/div|/p|/li)[^>]*>", "\n", html_text or "", flags=re.I)
    return re.sub(r"\n\s*\n+", "\n", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_FILENAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))}


def _safe_filename(name: str, fallback: str) -> str:
    """Имя файла, допустимое в Windows, macOS и Linux: запрещённые символы и разделители пути -> "_"."""
    name = _BAD_FILENAME_CHARS.sub("_", name).strip().rstrip(". ")
    if name.split(".")[0].strip().upper() in _RESERVED_FILENAMES:
        name = "_" + name
    return name or fallback


def _op(method: str, *args) -> dict:
    return {"method": method, "params": {"args": list(args)}}


def _page(items: list, limit: int) -> dict:
    return {"found": len(items), "server_cap_hit": len(items) >= SERVER_CAP, "items": items[:limit]}


def _find_tickets(query: dict) -> tuple[list, list]:
    """find по serviceCall с полями TICKET_FIELDS; поля без прав на чтение убираются автоматически."""
    fields = dict(TICKET_FIELDS)
    dropped = []
    while True:
        try:
            items = rpc("find", {"fqn": "serviceCall", "query": query, "view": [v for _, v in fields.values()]})
            return items or [], dropped
        except ToolError as e:
            m = PERMISSION_ERROR_RE.search(str(e))
            if not m:
                raise
            titles = {t: code for code, (t, _) in fields.items() if t}
            close = difflib.get_close_matches(m.group(1).strip(), list(titles), n=1, cutoff=0.6)
            if not close:
                raise
            dropped.append(titles[close[0]])
            del fields[titles[close[0]]]


@mcp.tool()
def itsm_find(fqn: str, query: dict | None = None, view: list | None = None, limit: int = 50) -> dict:
    """Универсальный поиск объектов (JSON-RPC find).

    fqn - код метакласса: serviceCall (заявка; подтипы serviceCall$request, serviceCall$project ...),
          ou (организация / объект обслуживания), team (команда), employee (сотрудник).
    query - фильтр {атрибут: значение} (точное совпадение) или {атрибут: {"method": M, "params": {"args": [...]}}},
          M: not, in, like (шаблон с %, чувствителен к регистру), isNotNull (без params).
          gt/lt/between сервер не поддерживает. Пример - незакрытые заявки объектов:
          {"state": {"method": "not", "params": {"args": ["closed"]}},
           "office": {"method": "in", "params": {"args": ["ou$3013403"]}}}
    view - возвращаемые атрибуты: ["UUID", "number", {"service": ["title"]}] (вложенный объект - для ссылок).
          Атрибуты заявки: UUID, number, title, state (closed, resolved, ...), shortDescr, metaClass, clientEmployee,
          office, responsibleTeam, service, mark, creationDate, dateDecision, lastModifiedDate,
          reactOverdue, overdue, escalation.
    limit - сколько объектов вернуть в ответе (обрезается на стороне MCP; сервер limit игнорирует).

    Ответ: found - сколько отдал сервер; server_cap_hit=true - упёрлись в лимит 1000, сузьте query.
    """
    return _page(rpc("find", {"fqn": fqn, "query": query or {}, "view": view or ["UUID", "title"]}) or [], limit)


@mcp.tool()
def itsm_count(fqn: str, query: dict | None = None):
    """Количество объектов по фильтру (JSON-RPC count). fqn и query - как в itsm_find."""
    return rpc("count", {"fqn": fqn, "query": query or {}})


@mcp.tool()
def itsm_get(uuid: str, view: list | None = None):
    """Один объект по UUID, например serviceCall$12345 или ou$3013403. view - как в itsm_find.
    Нет такого объекта - null."""
    # JSON-RPC get на сервере падает с 500 (collectEntries() on null object), поэтому find по UUID
    items = rpc("find", {"fqn": uuid.split("$")[0], "query": {"UUID": uuid}, "view": view or ["UUID", "title"]}) or []
    return items[0] if items else None


@mcp.tool()
def get_ticket(number: int) -> dict:
    """Заявка по номеру: статус, тема, контрагент, объект обслуживания, команда, услуга, дата создания,
    текст описания (description) и все комментарии (comments: автор, дата, private, текст) - HTML очищен до текста."""
    items, dropped = _find_tickets({"number": number})
    for t in items:
        try:
            d = rpc("find", {"fqn": "serviceCall", "query": {"number": number}, "view": ["UUID", "descriptionRTF"]})
            t["description"] = _text((d or [{}])[0].get("descriptionRTF"))
        except ToolError as e:
            if not PERMISSION_ERROR_RE.search(str(e)):
                raise
            dropped.append("descriptionRTF")
        comments = rpc("find", {"fqn": "comment", "query": {"source": t["UUID"]},
                                "view": ["UUID", "text", "creationDate", {"author": ["title"]}, "private"]}) or []
        t["comments"] = [{"author": (c.get("author") or {}).get("title"),
                          "date": (c.get("creationDate") or {}).get("delegate"),
                          "private": c.get("private"), "text": _text(c.get("text"))} for c in comments]
    return {"items": items, "fields_without_read_access": dropped}


@mcp.tool()
def find_organizations(text: str, limit: int = 50) -> dict:
    """Организации (объекты обслуживания, класс ou), в названии которых есть text - например код объекта 231157777.
    Регистр важен."""
    return _page(rpc("find", {"fqn": "ou", "query": {"title": _op("like", f"%{text}%")},
                              "view": ["UUID", "title"]}) or [], limit)


@mcp.tool()
def find_teams(text: str, limit: int = 50) -> dict:
    """Команды (класс team), в названии которых есть text. Регистр важен."""
    return _page(rpc("find", {"fqn": "team", "query": {"title": _op("like", f"%{text}%")},
                              "view": ["UUID", "title"]}) or [], limit)


@mcp.tool()
def open_tickets(object_codes: list[str] | None = None, team_uuids: list[str] | None = None, limit: int = 100) -> dict:
    """Незакрытые заявки (state != closed).

    object_codes - коды объектов обслуживания (например ["231157777"]) или готовые UUID вида ou$3013403;
                   код ищется в названии организации как "/ <код> /" и должен дать ровно одну организацию.
    team_uuids - UUID ответственных команд (см. find_teams).
    Без фильтров - все незакрытые заявки, видимые ключу доступа.
    """
    query = {"state": _op("not", "closed")}
    if object_codes:
        offices = []
        for code in object_codes:
            if "$" in code:
                offices.append(code)
                continue
            found = rpc("find", {"fqn": "ou", "query": {"title": _op("like", f"%/ {code} /%")},
                                 "view": ["UUID", "title"]}) or []
            if len(found) != 1:
                raise ToolError(f"Код объекта {code}: ожидалась одна организация, найдено {len(found)}: {found}")
            offices.append(found[0]["UUID"])
        query["office"] = _op("in", *offices)
    if team_uuids:
        query["responsibleTeam"] = _op("in", *team_uuids)
    items, dropped = _find_tickets(query)
    return {**_page(items, limit), "fields_without_read_access": dropped}


@mcp.tool()
def download_ticket_files(number: int, max_mb: int = 50) -> dict:
    """Скачать вложения заявки на диск в папку <files_dir>/<номер заявки>/.

    Берутся файлы заявки, файлы, прикреплённые к комментариям (оригиналы картинок), и картинки,
    вставленные в текст комментариев (обычно уменьшенные копии). Файлы больше max_mb МБ пропускаются.
    Вложение комментария, продублированное на заявке (то же имя и размер), скачивается один раз.
    Символы, недопустимые в именах файлов Windows, заменяются на "_".
    files_dir - параметр config.ini (секция [itsm365]) или ITSM365_FILES_DIR; по умолчанию ../method_api.
    Ответ: folder - папка, saved / skipped / errors - списки файлов.
    """
    cfg = _config()
    tickets = rpc("find", {"fqn": "serviceCall", "query": {"number": number}, "view": ["UUID"]}) or []
    if len(tickets) != 1:
        raise ToolError(f"Заявка {number}: найдено {len(tickets)}")
    ticket = tickets[0]["UUID"]
    comments = rpc("find", {"fqn": "comment", "query": {"source": ticket}, "view": ["UUID", "text"]}) or []
    # Файлы ищутся и у заявки, и у её комментариев: вложение комментария имеет source = comment$...
    sources = [ticket] + [c["UUID"] for c in comments]
    found = []
    for i in range(0, len(sources), 100):
        found += rpc("find", {"fqn": "file", "query": {"source": _op("in", *sources[i:i + 100])},
                              "view": ["UUID", "title", "fileSize", "source"]}) or []
    # Сначала файлы заявки, затем файлы комментариев, которых на заявке нет (сравнение по имени и размеру)
    files, seen = {}, set()
    for f in sorted(found, key=lambda f: f.get("source") != ticket):
        key = (f.get("title"), f.get("fileSize"))
        if f.get("source") == ticket or key not in seen:
            files[f["UUID"]] = f
        seen.add(key)
    for c in comments:
        for uuid in re.findall(r"file\$\d+", c.get("text") or ""):
            files.setdefault(uuid, {"UUID": uuid, "title": f"comment_{uuid.split('$')[1]}.png"})

    base = (os.environ.get("ITSM365_FILES_DIR") or cfg.get("files_dir")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "method_api"))
    folder = os.path.abspath(os.path.join(os.path.expanduser(base), str(number)))
    os.makedirs(folder, exist_ok=True)

    saved, skipped, errors, names = [], [], [], set()
    for uuid, f in files.items():
        name = _safe_filename(f.get("title") or "", uuid.replace("$", "_"))
        if name.lower() in names:  # в Windows имена файлов регистронезависимы
            name = f"{uuid.split('$')[1]}_{name}"
        names.add(name.lower())
        size = f.get("fileSize") or 0
        if size > max_mb * 1024 * 1024:
            skipped.append({"file": name, "size": size, "reason": f"больше {max_mb} МБ"})
            continue
        url = f"{cfg['server']}/sd/services/rest/get-file/{uuid}?accessKey={cfg['access_key']}"
        try:
            with urllib.request.urlopen(url, timeout=300) as resp, open(os.path.join(folder, name), "wb") as out:
                shutil.copyfileobj(resp, out)
            saved.append({"file": name, "size": os.path.getsize(os.path.join(folder, name))})
        except urllib.error.HTTPError as e:
            errors.append({"file": name, "error": f"HTTP {e.code}"})
        except urllib.error.URLError as e:
            errors.append({"file": name, "error": f"Нет соединения: {e.reason}"})
    return {"folder": folder, "saved": saved, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    mcp.run()
