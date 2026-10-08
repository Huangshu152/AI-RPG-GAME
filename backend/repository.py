"""数据访问层：全项目唯一写 SQL 的地方。

约定：
- 所有函数第一个参数是 conn（sqlite3.Connection），由调用方控制事务。
- 返回领域模型（models.py 里的 dataclass）或 dict，绝不返回 sqlite3.Row 给上层。
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config
from .models import Exit, GameEvent, Item, Location, Memory, NPC, Player, World


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# World
# ============================================================
def _row_to_world(row: sqlite3.Row) -> World:
    return World(
        id=row["id"],
        name=row["name"],
        current_time=row["current_time"],
        current_location_id=row["current_location_id"],
    )


def insert_world(conn: sqlite3.Connection, name: str, current_time: int) -> int:
    cur = conn.execute(
        "INSERT INTO worlds (name, current_time, current_location_id) VALUES (?, ?, NULL)",
        (name, current_time),
    )
    return int(cur.lastrowid)


def get_world(conn: sqlite3.Connection, world_id: int) -> Optional[World]:
    row = conn.execute("SELECT * FROM worlds WHERE id = ?", (world_id,)).fetchone()
    return _row_to_world(row) if row else None


def update_world_time(conn: sqlite3.Connection, world_id: int, current_time: int) -> None:
    conn.execute("UPDATE worlds SET current_time = ? WHERE id = ?", (current_time, world_id))


def update_world_location(conn: sqlite3.Connection, world_id: int, location_id: int) -> None:
    conn.execute("UPDATE worlds SET current_location_id = ? WHERE id = ?", (location_id, world_id))


def restore_world(
    conn: sqlite3.Connection,
    world_id: int,
    name: str,
    current_time: int,
    current_location_id: Optional[int],
) -> None:
    """按快照把世界行整体写回。"""
    conn.execute(
        "UPDATE worlds SET name = ?, current_time = ?, current_location_id = ? WHERE id = ?",
        (name, current_time, current_location_id, world_id),
    )


# ============================================================
# Location / Exit
# ============================================================
def insert_location(conn: sqlite3.Connection, world_id: int, name: str, description: str) -> int:
    cur = conn.execute(
        "INSERT INTO locations (world_id, name, description) VALUES (?, ?, ?)",
        (world_id, name, description),
    )
    return int(cur.lastrowid)


def get_location(conn: sqlite3.Connection, location_id: int) -> Optional[Location]:
    row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if not row:
        return None
    return Location(id=row["id"], name=row["name"], description=row["description"])


def insert_exit(
    conn: sqlite3.Connection, world_id: int, from_id: int, to_id: int, label: str
) -> int:
    cur = conn.execute(
        "INSERT INTO exits (world_id, from_location_id, to_location_id, label) VALUES (?, ?, ?, ?)",
        (world_id, from_id, to_id, label),
    )
    return int(cur.lastrowid)


def list_exits(conn: sqlite3.Connection, from_location_id: int) -> List[Exit]:
    rows = conn.execute(
        """
        SELECT e.label AS label, e.to_location_id AS target_id, l.name AS target_name
        FROM exits e
        JOIN locations l ON l.id = e.to_location_id
        WHERE e.from_location_id = ?
        ORDER BY e.id
        """,
        (from_location_id,),
    ).fetchall()
    return [Exit(label=r["label"], target_id=r["target_id"], target_name=r["target_name"]) for r in rows]


def find_exit(conn: sqlite3.Connection, from_location_id: int, text: str) -> Optional[Exit]:
    """按标签或目标地点名匹配出口（先精确、再包含）。

    多个候选时报歧义错误，而不是静默取第一个。
    """
    text = (text or "").strip()
    if not text:
        return None
    exits = list_exits(conn, from_location_id)
    for ex in exits:
        if ex.label == text or ex.target_name == text:
            return ex
    low = text.lower()
    hits = [ex for ex in exits if low in ex.label.lower() or low in ex.target_name.lower()]
    if len(hits) > 1:
        raise AmbiguousMatch(text, [ex.label for ex in hits])
    return hits[0] if hits else None


class AmbiguousMatch(Exception):
    """名称匹配到多个候选。让调用方提示玩家说得更具体，而不是随便挑一个。"""

    def __init__(self, text: str, candidates: List[str]):
        super().__init__("「%s」匹配到多个候选：%s" % (text, "、".join(candidates)))
        self.text = text
        self.candidates = candidates


# ============================================================
# Player
# ============================================================
def _row_to_player(row: sqlite3.Row) -> Player:
    return Player(
        id=row["id"],
        name=row["name"],
        hp=row["hp"],
        max_hp=row["max_hp"],
        san=row["san"],
        max_san=row["max_san"],
    )


def insert_player(
    conn: sqlite3.Connection,
    world_id: int,
    name: str,
    hp: int,
    max_hp: int,
    san: int,
    max_san: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO players (world_id, name, hp, max_hp, san, max_san)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (world_id, name, hp, max_hp, san, max_san),
    )
    return int(cur.lastrowid)


def get_player(conn: sqlite3.Connection, player_id: int) -> Optional[Player]:
    row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    return _row_to_player(row) if row else None


def update_player_vitals(
    conn: sqlite3.Connection, player_id: int, hp: int, san: int, name: Optional[str] = None
) -> None:
    if name is None:
        conn.execute("UPDATE players SET hp = ?, san = ? WHERE id = ?", (hp, san, player_id))
    else:
        conn.execute(
            "UPDATE players SET hp = ?, san = ?, name = ? WHERE id = ?",
            (hp, san, name, player_id),
        )


def restore_player(
    conn: sqlite3.Connection,
    player_id: int,
    name: str,
    hp: int,
    max_hp: int,
    san: int,
    max_san: int,
) -> None:
    """按快照把玩家行整体写回（包含上限——上限也是状态的一部分）。"""
    conn.execute(
        "UPDATE players SET name = ?, hp = ?, max_hp = ?, san = ?, max_san = ? WHERE id = ?",
        (name, hp, max_hp, san, max_san, player_id),
    )


# ============================================================
# Item
# ============================================================
def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        quantity=row["quantity"],
    )


def insert_item(
    conn: sqlite3.Connection,
    world_id: int,
    name: str,
    description: str,
    quantity: int,
    owner_kind: str,
    owner_id: Optional[int],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO items (world_id, name, description, quantity, owner_kind, owner_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (world_id, name, description, quantity, owner_kind, owner_id),
    )
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> Optional[Item]:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def list_items(conn: sqlite3.Connection, owner_kind: str, owner_id: int) -> List[Item]:
    rows = conn.execute(
        "SELECT * FROM items WHERE owner_kind = ? AND owner_id = ? ORDER BY id",
        (owner_kind, owner_id),
    ).fetchall()
    return [_row_to_item(r) for r in rows]


def find_item(conn: sqlite3.Connection, owner_kind: str, owner_id: int, name: str) -> Optional[Item]:
    """按名称查找物品（先精确、再包含）。多个候选时报歧义错误。"""
    name = (name or "").strip()
    if not name:
        return None
    items = list_items(conn, owner_kind, owner_id)
    for it in items:
        if it.name == name:
            return it
    low = name.lower()
    hits = [it for it in items if low in it.name.lower()]
    if len(hits) > 1:
        raise AmbiguousMatch(name, [it.name for it in hits])
    return hits[0] if hits else None


def apply_item_state(
    conn: sqlite3.Connection,
    item_id: int,
    owner_kind: str,
    owner_id: Optional[int],
    quantity: int,
) -> None:
    conn.execute(
        "UPDATE items SET owner_kind = ?, owner_id = ?, quantity = ? WHERE id = ?",
        (owner_kind, owner_id, quantity, item_id),
    )


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


def restore_item(conn: sqlite3.Connection, item: Dict[str, Any]) -> None:
    """按快照插回一行物品，保留原 id（用于回滚"存档时存在、之后被删"的物品）。"""
    conn.execute(
        """
        INSERT INTO items (id, world_id, name, description, quantity, owner_kind, owner_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(item["id"]),
            int(item["world_id"]),
            item.get("name", ""),
            item.get("description", ""),
            int(item.get("quantity", 1)),
            item.get("owner_kind", config.OWNER_LOCATION),
            item.get("owner_id"),
        ),
    )


