"""统一状态变更层：**Gameplay Action Engine**。

    Intent  →  Validation  →  State Mutation  →  Event

这里只放「玩家 / AI 在**世界里做的动作**」：

    move    去某处
    take    拿走某物
    rest    休息
    （将来）attack / talk / use / search …

**只有这一层可以修改游戏世界状态。** Command（`commands.py`）与将来的 AI / Mock AI
都只能构造 `Action` 交给 `perform()`，不允许 import `repository`，也不允许拿 `conn` 写 SQL。

## 不属于这里的东西

`/save`、`/load`、Snapshot / Rollback 是**系统级存档操作**，不是 Gameplay Action。
它们保留自己的语义与流程，放在 `game_state.py` 的受控入口里
（`new_game()` / `save_game()` / `load_game()`），不在这里伪装成 Action——
理由见 `docs/ARCHITECTURE.md`：为了形式上"全部统一"而抽象存档操作，只会让
"玩家在世界里做了什么"和"系统把存档写回去了"混成同一个概念。

## 事件

结构化事件由引擎在变更前后对「状态指纹」做差分得出，**不由 handler 手写**：
手写的事件会和真实状态漂移，差分出来的按定义就是"这一回合实际变了什么"。

将来的 AI 只需要产出 `Action`：

    Action(type="move", target="阅览室")
    Action(type="take", target="泛黄的笔记")
    Action(type="rest", minutes=60)

`ACTIONS` 注册表就是将来的 tool / function schema 来源（每个 ActionSpec 自带 summary）。
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import config, game_state, repository
from .game_state import GameContext
from .models import format_world_time

Message = Dict[str, str]


def narration(text: str) -> Message:
    return {"kind": config.KIND_NARRATION, "text": text}


def system(text: str) -> Message:
    return {"kind": config.KIND_SYSTEM, "text": text}


def error(text: str) -> Message:
    return {"kind": config.KIND_ERROR, "text": text}


class ActionError(Exception):
    """动作不合法或无法执行。message 给玩家看，hint 是可选补充（如可用出口列表）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Action:
    """一次 Gameplay 意图。字段就是对外的 Action Schema：type / target / arguments。

    `type` 决定语义，`target` 是目标名（**不是数据库 id**），`arguments` 放具名参数。
    """

    type: str
    target: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)

    def argument(self, key: str, default: Any = None) -> Any:
        return self.arguments.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        # 输出时把空 target 还原成 null，和 Action Schema 的示例保持一致
        return {
            "type": self.type,
            "target": self.target or None,
            "arguments": dict(self.arguments),
        }

    # 便捷构造：对应需求里给的 AI 用法示例
    @staticmethod
    def move(target: str) -> "Action":
        return Action("move", target)

    @staticmethod
    def take(target: str) -> "Action":
        return Action("take", target)

    @staticmethod
    def rest(minutes: int = config.REST_MINUTES) -> "Action":
        return Action("rest", arguments={"minutes": minutes})


@dataclass
class ActionResult:
    ok: bool
    action: Action
    messages: List[Message] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""


ValidateFn = Callable[[sqlite3.Connection, Optional[GameContext], Action], None]
ExecuteFn = Callable[[sqlite3.Connection, Optional[GameContext], Action], List[Message]]


@dataclass
class ActionSpec:
    """一个 Gameplay Action。四个阶段里，引擎负责 Intent 分派与 Event，这里负责中间两段。"""

    name: str
    summary: str
    validate: ValidateFn
    execute: ExecuteFn


ACTIONS: Dict[str, ActionSpec] = {}


def register(spec: ActionSpec) -> ActionSpec:
    ACTIONS[spec.name] = spec
    return spec


# ============================================================
# 引擎
# ============================================================
def perform(conn: sqlite3.Connection, ctx: Optional[GameContext], action: Action) -> ActionResult:
    """唯一的 Gameplay 状态变更入口：验证 → 执行 → 产出事件。"""
    spec = ACTIONS.get(action.type)
    if spec is None:
        return ActionResult(
            ok=False,
            action=action,
            messages=[error("未知动作：%s" % action.type)],
            error="未知动作：%s" % action.type,
        )

    # ---- 1. 验证：不合法就完全不碰状态 ----
    try:
        spec.validate(conn, ctx, action)
    except ActionError as exc:
        messages = [error(exc.message)]
        if exc.hint:
            messages.append(system(exc.hint))
        return ActionResult(ok=False, action=action, messages=messages, error=exc.message)

    # ---- 2. 执行（包在 SAVEPOINT 里，失败不留半截状态）----
    before = game_state.capture_fingerprint(conn)
    conn.execute("SAVEPOINT action")
    try:
        messages = spec.execute(conn, ctx, action)
    except ActionError as exc:
        conn.execute("ROLLBACK TO action")
        conn.execute("RELEASE action")
        messages = [error(exc.message)]
        if exc.hint:
            messages.append(system(exc.hint))
        return ActionResult(ok=False, action=action, messages=messages, error=exc.message)
    except Exception:
        conn.execute("ROLLBACK TO action")
        conn.execute("RELEASE action")
        raise
    conn.execute("RELEASE action")

    # ---- 3. 事件：由状态差分得出 ----
    after = game_state.capture_fingerprint(conn)
    events = game_state.diff_fingerprints(before, after)

    return ActionResult(ok=True, action=action, messages=messages, events=events)


