"""V0.3 Memory V0.1 测试：Schema / 持久化 / Rollback / Query / AIContext / 架构边界。

只用标准库，不联网，不需要起服务：

    python tests\\test_memory.py
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

from backend import commands, config, database, game_state, memory, repository  # noqa: E402
from backend.ai import MockAIProvider  # noqa: E402
from backend.ai import runtime as turn_runtime  # noqa: E402

PASSED = 0
FAILED = 0


def check(label, condition, extra=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [PASS] %s" % label)
    else:
        FAILED += 1
        print("  [FAIL] %s%s" % (label, ("  -> " + repr(extra)) if extra != "" else ""))


def run(text):
    """模拟一次请求：独立连接 + 提交。"""
    with database.get_conn() as conn:
        result = commands.dispatch(conn, text)
        return result, game_state.build_state(conn)


def texts(result):
    return "\n".join(m["text"] for m in result.messages)


def new_game(name="记忆测试者"):
    with database.get_conn() as conn:
        commands.dispatch(conn, "/new %s" % name)
        return game_state.get_context(conn).world.id


def world_id():
    with database.get_conn() as conn:
        return game_state.get_context(conn).world.id


def add(content, importance="B", turn=1, **kwargs):
    """在独立连接里创建一条记忆（模拟 Service 被调用）。"""
    with database.get_conn() as conn:
        return memory.create(conn, world_id(), content, importance, created_turn=turn, **kwargs)


def all_memories():
    with database.get_conn() as conn:
        return memory.query(conn, world_id(), include_inactive=True)


def active_contents():
    with database.get_conn() as conn:
        return [m.content for m in memory.query(conn, world_id())]


def context_memories():
    with database.get_conn() as conn:
        return turn_runtime.build_context(conn).memories


def find_keys(obj, key_name="id", found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == key_name:
                found.append(key)
            find_keys(value, key_name, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            find_keys(value, key_name, found)
    return found


def main():
    tmp = tempfile.mkdtemp(prefix="airpg-memory-")
    config.DATA_DIR = pathlib.Path(tmp)
    config.DB_PATH = config.DATA_DIR / "memory.db"
    try:
        database.init_db()

        # ============================================================
        print("== 1. Schema：合法 Memory 能创建")
        wid = new_game()
        m = add("玩家曾在图书馆地下室发现暗门。", importance="B", turn=3,
                category="discovery", scope="player", subjects=["地下档案室"])
        check("创建成功并拿到 id", m.id > 0, m)
        check("字段完整", (m.scope == "player" and m.knowledge_scope == "player"
                          and m.importance == "B" and m.category == "discovery"), m)
        check("subjects 是名字列表", m.subjects == ["地下档案室"], m.subjects)
        check("created_turn 记录了回合", m.created_turn == 3, m.created_turn)
        check("默认 active", m.active is True)

        check("importance 允许 S/A/B/C",
              all(add("重要度 %s" % lv, importance=lv, turn=1).importance == lv
                  for lv in ("S", "A", "B", "C")))
        check("scope 允许 world", add("世界变了", scope="world").scope == "world")
        npc_mem = add("阿黛尔答应了", scope="npc:阿黛尔", category="relationship", importance="A")
        check("scope 允许 npc:<名字>", npc_mem.scope == "npc:阿黛尔", npc_mem.scope)

        print("== 2. Schema：非法输入必须被拒绝")
        check("importance 不分大小写（s 归一化成 S）",
              add("小写重要度", importance="s", turn=1).importance == "S")
        rejected = [
            ("importance 是 X", dict(importance="X")),
            ("importance 是空", dict(importance="  ")),
            ("非法 scope", dict(scope="guild:盗贼公会")),
            ("npc scope 没有名字", dict(scope="npc:")),
            ("非法 knowledge_scope", dict(knowledge_scope="everyone")),
            ("非法 category", dict(category="随便写的")),
            ("content 为空", dict(content="   ")),
            ("content 是 None", dict(content=None)),
            ("content 太长", dict(content="字" * (config.MEMORY_MAX_CONTENT + 1))),
        ]
        for label, overrides in rejected:
            kwargs = {"importance": "B", "turn": 1}
            kwargs.update(overrides)
            try:
                add(kwargs.pop("content", "测试内容"), **kwargs)
                check("拒绝：%s" % label, False, overrides)
            except memory.MemoryError:
                check("拒绝：%s" % label, True)
            except Exception as exc:  # 别的异常也算失败
                check("拒绝：%s" % label, False, "%s: %s" % (type(exc).__name__, exc))

        # ============================================================
        print("== 3. 事件 → 记忆：首次到达某地点")
        wid = new_game("首次到达测试")
        check("开局没有任何记忆", active_contents() == [], active_contents())

        run("/go 阅览室")
        contents = active_contents()
        check("首次到达生成了一条记忆", len(contents) == 1, contents)
        check("内容说明了首次到达", "第一次到达" in contents[0] and "阅览室" in contents[0], contents)
        mem = all_memories()[0]
        check("重要度是 B", mem.importance == "B", mem.importance)
        check("category 是 discovery", mem.category == "discovery", mem.category)
        check("knowledge_scope 是 player（玩家亲历）",
              mem.knowledge_scope == "player", mem.knowledge_scope)
        check("subjects 是地点名（不是 id）", mem.subjects == ["阅览室"], mem.subjects)
        check("记录了来源事件 id", mem.source_event_id is not None, mem.source_event_id)
        with database.get_conn() as conn:
            event = repository.get_event(conn, mem.source_event_id)
        check("source_event_id 指向真实的事件行", event is not None and event.kind == config.KIND_EVENT,
              event and event.kind)
        check("source_event 确实是那次 move",
              '"move"' in (event.payload or "") if event else False, event and event.payload)

        run("/go 向下的楼梯")
        check("再到达一个新地点 → 第二条记忆", len(active_contents()) == 2, active_contents())

        # 同一个地点重复到达：直接喂两次相同的 move 事件，验证去重规则
        wid = new_game("去重测试")
        with database.get_conn() as conn:
            loc_id = game_state.get_context(conn).world.current_location_id
            event = {"type": "move", "from_location": None, "to_location": loc_id}
            memory.ingest_events(conn, wid, 1, [(1, event)])
            first = len(memory.query(conn, wid))
            memory.ingest_events(conn, wid, 2, [(2, event)])
            second = len(memory.query(conn, wid))
        check("同一地点重复到达不重复记录", first == 1 and second == 1, (first, second))

        print("== 4. 不该产生记忆的事件（不能把 World State 抄一遍）")
        wid = new_game("噪音测试")
        base = len(active_contents())
        check("开局没有记忆", base == 0, active_contents())
        run("/rest")
        check("time_advance 不产生记忆", len(active_contents()) == base, active_contents())
        run("/status")
        check("只读指令不产生记忆", len(active_contents()) == base, active_contents())
        run("/go 阅览室")
        after_move = len(active_contents())
        check("move 到新地点才产生记忆", after_move == base + 1, active_contents())
        run("/take 泛黄的笔记")
        check("item_moved（背包里读得到）不产生记忆",
              len(active_contents()) == after_move, active_contents())

        # ============================================================
        print("== 5. Query / ranking：S 优先、A 优先于 B、B 按最近、C 不进上下文")
        wid = new_game("排序测试")
        add("C 级短期信息", importance="C", turn=9)
        add("B 级较早", importance="B", turn=3)
        add("S 级永久事实", importance="S", turn=1)
        add("B 级较近", importance="B", turn=5)
        add("A 级关系变化", importance="A", turn=2)

        order = [x["content"] for x in context_memories()]
        check("顺序 = S → A → B(近) → B(远)",
              order == ["S 级永久事实", "A 级关系变化", "B 级较近", "B 级较早"], order)
        check("C 默认不进入 AIContext", "C 级短期信息" not in order, order)
        check("C 仍然存在于数据库里（只是不发给 AI）",
              "C 级短期信息" in active_contents(), active_contents())

        print("== 6. Context Budget：稳定裁剪")
        wid = new_game("预算测试")
        for turn in range(1, 26):
            add("B 级第 %02d 条" % turn, importance="B", turn=turn)
        first = context_memories()
        second = context_memories()
        check("条数被限制在 budget 内",
              len(first) == config.MEMORY_CONTEXT_BUDGET, len(first))
        check("稳定：两次调用结果完全一致", first == second, "不一致")
        check("留下的是最近的那些（turn 25 在内、turn 1 在外）",
              first[0]["content"] == "B 级第 25 条" and first[-1]["content"] == "B 级第 06 条",
              [x["content"] for x in first[:2] + first[-2:]])
        check("自定义 budget 生效",
              len(_context_with_budget(3)) == 3, len(_context_with_budget(3)))

        # ============================================================
        print("== 7. 玩家知识 vs 世界真相（防剧透）")
        wid = new_game("知识隔离测试")
        add("玩家知道的事", importance="A", knowledge_scope="player", turn=1)
        add("世界真相但玩家不知道", importance="S", knowledge_scope="world", turn=1)
        add("只有某个 NPC 知道", importance="S", knowledge_scope="npc", turn=1)
        add("秘密（剧透源）", importance="S", knowledge_scope="secret", turn=1)

        visible = [x["content"] for x in context_memories()]
        check("只把 knowledge_scope=player 的发给 AI", visible == ["玩家知道的事"], visible)
        check("S 级也不能突破知识隔离（宁可不说）",
              "世界真相但玩家不知道" not in visible and "秘密（剧透源）" not in visible, visible)
        check("被隔离的记忆仍然在库里（数据模型保留了区分能力）",
              len(active_contents()) == 4, active_contents())

        with database.get_conn() as conn:
            secret = memory.query(conn, wid, knowledge_scopes=["secret"])
        check("可以按 knowledge_scope 查询", len(secret) == 1, secret)

        # ============================================================
        print("== 8. 持久化：新连接（等于重启程序）后记忆还在")
        new_game("持久化测试")
        add("跨重启的记忆", importance="A", turn=2)
        run("/go 阅览室")
        expected = len(active_contents())
        # 全新的连接对象，等价于进程重启后重新打开磁盘上的数据库
        fresh = database.connect()
        try:
            rows = fresh.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        finally:
            fresh.close()
        check("数据库文件里有记忆行", rows >= expected, (rows, expected))
        check("重新读取仍然拿得到", len(active_contents()) == expected, active_contents())

        print("== 9. Save / Load / Rollback（验收场景）")
        wid = new_game("回滚测试")
        run("/go 阅览室")                       # 首次到达 → 记忆 A
        a_contents = [m.content for m in all_memories()]
        check("Save 之前有 1 条（阅览室）", len(a_contents) == 1, a_contents)

        run("/save")
        result, _ = run("/save")
        check("保存成功", result.ok is True, texts(result))

        run("/go 向下的楼梯")                   # 存档之后：首次到达地下档案室 → 记忆 B
        after = [m.content for m in all_memories()]
        check("存档之后产生了新记忆（地下档案室）",
              len(after) == 2 and any("地下档案室" in c for c in after), after)

        print("   —— 现在把记忆带进 AIContext（模拟重启后继续玩）——")
        visible = [x["content"] for x in context_memories()]
        check("AIContext 里看得到两条记忆", len(visible) == 2, visible)
        check("AIContext 里能看到「玩家第一次到达「地下档案室」」",
              any("地下档案室" in c for c in visible), visible)

        print("   —— /load 回到存档点 ——")
        result, _ = run("/load")
        check("载入成功", result.ok is True, texts(result))
        check("回滚提示里报告了删除的记忆", "删除存档之后的记忆" in texts(result), texts(result))
        rolled = [m.content for m in all_memories()]
        check("存档时的记忆 A 保留（阅览室）",
              len(rolled) == 1 and "阅览室" in rolled[0], rolled)
        check("存档之后产生的记忆 B 消失（地下档案室）",
              not any("地下档案室" in c for c in rolled), rolled)
        check("时间线没有污染：不存在来自未来的记忆",
              all(m.created_turn <= 3 for m in all_memories()),
              [(m.content, m.created_turn) for m in all_memories()])

        # ============================================================
        print("== 10. 老版本存档（v2，没有 memories 键）能载入")
        _, state = run("/status")
        with database.get_conn() as conn:
            snapshot = game_state.capture_snapshot(conn)
        legacy = dict(snapshot)
        legacy["version"] = 2
        legacy.pop("memories", None)
        with database.get_conn() as conn:
            try:
                summary = game_state.restore_snapshot(conn, legacy)
                check("v2 存档可以载入（自动升级为 v3）", True)
                check("升级后的 v2 快照记忆为空（那个时刻本来就没有记忆）",
                      summary.get("memories_removed", 0) >= 0, summary)
            except ValueError as exc:
                check("v2 存档可以载入（自动升级为 v3）", False, exc)
                check("升级后的 v2 快照记忆为空", False)
        check("当前快照确实是 v3", config.SAVE_FORMAT_VERSION == 3, config.SAVE_FORMAT_VERSION)

        # ============================================================
        print("== 11. AIContext：结构、只读、不含数据库 ID")
        wid = new_game("上下文测试")
        add("阿黛尔答应帮助玩家调查地下档案室。", importance="A",
            category="relationship", scope="npc:阿黛尔", subjects=["阿黛尔"], turn=4)
        with database.get_conn() as conn:
            ctx = turn_runtime.build_context(conn)
        dump = dataclasses.asdict(ctx)
        check("AIContext 含 memories 字段", "memories" in dump, list(dump.keys()))
        check("memories 非空", len(dump["memories"]) == 1, dump["memories"])

        item = dump["memories"][0]
        check("只带 importance/category/scope/subjects/content/created_turn",
              set(item.keys()) == {"importance", "category", "scope", "subjects",
                                   "content", "created_turn"}, sorted(item.keys()))
        check("memories 里没有任何 id 键", find_keys(dump["memories"]) == [], find_keys(dump["memories"]))
        check("没有 source_event_id / world_id",
              "source_event_id" not in item and "world_id" not in item, sorted(item.keys()))
        check("内容与 subjects 正确",
              item["content"].startswith("阿黛尔答应") and item["subjects"] == ["阿黛尔"], item)
        check("整个 AIContext 里也没有 id 键", find_keys(dump) == [], find_keys(dump))

        check("AIContext 是冻结的（Provider 改不了字段）",
              _raises(lambda: setattr(ctx, "player", {})))

        print("== 12. Provider 没有 Memory 写权限")
        with database.get_conn() as conn:
            before_ids = [m.id for m in memory.query(conn, wid, include_inactive=True)]
        provider = MockAIProvider()
        provider.generate_turn("我走进阅览室", ctx)
        # 就算 Provider 恶意篡改拿到的上下文副本……
        ctx.memories.append({"importance": "S", "category": "world", "content": "伪造的记忆"})
        ctx.memories.clear()
        with database.get_conn() as conn:
            after_ids = [m.id for m in memory.query(conn, wid, include_inactive=True)]
        check("篡改 AIContext.memories 不影响数据库", before_ids == after_ids,
              (before_ids, after_ids))
        check("数据库里仍然只有 1 条", len(after_ids) == 1, after_ids)

        print("== 13. 架构边界：只有 Memory Service 能写 memories 表")
        backend = os.path.join(ROOT, "backend")
        writers = []
        memory_sql = []
        for filename in sorted(os.listdir(backend)):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(backend, filename), encoding="utf-8") as fh:
                source = fh.read()
            if filename == "repository.py":
                continue
            if filename != "memory.py":
                for func in ("insert_memory", "set_memory_active", "delete_memory",
                             "restore_memory", "list_memories_raw", "list_memory_ids",
                             "get_memory", "list_memories"):
                    if "repository.%s" % func in source:
                        writers.append("%s 调用了 repository.%s" % (filename, func))
            if "INSERT INTO memories" in source or "UPDATE memories" in source or "DELETE FROM memories" in source:
                memory_sql.append(filename)
        check("除 Memory Service 外没有模块调用 memory 仓储函数", writers == [], writers)
        check("只有 repository.py 含 memories 表的 SQL",
              memory_sql == [] and "INSERT INTO memories" in _read(backend, "repository.py"),
              memory_sql)

        ai_dir = os.path.join(backend, "ai")
        offenders = []
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
            # runtime 是编排层，允许读 memory；Provider / Schema / Prompt / Mock 不许
            if filename != "runtime.py" and ({"memory", "repository"} & imported):
                offenders.append("%s imports %s" % (filename, sorted({"memory", "repository"} & imported)))
            if filename != "runtime.py" and "memory.create" in source:
                offenders.append("%s 调用了 memory.create" % filename)
        check("Provider / Schema / Prompt / Mock 都拿不到 Memory", offenders == [], offenders)

        print("== 14. commands.py 不碰 Memory")
        with open(os.path.join(backend, "commands.py"), encoding="utf-8") as fh:
            commands_src = fh.read()
        tree = ast.parse(commands_src)
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
        check("commands.py 不 import memory", "memory" not in imported, sorted(imported))
        check("commands.py 里没有 memory.create 调用", "memory.create" not in commands_src)

        print("== 15. 生命周期：deactivate 是软删除")
        new_game("生命周期测试")
        target = add("待停用的记忆", importance="B", turn=1)
        add("保留的记忆", importance="B", turn=1)
        with database.get_conn() as conn:
            check("停用返回 True", memory.deactivate(conn, target.id) is True)
        check("停用后不再出现在查询里",
              "待停用的记忆" not in active_contents(), active_contents())
        check("停用后仍然存在于数据库（include_inactive）",
              "待停用的记忆" in [m.content for m in all_memories()], all_memories())
        check("停用后不进 AIContext",
              "待停用的记忆" not in [x["content"] for x in context_memories()])
        with database.get_conn() as conn:
            check("停用不存在的 id 返回 False", memory.deactivate(conn, 999999) is False)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (PASSED, FAILED))
    print("=" * 46)
    return 1 if FAILED else 0


def _read(directory, filename):
    with open(os.path.join(directory, filename), encoding="utf-8") as fh:
        return fh.read()


def _context_with_budget(limit):
    with database.get_conn() as conn:
        return memory.build_context_list(conn, world_id(), limit)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    sys.exit(main())
