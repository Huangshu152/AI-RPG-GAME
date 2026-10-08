"""AI Provider 抽象与只读上下文。

这一层只定义**形状**，不实现任何模型调用。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class AIContext:
    """交给 Provider 的**只读**上下文快照。

    三条刻意的约束：

    1. **只有当前回合真正需要的信息**——不是整个数据库，也不是完整聊天记录。
    2. **不含任何数据库 id**。AI 只能说"我要去阅览室"，不能说"我要去 id=6"。
       名字怎么解释成实际对象、合不合法，全部由 Action Engine 负责。
    3. 它是普通 dict / list 的值拷贝。Provider 拿不到 `conn`，
       所以它在结构上就没有写权限，而不是"靠自觉不写"。
    """

    world: Dict[str, Any]
    current_location: Optional[Dict[str, Any]]
    player: Dict[str, Any]
    visible_npcs: List[Dict[str, Any]] = field(default_factory=list)
    visible_items: List[Dict[str, Any]] = field(default_factory=list)
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    # 长期记忆。**只包含玩家有资格知道的**（memory.select_for_context 过滤），
    # 且已剔除一切数据库 ID。Memory 永远低于 World State 的权威性。
    memories: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AIResponse:
    """Provider 的原始提案。

    `actions` 里是**未经校验**的原始字典：Provider 提出，Runtime 校验。
    这样即使 Provider 写错了（或真实模型返回了奇怪的 JSON），
    也过不了 Action Schema 那关。
    """

    narrative: str = ""
    actions: List[Dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class AIProvider(Protocol):
    """最小 Provider 接口。

    实现者只做一件事：根据玩家输入和上下文，**提出**叙事文本与候选 Action。

    实现者不得：修改 GameState、执行 Action、操作数据库、产生数据库 Event、
    调用 repository。违反这些的实现拿不到 `conn`，因此也做不到。
    """

    name: str

    def is_configured(self) -> bool:
        """能不能干活。例如真实 Provider 缺 API Key 时返回 False。

        只用于健康检查 / 前端提示，**不用于跳过调用**——
        真的调用时仍然要给出明确错误。
        """
        ...

    def generate_turn(self, user_input: str, context: AIContext) -> AIResponse:
        ...