def action_catalog() -> List[Dict[str, Any]]:
    """动作清单。将来的 AI Provider 用它生成 tool / function schema。"""
    return [{"type": spec.name, "summary": spec.summary} for spec in ACTIONS.values()]


# ============================================================
# 动作：移动
# ============================================================
def _validate_move(conn, ctx, action):
    if ctx is None:
        raise ActionError("还没有进行中的游戏。")
    if ctx.location is None:
        raise ActionError("当前地点不存在，请重新创建游戏。")
    exits = repository.list_exits(conn, ctx.location.id)
    labels = "、".join(e.label for e in exits) if exits else "（无）"
    if not action.target.strip():
        raise ActionError("用法：/go <出口名>", "可用出口：%s" % labels)
    try:
        target = repository.find_exit(conn, ctx.location.id, action.target)
    except repository.AmbiguousMatch as exc:
        raise ActionError(str(exc), "请说得更具体一些。可用出口：%s" % labels)
    if target is None:
        raise ActionError(
            "这里没有通往「%s」的路。" % action.target.strip(), "可用出口：%s" % labels
        )


def _execute_move(conn, ctx, action):
    target = repository.find_exit(conn, ctx.location.id, action.target)
    repository.update_world_location(conn, ctx.world.id, target.target_id)
    return [narration("你前往%s。" % target.label)]


register(
    ActionSpec(
        name="move",
        summary="移动到相邻地点（target = 出口名或目标地点名）",
        validate=_validate_move,
        execute=_execute_move,
    )
)


# ============================================================
# 动作：拾取
# ============================================================
def _resolve_take_item(conn, ctx, action):
    if ctx is None:
        raise ActionError("还没有进行中的游戏。")
    if ctx.location is None:
        raise ActionError("当前地点不存在，请重新创建游戏。")
    name = action.target.strip()
    if not name:
        raise ActionError("用法：/take <物品名>")
    try:
        item = repository.find_item(conn, config.OWNER_LOCATION, ctx.location.id, name)
    except repository.AmbiguousMatch as exc:
        raise ActionError(str(exc), "请说得更具体一些。")
    if item is None:
        raise ActionError("这里没有「%s」。" % name)
    return item


def _execute_take(conn, ctx, action):
    item = _resolve_take_item(conn, ctx, action)
    # 背包里已有同名物品时合并数量
    existing = repository.find_item(conn, config.OWNER_PLAYER, ctx.player.id, item.name)
    if existing is not None and existing.name == item.name:
        repository.apply_item_state(
            conn,
            existing.id,
            config.OWNER_PLAYER,
            ctx.player.id,
            existing.quantity + item.quantity,
        )
        repository.delete_item(conn, item.id)
    else:
        repository.apply_item_state(
            conn, item.id, config.OWNER_PLAYER, ctx.player.id, item.quantity
        )
    return [
        narration("你拾取了%s。" % item.name),
        system("输入 /inventory 查看背包。"),
    ]


register(
    ActionSpec(
        name="take",
        summary="拾取当前地点的物品（target = 物品名）",
        validate=_resolve_take_item,
        execute=_execute_take,
    )
)


# ============================================================
# 动作：休息
# ============================================================
def _rest_minutes(action) -> int:
    minutes = action.argument("minutes", config.REST_MINUTES)
    try:
        return int(minutes)
    except (TypeError, ValueError):
        raise ActionError("休息时长必须是整数分钟。")


def _validate_rest(conn, ctx, action):
    if ctx is None:
        raise ActionError("还没有进行中的游戏。")
    minutes = _rest_minutes(action)
    if minutes <= 0:
        raise ActionError("休息时长必须大于 0 分钟。")
    if minutes > 24 * 60:
        raise ActionError("一次最多休息 24 小时。")


def _execute_rest(conn, ctx, action):
    minutes = _rest_minutes(action)
    # 恢复量按"休息了几个整小时"计算：默认 60 分钟 = 1 份，外部行为与之前完全一致
    hours = max(1, minutes // config.REST_MINUTES)
    player = ctx.player
    hp_before, san_before = player.hp, player.san
    hp_after = min(player.max_hp, hp_before + config.REST_HP_GAIN * hours)
    san_after = min(player.max_san, san_before + config.REST_SAN_GAIN * hours)
    repository.update_player_vitals(conn, player.id, hp_after, san_after)

    world = repository.get_world(conn, ctx.world.id)
    new_time = world.current_time + minutes
    repository.update_world_time(conn, ctx.world.id, new_time)

    if hp_after == hp_before and san_after == san_before:
        messages = [narration("你的状态已经是最好的了，但还是花了一点时间喘口气。")]
    else:
        messages = [
            narration(
                "你坐下来休息了一会儿。（HP %d → %d，SAN %d → %d）"
                % (hp_before, hp_after, san_before, san_after)
            )
        ]
    messages.append(system("世界时间推进到 %s。" % format_world_time(new_time)))
    return messages


register(
    ActionSpec(
        name="rest",
        summary="休息，恢复少量 HP / SAN 并推进世界时间（arguments.minutes，默认 60）",
        validate=_validate_rest,
        execute=_execute_rest,
    )
)
