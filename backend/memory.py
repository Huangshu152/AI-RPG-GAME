"""Memory V0.1：结构化长期记忆（Memory Service）。

## 三种信息必须分开

| 种类 | 是什么 | 权威性 |
| --- | --- | --- |
| **World State** | 当前真实状态（`Player HP = 8`） | **最高** |
| **Event Log** | 实际发生过什么（`玩家被狼人攻击，受到 4 点伤害`） | 中 |
| **Memory** | 为未来剧情保留的长期信息（`玩家曾被狼人重伤，因此对狼人保持警惕`） | 最低 |

冲突时永远 **World State > Event > Memory**。Memory **不能反向修改游戏世界**。

## 什么才配进 Memory

判断标准：**能不能从当前 World State 直接推导出来？**

- `item_moved`（背包里能读到）→ **不记**，那是 World State
- `npc_status`（当前状态能读到）→ **不记**
- `time_advance`（没有任何长期价值）→ **不记**
- `move` 到**首次**到达的地点 → **记**。因为"去过哪里"是历史，从当前状态根本读不出来

宁可少记，也不要把 World State 抄一遍。V0.3 只有这一条自动规则。

## 谁可以写

只有本模块。`commands.py` / `ai/provider.py` / 前端都不允许碰 `memories` 表，
架构测试盯着这一点。任何写入都必须走 `create()`。

## 本阶段刻意不做

不做 RAG、不用 Embedding、不做向量检索；也**不让 LLM 自动总结或决定**记什么、
改重要度、删记忆。那些留给下一阶段的 Memory Extractor。
"""

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config, repository
from .models import Memory


class MemoryError(Exception):
    """记忆不合法（重要度/scope/category/content 校验失败）。"""


# ------------------------------------------------------------------
# 校验
# ------------------------------------------------------------------
def _clean_subjects(subjects: Optional[Iterable[Any]]) -> List[str]:
    if not subjects:
        return []
    result: List[str] = []
    for item in subjects:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return result


def validate_scope(scope: str) -> str:
    scope = (scope or "").strip()
    if scope in config.MEMORY_SCOPES:
        return scope
    # npc:<名字> —— 用名字，不用数据库 id
    if scope.startswith(config.MEMORY_NPC_SCOPE_PREFIX):
        name = scope[len(config.MEMORY_NPC_SCOPE_PREFIX):].strip()
        if name:
            return config.MEMORY_NPC_SCOPE_PREFIX + name
    raise MemoryError(
        "非法 scope：%r（允许 %s，或 npc:<名字>）"
        % (scope, "、".join(config.MEMORY_SCOPES))
    )


def validate_importance(importance: str) -> str:
    """重要度只允许 S/A/B/C。

    单字母枚举**不分大小写**：`"s"` 会归一化成 `"S"`（字母大小写在这里没有语义，
    拒绝它只会让调用方困惑）。其它取"词"的字段（category / knowledge_scope）仍然严格。
    """
    value = (importance or "").strip().upper()
    if value not in config.MEMORY_IMPORTANCE_LEVELS:
        raise MemoryError(
            "非法 importance：%r（只允许 %s）"
            % (importance, "/".join(config.MEMORY_IMPORTANCE_LEVELS))
        )
    return value


def validate_knowledge_scope(scope: str) -> str:
    value = (scope or "").strip()
    if value not in config.MEMORY_KNOWLEDGE_SCOPES:
        raise MemoryError(
            "非法 knowledge_scope：%r（只允许 %s）"
            % (scope, "/".join(config.MEMORY_KNOWLEDGE_SCOPES))
        )
    return value


def validate_category(category: str) -> str:
    value = (category or "").strip()
    if value not in config.MEMORY_CATEGORIES:
        raise MemoryError(
            "非法 category：%r（只允许 %s）" % (category, "/".join(config.MEMORY_CATEGORIES))
        )
    return value


def validate_content(content: str) -> str:
    if content is None:
        raise MemoryError("content 必须存在")
    text = str(content).strip()
    if not text:
        raise MemoryError("content 不能为空")
    if len(text) > config.MEMORY_MAX_CONTENT:
        raise MemoryError(
            "content 太长（%d > %d）" % (len(text), config.MEMORY_MAX_CONTENT)
        )
    return text


