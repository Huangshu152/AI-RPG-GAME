"""V0.1 冒烟测试：跑通「创建玩家 → 指令 → 改状态 → 保存 → 重启 → 读回」。

只用标准库，不需要装 fastapi 就能运行：

    python tests\test_smoke.py

它在一个临时目录里建独立的数据库，不会碰 data/game.db。
"""

import ast
import dataclasses
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 只有在输出被重定向/管道时才强制 UTF-8。
# 真实控制台交给 Python 自己处理（Windows 下走 Unicode 控制台 API，中文正常），
# 强行改成 UTF-8 反而会在 GBK 控制台里变成乱码。
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # 老解释器没有 reconfigure
        pass

from backend import actions, commands, config, database, game_state, repository  # noqa: E402

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
    """模拟一次 HTTP 请求：独立连接 + 提交。"""
    with database.get_conn() as conn:
        result = commands.dispatch(conn, text)
        return result, game_state.build_state(conn)


def read_state():
    with database.get_conn() as conn:
        return game_state.build_state(conn)


def texts(result):
    return "\n".join(m["text"] for m in result.messages)


def use_temp_db():
    tmp = tempfile.mkdtemp(prefix="airpg-smoke-")
    config.DATA_DIR = pathlib.Path(tmp)
    config.DB_PATH = config.DATA_DIR / "test.db"
    return tmp


