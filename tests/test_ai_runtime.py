"""V0.2 AI Runtime 测试：Action Schema / Provider / Turn Runtime / 架构边界。

只用标准库，不需要装 fastapi：

    python tests\\test_ai_runtime.py

在系统临时目录里建独立数据库，不影响 data/game.db。
"""

import ast
import dataclasses
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend import actions, commands, config, database, game_state, repository  # noqa: E402
from backend.ai import mock as mock_module  # noqa: E402
from backend.ai import runtime as turn_runtime  # noqa: E402
from backend.ai import schema as ai_schema  # noqa: E402
from backend.ai.provider import AIContext, AIProvider, AIResponse  # noqa: E402
from backend.ai.mock import MockAIProvider  # noqa: E402

PASSED = 0
FAILED = 0
PROVIDER = MockAIProvider()


def check(label, condition, extra=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [PASS] %s" % label)
    else:
        FAILED += 1
        print("  [FAIL] %s%s" % (label, ("  -> " + repr(extra)) if extra != "" else ""))


def make_game():
    with database.get_conn() as conn:
        commands.dispatch(conn, "/new 回合测试者")


def turn(text, provider=None):
    with database.get_conn() as conn:
        result = turn_runtime.process_turn(conn, text, provider or PROVIDER)
        return result, game_state.build_state(conn)


def read_state():
    with database.get_conn() as conn:
        return game_state.build_state(conn)


def texts(messages):
    return "\n".join(m["text"] for m in messages)


def _find_id_keys(obj, found=None):
    """递归找出所有叫 "id" 的键——用于断言上下文里没有数据库 id。"""
    if found is None:
        found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "id":
                found.append(key)
            _find_id_keys(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _find_id_keys(value, found)
    return found


# ---------------------------------------------------------------- 测试替身
class UnknownActionProvider:
    """提出一个 Schema 里不存在的动作。"""

    name = "bad-unknown-action"

    def generate_turn(self, user_input, context):
        return AIResponse(narrative="我要放个火球。", actions=[{"type": "fireball", "target": "门"}])


class TwoActionProvider:
    """一次提出两个动作，用来验证单动作上限。"""

    name = "bad-two-actions"

    def generate_turn(self, user_input, context):
        return AIResponse(
            narrative="你一边走一边伸手。",
            actions=[
                {"type": "move", "target": "阅览室", "arguments": {}},
                {"type": "take", "target": "泛黄的笔记", "arguments": {}},
            ],
        )


class SilentProvider:
    """既没叙事也没动作。"""

    name = "silent"

    def generate_turn(self, user_input, context):
        return AIResponse(narrative="", actions=[])


class NotAResponseProvider:
    """返回的不是 AIResponse，验证 Runtime 不会盲信 Provider。"""

    name = "not-a-response"

    def generate_turn(self, user_input, context):
        return "我只是一个字符串"


class BadArgumentProvider:
    name = "bad-arg"

    def generate_turn(self, user_input, context):
        return AIResponse(narrative="", actions=[{"type": "rest", "arguments": {"minutes": True}}])


def main():
    tmp = tempfile.mkdtemp(prefix="airpg-ai-")
    config.DATA_DIR = pathlib.Path(tmp)
    config.DB_PATH = config.DATA_DIR / "ai.db"
    try:
        database.init_db()
        make_game()

        # ============================================================
        print("== 1. Action Schema：合法动作")
        move = ai_schema.parse_action({"type": "move", "target": "阅览室", "arguments": {}})
        check("合法 move 通过", move.type == "move" and move.target == "阅览室", move)
        take = ai_schema.parse_action({"type": "take", "target": "泛黄的笔记", "arguments": {}})
        check("合法 take 通过", take.type == "take" and take.target == "泛黄的笔记", take)
        rest = ai_schema.parse_action({"type": "rest", "target": None, "arguments": {"minutes": 60}})
        check("合法 rest 通过", rest.type == "rest" and rest.argument("minutes") == 60, rest)
        check("arguments 可以省略", ai_schema.parse_action({"type": "move", "target": "x"}).arguments == {})
        check("target 可以是 null", ai_schema.parse_action({"type": "rest", "target": None}).target == "")
        check("Action Schema 支持序列化回字典",
              move.to_dict() == {"type": "move", "target": "阅览室", "arguments": {}}, move.to_dict())

        print("== 2. Action Schema：必须拒绝的东西")
        rejected = [
            ("未知 action type", {"type": "fireball", "target": "门"}),
            ("缺少 type", {"target": "门"}),
            ("type 不是字符串", {"type": 42}),
            ("未知字段", {"type": "move", "target": "门", "id": 6}),
            ("未知字段（arguments 之外）", {"type": "move", "target": "门", "sql": "DROP"}),
            ("move 不接受参数", {"type": "move", "target": "门", "arguments": {"force": True}}),
            ("rest 参数类型错误", {"type": "rest", "arguments": {"minutes": "一小时"}}),
            ("bool 不能被当成 int", {"type": "rest", "arguments": {"minutes": True}}),
            ("target 类型错误", {"type": "move", "target": 6}),
            ("arguments 不是对象", {"type": "move", "target": "门", "arguments": []}),
            ("整体不是对象", "move"),
            ("整体是列表", ["move"]),
        ]
        for label, raw in rejected:
            try:
                ai_schema.parse_action(raw)
                check("拒绝：%s" % label, False, raw)
            except ai_schema.SchemaError:
                check("拒绝：%s" % label, True)

        print("== 3. AI Response Schema")
        narrative, parsed = ai_schema.validate_ai_response(
            AIResponse(narrative="你推开了门。", actions=[{"type": "move", "target": "阅览室"}])
        )
        check("合法响应通过", narrative == "你推开了门。" and len(parsed) == 1, (narrative, parsed))
        narrative, parsed = ai_schema.validate_ai_response(AIResponse(narrative="", actions=[]))
        check("narrative 可以为空、actions 可以为空", narrative == "" and parsed == [])
        check("actions 必须是数组",
              _raises(lambda: ai_schema.validate_ai_response(AIResponse(actions="move"))))
        check("narrative 必须是字符串",
              _raises(lambda: ai_schema.validate_ai_response(AIResponse(narrative=123))))
        check("Provider 必须返回 AIResponse",
              _raises(lambda: ai_schema.validate_ai_response("随便")))

        print("== 4. Provider 接口与 Mock 行为")
        check("MockAIProvider 满足 AIProvider 协议", isinstance(PROVIDER, AIProvider))
        ctx = _empty_context()

        cases = [
            ("去阅览室", "move", "阅览室"),
            ("走进阅览室", "move", "阅览室"),
            ("我走进阅览室", "move", "阅览室"),
            ("前往阅览室", "move", "阅览室"),
            ("拿起泛黄的笔记", "take", "泛黄的笔记"),
            ("我捡起泛黄的笔记", "take", "泛黄的笔记"),
        ]
        for text, want_type, want_target in cases:
            got = PROVIDER.generate_turn(text, ctx).actions
            check(
                "「%s」→ %s(%s)" % (text, want_type, want_target),
                len(got) == 1 and got[0]["type"] == want_type and got[0]["target"] == want_target,
                got,
            )

        got = PROVIDER.generate_turn("休息", ctx).actions
        check("「休息」→ rest(minutes=60)",
              len(got) == 1 and got[0]["type"] == "rest" and got[0]["arguments"] == {"minutes": 60}, got)

        unknown = PROVIDER.generate_turn("今天天气不错", ctx)
        check("无法识别 → actions 为空数组", unknown.actions == [], unknown.actions)
        check("无法识别 → 仍然给出 narrative", bool(unknown.narrative.strip()), unknown.narrative)

        check("输出确定性（同一输入两次结果相同）",
              PROVIDER.generate_turn("去阅览室", ctx).actions == PROVIDER.generate_turn("去阅览室", ctx).actions)
        check("narrative 带 Mock 标记（不会被误认为真 Keeper）",
              mock_module.MOCK_PREFIX in PROVIDER.generate_turn("去阅览室", ctx).narrative)

        # Mock 只提出、不判断合法性：引擎会拒绝它
        proposed = PROVIDER.generate_turn("去月亮", ctx).actions
        check("Mock 对不存在的目标也照常提出（把判定交给引擎）",
              proposed and proposed[0]["target"] == "月亮", proposed)

        # ============================================================
        print("== 5. AIContext：只读、且不含数据库 id")
        with database.get_conn() as conn:
            ai_context = turn_runtime.build_context(conn)
        dump = dataclasses.asdict(ai_context)
        check("上下文含 world", bool(dump["world"]), dump.get("world"))
        check("上下文含 current_location", dump["current_location"] is not None)
        check("上下文含 player", bool(dump["player"]))
        check("上下文含 visible_npcs", isinstance(dump["visible_npcs"], list))
        check("上下文含 visible_items", isinstance(dump["visible_items"], list))
        check("上下文含 recent_events", isinstance(dump["recent_events"], list))
        check("上下文里没有任何数据库 id（AI 不能挑选 id）", _find_id_keys(dump) == [], _find_id_keys(dump))
        check("上下文是冻结的（Provider 改不了它）",
              _raises(lambda: setattr(ai_context, "player", {})))

        # ============================================================
        print("== 6. Turn Runtime：自然语言 → GameState 真实变化")
        before = read_state()
        check("起点是大厅", before["location"]["name"] == "灰塔图书馆·大厅", before["location"]["name"])

        result, after = turn("我走进阅览室")
        check("回合成功", result.ok is True, result.error)
        check("地点真的变了", after["location"]["name"] == "阅览室", after["location"]["name"])
        check("返回了 narrative", bool(result.narrative.strip()), result.narrative)
        check("返回了被提出的 actions", len(result.proposed_actions) == 1, result.proposed_actions)
        check("executed 是 move", result.executed_action and result.executed_action.type == "move",
              result.executed_action)
        check("events 里有 move",
              any(e["type"] == "move" for e in result.events), result.events)
        check("events 里带目的地", any(e.get("to_location") for e in result.events), result.events)

        result, after = turn("拿起泛黄的笔记")
        check("take 回合成功", result.ok is True, result.error)
        check("笔记进了背包",
              "泛黄的笔记" in [i["name"] for i in after["player"]["items"]], after["player"]["items"])
        check("地点里没有笔记了",
              "泛黄的笔记" not in [i["name"] for i in after["location"]["items"]], after["location"]["items"])
        check("events 里有 item_moved",
              any(e["type"] == "item_moved" for e in result.events), result.events)

        time_before = read_state()["world"]["time"]
        result, after = turn("休息")
        check("rest 回合成功", result.ok is True, result.error)
        check("世界时间变了", after["world"]["time"] != time_before, (time_before, after["world"]["time"]))
        check("events 里有 time_advance",
              any(e["type"] == "time_advance" for e in result.events), result.events)

        print("== 7. Turn Runtime：无法识别时什么都不做")
        state_before = read_state()
        with database.get_conn() as conn:
            fingerprint_before = game_state.capture_fingerprint(conn)
        result, state_after_unknown = turn("今天天气不错")
        with database.get_conn() as conn:
            fingerprint_after = game_state.capture_fingerprint(conn)
        check("回合仍然是 ok（只是没动作）", result.ok is True, result.error)
        check("没有提出任何动作", result.proposed_actions == [], result.proposed_actions)
        check("GameState 完全没变", fingerprint_before == fingerprint_after)
        check("没有产生事件", result.events == [], result.events)
        check("玩家仍看得到叙事", bool(texts(result.messages)), result.messages)

        # ============================================================
        print("== 8. 错误处理 B：AI 提出不存在的动作类型 → 拒绝")
        with database.get_conn() as conn:
            fingerprint_before = game_state.capture_fingerprint(conn)
        result, _ = turn("我要放火球", provider=UnknownActionProvider())
        with database.get_conn() as conn:
            fingerprint_after = game_state.capture_fingerprint(conn)
        check("回合失败", result.ok is False)
        check("有明确错误信息", bool(result.error), result.error)
        check("状态一点没变", fingerprint_before == fingerprint_after)
        check("没有产生任何事件", result.events == [], result.events)
        check("没有 executed_action", result.executed_action is None)
        check("错误里点名了未知类型", "fireball" in result.error, result.error)

        print("== 9. 错误处理 C：Schema 合法但引擎拒绝（move 到不存在的地方）")
        state_before = read_state()
        loc_before = state_before["location"]["name"]
        with database.get_conn() as conn:
            fingerprint_before = game_state.capture_fingerprint(conn)
        result, state_after = turn("去月亮")
        with database.get_conn() as conn:
            fingerprint_after = game_state.capture_fingerprint(conn)
        check("动作通过了 Schema（确实提出了 move）",
              len(result.proposed_actions) == 1 and result.proposed_actions[0].target == "月亮",
              result.proposed_actions)
        check("但回合被引擎拒绝", result.ok is False, result.error)
        check("地点没有变", state_after["location"]["name"] == loc_before, state_after["location"]["name"])
        check("状态指纹完全没变", fingerprint_before == fingerprint_after)
        check("没有虚假的成功事件", result.events == [], result.events)
        check("executed_action 为空", result.executed_action is None)
        check("错误信息说明没有这条路", "月亮" in texts(result.messages), result.messages)

        print("== 10. 错误处理 D：参数非法 / Provider 返回垃圾")
        result, _ = turn("休息", provider=BadArgumentProvider())
        check("非法参数回合失败", result.ok is False, result.error)
        check("错误提到 Schema 或参数", "Schema" in result.error or "minutes" in result.error, result.error)

        result, _ = turn("随便", provider=NotAResponseProvider())
        check("Provider 返回非 AIResponse → 拒绝", result.ok is False, result.error)
        check("拒绝时不产生事件", result.events == [], result.events)

        print("== 11. 单动作上限（Schema 兼容数组，Runtime 只执行一个）")
        make_game()  # 回到大厅，这样 move("阅览室") 才是合法出口
        with database.get_conn() as conn:
            fingerprint_before = game_state.capture_fingerprint(conn)
        result, _ = turn("边走边拿", provider=TwoActionProvider())
        with database.get_conn() as conn:
            fingerprint_after = game_state.capture_fingerprint(conn)
        check("两个动作都通过了 Schema", len(result.proposed_actions) == 2, result.proposed_actions)
        check("只执行了第一个（move）",
              result.executed_action is not None and result.executed_action.type == "move",
              result.executed_action)
        check("报告了被忽略的数量", result.ignored_actions == 1, result.ignored_actions)
        check("确实发生了移动", fingerprint_before != fingerprint_after)

        print("== 12. 既没叙事也没动作时给玩家一句说明")
        result, _ = turn("嗯", provider=SilentProvider())
        check("回合仍然 ok", result.ok is True, result.error)
        check("有一条说明性消息", bool(texts(result.messages)), result.messages)

        # ============================================================
        print("== 13. 回合写进历史（和斜杠指令共用同一套历史）")
        make_game()
        with database.get_conn() as conn:
            world_id = game_state.get_context(conn).world.id
            before_count = len(repository.list_events(conn, world_id, narrative_only=True))
        turn("我走进阅览室")
        with database.get_conn() as conn:
            events = repository.list_events(conn, world_id, narrative_only=True)
            raw = repository.list_events(conn, world_id)
        check("历史条数增加了", len(events) > before_count, (before_count, len(events)))
        check("叙事进了历史", any("Mock AI" in e.text for e in events), [e.text[:20] for e in events])
        check("结构化事件也落库了",
              any(e.kind == config.KIND_EVENT for e in raw), [e.kind for e in raw])

        print("== 14. GameState 复读一致（HTTP 层读到的就是它）")
        with database.get_conn() as conn:
            api_state = game_state.build_state(conn, include_history=True)
        check("state 里地点是阅览室", api_state["location"]["name"] == "阅览室", api_state["location"]["name"])
        check("state 带 history", "history" in api_state and len(api_state["history"]) > 0)

        # ============================================================
        print("== 15. 架构边界（AI Runtime 不得碰数据层）")
        ai_dir = os.path.join(ROOT, "backend", "ai")
        pure_modules = {"provider.py", "schema.py", "mock.py"}
        # Provider / Schema / Mock 连"状态机制"都不该 import。
        # 注意 actions 不在禁止列表里：schema.py 需要读 ACTIONS 注册表来知道
        # 有哪些 action type（单一事实来源）。读注册表 ≠ 改状态，
        # 所以下面额外断言它们不调用 perform()。
        forbidden_imports = {"repository", "sqlite3", "game_state"}
        offenders = []
        pure_offenders = []
        for filename in sorted(os.listdir(ai_dir)):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(ai_dir, filename), encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source)
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module.split(".")[-1])
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
            # 任何 ai 模块都不许直接碰 repository / SQL
            if "repository" in imported:
                offenders.append("%s imports repository" % filename)
            if any(word in source for word in ("INSERT INTO", "UPDATE ", "DELETE FROM")):
                offenders.append("%s 含内联 SQL" % filename)
            calls = [
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executescript")
            ]
            if calls:
                offenders.append("%s 调用 %s" % (filename, calls))
            # Provider / Schema 这些纯模块不该 import 状态机制
            if filename in pure_modules:
                hit = imported & forbidden_imports
                if hit:
                    pure_offenders.append("%s imports %s" % (filename, sorted(hit)))
                if ".perform(" in source:
                    pure_offenders.append("%s 调用了 Action Engine" % filename)

        check("backend/ai/ 下没有任何模块碰 repository 或写 SQL", offenders == [], offenders)
        check("Provider / Schema / Mock 不 import 数据层或状态层、也不执行动作",
              pure_offenders == [], pure_offenders)
        check("schema.py 只从 actions 读注册表（不执行）",
              ".perform(" not in open(os.path.join(ai_dir, "schema.py"), encoding="utf-8").read())
        check("Mock 只用标准库 + provider 模块",
              "game_state" not in open(os.path.join(ai_dir, "mock.py"), encoding="utf-8").read())

        print("== 16. Gameplay 变更仍然只有 Action Engine 一条路")
        runtime_src = open(os.path.join(ai_dir, "runtime.py"), encoding="utf-8").read()
        check("Turn Runtime 通过 Action Engine 执行动作",
              "action_engine.perform" in runtime_src, runtime_src[:0])
        mutation_calls = [
            name
            for name in (
                "update_world_location",
                "update_world_time",
                "update_player_vitals",
                "apply_item_state",
                "apply_npc_state",
                "delete_item",
                "delete_npc",
                "restore_snapshot",
                "new_game",
                "save_game",
                "load_game",
            )
            if name in runtime_src
        ]
        check("Turn Runtime 不直接调用任何状态变更函数", mutation_calls == [], mutation_calls)
        check("System Command 没有被塞进 Action Engine",
              not ({"new_game", "save", "load"} & set(actions.ACTIONS)), sorted(actions.ACTIONS))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (PASSED, FAILED))
    print("=" * 46)
    return 1 if FAILED else 0


def _empty_context():
    return AIContext(world={"name": "测试", "time": "第 1 日 08:00"}, current_location=None, player={"name": "甲"})


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    sys.exit(main())
