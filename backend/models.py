"""领域模型。

V0.1 只做「数据结构 + 显示工具」，不含业务规则。
"""

from dataclasses import dataclass, field
from typing import List, Optional

from . import config


@dataclass
class Location:
    id: int
    name: str
    description: str


@dataclass
class Exit:
    """一条出口：从当前地点通往 target 的可见标签。"""

    label: str
    target_id: int
    target_name: str = ""


@dataclass
class Item:
    id: int
    name: str
    description: str
    quantity: int


@dataclass
class NPC:
    id: int
    name: str
    status: str
    location_id: Optional[int] = None
    location_name: str = ""


@dataclass
class Player:
    id: int
    name: str
    hp: int
    max_hp: int
    san: int
    max_san: int


@dataclass
class GameEvent:
    """一条日志行（叙事）或一条结构化事件（kind='event'）。

    payload 保持原始 JSON 字符串：数据层不解释它，由上层决定是否解析。
    """

    id: int
    world_id: int
    seq: int
    kind: str
    text: str
    payload: str
    created_at: str


@dataclass
class Memory:
    """一条结构化长期记忆。

    `source_event_id` 只用于内部追溯（这条记忆来自哪次真实事件），
    `memory.to_ai_dict()` 会把它剔除——**数据库 ID 不进 AIContext**。
    """

    id: int
    world_id: int
    scope: str
    knowledge_scope: str
    importance: str
    category: str
    content: str
    subjects: List[str] = field(default_factory=list)
    source_event_id: Optional[int] = None
    created_turn: int = 0
    active: bool = True
    created_at: str = ""


@dataclass
class World:
    id: int
    name: str
    current_time: int
    current_location_id: Optional[int] = None


@dataclass
class GameData:
    """一次「读取当前游戏」的完整结果。"""

    world: World
    player: Player
    location: Optional[Location] = None
    npcs: List[NPC] = field(default_factory=list)


def format_world_time(minutes: int) -> str:
    """把分钟数格式化成「第 N 日 HH:MM」。"""
    minutes = max(0, int(minutes))
    day = minutes // config.MINUTES_PER_DAY + 1
    rest = minutes % config.MINUTES_PER_DAY
    return "第 %d 日 %02d:%02d" % (day, rest // 60, rest % 60)