def main():
    tmp = use_temp_db()
    try:
        print("== 1. 建库（临时目录 %s）" % tmp)
        database.init_db()
        check("数据库文件已创建", config.DB_PATH.exists(), config.DB_PATH)

        print("== 2. 没有游戏时的状态")
        state = read_state()
        check("has_game 为 False", state["has_game"] is False, state)
        result, _ = run("/status")
        check("未创建游戏时 /status 报错", result.ok is False, texts(result))

        print("== 3. 创建玩家")
        result, state = run("/new 林默")
        check("/new 成功", result.ok is True, texts(result))
        check("has_game 为 True", state["has_game"] is True)
        check("玩家名 = 林默", state["player"]["name"] == "林默", state["player"]["name"])
        check("HP = 12/12", (state["player"]["hp"], state["player"]["max_hp"]) == (12, 12), state["player"])
        check("SAN = 50/50", (state["player"]["san"], state["player"]["max_san"]) == (50, 50), state["player"])
        check("起始地点 = 灰塔图书馆·大厅", state["location"]["name"] == "灰塔图书馆·大厅", state["location"])
        inv = {i["name"]: i["quantity"] for i in state["player"]["items"]}
        check("初始物品 = 手电筒×1 + 绷带×2", inv == {"手电筒": 1, "绷带": 2}, inv)
        check("世界时间 = 第 1 日 08:00", state["world"]["time"] == "第 1 日 08:00", state["world"]["time"])
        check("NPC 已就位（3 个）", len(state["npcs"]) == 3, state["npcs"])

        print("== 4. /look 当前地点")
        result, state = run("/look")
        body = texts(result)
        check("/look 成功", result.ok is True, body)
        check("/look 描述当前地点", "灰塔图书馆·大厅" in body, body)
        check("/look 列出出口", "阅览室" in body and "大门" in body, body)
        check("/look 列出在场 NPC", "阿黛尔" in body, body)
        check("/look 列出可见物品", "老旧的黄铜钥匙" in body, body)

        print("== 5. /status 与 /inventory")
        result, _ = run("/status")
        check("/status 包含 HP/SAN/时间", "HP" in texts(result) and "SAN" in texts(result) and "第 1 日" in texts(result), texts(result))
        result, _ = run("/inventory")
        check("/inventory 列出绷带×2", "绷带 ×2" in texts(result), texts(result))

        print("== 6. /go 移动")
        result, state = run("/go 阅览室")
        check("/go 成功", result.ok is True, texts(result))
        check("地点已变为阅览室", state["location"]["name"] == "阅览室", state["location"]["name"])
        result, state = run("/go 不存在的地方")
        check("错误出口被拒绝", result.ok is False, texts(result))

        print("== 7. /take 拾取物品")
        result, state = run("/take 泛黄的笔记")
        check("/take 成功", result.ok is True, texts(result))
        check("笔记进入背包", "泛黄的笔记" in [i["name"] for i in state["player"]["items"]], state["player"]["items"])
        check("地点物品已清空", state["location"]["items"] == [], state["location"]["items"])

        print("== 8. /rest 恢复少量 HP")
        player_id = state["player"]["id"]
        with database.get_conn() as conn:  # 制造一点伤势（V0.1 还没有战斗）
            repository.update_player_vitals(conn, player_id, 5, 40)
        result, state = run("/rest")
        check("HP 5 → 8（+3）", state["player"]["hp"] == 8, state["player"]["hp"])
        check("SAN 40 → 42（+2）", state["player"]["san"] == 42, state["player"]["san"])
        check("世界时间推进到 09:00", state["world"]["time"] == "第 1 日 09:00", state["world"]["time"])
        result, state = run("/rest")
        check("继续休息 HP 8 → 11", state["player"]["hp"] == 11, state["player"]["hp"])
        result, state = run("/rest")
        check("HP 在上限处被截断（11 → 12）", state["player"]["hp"] == 12, state["player"]["hp"])
        result, state = run("/rest")
        check("HP 已满时休息不再增长", state["player"]["hp"] == 12, state["player"]["hp"])

        print("== 9. /save 保存")
        result, pre_save = run("/save")
        check("/save 成功", result.ok is True, texts(result))
        hp_at_save = pre_save["player"]["hp"]
        san_at_save = pre_save["player"]["san"]
        time_at_save = pre_save["world"]["time"]
        with database.get_conn() as conn:
            saved = repository.get_latest_save(
                conn, world_id=pre_save["world"]["id"], slot=config.DEFAULT_SLOT
            )
        check("存档行已写入", saved is not None)
        check("存档含快照 JSON", bool(saved and saved["snapshot"]), saved and saved["snapshot"][:60])

        print("== 10. 保存后继续改状态，再 /load 回滚")
        run("/go 向下的楼梯")
        moved = read_state()
        check("已移动到地下档案室", moved["location"]["name"] == "地下档案室", moved["location"]["name"])
        result, rolled = run("/load")
        check("/load 成功", result.ok is True, texts(result))
        check("回滚到保存时的地点（阅览室）", rolled["location"]["name"] == "阅览室", rolled["location"]["name"])
        check("回滚到保存时的 HP（%d）" % hp_at_save, rolled["player"]["hp"] == hp_at_save, rolled["player"]["hp"])
        check("回滚到保存时的 SAN（%d）" % san_at_save, rolled["player"]["san"] == san_at_save, rolled["player"]["san"])
        check("回滚到保存时的世界时间", rolled["world"]["time"] == time_at_save, rolled["world"]["time"])
        check("回滚后背包仍有笔记", "泛黄的笔记" in [i["name"] for i in rolled["player"]["items"]], rolled["player"]["items"])

        print("== 11. 模拟重启程序：全新连接重新读取")
        fresh = read_state()
        check("重启后玩家仍是林默", fresh["player"]["name"] == "林默", fresh["player"])
        check("重启后地点仍是阅览室", fresh["location"]["name"] == "阅览室", fresh["location"])
        check("重启后 HP 保持 %d" % hp_at_save, fresh["player"]["hp"] == hp_at_save, fresh["player"]["hp"])
        check("重启后世界时间保持", fresh["world"]["time"] == time_at_save, fresh["world"])

        print("== 12. 自由输入与其他分支")
        result, _ = run("我推开门")
        check("自由输入不报错但提示未接入 AI", result.ok is True and "AI" in texts(result), texts(result))
        result, _ = run("/foo")
        check("未知指令被拒绝", result.ok is False and "未知指令" in texts(result), texts(result))
        result, _ = run("/help")
        check("/help 列出全部指令", all(c in texts(result) for c in ["/look", "/status", "/inventory", "/rest", "/save", "/load"]), texts(result))
        result, _ = run("/")
        check("单独的 / 等价于 /help", result.ok is True and "/look" in texts(result), texts(result))

        print("== 13. 自定义存档位")
        result, _ = run("/save 章节一")
        check("/save 章节一 成功", result.ok is True, texts(result))
        with database.get_conn() as conn:
            slots = sorted(s["slot"] for s in repository.list_saves(conn))
        check("两个存档位都存在", slots == ["quicksave", "章节一"], slots)
        result, _ = run("/load 章节一")
        check("/load 章节一 成功", result.ok is True, texts(result))
        result, _ = run("/load 不存在的存档位")
        check("载入不存在的存档位会报错", result.ok is False, texts(result))

        print("== 14. 新游戏不破坏旧存档")
        result, new_state = run("/new 第二人")
        check("可以开新游戏", result.ok is True and new_state["player"]["name"] == "第二人", texts(result))
        check("新世界是新实例", new_state["world"]["id"] != rolled["world"]["id"], (new_state["world"]["id"], rolled["world"]["id"]))
        with database.get_conn() as conn:
            old_world_save = repository.get_latest_save(conn, world_id=rolled["world"]["id"], slot="章节一")
        check("旧世界的存档仍在", old_world_save is not None)
        result, back = run("/load 章节一")
        check("可以载回旧世界", result.ok is True and back["player"]["name"] == "林默", texts(result))

        print("== 15. 数据库确实落盘")
        size = config.DB_PATH.stat().st_size
        check("game.db 大小 > 0", size > 0, size)

        print("== 16. P0-2 快照是权威的：存档之后新建的东西必须被回滚掉")
        _, st16 = run("/new 甲")
        wid16 = st16["world"]["id"]
        pid16 = st16["player"]["id"]
        run("/save")
        with database.get_conn() as conn:  # 模拟"AI 在剧情里生成了道具和 NPC"
            repository.insert_item(
                conn, wid16, "AI 生成的黑曜石匕首", "存档时还不存在", 1,
                config.OWNER_PLAYER, pid16,
            )
            repository.insert_npc(conn, wid16, "AI 生成的幽灵", st16["location"]["id"], "刚出现")
        _, before16 = run("/inventory")
        check(
            "存档后新建的物品确实在背包里",
            "AI 生成的黑曜石匕首" in [i["name"] for i in before16["player"]["items"]],
            before16["player"]["items"],
        )
        result16, _ = run("/load")
        _, after16 = run("/inventory")
        check(
            "载入后：存档里没有的物品被删除",
            "AI 生成的黑曜石匕首" not in [i["name"] for i in after16["player"]["items"]],
            after16["player"]["items"],
        )
        check(
            "载入后：存档里没有的 NPC 被删除",
            "AI 生成的幽灵" not in [n["name"] for n in after16["npcs"]],
            [n["name"] for n in after16["npcs"]],
        )
        check("载入消息报告了这次回滚", "删掉存档里没有的物品" in texts(result16), texts(result16))

        print("== 17. P0-2 快照是权威的：存档时存在、之后被删的东西必须被补回")
        _, st17 = run("/new 乙")
        loc17 = st17["location"]["id"]
        run("/save")
        with database.get_conn() as conn:
            key17 = repository.find_item(conn, config.OWNER_LOCATION, loc17, "老旧的黄铜钥匙")
            check("存档时地点里有黄铜钥匙", key17 is not None)
            repository.delete_item(conn, key17.id)
        _, gone17 = run("/look")
        check(
            "手动删除后地点里没有钥匙了",
            "老旧的黄铜钥匙" not in [i["name"] for i in gone17["location"]["items"]],
            gone17["location"]["items"],
        )
        result17, _ = run("/load")
        _, back17 = run("/look")
        check(
            "载入后被删的物品被补回",
            "老旧的黄铜钥匙" in [i["name"] for i in back17["location"]["items"]],
            back17["location"]["items"],
        )
        check("载入消息报告了补回", "补回被删的物品" in texts(result17), texts(result17))

        print("== 18. P1-7 上限（max_hp）也是状态，随快照回滚")
        pid17 = back17["player"]["id"]
        with database.get_conn() as conn:  # 模拟上限被别的东西改过（走 repository，不写裸 SQL）
            cur18 = repository.get_player(conn, pid17)
            repository.restore_player(conn, pid17, cur18.name, cur18.hp, 99, cur18.san, cur18.max_san)
        _, rolled18 = run("/load")
        check("载入后 max_hp 回到快照里的 12", rolled18["player"]["max_hp"] == 12, rolled18["player"])

        print("== 19. P0-1 结构化 events（由状态差分得出）")
        with database.get_conn() as conn:
            repository.update_player_vitals(conn, pid17, 5, 40)
        result19, _ = run("/rest")
        types19 = [e["type"] for e in result19.events]
        check("/rest 产出了结构化事件", bool(types19), result19.events)
        check(
            "事件里带 HP 5 → 8（而不是只有一句中文）",
            any(e.get("field") == "hp" and e.get("from") == 5 and e.get("to") == 8 for e in result19.events),
            result19.events,
        )
        check(
            "事件里带时间推进 60 分钟",
            any(e["type"] == "time_advance" and e.get("minutes") == 60 for e in result19.events),
            result19.events,
        )

        print("== 20. P0-3 历史落库，并且剧情跟着存档一起回滚")
        _, st20 = run("/new 丙")
        wid20 = st20["world"]["id"]
        run("/look")
        run("/save")
        with database.get_conn() as conn:
            hist_at_save = repository.list_events(conn, wid20, narrative_only=True)
        check("历史已经落库", len(hist_at_save) > 0, len(hist_at_save))
        check("历史里有多条叙事", any(e.kind == "narration" for e in hist_at_save), [e.kind for e in hist_at_save])

        run("/status")
        run("/inventory")
        with database.get_conn() as conn:
            texts_later = [e.text for e in repository.list_events(conn, wid20, narrative_only=True)]
        check(
            "存档之后的回合也被记进历史",
            any(t.startswith("背包（") for t in texts_later),
            texts_later,
        )
        run("/load")
        with database.get_conn() as conn:
            texts_rolled = [e.text for e in repository.list_events(conn, wid20, narrative_only=True)]
        check(
            "载入后看不到存档之后的剧情（背包输出被截断）",
            not any(t.startswith("背包（") for t in texts_rolled),
            texts_rolled,
        )

        print("== 21. 刷新页面能读回剧情（/api/state 的 history）")
        with database.get_conn() as conn:
            with_history = game_state.build_state(conn, include_history=True)
        check("带 history 字段", "history" in with_history, list(with_history.keys()))
        check("history 非空", len(with_history["history"]) > 0, with_history.get("history"))
        check(
            "history 只含给人读的行（不含结构化 event 行）",
            all(h["kind"] != config.KIND_EVENT for h in with_history["history"]),
            with_history["history"][:3],
        )
        with database.get_conn() as conn:
            plain_state = game_state.build_state(conn)
        check("普通 build_state 不背历史（指令响应用）", "history" not in plain_state, list(plain_state.keys()))

        print("== 22. 结构化事件也落库了（供将来的 AI 消费）")
        with database.get_conn() as conn:
            raw20 = repository.list_events(conn, wid20)
        event_rows = [e for e in raw20 if e.kind == config.KIND_EVENT]
        check("库里存在 kind=event 的行", len(event_rows) > 0, len(event_rows))
        check(
            "event 行的 payload 是合法 JSON",
            all(json.loads(e.payload) for e in event_rows),
            [e.payload[:40] for e in event_rows[:3]],
        )

        print("== 23. 架构边界：命令层不碰数据层，只有 repository 能写 SQL")
        backend_dir = os.path.join(ROOT, "backend")

        with open(os.path.join(backend_dir, "commands.py"), encoding="utf-8") as fh:
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
        check("commands.py 不 import repository", "repository" not in imported, sorted(imported))
        check("commands.py 不 import world_data", "world_data" not in imported, sorted(imported))

        raw_sql_calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("execute", "executescript")
        ]
        check("commands.py 里没有 conn.execute 之类的裸 SQL 调用", raw_sql_calls == [], raw_sql_calls)

        allowed_writers = {"repository.py", "database.py"}  # repository 是全项目唯一写 DML 的地方；database 只负责建表 DDL
        offenders = []
        for filename in sorted(os.listdir(backend_dir)):
            if not filename.endswith(".py") or filename in allowed_writers:
                continue
            with open(os.path.join(backend_dir, filename), encoding="utf-8") as fh:
                body = fh.read()
            for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                if keyword in body:
                    offenders.append("%s 含 %r" % (filename, keyword))
        check("除 repository/database 外没有任何模块内联写 SQL", offenders == [], offenders)

        print("== 24. 验证失败时状态一动不动（Intent → Validation 先于 Mutation）")
        run("/new 校验者")
        with database.get_conn() as conn:
            ctx = game_state.get_context(conn)
            before24 = game_state.capture_fingerprint(conn)
            res24 = actions.perform(conn, ctx, actions.Action("move", "不存在的地方"))
            after24 = game_state.capture_fingerprint(conn)
        check("非法移动被拒绝", res24.ok is False, res24.messages)
        check("拒绝时状态指纹完全没变", before24 == after24)
        check("拒绝时不产生任何事件", res24.events == [], res24.events)

        with database.get_conn() as conn:
            ctx = game_state.get_context(conn)
            res24b = actions.perform(conn, ctx, actions.Action("不存在的动作"))
        check("未知动作被干净地拒绝", res24b.ok is False and "未知动作" in res24b.messages[0]["text"],
              res24b.messages)

        print("== 25. 未来 AI 的路径：不写指令文本，直接提交 Action")
        run("/new 行动者")
        with database.get_conn() as conn:
            ctx = game_state.get_context(conn)
            res25 = commands.run_action(conn, ctx, actions.Action(type="move", target="阅览室"))
        check("Action(type=move) 成功", res25.ok is True, res25.messages)
        check("产出了 move 事件", any(e["type"] == "move" for e in res25.events), res25.events)
        check("移动后有场景描述（和 /go 一样的输出）",
              any("阅览室" in m["text"] for m in res25.messages), res25.messages)

        with database.get_conn() as conn:
            ctx = game_state.get_context(conn)
            res25b = commands.run_action(conn, ctx, actions.Action(type="take", target="泛黄的笔记"))
        check("Action(type=take) 成功", res25b.ok is True, res25b.messages)
        check("产出了 item_moved 事件",
              any(e["type"] == "item_moved" for e in res25b.events), res25b.events)

        with database.get_conn() as conn:
            ctx = game_state.get_context(conn)
            res25c = commands.run_action(conn, ctx, actions.Action(type="rest", arguments={"minutes": 120}))
        check("Action(type=rest, minutes=120) 成功", res25c.ok is True, res25c.messages)
        check("休息 120 分钟推进了 2 小时",
              any(e["type"] == "time_advance" and e["minutes"] == 120 for e in res25c.events),
              res25c.events)

        check("便捷构造器可用（对应需求里的示例写法）",
              actions.Action.move("x").type == "move"
              and actions.Action.take("y").target == "y"
              and actions.Action.rest().argument("minutes") == config.REST_MINUTES)

        with database.get_conn() as conn:
            ctx25 = game_state.get_context(conn)
            wid25 = ctx25.world.id
            hist25 = repository.list_events(conn, wid25, narrative_only=True)
        check("走 Action 路径的回合也进了历史", len(hist25) > 0, len(hist25))

        print("== 26. Action Engine 只装 Gameplay Action")
        catalog = {a["type"] for a in actions.action_catalog()}
        check("注册表恰好是 move / take / rest",
              catalog == {"move", "take", "rest"}, sorted(catalog))
        check("每个动作都带 summary（可生成给 AI 的描述）",
              all(a["summary"] for a in actions.action_catalog()), actions.action_catalog())
        spec_fields = {f.name for f in dataclasses.fields(actions.ActionSpec)}
        check("ActionSpec 没有被 save/load 撑出来的多余抽象（mutates / build_events）",
              spec_fields == {"name", "summary", "validate", "execute"}, sorted(spec_fields))

        print("== 27. 需求第 9 条：Gameplay Action 与 System Command 分开")
        by_kind = {}
        for spec in commands.COMMANDS.values():
            by_kind.setdefault(spec.kind, set()).add(spec.name)
        check("Gameplay = go / take / rest",
              by_kind.get("gameplay") == {"go", "take", "rest"}, by_kind)
        check("System = new / save / load",
              by_kind.get("system") == {"new", "save", "load"}, by_kind)
        check("Read = look / status / inventory / help",
              by_kind.get("read") == {"look", "status", "inventory", "help"}, by_kind)
        check("Gameplay 指令全部指向 Action Engine",
              all(commands.COMMANDS[n].action is not None for n in ("go", "take", "rest")), None)
        check("System 指令全部不经过 Action Engine（不伪装成 Gameplay）",
              all(commands.COMMANDS[n].action is None for n in ("new", "save", "load")), None)

        run("/new 存档者")
        with database.get_conn() as conn:
            before27 = game_state.capture_fingerprint(conn)
            res27 = commands.dispatch(conn, "/save")
            after27 = game_state.capture_fingerprint(conn)
        check("/save 完全不改动游戏世界状态", before27 == after27)
        check("/save 仍声明了 saved 事件",
              [e["type"] for e in res27.events] == ["saved"], res27.events)

        with database.get_conn() as conn:
            ctx27 = game_state.get_context(conn)
            repository.update_player_vitals(conn, ctx27.player.id, 1, 1)
        res27b, _ = run("/load")
        check("/load 走受控恢复入口", res27b.ok is True, res27b.messages)
        check("/load 产生 rollback 事件（但不是 Gameplay Action）",
              [e["type"] for e in res27b.events] == ["rollback"], res27b.events)
        _, state27 = run("/status")
        check("/load 确实把状态还原了（HP 回到 12）",
              state27["player"]["hp"] == 12, state27["player"])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (PASSED, FAILED))
    print("=" * 46)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
