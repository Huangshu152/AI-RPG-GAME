"""MockAIProvider：完全本地、无网络、确定性、不需要 API Key。

**它的存在只是为了验证 Runtime 是通的，不是最终 AI。** 所以每句叙事都带
`（Mock AI）` 前缀，避免被误认为 Keeper 真的在讲故事。

规则很简单（固定优先级：休息 → 拿起 → 前往），命中就提出一个 Action，
命中不了就只给叙事、`actions = []`——**绝不伪造动作**。

它只"提出"：目标存不存在、出口通不通，一律交给 Action Engine 判定。
所以 `MockAIProvider` 对"去月亮"也会照样提出 `move("月亮")`，然后被引擎拒绝。
这正是这一层该有的行为。
"""

from typing import List, Optional, Sequence

from .provider import AIContext, AIResponse

MOCK_PREFIX = "（Mock AI）"

# 动词表：**长的排在前面**，否则"拿起来"会被"拿起"截断
_MOVE_VERBS: Sequence[str] = ("走进", "走入", "进入", "走到", "走向", "前往", "去往", "回到", "去", "回")
_TAKE_VERBS: Sequence[str] = ("拿起来", "拿起", "拿走", "捡起", "拾取", "拾起", "拿", "取")
_REST_WORDS: Sequence[str] = ("休息", "歇一会", "歇一歇", "睡一会", "睡觉")

_TRIM = "。！？!?.,，、；;：: \t\n"


def _clean(text: str) -> str:
    return (text or "").strip().strip(_TRIM).strip()


def _drop_leading_pronoun(text: str) -> str:
    """去掉开头的称呼 / 意愿词，让"我走进阅览室"和"走进阅览室"等价。"""
    for prefix in ("让我们", "让我", "我要", "我想", "咱们", "我们", "我"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _match_verb(text: str, verbs: Sequence[str]) -> Optional[str]:
    """找出文本里最先出现的动词，返回它后面的目标；没有目标就返回 None。"""
    best_index: Optional[int] = None
    best_verb = ""
    for verb in verbs:
        index = text.find(verb)
        if index >= 0 and (best_index is None or index < best_index):
            best_index = index
            best_verb = verb
    if best_index is None:
        return None
    target = _clean(text[best_index + len(best_verb):])
    return target or None


class MockAIProvider:
    """确定性规则匹配。同样的 (输入, 上下文) 永远得到同样的输出。"""

    name = "mock"

    def is_configured(self) -> bool:
        """本地规则匹配，永远可用。"""
        return True

    def generate_turn(self, user_input: str, context: AIContext) -> AIResponse:
        text = _clean(user_input)
        body = _drop_leading_pronoun(text)

        # 1) 休息（默认 60 分钟）
        if any(word in body for word in _REST_WORDS):
            return AIResponse(
                narrative="%s你打算在原地休息一会儿。" % MOCK_PREFIX,
                actions=[{"type": "rest", "target": None, "arguments": {"minutes": 60}}],
            )

        # 2) 拿起某物
        take_target = _match_verb(body, _TAKE_VERBS)
        if take_target:
            return AIResponse(
                narrative="%s你打算拿起「%s」。" % (MOCK_PREFIX, take_target),
                actions=[{"type": "take", "target": take_target, "arguments": {}}],
            )

        # 3) 前往某处
        move_target = _match_verb(body, _MOVE_VERBS)
        if move_target:
            return AIResponse(
                narrative="%s你打算前往「%s」。" % (MOCK_PREFIX, move_target),
                actions=[{"type": "move", "target": move_target, "arguments": {}}],
            )

        # 4) 没识别出来：只叙事，不伪造动作（这是刻意的行为，不是漏做）
        return AIResponse(
            narrative="%s我没听懂「%s」，这一回合什么都不会发生。%s"
            % (MOCK_PREFIX, text, self._hint(context)),
            actions=[],
        )

    @staticmethod
    def _hint(context: AIContext) -> str:
        """用上下文给一句可用提示——顺便说明 Provider 确实看得到只读上下文。"""
        exits = (context.current_location or {}).get("exits") or []
        labels: List[str] = [str(e.get("label", "")) for e in exits if e.get("label")]
        if not labels:
            return ""
        return "你可以去：" + "、".join(labels) + "。"
