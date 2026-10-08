"""Turn Runtime：一个自然语言回合的编排。

    Player Input
        → build_context()         只读上下文，不含数据库 id
        → provider.generate_turn() Provider 只**提出**叙事与候选 Action
        → validate_ai_response()   结构校验（Action Schema）
        → actions.perform()        Action Engine：领域校验 + 唯一的状态变更
        → TurnResult               narrative / actions / events / 状态由调用方读取

它**不负责**：SQL、数据库细节、Prompt 拼接、模型 HTTP 调用、直接改状态。
这里只有编排。
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import actions as action_engine
from .. import config, game_state, memory
from ..actions import Action, error, narration, system
from ..game_state import GameContext
from ..models import format_world_time
from .errors import ProviderError
from .provider import AIContext, AIProvider
from .schema import SchemaError, validate_ai_response

Message = Dict[str, str]


@dataclass
class TurnResult:
    ok: bool
    narrative: str = ""
    # AI 提出的全部动作（已通过 Schema，未执行）
    proposed_actions: List[Action] = field(default_factory=list)
    # 真正被 Action Engine 执行且成功的那个（V0.2 最多一个）
    executed_action: Optional[Action] = None
    # 玩家可见的消息：AI 叙事 + Action Engine 的确认/报错
    messages: List[Message] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    # 因为单动作上限而被忽略的候选动作数量
    ignored_actions: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """给 HTTP 层序列化用。注意这里**不含** state——那由路由层从 GameState 读。"""
        return {
            "ok": self.ok,
            "narrative": self.narrative,
            "actions": [a.to_dict() for a in self.proposed_actions],
            "executed": self.executed_action.to_dict() if self.executed_action else None,
            "ignored_actions": self.ignored_actions,
            "messages": self.messages,
            "events": self.events,
            "error": self.error,
        }


def build_context(conn: sqlite3.Connection) -> AIContext:
    """把当前 GameState 压成一个只读、**不含 id** 的上下文快照。"""
    data = game_state.get_game_data(conn)
    if data is None:
        raise ValueError("没有进行中的游戏")

    world, player, loc = data.world, data.player, data.location

    location: Optional[Dict[str, Any]] = None
    visible_npcs: List[Dict[str, Any]] = []
    visible_items: List[Dict[str, Any]] = []
    if loc is not None:
        location = {
            "name": loc.name,
            "description": loc.description,
            "exits": [
                {"label": e.label, "to": e.target_name}
                for e in game_state.list_exits(conn, loc.id)
            ],
        }
        visible_npcs = [
            {"name": n.name, "status": n.status}
            for n in game_state.list_npcs_at(conn, loc.id)
        ]
        visible_items = [
            {"name": i.name, "description": i.description, "quantity": i.quantity}
            for i in game_state.list_items(conn, config.OWNER_LOCATION, loc.id)
        ]

    recent = game_state.history_for_api(conn, world.id)[-config.AI_RECENT_EVENTS:]
    memories = memory.build_context_list(conn, world.id, config.MEMORY_CONTEXT_BUDGET)

    return AIContext(
        world={
            "name": world.name,
            "time": format_world_time(world.current_time),
            "time_minutes": world.current_time,
        },
        current_location=location,
        player={
            "name": player.name,
            "hp": player.hp,
            "max_hp": player.max_hp,
            "san": player.san,
            "max_san": player.max_san,
            "items": [
                {"name": i.name, "description": i.description, "quantity": i.quantity}
                for i in game_state.list_items(conn, config.OWNER_PLAYER, player.id)
            ],
        },
        visible_npcs=visible_npcs,
        visible_items=visible_items,
        recent_events=recent,
        memories=memories,
    )


def _finish(conn: sqlite3.Connection, ctx: GameContext, result: TurnResult) -> TurnResult:
    """把这一回合写进历史（和玩家敲指令走的是同一套历史）。"""
    if result.messages:
        game_state.record_turn(conn, ctx.world.id, result.messages, result.events)
    return result


def process_turn(
    conn: sqlite3.Connection, user_input: str, provider: AIProvider
) -> TurnResult:
    """处理一个自然语言回合。"""
    text = (user_input or "").strip()
    if not text:
        message = "请输入内容。"
        return TurnResult(ok=False, error=message, messages=[error(message)])

    ctx = game_state.get_context(conn)
    if ctx is None:
        message = "还没有进行中的游戏。请先创建新游戏或 /load。"
        return TurnResult(ok=False, error=message, messages=[error(message)])

    # ---- 1. Provider 只提出（真实模型可能失败，统一转成失败的回合）----
    ai_context = build_context(conn)
    try:
        response = provider.generate_turn(text, ai_context)
    except ProviderError as exc:
        provider_name = getattr(provider, "name", "?")
        message = "AI Provider（%s）调用失败：%s" % (provider_name, exc)
        # 不产生事件、不改状态；错误如实告诉玩家
        return _finish(conn, ctx, TurnResult(ok=False, error=message, messages=[error(message)]))

    # ---- 2. 结构校验：不合法就不执行、不产生事件 ----
    try:
        narrative, proposed = validate_ai_response(response)
    except SchemaError as exc:
        message = "AI 提出的动作不符合 Action Schema：%s" % exc
        # 叙事一并丢弃：它很可能在描述那个没被接受的动作，显示出来会误导玩家
        return _finish(conn, ctx, TurnResult(ok=False, error=message, messages=[error(message)]))

    # ---- 3. 没有动作：只叙事，不改任何状态 ----
    if not proposed:
        if narrative:
            messages = [narration(narrative)]
        else:
            # narrative 允许为空；但如果既没叙事也没动作，回合就是静默的，
            # 玩家会以为程序坏了。给一句明确的说明。
            messages = [system("（AI 这一回合没有给出叙事，也没有提出动作。）")]
        return _finish(conn, ctx, TurnResult(ok=True, narrative=narrative, messages=messages))

    # ---- 4. 最多执行一个 Gameplay Action（V0.2 的上限，刻意不做动作编排）----
    action = proposed[0]
    ignored = max(0, len(proposed) - config.AI_MAX_ACTIONS_PER_TURN)

    engine_result = action_engine.perform(conn, ctx, action)

    messages: List[Message] = []
    if narrative:
        messages.append(narration(narrative))
    messages.extend(engine_result.messages)
    if ignored:
        messages.append(
            system("（V0.2 每回合只执行第一个动作，其余 %d 个已忽略。）" % ignored)
        )

    return _finish(
        conn,
        ctx,
        TurnResult(
            ok=engine_result.ok,
            narrative=narrative,
            proposed_actions=proposed,
            # 只有真的成功了才算"执行了"；失败时事件为空，不会留下虚假成功记录
            executed_action=action if engine_result.ok else None,
            messages=messages,
            events=engine_result.events,
            error=engine_result.error,
            ignored_actions=ignored,
        ),
    )
