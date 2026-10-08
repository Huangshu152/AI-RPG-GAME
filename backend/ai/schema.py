"""稳定的 Action Schema 与 AI Response Schema。**与具体 Provider 无关。**

校验分两层，刻意不合并：

| 层 | 在哪 | 管什么 |
| --- | --- | --- |
| **结构校验** | 本模块 | type 认不认识、字段合不合法、参数能不能解释、类型对不对 |
| **领域校验** | Action Engine（`actions.ActionSpec.validate`） | 这个出口通不通、这里有没有这件东西 |

所以 `move("不存在的地点")` 能通过结构校验（它是个形状正确的 move），
再由 Action Engine 拒绝——**不能因为 AI 说了"去那里"就强行执行**。

本模块只做前一层；它不接触数据库，也不知道 Action Engine 怎么执行。
"""

from typing import Any, Dict, List, Tuple, Type

from .. import actions
from ..actions import Action
from .provider import AIResponse


class SchemaError(Exception):
    """结构化输入不合法：未知动作、非法字段、无法解释的参数。"""


# 认识的 action type —— 唯一来源是 Action Engine 的注册表，不另立一份名单
ACTION_TYPES = frozenset(actions.ACTIONS)

# 每个 action 允许的具名参数。没列出的动作**不接受任何参数**。
# 当前只有 rest 有参数；不要提前给未实现的动作加占位参数。
ACTION_ARGUMENTS: Dict[str, Dict[str, Type[Any]]] = {
    "rest": {"minutes": int},
}

# action 对象允许出现的字段，多一个都拒绝
ACTION_FIELDS = frozenset({"type", "target", "arguments"})


def parse_ai_response_payload(raw: Any) -> AIResponse:
    """把 Provider 解析出来的 JSON 对象校验成 `AIResponse`（**只校验外层信封**）。

    个别 action 是否合法**不在这里**判断——那是 Turn Runtime 的
    `validate_ai_response()` 和 Action Engine 的事。这样"模型输出了未知 action"
    会由既有机制拒绝，Provider 不会自作主张把它修成另一个动作。

    对信封的要求是严格的（narrative 必须是字符串、actions 必须是数组、
    每个 action 必须是对象），但**多出来的顶层字段会被忽略**——
    真实模型偶尔会多塞一个键，为此整回合失败不值得。Action 本身仍然一个字都不许多。
    """
    if not isinstance(raw, dict):
        raise SchemaError("AI 响应必须是 JSON 对象，收到 %s" % type(raw).__name__)

    if "narrative" not in raw:
        raise SchemaError("AI 响应缺少 narrative 字段")
    narrative = raw.get("narrative")
    if narrative is None:
        narrative = ""
    if not isinstance(narrative, str):
        raise SchemaError("narrative 必须是字符串（可以为空）")

    actions = raw.get("actions")
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        raise SchemaError("actions 必须是数组")
    for index, item in enumerate(actions):
        if not isinstance(item, dict):
            raise SchemaError("actions[%d] 必须是对象" % index)

    return AIResponse(narrative=narrative, actions=[dict(item) for item in actions])


def parse_action(raw: Any) -> Action:
    """把一处原始 action 解析成 `Action`，任何不合规都抛 SchemaError。"""
    if not isinstance(raw, dict):
        raise SchemaError("action 必须是对象，收到 %s" % type(raw).__name__)

    unknown_fields = set(raw) - ACTION_FIELDS
    if unknown_fields:
        raise SchemaError("action 含未知字段：%s" % "、".join(sorted(unknown_fields)))

    if "type" not in raw:
        raise SchemaError("action 缺少 type 字段")

    action_type = raw.get("type")
    if not isinstance(action_type, str) or not action_type:
        raise SchemaError("action.type 必须是非空字符串")
    if action_type not in ACTION_TYPES:
        raise SchemaError(
            "未知动作类型：%s（当前支持 %s）"
            % (action_type, "、".join(sorted(ACTION_TYPES)))
        )

    target = raw.get("target")
    if target is not None and not isinstance(target, str):
        raise SchemaError("action.target 必须是字符串或 null")

    arguments = raw.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise SchemaError("action.arguments 必须是对象")

    allowed = ACTION_ARGUMENTS.get(action_type, {})
    unknown_args = set(arguments) - set(allowed)
    if unknown_args:
        raise SchemaError(
            "%s 不接受参数：%s" % (action_type, "、".join(sorted(unknown_args)))
        )
    for key, value in arguments.items():
        expected = allowed[key]
        # bool 是 int 的子类，必须显式排除，否则 minutes=True 会被放过去
        if isinstance(value, bool) or not isinstance(value, expected):
            raise SchemaError(
                "%s 的参数 %s 必须是 %s" % (action_type, key, expected.__name__)
            )

    return Action(type=action_type, target=target or "", arguments=dict(arguments))


def validate_ai_response(response: AIResponse) -> Tuple[str, List[Action]]:
    """校验 Provider 的输出，返回 (narrative, 已解析的 Action 列表)。

    任何不合规都抛 SchemaError，由 Turn Runtime 转成一个失败的回合——
    不执行、不产生事件。
    """
    if not isinstance(response, AIResponse):
        raise SchemaError("Provider 必须返回 AIResponse，收到 %s" % type(response).__name__)

    narrative = response.narrative
    if narrative is None:
        narrative = ""
    if not isinstance(narrative, str):
        raise SchemaError("narrative 必须是字符串（可以为空，但必须存在）")

    if not isinstance(response.actions, list):
        raise SchemaError("actions 必须是数组")

    return narrative, [parse_action(raw) for raw in response.actions]
