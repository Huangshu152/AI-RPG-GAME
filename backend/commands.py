"""指令层：把玩家文本翻译成「只读查询」「Gameplay Action」或「System Command」。

这一层**不允许修改游戏世界**，也不 import `repository`。

三类指令，边界是刻意的（见 `docs/ARCHITECTURE.md`）：

| kind | 例子 | 走哪里 |
| --- | --- | --- |
| `read` | `/look` `/status` `/inventory` `/help` | `game_state` 只读查询 |
| `gameplay` | `/go` `/take` `/rest`（将来 AI 的 `attack` / `talk`） | **Action Engine**：`actions.perform()` |
| `system` | `/new` `/save` `/load`（Snapshot / Rollback） | `game_state` 的系统级受控入口 |

不把 `/save` `/load` 做成 Gameplay Action 是刻意的：它们是系统级存档操作，
不是"玩家在世界里做的动作"。见 `backend/actions.py` 顶部说明。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import actions, config, game_state
from .actions import Action, ActionResult, error, narration, system
from .game_state import GameContext
from .models import format_world_time

# 日志消息类型（字面量在 config，全项目共用一套）
KIND_NARRATION = config.KIND_NARRATION
KIND_SYSTEM = config.KIND_SYSTEM
KIND_ERROR = config.KIND_ERROR

# 指令种类
CMD_READ = "read"
CMD_GAMEPLAY = "gameplay"
CMD_SYSTEM = "system"

Message = Dict[str, str]


@dataclass
class CommandResult:
    ok: bool
    messages: List[Message]
    # 机器可读的机制事实。Gameplay 动作由引擎差分得出；系统命令自行声明。
    events: List[Dict[str, Any]] = field(default_factory=list)


HandlerFn = Callable[[sqlite3.Connection, Optional[GameContext], str], List[Message]]
ActionFn = Callable[[str], Action]
ComposeFn = Callable[[sqlite3.Connection, ActionResult], List[Message]]
EventFn = Callable[[sqlite3.Connection, Optional[GameContext], str], List[Dict[str, Any]]]


@dataclass
class CommandSpec:
    name: str
    usage: str
    summary: str
    kind: str = CMD_READ
    needs_game: bool = True
    # 是否把这一回合写进历史。/help 这类参考输出不该混进剧情。
    record: bool = True
    # read / system 走 handler；gameplay 走 action
    handler: Optional[HandlerFn] = None
    action: Optional[ActionFn] = None
    # gameplay：动作执行完如何拼装最终消息
    compose: Optional[ComposeFn] = None
    # system：显式声明本命令产生什么事件（例如回滚不是普通状态差分）
    events: Optional[EventFn] = None


COMMANDS: Dict[str, CommandSpec] = {}


def register(spec: CommandSpec) -> CommandSpec:
    COMMANDS[spec.name] = spec
    return spec


# ============================================================
# 只读指令
# ============================================================
def _look_messages(conn, ctx: Optional[GameContext], args: str = "") -> List[Message]:
    if ctx is None or ctx.location is None:
        return [error("当前地点不存在，请重新创建游戏。")]
    loc = ctx.location
    messages = [narration("【%s】\n%s" % (loc.name, loc.description))]

    exits = game_state.list_exits(conn, loc.id)
    if exits:
        messages.append(
            system("出口：" + "、".join("%s（→ %s）" % (e.label, e.target_name) for e in exits))
        )
    else:
        messages.append(system("这里没有通往别处的出口。"))

    npcs = game_state.list_npcs_at(conn, loc.id)
    if npcs:
        messages.append(system("在场：" + "、".join("%s（%s）" % (n.name, n.status) for n in npcs)))

    items = game_state.list_items(conn, config.OWNER_LOCATION, loc.id)
    if items:
        messages.append(system("可见物品：" + "、".join(i.name for i in items)))

    return messages


def _items_brief(items) -> str:
    return "、".join(i.name + ("×%d" % i.quantity if i.quantity > 1 else "") for i in items)


def _status_messages(conn, ctx: Optional[GameContext], args: str = "") -> List[Message]:
    if ctx is None:
        return [error("还没有进行中的游戏。")]
    p = ctx.player
    items = game_state.list_items(conn, config.OWNER_PLAYER, p.id)
    lines = [
        "玩家：%s" % p.name,
        "HP：%d / %d" % (p.hp, p.max_hp),
        "SAN：%d / %d" % (p.san, p.max_san),
        "当前地点：%s" % (ctx.location.name if ctx.location else "未知"),
        "世界时间：%s" % format_world_time(ctx.world.current_time),
        "物品：%s" % (_items_brief(items) if items else "（空）"),
    ]
    return [narration("\n".join(lines))]


def _inventory_messages(conn, ctx: Optional[GameContext], args: str = "") -> List[Message]:
    if ctx is None:
        return [error("还没有进行中的游戏。")]
    items = game_state.list_items(conn, config.OWNER_PLAYER, ctx.player.id)
    if not items:
        return [narration("你的背包是空的。")]
    lines = []
    for i in items:
        line = "- %s ×%d" % (i.name, i.quantity)
        if i.description:
            line += "：%s" % i.description
        lines.append(line)
    return [narration("背包（%d 件）：\n%s" % (len(items), "\n".join(lines)))]


def _help_messages(conn, ctx, args: str = "") -> List[Message]:
    lines = [
        "[%s] %s —— %s" % (spec.kind, spec.usage, spec.summary) for spec in COMMANDS.values()
    ]
    return [narration("可用指令："), narration("\n".join(lines))]


register(CommandSpec(name="help", usage="/help", summary="列出全部可用指令",
                     kind=CMD_READ, needs_game=False, record=False, handler=_help_messages))
register(CommandSpec(name="look", usage="/look", summary="查看当前地点",
                     kind=CMD_READ, handler=_look_messages))
register(CommandSpec(name="status", usage="/status", summary="查看玩家状态",
                     kind=CMD_READ, handler=_status_messages))
register(CommandSpec(name="inventory", usage="/inventory", summary="查看物品",
                     kind=CMD_READ, handler=_inventory_messages))


# ============================================================
# Gameplay 指令：一律构造 Action 交给 Action Engine
# ============================================================
def _compose_move(conn, result: ActionResult) -> List[Message]:
    """移动之后紧接着展示新地点，保持 /go 原有的输出。"""
    if not result.ok:
        return result.messages
    new_ctx = game_state.get_context(conn)
    return result.messages + _look_messages(conn, new_ctx)


register(CommandSpec(name="go", usage="/go <出口名>", summary="移动到相邻地点",
                     kind=CMD_GAMEPLAY,
                     action=lambda args: Action("move", args.strip()), compose=_compose_move))
register(CommandSpec(name="take", usage="/take <物品名>", summary="拾取当前地点的物品",
                     kind=CMD_GAMEPLAY, action=lambda args: Action("take", args.strip())))
register(CommandSpec(name="rest", usage="/rest", summary="休息，恢复少量 HP / SAN（推进世界时间）",
                     kind=CMD_GAMEPLAY,
                     action=lambda args: Action("rest", arguments={"minutes": config.REST_MINUTES})))


# ============================================================
# System 指令：世界生命周期与存档（不是 Gameplay Action）
# ============================================================
def _new_handler(conn, ctx, args: str) -> List[Message]:
    name = args.strip() or config.DEFAULT_PLAYER_NAME
    if len(name) > 32:
        return [error("玩家名称太长（最多 32 个字符）。")]
    game_state.new_game(conn, name)
    return [
        narration("%s 走进了测试世界。" % name),
        system("新游戏已创建。输入 /look 查看四周，/help 查看全部指令。"),
    ]


def _new_events(conn, ctx, args: str) -> List[Dict[str, Any]]:
    now = game_state.get_context(conn)
    if now is None:
        return []
    events: List[Dict[str, Any]] = []
    # 换世界时给出 session_switch：前端据此清空并重建日志
    if ctx is not None and ctx.world.id != now.world.id:
        events.append(
            {
                "type": "session_switch",
                "from_world": ctx.world.id,
                "to_world": now.world.id,
                "to_player": now.player.id,
            }
        )
    events.append(
        {"type": "game_created", "world_id": now.world.id, "player_id": now.player.id}
    )
    return events


register(CommandSpec(name="new", usage="/new <玩家名>", summary="创建新游戏并进入测试世界",
                     kind=CMD_SYSTEM, needs_game=False,
                     handler=_new_handler, events=_new_events))


def _save_handler(conn, ctx, args: str) -> List[Message]:
    slot = args.strip() or config.DEFAULT_SLOT
    if len(slot) > 32:
        return [error("存档位名称太长（最多 32 个字符）。")]
    saved = game_state.save_game(conn, ctx, slot)
    stamp = saved.get("created_at") or "刚刚"
    return [system("已保存到存档位「%s」（%s）。" % (slot, stamp))]


def _save_events(conn, ctx, args: str) -> List[Dict[str, Any]]:
    # 存档不改变游戏世界状态，所以这里显式声明事件，而不是靠状态差分
    return [{"type": "saved", "slot": args.strip() or config.DEFAULT_SLOT}]


register(CommandSpec(name="save", usage="/save [存档位]", summary="保存游戏",
                     kind=CMD_SYSTEM, handler=_save_handler, events=_save_events))


def _load_handler(conn, ctx, args: str) -> List[Message]:
    try:
        outcome = game_state.load_game(conn, ctx, args.strip() or None)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [error("载入失败：%s" % exc)]

    save = outcome["save"]
    summary = outcome["summary"]
    restored = outcome["restored"]

    lines = ["已载入存档位「%s」（保存于 %s）。" % (save["slot"], save["created_at"])]
    if save.get("label"):
        lines.append("存档内容：%s" % save["label"])
    if restored is not None and restored.location is not None:
        lines.append(
            "当前：%s @ %s，%s"
            % (
                restored.player.name,
                restored.location.name,
                format_world_time(restored.world.current_time),
            )
        )

    rolled = []
    for key, what in (
        ("items_removed", "删掉存档里没有的物品"),
        ("items_restored", "补回被删的物品"),
        ("npcs_removed", "删掉存档里没有的 NPC"),
        ("npcs_restored", "补回被删的 NPC"),
        ("events_removed", "截断存档之后的剧情"),
        ("events_restored", "补回缺失的剧情"),
        ("memories_removed", "删除存档之后的记忆"),
        ("memories_restored", "补回缺失的记忆"),
    ):
        count = summary.get(key, 0)
        if count:
            rolled.append("%s %d 项" % (what, count))
    lines.append("回滚：" + ("、".join(rolled) if rolled else "状态与存档一致，无需修正"))

    return [system("\n".join(lines))]


def _load_events(conn, ctx, args: str) -> List[Dict[str, Any]]:
    # 回滚是系统级操作，用一条明确的事件表示；它不等于一次 Gameplay Action
    now = game_state.get_context(conn)
    if now is None:
        return []
    return [{"type": "rollback", "world_id": now.world.id, "player_id": now.player.id}]


register(CommandSpec(name="load", usage="/load [存档位]", summary="载入存档",
                     kind=CMD_SYSTEM, needs_game=False,
                     handler=_load_handler, events=_load_events))


# ============================================================
# 分发
# ============================================================
def _free_text(text: str) -> List[Message]:
    return [
        system("（V0.1 还没有接入 AI，自由输入不会被处理。）"),
        system("请输入指令，例如 /look、/status、/inventory、/rest、/save；/help 查看全部。"),
    ]


def _record(conn: sqlite3.Connection, result: CommandResult) -> CommandResult:
    """把一个回合写进历史；没有进行中的游戏就只把结果返回。"""
    ctx = game_state.get_context(conn)
    if ctx is not None:
        game_state.record_turn(conn, ctx.world.id, result.messages, result.events)
    return result


def run_action(
    conn: sqlite3.Connection, ctx: Optional[GameContext], action: Action
) -> CommandResult:
    """执行一个 Gameplay Action 并组装成指令结果，同时把这回合写进历史。

    独立出来是为了让将来的 AI / Mock AI 能直接走这条路，而不是伪造斜杠指令：
    AI 产出 Action → 这里执行 → 同样的 messages / events / 历史记录。
    """
    result = actions.perform(conn, ctx, action)
    return _record(conn, CommandResult(ok=result.ok, messages=result.messages, events=result.events))


def dispatch(conn: sqlite3.Connection, raw_text: str) -> CommandResult:
    """解析并执行一条输入。"""
    text = (raw_text or "").strip()
    if not text:
        return CommandResult(False, [error("请输入内容。")])

    # 自由输入：V0.1 还没有 AI，这里就是将来接 Keeper 的接缝。
    if not text.startswith("/"):
        return _record(conn, CommandResult(True, _free_text(text)))

    parts = text[1:].split(None, 1)
    name = parts[0].lower() if parts and parts[0] else "help"
    args = parts[1] if len(parts) > 1 else ""

    spec = COMMANDS.get(name)
    if spec is None:
        return _record(
            conn,
            CommandResult(
                False,
                [error("未知指令：/%s" % name), system("输入 /help 查看可用指令。")],
            ),
        )

    ctx = game_state.get_context(conn)
    if spec.needs_game and ctx is None:
        return _record(
            conn,
            CommandResult(
                False,
                [
                    error(
                        "还没有进行中的游戏。请先在左侧「创建新游戏」，或输入 /new <玩家名>，"
                        "也可以用 /load 载入已有存档。"
                    )
                ],
            ),
        )

    if spec.kind == CMD_GAMEPLAY:
        result = actions.perform(conn, ctx, spec.action(args))
        messages = spec.compose(conn, result) if spec.compose else result.messages
        outcome = CommandResult(ok=result.ok, messages=messages, events=result.events)
    else:
        messages = spec.handler(conn, ctx, args)
        # 只读指令在结构上就不改状态，因此不产生事件；
        # 系统命令自己声明会产生什么事件。
        ok = not any(m["kind"] == KIND_ERROR for m in messages)
        events = spec.events(conn, ctx, args) if (ok and spec.events is not None) else []
        outcome = CommandResult(ok, messages, events)

    if not spec.record:
        return outcome
    return _record(conn, outcome)
