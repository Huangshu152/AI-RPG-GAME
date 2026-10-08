"""游戏状态聚合层。

职责：
- 把分散在数据库里的世界/玩家/地点/NPC/物品组装成一份完整的 GameState（给前端用）。
- 生成与还原存档快照（/save 与 /load 的语义在这里）——**快照是权威的**。
- 记录回合历史（叙事 + 结构化事件）。
- 用「状态指纹差分」产出结构化事件，这样事实不可能和实际状态漂移。

不负责：SQL 细节（repository）、指令解析与措辞（commands）。
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import config, memory, repository, world_data
from .models import Exit, GameData, Item, Location, NPC, Player, World, format_world_time


@dataclass
class GameContext:
    """一次指令执行所需的上下文。"""

    conn: sqlite3.Connection
    world: World
    player: Player
    location: Optional[Location]


# ============================================================
# 读取当前游戏
# ============================================================
def get_context(conn: sqlite3.Connection) -> Optional[GameContext]:
    """读取 game_session 指向的当前游戏；没有进行中的游戏则返回 None。"""
    session = repository.get_session(conn)
    if not session or session.get("world_id") is None or session.get("player_id") is None:
        return None

    world = repository.get_world(conn, session["world_id"])
    player = repository.get_player(conn, session["player_id"])
    if world is None or player is None:
        return None

    location = None
    if world.current_location_id is not None:
        location = repository.get_location(conn, world.current_location_id)
    return GameContext(conn=conn, world=world, player=player, location=location)


def get_game_data(conn: sqlite3.Connection) -> Optional[GameData]:
    ctx = get_context(conn)
    if ctx is None:
        return None
    return GameData(
        world=ctx.world,
        player=ctx.player,
        location=ctx.location,
        npcs=repository.list_npcs(conn, ctx.world.id),
    )


# ============================================================
# 只读查询包装
# ============================================================
# 存在的意义是让上层（commands / 未来的 AI）不必 import repository：
# **读**走这里，**写**走 actions.perform()。
def list_exits(conn: sqlite3.Connection, location_id: int) -> List[Exit]:
    return repository.list_exits(conn, location_id)


def list_items(conn: sqlite3.Connection, owner_kind: str, owner_id: int) -> List[Item]:
    return repository.list_items(conn, owner_kind, owner_id)


def list_npcs_at(conn: sqlite3.Connection, location_id: int) -> List[NPC]:
    return repository.list_npcs_at(conn, location_id)


# ============================================================
# 组装给前端的 GameState
# ============================================================
def _item_to_dict(item) -> Dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "quantity": item.quantity,
    }


def _empty_state() -> Dict[str, Any]:
    return {
        "has_game": False,
        "world": None,
        "player": None,
        "location": None,
        "npcs": [],
    }


def history_for_api(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """给前端的日志（只要给人读的行，不要结构化事件行）。"""
    rows = repository.list_events(
        conn, world_id, limit=config.HISTORY_LIMIT, narrative_only=True
    )
    return [{"seq": r.seq, "kind": r.kind, "text": r.text} for r in rows]


def build_state(conn: sqlite3.Connection, include_history: bool = False) -> Dict[str, Any]:
    """生成前端需要的完整状态。没有进行中的游戏时返回 has_game=False。

    include_history 只在 GET /api/state 时为 True：指令响应不需要背着整份历史。
    """
    data = get_game_data(conn)
    if data is None:
        state = _empty_state()
        if include_history:
            state["history"] = []
        return state

    world = data.world
    player = data.player
    location = data.location

    player_items = repository.list_items(conn, config.OWNER_PLAYER, player.id)

    location_dict = None
    if location is not None:
        exits = repository.list_exits(conn, location.id)
        location_items = repository.list_items(conn, config.OWNER_LOCATION, location.id)
        location_dict = {
            "id": location.id,
            "name": location.name,
            "description": location.description,
            "exits": [
                {"label": e.label, "target_id": e.target_id, "target_name": e.target_name}
                for e in exits
            ],
            "items": [_item_to_dict(i) for i in location_items],
        }

    state = {
        "has_game": True,
        "world": {
            "id": world.id,
            "name": world.name,
            "time": format_world_time(world.current_time),
            "time_minutes": world.current_time,
        },
        "player": {
            "id": player.id,
            "name": player.name,
            "hp": player.hp,
            "max_hp": player.max_hp,
            "san": player.san,
            "max_san": player.max_san,
            "items": [_item_to_dict(i) for i in player_items],
        },
        "location": location_dict,
        "npcs": [
            {
                "id": n.id,
                "name": n.name,
                "status": n.status,
                "location_id": n.location_id,
                "location_name": n.location_name,
                "here": n.location_id == (location.id if location else None),
            }
            for n in data.npcs
        ],
    }
    if include_history:
        state["history"] = history_for_api(conn, world.id)
    return state


# ============================================================
# 状态指纹与差分 —— 结构化事件的来源
# ============================================================
def capture_fingerprint(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """把"可变状态"压成一个小字典，用于前后对比。

    事件由差分得出，而不是让每个 handler 手写：手写的事件会和真实状态漂移，
    差分出来的不会——它按定义就是"这一回合实际变了什么"。
    """
    ctx = get_context(conn)
    if ctx is None:
        return None
    return {
        "world_id": ctx.world.id,
        "player_id": ctx.player.id,
        "location_id": ctx.world.current_location_id,
        "time": ctx.world.current_time,
        "name": ctx.player.name,
        "hp": ctx.player.hp,
        "max_hp": ctx.player.max_hp,
        "san": ctx.player.san,
        "max_san": ctx.player.max_san,
        "items": {
            int(r["id"]): (r["owner_kind"], r["owner_id"], int(r["quantity"]))
            for r in repository.list_items_raw(conn, ctx.world.id)
        },
        "npcs": {
            int(r["id"]): (r["location_id"], r["status"])
            for r in repository.list_npcs_raw(conn, ctx.world.id)
        },
    }


def diff_fingerprints(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """把两次指纹的差异转成结构化事件列表。"""
    if before is None or after is None:
        return []
    if before["world_id"] != after["world_id"]:
        return [
            {
                "type": "session_switch",
                "from_world": before["world_id"],
                "to_world": after["world_id"],
                "to_player": after["player_id"],
            }
        ]

    events: List[Dict[str, Any]] = []

    if before["location_id"] != after["location_id"]:
        events.append(
            {
                "type": "move",
                "from_location": before["location_id"],
                "to_location": after["location_id"],
            }
        )
    if before["time"] != after["time"]:
        events.append(
            {
                "type": "time_advance",
                "from": before["time"],
                "to": after["time"],
                "minutes": after["time"] - before["time"],
            }
        )
    for field in ("hp", "max_hp", "san", "max_san", "name"):
        if before[field] != after[field]:
            events.append(
                {
                    "type": "player_change",
                    "field": field,
                    "from": before[field],
                    "to": after[field],
                }
            )

    for item_id, (kind, owner, qty) in sorted(after["items"].items()):
        if item_id not in before["items"]:
            events.append(
                {
                    "type": "item_appear",
                    "item_id": item_id,
                    "owner_kind": kind,
                    "owner_id": owner,
                    "quantity": qty,
                }
            )
            continue
        b_kind, b_owner, b_qty = before["items"][item_id]
        if (b_kind, b_owner) != (kind, owner):
            events.append(
                {
                    "type": "item_moved",
                    "item_id": item_id,
                    "from_kind": b_kind,
                    "from_id": b_owner,
                    "to_kind": kind,
                    "to_id": owner,
                }
            )
        if b_qty != qty:
            events.append(
                {"type": "item_quantity", "item_id": item_id, "from": b_qty, "to": qty}
            )
    for item_id in sorted(before["items"]):
        if item_id not in after["items"]:
            events.append({"type": "item_removed", "item_id": item_id})

    for npc_id, (loc, status) in sorted(after["npcs"].items()):
        if npc_id not in before["npcs"]:
            events.append(
                {"type": "npc_appear", "npc_id": npc_id, "location_id": loc, "status": status}
            )
            continue
        b_loc, b_status = before["npcs"][npc_id]
        if b_loc != loc:
            events.append(
                {"type": "npc_moved", "npc_id": npc_id, "from_location": b_loc, "to_location": loc}
            )
        if b_status != status:
            events.append(
                {"type": "npc_status", "npc_id": npc_id, "from": b_status, "to": status}
            )
    for npc_id in sorted(before["npcs"]):
        if npc_id not in after["npcs"]:
            events.append({"type": "npc_removed", "npc_id": npc_id})

    return events


# ============================================================
# 历史记录
# ============================================================
def record_turn(
    conn: sqlite3.Connection,
    world_id: int,
    messages: List[Dict[str, str]],
    events: List[Dict[str, Any]],
) -> int:
    """把一个回合写进历史：先写结构化事件行，再写给人读的日志行，共用同一个 seq。

    这是**所有回合的唯一落库点**（玩家敲指令、AI 回合都经过这里），
    所以「事件 → 记忆」也挂在这里：一次真实事件之后，由 Memory Service
    按确定性规则决定要不要留下长期记忆。
    """
    seq = repository.next_event_seq(conn, world_id)
    event_rows: List[Tuple[int, Dict[str, Any]]] = []
    for event in events:
        event_id = repository.insert_event(
            conn,
            world_id,
            seq,
            config.KIND_EVENT,
            "",
            json.dumps(event, ensure_ascii=False),
        )
        event_rows.append((event_id, event))
    for message in messages:
        repository.insert_event(
            conn,
            world_id,
            seq,
            message.get("kind", config.KIND_NARRATION),
            message.get("text", ""),
        )
    # 结构化事件 → 长期记忆。**确定性规则，不经 LLM**，也不产生新事件（不会递归）。
    memory.ingest_events(conn, world_id, seq, event_rows)
    return seq


# ============================================================
# 状态变更的边界
# ============================================================
# 本模块**不提供任何 Gameplay 变更函数**。玩家/AI 在世界里做的动作
# （move / take / rest / 将来的 attack、talk）全部在 `backend/actions.py`
# 的 Action Engine 里，只能通过 actions.perform() 到达。
#
# 本模块负责的是另一类东西：**系统级操作**——它们不是"在世界里做的动作"：
#   - new_game()      创建一个新的世界实例（世界生命周期）
#   - save_game()     把快照写进存档位（不改变游戏世界状态）
#   - load_game()     受控的状态恢复入口（整体回滚）
#
# 加上上面的读取 / 差分 / 历史，本模块就是"状态与存档"的唯一归口。
# 两条入口的区分是刻意的，见 docs/ARCHITECTURE.md：把存档操作伪装成 Gameplay Action
# 只会让"玩家做了什么"和"系统把存档写回去了"混成一个概念。


# ============================================================
# 存档快照
# ============================================================
def capture_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    """把当前游戏的可变状态 + 叙事历史打包成 JSON 可序列化的快照。"""
    data = get_game_data(conn)
    if data is None:
        raise ValueError("没有进行中的游戏")

    world, player = data.world, data.player
    return {
        "version": config.SAVE_FORMAT_VERSION,
        "world": {
            "id": world.id,
            "name": world.name,
            "current_time": world.current_time,
            "current_location_id": world.current_location_id,
        },
        "player": {
            "id": player.id,
            "name": player.name,
            "hp": player.hp,
            "max_hp": player.max_hp,
            "san": player.san,
            "max_san": player.max_san,
        },
        "items": repository.list_items_raw(conn, world.id),
        "npcs": repository.list_npcs_raw(conn, world.id),
        "events": repository.list_events_raw(conn, world.id),
        # 记忆必须进快照，否则 /load 之后会出现"世界回滚了、记忆停在未来"。
        # 走 Memory Service，不直接碰 memories 表。
        "memories": memory.snapshot_rows(conn, world.id),
    }


def _upgrade_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """把旧版本快照升级到当前格式。

    v2 → v3：v2 快照产生于 Memory 功能存在**之前**，所以"那个时刻没有任何记忆"
    是事实、不是猜测，补一个空列表即可。这样老存档仍然能载入，
    也不会把记忆错误地留在未来。
    """
    if snapshot.get("version") == 2:
        upgraded = dict(snapshot)
        upgraded["memories"] = []
        upgraded["version"] = 3
        return upgraded
    return snapshot


def restore_snapshot(conn: sqlite3.Connection, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """把快照写回数据库，返回回滚摘要。

    快照是**权威**的：该世界实例里"快照没提到的东西"会被删除，
    "快照提到但当前缺失的"会被补回来。否则 /load 会得到一个从未存在过的混合状态
    （旧的丢了、新的还在），这在 AI 能生成物品/NPC 之后会立刻变成存档漏洞。

    Memory 也遵循同一条规则：世界、历史、记忆三者必须一起回到同一个历史时刻。
    """
    snapshot = _upgrade_snapshot(snapshot)
    version = snapshot.get("version")
    if version != config.SAVE_FORMAT_VERSION:
        raise ValueError(
            "存档格式版本不匹配（存档 %s，当前 %s）" % (version, config.SAVE_FORMAT_VERSION)
        )
    world_snap = snapshot.get("world") or {}
    player_snap = snapshot.get("player") or {}
    world_id = world_snap.get("id")
    player_id = player_snap.get("id")

    if world_id is None or player_id is None:
        raise ValueError("存档内容不完整")

    world = repository.get_world(conn, world_id)
    if world is None:
        raise ValueError("存档对应的世界已不存在")
    player = repository.get_player(conn, player_id)
    if player is None:
        raise ValueError("存档对应的玩家已不存在")

    location_id = world_snap.get("current_location_id")
    if location_id is not None and repository.get_location(conn, location_id) is None:
        raise ValueError("存档中的地点已不存在")

    repository.restore_world(
        conn,
        world_id,
        world_snap.get("name", world.name),
        int(world_snap.get("current_time", 0)),
        location_id,
    )
    repository.restore_player(
        conn,
        player_id,
        player_snap.get("name", player.name),
        int(player_snap.get("hp", player.hp)),
        int(player_snap.get("max_hp", player.max_hp)),
        int(player_snap.get("san", player.san)),
        int(player_snap.get("max_san", player.max_san)),
    )

    summary: Dict[str, Any] = {"world_id": int(world_id), "player_id": int(player_id)}

    # 物品：先删掉快照里没有的，再补回快照里有而当前缺的，最后更新归属/数量
    snap_items = snapshot.get("items") or []
    keep_items = {int(i["id"]) for i in snap_items}
    summary["items_removed"] = _delete_missing(conn, repository.list_item_ids, repository.delete_item, world_id, keep_items)
    summary["items_restored"] = 0
    for item in snap_items:
        item_id = int(item["id"])
        if repository.get_item(conn, item_id) is None:
            repository.restore_item(conn, item)
            summary["items_restored"] += 1
        else:
            repository.apply_item_state(
                conn,
                item_id,
                item.get("owner_kind", config.OWNER_LOCATION),
                item.get("owner_id"),
                int(item.get("quantity", 1)),
            )

    # NPC：同上
    snap_npcs = snapshot.get("npcs") or []
    keep_npcs = {int(n["id"]) for n in snap_npcs}
    summary["npcs_removed"] = _delete_missing(conn, repository.list_npc_ids, repository.delete_npc, world_id, keep_npcs)
    summary["npcs_restored"] = 0
    for npc in snap_npcs:
        npc_id = int(npc["id"])
        if repository.get_npc(conn, npc_id) is None:
            repository.restore_npc(conn, npc)
            summary["npcs_restored"] += 1
        else:
            repository.apply_npc_state(
                conn, npc_id, npc.get("location_id"), npc.get("status", "")
            )

    # 历史：剧情和状态一起回滚，否则载入后"状态是过去的、对话是未来的"
    snap_events = snapshot.get("events") or []
    keep_events = {int(e["id"]) for e in snap_events}
    summary["events_removed"] = _delete_missing(conn, repository.list_event_ids, repository.delete_event, world_id, keep_events)
    summary["events_restored"] = 0
    for event in snap_events:
        event_id = int(event["id"])
        if repository.get_event(conn, event_id) is None:
            repository.restore_event(conn, event)
            summary["events_restored"] += 1

    # 记忆：和物品/NPC/历史一样以快照为准，走 Memory Service。
    # 少了这一步就会出现"World rollback ✅ / Memory 留在未来 ❌"的时间线污染。
    mem_summary = memory.reconcile_snapshot(conn, world_id, snapshot.get("memories") or [])
    summary["memories_removed"] = mem_summary["removed"]
    summary["memories_restored"] = mem_summary["restored"]

    repository.set_session(conn, world_id, player_id)
    return summary


def _delete_missing(conn, id_lister, deleter, world_id: int, keep_ids: set) -> int:
    """删除该世界里不在 keep_ids 中的行，返回删除数量。"""
    removed = 0
    for row_id in id_lister(conn, world_id):
        if row_id not in keep_ids:
            deleter(conn, row_id)
            removed += 1
    return removed


def dumps(snapshot: Dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False)


def loads(text: str) -> Dict[str, Any]:
    return json.loads(text)


# ============================================================
# 系统级操作：世界生命周期与存档
# ============================================================
# 这些不是 Gameplay Action，因此不进 actions.ACTIONS。
# 但它们同样是**受控入口**：不许在别处散落地直接改数据库。
def new_game(conn: sqlite3.Connection, player_name: str) -> Tuple[int, int]:
    """创建一个新的世界实例与新玩家，并设为当前游戏。返回 (world_id, player_id)。"""
    world_id, player_id = world_data.seed_test_world(conn, player_name)
    repository.set_session(conn, world_id, player_id)
    return world_id, player_id


def find_save(
    conn: sqlite3.Connection, ctx: Optional[GameContext], slot: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """按存档位找存档。

    保留了原有的跨世界回退：显式指定存档位时会去别的世界找同名的；
    不指定时就取当前世界最新的那一份。
    """
    slot = (slot or "").strip() or None
    current_world = ctx.world.id if ctx else None

    save = repository.get_latest_save(conn, world_id=current_world, slot=slot)
    if save is None and slot is not None:
        save = repository.get_latest_save(conn, world_id=None, slot=slot)
    if save is None and slot is None and current_world is not None:
        save = repository.get_latest_save(conn, world_id=None, slot=None)
    return save


def save_game(conn: sqlite3.Connection, ctx: GameContext, slot: str) -> Dict[str, Any]:
    """把当前状态写成存档快照。**不改变游戏世界状态。**"""
    snapshot = capture_snapshot(conn)
    repository.upsert_save(
        conn,
        world_id=ctx.world.id,
        player_id=ctx.player.id,
        slot=slot,
        label="%s / %s" % (ctx.world.name, ctx.player.name),
        snapshot=dumps(snapshot),
    )
    return repository.get_latest_save(conn, world_id=ctx.world.id, slot=slot) or {}


def load_game(
    conn: sqlite3.Connection, ctx: Optional[GameContext], slot: Optional[str] = None
) -> Dict[str, Any]:
    """受控的状态恢复入口：找到存档并整体还原。

    返回 {"save", "summary", "restored"}。找不到存档或存档损坏时抛 ValueError。
    """
    save = find_save(conn, ctx, slot)
    if save is None:
        raise ValueError("没有找到可载入的存档，请先用 /save 保存一次。")
    snapshot = loads(save["snapshot"])
    summary = restore_snapshot(conn, snapshot)
    return {"save": save, "summary": summary, "restored": get_game_data(conn)}