# ------------------------------------------------------------------
# Memory Service API：create / get / query / deactivate
# ------------------------------------------------------------------
def create(
    conn: sqlite3.Connection,
    world_id: int,
    content: str,
    importance: str,
    category: str = "general",
    scope: str = "player",
    knowledge_scope: str = config.MEMORY_AI_KNOWLEDGE_SCOPE,
    subjects: Optional[Iterable[Any]] = None,
    source_event_id: Optional[int] = None,
    created_turn: int = 0,
) -> Memory:
    """创建一条记忆。**这是唯一的写入口。**

    `knowledge_scope` 默认 `player`（玩家知道的事），这样它才会进 AIContext；
    想记"世界真相但玩家不知道"的事就传 `secret` / `world`，它**不会**发给 AI。
    """
    cleaned = {
        "content": validate_content(content),
        "importance": validate_importance(importance),
        "category": validate_category(category),
        "scope": validate_scope(scope),
        "knowledge_scope": validate_knowledge_scope(knowledge_scope),
        "subjects": _clean_subjects(subjects),
    }
    memory_id = repository.insert_memory(
        conn,
        world_id=world_id,
        scope=cleaned["scope"],
        knowledge_scope=cleaned["knowledge_scope"],
        importance=cleaned["importance"],
        category=cleaned["category"],
        content=cleaned["content"],
        subjects=cleaned["subjects"],
        source_event_id=int(source_event_id) if source_event_id is not None else None,
        created_turn=int(created_turn),
    )
    memory = repository.get_memory(conn, memory_id)
    if memory is None:  # pragma: no cover - 刚插入就读不到属于数据库异常
        raise MemoryError("创建记忆后读不回来（数据库异常）")
    return memory


def get(conn: sqlite3.Connection, memory_id: int) -> Optional[Memory]:
    return repository.get_memory(conn, memory_id)


def query(
    conn: sqlite3.Connection,
    world_id: int,
    scopes: Optional[Sequence[str]] = None,
    subjects: Optional[Sequence[str]] = None,
    importance: Optional[Sequence[str]] = None,
    knowledge_scopes: Optional[Sequence[str]] = None,
    include_inactive: bool = False,
) -> List[Memory]:
    """确定性查询（**不做语义搜索**）。所有过滤条件都是精确匹配。"""
    rows = repository.list_memories(conn, world_id, active_only=not include_inactive)
    result: List[Memory] = []
    for memory in rows:
        if scopes and memory.scope not in scopes:
            continue
        if importance and memory.importance not in importance:
            continue
        if knowledge_scopes and memory.knowledge_scope not in knowledge_scopes:
            continue
        if subjects and not set(subjects) & set(memory.subjects):
            continue
        result.append(memory)
    return result


def deactivate(conn: sqlite3.Connection, memory_id: int) -> bool:
    """软删除。返回是否命中。V0.3 不做自动清理，只提供这个入口。"""
    memory = repository.get_memory(conn, memory_id)
    if memory is None:
        return False
    repository.set_memory_active(conn, memory_id, False)
    return True


# ------------------------------------------------------------------
# 事件 → 记忆（确定性规则，不经 LLM）
# ------------------------------------------------------------------
_FIRST_VISIT_IMPORTANCE = "B"
_FIRST_VISIT_CATEGORY = "discovery"


def _has_first_visit(conn: sqlite3.Connection, world_id: int, location_name: str) -> bool:
    for memory in repository.list_memories(conn, world_id, active_only=True):
        if memory.category == _FIRST_VISIT_CATEGORY and location_name in memory.subjects:
            return True
    return False


def ingest_events(
    conn: sqlite3.Connection,
    world_id: int,
    turn: int,
    event_rows: Sequence[Tuple[int, Dict[str, Any]]],
) -> List[Memory]:
    """把一回合的结构化事件翻译成记忆。

    `event_rows` 是 `[(event_id, event_dict), ...]`，event_id 会作为
    `source_event_id` 记下来，方便追溯"这条长期记忆来自哪次真实事件"。

    **大部分事件不会产生记忆**——这是刻意的，见模块开头的判断标准。
    """
    created: List[Memory] = []
    for event_id, event in event_rows:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "move":
            created.extend(_from_move(conn, world_id, turn, event_id, event))
    return created