def list_item_ids(conn: sqlite3.Connection, world_id: int) -> List[int]:
    rows = conn.execute("SELECT id FROM items WHERE world_id = ?", (world_id,)).fetchall()
    return [int(r["id"]) for r in rows]


def list_items_raw(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """存档快照用：带上 name / description，因为快照必须能完整重建一行物品。"""
    rows = conn.execute(
        """
        SELECT id, world_id, name, description, quantity, owner_kind, owner_id
        FROM items WHERE world_id = ? ORDER BY id
        """,
        (world_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# NPC
# ============================================================
def insert_npc(
    conn: sqlite3.Connection, world_id: int, name: str, location_id: int, status: str
) -> int:
    cur = conn.execute(
        "INSERT INTO npcs (world_id, name, location_id, status) VALUES (?, ?, ?, ?)",
        (world_id, name, location_id, status),
    )
    return int(cur.lastrowid)


def _npc_query(where: str) -> str:
    return """
        SELECT n.id, n.name, n.status, n.location_id, COALESCE(l.name, '') AS location_name
        FROM npcs n
        LEFT JOIN locations l ON l.id = n.location_id
        WHERE %s
        ORDER BY n.id
    """ % where


def _row_to_npc(row: sqlite3.Row) -> NPC:
    return NPC(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        location_id=row["location_id"],
        location_name=row["location_name"],
    )


def list_npcs(conn: sqlite3.Connection, world_id: int) -> List[NPC]:
    rows = conn.execute(_npc_query("n.world_id = ?"), (world_id,)).fetchall()
    return [_row_to_npc(r) for r in rows]


def list_npcs_at(conn: sqlite3.Connection, location_id: int) -> List[NPC]:
    rows = conn.execute(_npc_query("n.location_id = ?"), (location_id,)).fetchall()
    return [_row_to_npc(r) for r in rows]


def get_npc(conn: sqlite3.Connection, npc_id: int) -> Optional[NPC]:
    rows = conn.execute(_npc_query("n.id = ?"), (npc_id,)).fetchall()
    return _row_to_npc(rows[0]) if rows else None


def apply_npc_state(
    conn: sqlite3.Connection, npc_id: int, location_id: Optional[int], status: str
) -> None:
    conn.execute(
        "UPDATE npcs SET location_id = ?, status = ? WHERE id = ?",
        (location_id, status, npc_id),
    )


def delete_npc(conn: sqlite3.Connection, npc_id: int) -> None:
    conn.execute("DELETE FROM npcs WHERE id = ?", (npc_id,))


def restore_npc(conn: sqlite3.Connection, npc: Dict[str, Any]) -> None:
    """按快照插回一行 NPC，保留原 id。"""
    conn.execute(
        "INSERT INTO npcs (id, world_id, name, location_id, status) VALUES (?, ?, ?, ?, ?)",
        (
            int(npc["id"]),
            int(npc["world_id"]),
            npc.get("name", ""),
            npc.get("location_id"),
            npc.get("status", ""),
        ),
    )


def list_npc_ids(conn: sqlite3.Connection, world_id: int) -> List[int]:
    rows = conn.execute("SELECT id FROM npcs WHERE world_id = ?", (world_id,)).fetchall()
    return [int(r["id"]) for r in rows]


def list_npcs_raw(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """存档快照用：带上 name，理由同 list_items_raw。"""
    rows = conn.execute(
        "SELECT id, world_id, name, location_id, status FROM npcs WHERE world_id = ? ORDER BY id",
        (world_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Event（叙事历史 + 结构化事件）
# ============================================================
def _row_to_event(row: sqlite3.Row) -> GameEvent:
    return GameEvent(
        id=row["id"],
        world_id=row["world_id"],
        seq=row["seq"],
        kind=row["kind"],
        text=row["text"],
        payload=row["payload"],
        created_at=row["created_at"],
    )


def insert_event(
    conn: sqlite3.Connection,
    world_id: int,
    seq: int,
    kind: str,
    text: str,
    payload: str = "{}",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (world_id, seq, kind, text, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (world_id, seq, kind, text, payload, _now()),
    )
    return int(cur.lastrowid)


def get_event(conn: sqlite3.Connection, event_id: int) -> Optional[GameEvent]:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_event(row) if row else None


def delete_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


def restore_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    """按快照插回一条历史，保留原 id / seq / created_at（历史是只读的，不需要 UPDATE）。"""
    conn.execute(
        """
        INSERT INTO events (id, world_id, seq, kind, text, payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(event["id"]),
            int(event["world_id"]),
            int(event["seq"]),
            event.get("kind", config.KIND_NARRATION),
            event.get("text", ""),
            event.get("payload", "{}"),
            event.get("created_at", _now()),
        ),
    )


def list_events(
    conn: sqlite3.Connection,
    world_id: int,
    limit: Optional[int] = None,
    narrative_only: bool = False,
) -> List[GameEvent]:
    """按时间顺序返回历史。给 limit 时返回**最后** limit 条（仍按时间升序）。"""
    where = "world_id = ?"
    params: List[Any] = [world_id]
    if narrative_only:
        where += " AND kind <> ?"
        params.append(config.KIND_EVENT)
    if limit is None:
        rows = conn.execute(
            "SELECT * FROM events WHERE %s ORDER BY seq, id" % where, params
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM events WHERE %s ORDER BY seq DESC, id DESC LIMIT ?)"
            " ORDER BY seq, id" % where,
            params + [int(limit)],
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def list_events_raw(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """存档快照用：全量历史。"""
    rows = conn.execute(
        """
        SELECT id, world_id, seq, kind, text, payload, created_at
        FROM events WHERE world_id = ? ORDER BY seq, id
        """,
        (world_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_event_ids(conn: sqlite3.Connection, world_id: int) -> List[int]:
    rows = conn.execute("SELECT id FROM events WHERE world_id = ?", (world_id,)).fetchall()
    return [int(r["id"]) for r in rows]


def next_event_seq(conn: sqlite3.Connection, world_id: int) -> int:
    """下一个回合号。用 MAX(seq)+1 推导，避免再给 worlds 加一列（那需要 schema 迁移）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE world_id = ?", (world_id,)
    ).fetchone()
    return int(row["s"]) + 1


# ============================================================
# Memory（结构化长期记忆）
# ============================================================
# 注意：这些函数只应由 backend/memory.py（Memory Service）调用。
# commands.py / ai/* 都不允许直接碰 Memory 表。
def _row_to_memory(row: sqlite3.Row) -> Memory:
    try:
        subjects = json.loads(row["subjects"]) if row["subjects"] else []
    except (json.JSONDecodeError, TypeError):
        subjects = []
    if not isinstance(subjects, list):
        subjects = []
    return Memory(
        id=row["id"],
        world_id=row["world_id"],
        scope=row["scope"],
        knowledge_scope=row["knowledge_scope"],
        importance=row["importance"],
        category=row["category"],
        content=row["content"],
        subjects=[str(s) for s in subjects],
        source_event_id=row["source_event_id"],
        created_turn=row["created_turn"],
        active=bool(row["active"]),
        created_at=row["created_at"],
    )


def insert_memory(
    conn: sqlite3.Connection,
    world_id: int,
    scope: str,
    knowledge_scope: str,
    importance: str,
    category: str,
    content: str,
    subjects: List[str],
    source_event_id: Optional[int],
    created_turn: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO memories (world_id, scope, knowledge_scope, importance, category,
                              content, subjects, source_event_id, created_turn, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            world_id,
            scope,
            knowledge_scope,
            importance,
            category,
            content,
            json.dumps(list(subjects), ensure_ascii=False),
            source_event_id,
            created_turn,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def get_memory(conn: sqlite3.Connection, memory_id: int) -> Optional[Memory]:
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return _row_to_memory(row) if row else None


def list_memories(
    conn: sqlite3.Connection, world_id: int, active_only: bool = False
) -> List[Memory]:
    sql = "SELECT * FROM memories WHERE world_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY id"
    rows = conn.execute(sql, (world_id,)).fetchall()
    return [_row_to_memory(r) for r in rows]


def set_memory_active(conn: sqlite3.Connection, memory_id: int, active: bool) -> None:
    conn.execute(
        "UPDATE memories SET active = ? WHERE id = ?", (1 if active else 0, memory_id)
    )


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))


def list_memory_ids(conn: sqlite3.Connection, world_id: int) -> List[int]:
    rows = conn.execute("SELECT id FROM memories WHERE world_id = ?", (world_id,)).fetchall()
    return [int(r["id"]) for r in rows]


def list_memories_raw(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """存档快照用：完整一行，能按原 id 重建。"""
    rows = conn.execute(
        """
        SELECT id, world_id, scope, knowledge_scope, importance, category, content,
               subjects, source_event_id, created_turn, active, created_at
        FROM memories WHERE world_id = ? ORDER BY id
        """,
        (world_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def restore_memory(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """按快照插回一行记忆，保留原 id（回滚"存档时不存在"的记忆靠删，不是靠这个）。"""
    conn.execute(
        """
        INSERT INTO memories (id, world_id, scope, knowledge_scope, importance, category,
                              content, subjects, source_event_id, created_turn, active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(row["id"]),
            int(row["world_id"]),
            row.get("scope", "player"),
            row.get("knowledge_scope", config.MEMORY_AI_KNOWLEDGE_SCOPE),
            row.get("importance", "C"),
            row.get("category", "general"),
            row.get("content", ""),
            row.get("subjects") or "[]",
            row.get("source_event_id"),
            int(row.get("created_turn", 0)),
            1 if row.get("active", 1) else 0,
            row.get("created_at") or _now(),
        ),
    )


# ============================================================
# Session（当前进行中的游戏）
# ============================================================
def get_session(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM game_session WHERE id = 1").fetchone()
    return dict(row) if row else None


def set_session(conn: sqlite3.Connection, world_id: int, player_id: int) -> None:
    conn.execute(
        """
        INSERT INTO game_session (id, world_id, player_id, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            world_id = excluded.world_id,
            player_id = excluded.player_id,
            updated_at = excluded.updated_at
        """,
        (world_id, player_id, _now()),
    )


# ============================================================
# Save（存档位）
# ============================================================
def upsert_save(
    conn: sqlite3.Connection,
    world_id: int,
    player_id: int,
    slot: str,
    label: str,
    snapshot: str,
) -> int:
    """同一存档位只保留最新一份（经典 slot 语义）。"""
    conn.execute("DELETE FROM saves WHERE world_id = ? AND slot = ?", (world_id, slot))
    cur = conn.execute(
        """
        INSERT INTO saves (world_id, player_id, slot, label, snapshot, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (world_id, player_id, slot, label, snapshot, _now()),
    )
    return int(cur.lastrowid)


def get_latest_save(
    conn: sqlite3.Connection, world_id: Optional[int] = None, slot: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """取最新存档。可限定世界和/或存档位；都不限定则取全局最新。"""
    sql = "SELECT * FROM saves WHERE 1 = 1"
    params: List[Any] = []
    if world_id is not None:
        sql += " AND world_id = ?"
        params.append(world_id)
    if slot is not None:
        sql += " AND slot = ?"
        params.append(slot)
    sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def list_saves(conn: sqlite3.Connection, world_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if world_id is None:
        rows = conn.execute("SELECT * FROM saves ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM saves WHERE world_id = ? ORDER BY created_at DESC, id DESC",
            (world_id,),
        ).fetchall()
    return [dict(r) for r in rows]
