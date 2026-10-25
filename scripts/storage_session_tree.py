"""Report the session hierarchy around one session as a tree."""

from __future__ import annotations

import sqlite3

import storage_db
import storage_env
import storage_render as render


TITLE_COLUMNS = 40


class SessionTreeError(Exception):
    """The session could not be resolved, or the database cannot be read."""


def _connect(artifacts: storage_env.Artifacts) -> sqlite3.Connection:
    if not artifacts.db_path.is_file():
        raise SessionTreeError(f"database not found: {artifacts.db_path}")
    return storage_db.connect_readonly(artifacts.db_path)


def _message_stats(connection: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Return id -> (message count, bytes); V2 data wins over V1 data for the same id."""
    _session_table, message_table, _part_table = storage_db.session_tables(connection)
    tables = [message_table]
    if message_table != "message":
        tables.insert(0, "message")
    stats: dict[str, tuple[int, int]] = {}
    for table in tables:
        if not storage_db.table_exists(connection, table):
            continue
        for session_id, count, size in connection.execute(
            "SELECT session_id, COUNT(*), COALESCE(SUM(LENGTH(data)), 0) "
            f"FROM {table} GROUP BY session_id"
        ):
            stats[session_id] = (count, size)
    return stats


def _load(
    artifacts: storage_env.Artifacts, query: str
) -> tuple[list[dict], dict, dict[str, tuple[int, int]]]:
    """Resolve one session by id or unique prefix, then read its message statistics."""
    connection = _connect(artifacts)
    try:
        sessions = storage_db.session_candidates(connection)
        matched = storage_db.match_session(sessions, query)
        stats = _message_stats(connection)
    except storage_db.SessionLookupError as error:
        raise SessionTreeError(str(error)) from error
    except sqlite3.Error as error:
        raise SessionTreeError(f"cannot read {artifacts.db_path}: {error}") from error
    finally:
        connection.close()
    return sessions, matched, stats


def _detail(session: dict, stats: dict[str, tuple[int, int]]) -> dict:
    """Return one session's metadata with its message count and bytes."""
    count, size = stats.get(session["id"], (0, 0))
    return {
        "id": session["id"],
        "title": session["title"],
        "directory": session["directory"],
        "parent_id": session["parent_id"],
        "created": render.format_date(session["created"]),
        "updated": render.format_date(session["updated"]),
        "messages": count,
        "bytes": size,
    }


def _ancestors(index: dict[str, dict], session_id: str) -> list[str]:
    """Return the parent ids above a session, nearest first, stopping at a cycle."""
    chain: list[str] = []
    seen = {session_id}
    parent = index[session_id]["parent_id"] if session_id in index else ""
    while parent and parent not in seen:
        chain.append(parent)
        seen.add(parent)
        parent = index[parent]["parent_id"] if parent in index else ""
    return chain


def _children_map(index: dict[str, dict]) -> dict[str, list[dict]]:
    """Group sessions by parent id, oldest first within each group."""
    children: dict[str, list[dict]] = {}
    for session in sorted(
        index.values(), key=lambda item: (item["created"] or 0, item["id"])
    ):
        if session["parent_id"]:
            children.setdefault(session["parent_id"], []).append(session)
    return children


def _descendants(
    children: dict[str, list[dict]],
    stats: dict[str, tuple[int, int]],
    root: str,
    shown: set[str],
) -> tuple[list[dict], int]:
    """Build the nested tree of sessions below root, returning it with the node count."""
    nodes: list[dict] = []
    count = 0
    seen = set(shown)
    stack = [(root, nodes)]
    while stack:
        parent_id, parent_children = stack.pop()
        for session in children.get(parent_id, []):
            if session["id"] in seen:
                continue
            seen.add(session["id"])
            node = {
                "id": session["id"],
                "current": False,
                "detail": _detail(session, stats),
                "children": [],
            }
            parent_children.append(node)
            stack.append((session["id"], node["children"]))
            count += 1
    return nodes, count


def build_tree(artifacts: storage_env.Artifacts, query: str) -> dict:
    """Describe the ancestor chain, the session itself, and every session below it."""
    sessions, matched, stats = _load(artifacts, query)
    index = {session["id"]: session for session in sessions}
    chain = _ancestors(index, matched["id"])
    children = _children_map(index)
    descendant_nodes, descendant_count = _descendants(
        children, stats, matched["id"], set(chain) | {matched["id"]}
    )

    node = {
        "id": matched["id"],
        "current": True,
        "detail": _detail(matched, stats),
        "children": descendant_nodes,
    }
    # Wrap the current node in each ancestor, nearest first, so the root ends outermost.
    for ancestor_id in chain:
        ancestor = index.get(ancestor_id)
        node = {
            "id": ancestor_id,
            "current": False,
            "detail": _detail(ancestor, stats) if ancestor is not None else None,
            "children": [node],
        }

    parent = index.get(chain[0]) if chain else None
    return {
        "query": query,
        "sessions": len(chain) + 1 + descendant_count,
        "ancestors": len(chain),
        "descendants": descendant_count,
        "parent_id": chain[0] if chain else "",
        "parent": _detail(parent, stats) if parent is not None else None,
        "parent_missing": bool(chain) and parent is None,
        "tree": node,
    }


def _label(node: dict, console: render.Console) -> str:
    """Render one tree row: id, title, bytes, and a marker for the queried session."""
    detail = node["detail"]
    if detail is None:
        return f"{node['id']}  " + console.dim("(parent not in the database)")
    text = f"{detail['id']}  {render.truncate(detail['title'], TITLE_COLUMNS) or '(untitled)'}"
    text += console.dim(f"  {render.human_bytes(detail['bytes'])}")
    if node["current"]:
        text += console.dim("  ← this session")
    return text


def render_tree(report: dict, console: render.Console) -> None:
    """Draw the hierarchy with box-drawing connectors, root first."""
    console.section(f"Session tree ({render.human_count(report['sessions'])})")
    stack = [(report["tree"], "", True, True)]
    while stack:
        node, prefix, is_root, is_last = stack.pop()
        if is_root:
            console.write("  " + _label(node, console))
        else:
            console.write(
                "  " + prefix + ("└─ " if is_last else "├─ ") + _label(node, console)
            )
        child_prefix = prefix if is_root else prefix + ("   " if is_last else "│  ")
        children = node["children"]
        for position in range(len(children) - 1, -1, -1):
            stack.append(
                (
                    children[position],
                    child_prefix,
                    False,
                    position == len(children) - 1,
                )
            )
