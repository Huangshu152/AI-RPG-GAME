"""Prompt Builder：把 `AIContext` + 玩家输入变成 system / user 两条消息。

它属于 Provider 内部细节——Turn Runtime 不知道 Prompt 长什么样。

两个刻意的做法：

1. **允许的动作清单直接来自 `actions.ACTIONS` 注册表**，不是手写死的字符串。
   只要引擎里没注册 `attack`，Prompt 里就不可能声明它。
2. **上下文以 JSON 形式给模型，且里面没有任何数据库 id**——`AIContext` 本身
   就不含 id（见 `provider.py`），这里也不会额外补充。
"""

import json
from typing import Any, Dict, List

from .. import actions
from . import schema as ai_schema
from .provider import AIContext

SYSTEM_TEMPLATE = """你是一款文字 RPG 的 Keeper（主持人 / GM）。玩家用自然语言告诉你他想做什么，
你负责描述发生了什么，并**建议**他接下来执行什么游戏动作。

严格遵守以下规则：

1. 你是这个世界的 Keeper，负责叙事与扮演，但**不是**世界状态的权威。
2. 只能依据下面提供的「世界上下文」做判断，不要引入上下文之外的世界设定。
3. 上下文里没有的信息，不要当成已确证的事实。可以留白、可以描述不确定，
   但不要凭空捏造地点、人物、物品或已经发生过的剧情。
4. 绝对不要输出任何数据库 ID、内部标识符或字段名。玩家和动作都只用**名字**。
5. 你只能使用下面列出的动作。不要发明新的动作，也不要使用未列出的动作。
6. 你不能直接修改世界状态。你提出的动作只是**建议**。
7. 你提出的动作会交给游戏引擎校验，可能被拒绝（例如那里没有路）。这是正常的，
   照常给出叙事即可，不要为了让动作通过而编造世界。
8. **只输出一个 JSON 对象**，不要输出任何解释、前后缀、Markdown 代码块或多余文字。
9. `actions` 必须符合下面的结构。不确定该做什么时就返回空数组 `[]`。
10. **不要替换玩家的意图。** 如果玩家想做的事在上下文里做不到（要去一个不存在的地方、
    要拿一件不存在的东西、想做的动作不在下面的清单里），就照实说明做不到，
    并且 `actions` 返回 `[]`。**绝对不要**把它换成一个"看起来可行"的其他动作——
    玩家说去甲地，你不要把他送到乙地。
11. 上下文里的 `memories` 是**长期记忆**：玩家过去经历过、或已经知道的事。
    把它们当作"玩家记得的历史"来用，让叙事和之前保持一致。
    **但记忆不是当前事实**：如果记忆与当前状态（world / current_location / player /
    visible_*）冲突，**一律以当前状态为准**，不要按记忆去描述已经不存在的东西，
    也不要用记忆去改变世界。

可用的动作（这是全部，不要发明其他动作）：

__ALLOWED_ACTIONS__

输出格式（严格）：

{
  "narrative": "用中文描写这一回合发生了什么，2-4 句。",
  "actions": [
    {"type": "move", "target": "阅览室", "arguments": {}}
  ]
}

字段说明：
- `narrative`：字符串，必须有这个键（可以是空字符串）。
- `actions`：数组。每个元素只允许 `type` / `target` / `arguments` 三个键，多一个都不行。
- `target`：目标的名字（地点名或物品名），不是 ID；不需要目标时写 null。
- `arguments`：对象。只有 `rest` 接受 `{"minutes": 整数}`，其他动作必须是 `{}`。
- 一次最多给出 1 个动作。没有合适的动作就写 `"actions": []`。"""


def _allowed_actions_doc() -> str:
    """从 Action Engine 注册表生成动作说明——引擎没注册的动作不可能出现在这里。"""
    lines: List[str] = []
    for name in sorted(actions.ACTIONS):
        spec = actions.ACTIONS[name]
        arguments = ai_schema.ACTION_ARGUMENTS.get(name, {})
        if arguments:
            args_doc = "，".join(
                "%s: %s（可选）" % (key, kind.__name__) for key, kind in sorted(arguments.items())
            )
        else:
            args_doc = "无（arguments 必须是 {}）"
        lines.append("- %s：%s；参数：%s" % (name, spec.summary, args_doc))
    return "\n".join(lines)


def build_system_prompt() -> str:
    return SYSTEM_TEMPLATE.replace("__ALLOWED_ACTIONS__", _allowed_actions_doc())


def build_user_prompt(user_input: str, context: AIContext) -> str:
    """把只读上下文和玩家输入拼成一条 user 消息。"""
    payload: Dict[str, Any] = {
        "world": context.world,
        "current_location": context.current_location,
        "player": context.player,
        "visible_npcs": context.visible_npcs,
        "visible_items": context.visible_items,
        "recent_events": context.recent_events,
        "memories": context.memories,
    }
    return (
        "当前世界上下文（JSON）：\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n玩家说："
        + (user_input or "").strip()
        + "\n\n请按上面规定的 JSON 格式回复。"
    )


def build_messages(user_input: str, context: AIContext) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(user_input, context)},
    ]
