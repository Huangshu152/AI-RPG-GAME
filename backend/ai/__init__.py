"""AI Runtime：Provider 抽象、Action / Response Schema、Turn Runtime。

边界（详见 `docs/ARCHITECTURE.md`）：

    Player Input
        → Turn Runtime
        → AI Provider            ← 只提出叙事 + 候选 Action
        → Structured Response
        → Action Schema 校验      ← 结构校验（这里）
        → Action Engine          ← 领域校验 + 唯一的状态变更
        → State Mutation → Events
        → Turn Result

Provider **不得**修改 GameState、执行 Action、操作数据库或调用 repository。
它只能"提出"；能不能做、做没做成，由 Action Engine 说了算。

当前有两个 Provider：
    mock     —— 完全本地、确定性、不联网（默认）
    deepseek —— 真实 API，必须显式配置 `AI_PROVIDER=deepseek` 才会启用
"""

import os
from typing import Any, Dict, List, Optional

from .. import config
from .errors import ProviderError
from .mock import MockAIProvider
from .provider import AIContext, AIProvider, AIResponse
from .runtime import TurnResult, build_context, process_turn
from .schema import (
    ACTION_ARGUMENTS,
    ACTION_FIELDS,
    ACTION_TYPES,
    SchemaError,
    parse_action,
    parse_ai_response_payload,
    validate_ai_response,
)

# 支持的 Provider 名字。加一个真实 Provider = 在这里登记 + 在 get_provider 里加一行。
SUPPORTED_PROVIDERS = ("mock", "deepseek")


def configured_provider_name() -> str:
    """配置里选定的 Provider 名。

    默认 `mock`；真实 API **必须显式**设置 `AI_PROVIDER=deepseek`。
    装了 API Key 不会自动切换——否则测试会开始联网花钱。

    注意：这里**只读 os.environ**，不读 .env 文件。加载 .env 是应用启动
    （`main.py`）或手动脚本的职责。否则每次构造 Provider 都会读一次磁盘，
    而且单元测试没办法把自己和机器上的 .env 隔离开。
    """
    name = (os.environ.get("AI_PROVIDER") or config.DEFAULT_AI_PROVIDER).strip().lower()
    return name or config.DEFAULT_AI_PROVIDER


def get_provider(name: Optional[str] = None) -> AIProvider:
    """按名字构造 Provider。

    这里是整个项目**唯一**出现 "mock / deepseek" 分支的地方——
    业务层不会出现 `if provider == "deepseek": ...` 这种东西。
    """
    target = (name or configured_provider_name()).strip().lower()
    if target == "deepseek":
        from .deepseek import DeepSeekProvider  # 延迟导入：不用时不必加载

        return DeepSeekProvider()
    if target == "mock":
        return MockAIProvider()
    raise ValueError(
        "未知的 AI Provider：%r（支持 %s）" % (name or target, "、".join(SUPPORTED_PROVIDERS))
    )


def default_provider() -> AIProvider:
    """默认 Provider，由配置决定。"""
    return get_provider(None)


def provider_catalog() -> List[Dict[str, Any]]:
    """给前端做**最小**选择器用（不是多 Provider 动态路由）。

    构造 Provider 不做任何网络请求，所以这里可以放心地探测可用性。
    """
    active = configured_provider_name()
    catalog: List[Dict[str, Any]] = []
    for name in SUPPORTED_PROVIDERS:
        try:
            available = get_provider(name).is_configured()
        except Exception:
            available = False
        catalog.append({"name": name, "available": available, "active": name == active})
    return catalog


__all__ = [
    "AIContext",
    "AIProvider",
    "AIResponse",
    "ACTION_ARGUMENTS",
    "ACTION_FIELDS",
    "ACTION_TYPES",
    "MockAIProvider",
    "ProviderError",
    "SUPPORTED_PROVIDERS",
    "SchemaError",
    "TurnResult",
    "build_context",
    "configured_provider_name",
    "default_provider",
    "get_provider",
    "parse_action",
    "parse_ai_response_payload",
    "process_turn",
    "provider_catalog",
    "validate_ai_response",
]