def _from_move(
    conn: sqlite3.Connection, world_id: int, turn: int, event_id: int, event: Dict[str, Any]
) -> List[Memory]:
    """首次到达某个地点 → B 级 discovery 记忆。

    为什么这条值得记：当前位置是 World State，但"去过哪里"是历史，
    从当前状态推导不出来。
    """
    to_location = event.get("to_location")
    if to_location is None:
        return []
    location = repository.get_location(conn, int(to_location))
    if location is None:
        return []
    if _has_first_visit(conn, world_id, location.name):
        return []
    return [
        create(
            conn,
            world_id,
            content="玩家第一次到达「%s」。" % location.name,
            importance=_FIRST_VISIT_IMPORTANCE,
            category=_FIRST_VISIT_CATEGORY,
            scope="player",
            knowledge_scope=config.MEMORY_AI_KNOWLEDGE_SCOPE,
            subjects=[location.name],
            source_event_id=event_id,
            created_turn=turn,
        )
    ]


# ------------------------------------------------------------------
# 查询 → AIContext
# ------------------------------------------------------------------
def select_for_context(
    conn: sqlite3.Connection,
    world_id: int,
    limit: int = config.MEMORY_CONTEXT_BUDGET,
) -> List[Memory]:
    """挑出要给 AI 的记忆。**确定性排序**，无相似度、无模型参与。

    规则：
        1. 只给"玩家有资格知道"的（knowledge_scope == player）——防剧透
        2. C 级默认不进上下文
        3. 排序：S → A → B；同级按最近回合在前；再同则按 id 倒序（保证裁剪稳定）
        4. 截断到 limit（默认 20），不允许无限增长
    """
    rows = repository.list_memories(conn, world_id, active_only=True)
    visible = [m for m in rows if m.knowledge_scope == config.MEMORY_AI_KNOWLEDGE_SCOPE]
    ranked = [m for m in visible if m.importance in config.MEMORY_CONTEXT_MIN_IMPORTANCE]
    ranked.sort(
        key=lambda m: (
            config.MEMORY_IMPORTANCE_ORDER.get(m.importance, 99),
            -m.created_turn,
            -m.id,
        )
    )
    return ranked[: max(0, int(limit))]


def to_ai_dict(memory: Memory) -> Dict[str, Any]:
    """给 AI 的形状。**刻意剔除一切数据库内部 ID**（id / world_id / source_event_id）。"""
    return {
        "importance": memory.importance,
        "category": memory.category,
        "scope": memory.scope,
        "subjects": list(memory.subjects),
        "content": memory.content,
        "created_turn": memory.created_turn,
    }


def build_context_list(
    conn: sqlite3.Connection,
    world_id: int,
    limit: int = config.MEMORY_CONTEXT_BUDGET,
) -> List[Dict[str, Any]]:
    return [to_ai_dict(m) for m in select_for_context(conn, world_id, limit)]


# ------------------------------------------------------------------
# 存档支持
# ------------------------------------------------------------------
# 连快照的读写也走这里：**除 repository.py（SQL 层）之外，只有 memory.py 碰 memories 表**。
def snapshot_rows(conn: sqlite3.Connection, world_id: int) -> List[Dict[str, Any]]:
    """给存档快照用的原始行（含 id，用于按原 id 回滚）。"""
    return repository.list_memories_raw(conn, world_id)


def reconcile_snapshot(
    conn: sqlite3.Connection, world_id: int, rows: Sequence[Dict[str, Any]]
) -> Dict[str, int]:
    """按快照把记忆对齐：删掉快照里没有的，补回快照里有而当前缺的。

    这是"禁止时间线污染"的落点——`/load` 之后不允许存在"未来才产生的记忆"。
    """
    keep = {int(row["id"]) for row in rows}
    removed = 0
    for memory_id in repository.list_memory_ids(conn, world_id):
        if memory_id not in keep:
            repository.delete_memory(conn, memory_id)
            removed += 1
    restored = 0
    for row in rows:
        if repository.get_memory(conn, int(row["id"])) is None:
            repository.restore_memory(conn, row)
            restored += 1
    return {"removed": removed, "restored": restored}
