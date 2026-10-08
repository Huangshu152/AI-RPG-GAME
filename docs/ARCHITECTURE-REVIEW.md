# 架构审查报告 —— V0.1 → V0.2 之前该做什么

审查对象：AI RPG Engine V0.1（`backend/` + `frontend/` + `tests/`，20 个文件，88 项测试全绿）。
审查基准：**长期目标**，而不是"代码好不好看"：

1. 玩家可以进入不同的世界 / 剧本
2. AI 负责叙事、NPC 扮演、动态剧情
3. 程序负责世界状态、角色状态、规则、存档
4. 世界 / 剧本最终可以作为独立 World Pack 发布和导入
5. 未来允许不同 AI Provider

方法：先按长期目标找"会让未来返工"的结构问题，每条必须有代码证据；能动手验证的就做实验，
不靠猜。**V0.1 已经冻结并通过测试，所以本报告不主张重写，只给最小修改方案。**

---

## 实施状态

本报告写完之后，三个 P0 已经实施（V0.1 不再"冻结"，但每步都跑过全量测试）：

| 编号 | 项目 | 状态 |
| --- | --- | --- |
| P0-1 | 结构化 events 通道 | ✅ 已实施。**做法和本报告的建议不同**：不是让 handler 手写事件，而是由「状态指纹差分」在 `dispatch()` 边界自动产出。手写的事件会和真实状态漂移，差分出来的按定义就是"实际变了什么"，而且新增指令不需要埋点。 |
| P0-2 | 快照成为权威回滚 | ✅ 已实施（先删后写 + 按原 id 补回 + 报告回滚摘要）。报告里那两个实验现在变成了回归测试。 |
| P0-3 | 事件表 + 历史持久化 | ✅ 已实施。额外做了"剧情随存档一起回滚"：`/load` 会截断存档之后的剧情，前端按 `rollback` 事件重建日志，刷新页面也能读回剧情。 |
| P1-7 | 快照恢复 `max_hp` / `max_san` | ✅ 已实施 |
| P2-1 | 删掉死代码 `list_locations` | ✅ 已实施 |
| P2-2 | `saves.label` 只写不读 | ✅ 已实施（现在 `/load` 会显示存档内容与当前角色） |
| P2-4 | 没有回合 / 序号概念 | ✅ 已实施（`events.seq`，用 `MAX(seq)+1` 推导，避免为加列引入 schema 迁移） |
| P2-10 | `ok` 语义不可靠 | ⚠️ 部分改善。现在有 `events` 可判断"这一回合是否真有变更"，但 `ok` 仍然只表示"没有 error 消息"。 |
| P1-1 | 状态变更收口（`commands` 不再直连 `repository`） | ✅ 已实施。新增 `backend/actions.py` 作为 **Gameplay Action Engine**（Intent → Validation → Mutation → Event）；`/new` `/save` `/load` 作为 **System Command** 留在 `game_state` 的受控入口，**刻意不伪装成 Action**（把存档抽象成 Gameplay Action 是过度统一）。`commands.py` 现在完全不 import `repository` / `world_data`，也没有裸 SQL。边界由**测试强制**：AST 断言 + 全模块扫描 + 指令分类断言。 |
| P1-2 | 世界包自带规则 | ⬜ 未做 |
| P1-3 | 世界定义 / 实例分离 + `pack_id` | ⬜ 未做 |
| P1-4 | 存档格式迁移路径 | ⬜ 未做。`SAVE_FORMAT_VERSION` 已升到 2，但**当前不存在任何 v1 存档**（数据库是空的），所以没有写升级分支——避免造出没有实际用途的代码。等真有旧存档时再做。 |
| P1-6 | 裸 `/load` 跨世界 | ⬜ 未做 |
| P1-8 | schema 迁移机制 | ⬜ 未做。本次只**新增表**（`CREATE TABLE IF NOT EXISTS` 已足够），没有给已有表加列，所以暂时不需要迁移。 |
| P1-9 | 物品 `def_id` / `origin` | ⬜ 未做 |

测试规模从 88 项增长到 **140 项**（110 项逻辑 + 30 项接口），全绿。
其中第 23~27 组是**架构边界测试**：用 AST 断言命令层不 import 数据层、
全模块扫描确认没有内联 SQL、失败的动作状态指纹不变、绕过指令文本直接提交 `Action`，
以及断言「Gameplay Action」与「System Command」两类没有被混成一个概念。

---

## 0. 结论

骨架方向是对的，值得保留的部分见第 4 节。但有 **3 个 P0**：它们现在都还"看不出问题"，
因为 V0.1 世界里不能创建、也不能销毁任何东西；一旦开始接 AI（AI 会生成道具、NPC、剧情），
这 3 个问题会同时变成返工，而且其中 2 个已经用实验证明了。

一句话概括：**现在这套代码把"叙事"当成了指令的返回值，把"存档"当成了状态的子集。
而这两个假设正是长期目标要推翻的。**

---

## 1. P0（不修，下一步必返工）

### P0-1 指令把「状态变更」和「中文叙事」焊死在一起，AI 没有安全的介入缝

**证据**

`backend/commands.py` 里每个处理函数同时做两件事——算数值、写中文句子：

```python
# commands.py  cmd_rest
result = game_state.rest(conn, ctx)
messages = [
    narration(
        "你坐下来休息了一会儿。（HP %d → %d，SAN %d → %d）"
        % (result["hp_before"], result["hp_after"], result["san_before"], result["san_after"])
    )
]
```

返回值类型就是给人读的句子：`CommandResult(ok, messages: List[Dict[str, str]])`，
`backend/main.py` 原样转发给前端。

**后果（两条，都是实测出来的）**

1. **AI 没有可消费的东西。** 想让 AI 叙事，它需要知道"发生了什么"（HP 5→8、时间 +60 分钟、
   地点变了）。现在这些信息只以中文句子的形式存在，AI 要么去正则解析中文，
   要么自己重算一遍规则——后者等于把规则实现两遍，第二遍必然会和第一遍漂移。
2. **中文措辞已经变成事实上的接口契约。** 测试断言直接绑在句子上：

   ```
   test_smoke.py: 58 项断言中 20 项依赖中文文本
   test_api.py:   30 项断言中 12 项依赖中文文本
   例：check("/inventory 列出绷带×2", "绷带 ×2" in texts(result), ...)
   ```

   改一个措辞就会挂测试。这说明叙事文本被当成了机器接口——而它本该是**表现层**。

**最小修改方案**

给 `CommandResult` 增加一个与 `messages` 并列的结构化字段，**不删 `messages`**（V0.1 行为完全不变）：

```python
@dataclass
class CommandResult:
    ok: bool
    messages: List[Dict[str, str]]        # 保留：本地的中文渲染（今天就是它在渲染）
    events: List[Dict[str, Any]] = field(default_factory=list)   # 新增：机器可读的"发生了什么"
```

处理函数改成：先产生 events，再由一个本地渲染函数把 events 变成 messages。

```python
events = [{"type": "vitals_change", "field": "hp", "from": 5, "to": 8},
          {"type": "time_advance", "minutes": 60, "to": 540}]
messages = render_events(events)          # 今天的渲染器
```

这样：今天的输出一字不变；接 AI 时把 `render_events` 换成 AI 调用即可，规则代码不动；
测试可以逐步改成断言 events（我建议新测试一律断言 events）。
改动范围：`commands.py` 一个文件 + 一个小渲染函数；**不动数据库、不动前端。**

---

### P0-2 `restore_snapshot` 不是权威回滚（已实验证明）

**证据**

`backend/game_state.py:266-280`，恢复时对快照里的每一行"能改就改，改不了就跳过"：

```python
for item in snapshot.get("items") or []:
    if repository.get_item(conn, item["id"]) is None:
        continue                                    # ← 静默跳过
    repository.apply_item_state(...)

for npc in snapshot.get("npcs") or []:
    repository.apply_npc_state(...)                 # ← 只更新快照里出现过的
```

**实验（临时脚本，用独立数据库，未改项目文件）**

```
实验 1：存档之后【新建】的物品，/load 之后还在吗？
  载入前背包: ['手电筒', '绷带', 'AI 生成的黑曜石匕首']
  载入后背包: ['手电筒', '绷带', 'AI 生成的黑曜石匕首']
  >>> 没有回滚，它留下来了

实验 2：存档时【存在】的物品，被删掉后 /load 能救回来吗？
  存档时地点里有: 老旧的黄铜钥匙
  /load: 已载入存档位「quicksave」
  载入后地点物品: []
  >>> 没有回来（被静默跳过，永久丢失）
```

**后果**

`/load` 得到的是一个**从未存在过的混合状态**：旧时间线里存在的（被删的那个）没了，
新时间线里才有的（AI 生成的那个）还在。对一个"AI 当 GM、剧情里不断产生道具和 NPC"的游戏，
存档会变成不可信的东西——这直接伤害长期目标 2 和 3。

注意：这在 V0.1 里**暂时打不出来**（没有创建物品的指令，种子世界里也没有同名物品触发
`take_item` 的合并删除分支），所以测试全绿是正常的。它是"下一功能一上来就中招"的类型。

**最小修改方案**

把快照定义成"该世界实例的权威全量状态"，恢复 = 先清后写：

1. 删除该 world 下所有 `items` / `npcs` 行中**不在快照里的** id；
2. 快照里存在但数据库里没有的，**按原 id 重新 INSERT**（`INSERT INTO items (id, ...)` 允许显式 id）；
3. 其余照旧 upsert。

约 20 行，只需在 `repository.py` 加 2 个小函数（列出 id / 按 id 删除）。
建议**在任何"能创建实体"的指令或 AI 接入之前**先做掉这一步，否则你会先写坏一批存档。

---

### P0-3 叙事历史完全没有持久化，存档里也没有剧情

**证据**

- `backend/database.py` 的建表 SQL 里**没有 events / log 表**。
- `capture_snapshot`（`game_state.py:198-223`）的快照内容只有：
  `version / world / player / items / npcs` —— **没有一行历史**。
- 事件日志只活在前端内存里（`frontend/app.js` 的 `appendLog` 写 DOM），
  刷新即清空（README 第 5 节自己也承认了这条）。

**后果**

这是离长期目标 2 最远的一条，而且不是"少了点功能"：

- AI Keeper 要叙事一致，必须能看到**最近的剧情**作为上下文。现在没有任何地方存着它。
- `/load` 之后状态回滚了、剧情归零了，**叙事和状态彻底错位**：
  玩家看到的是"你刚进图书馆"，但背包里还留着三小时前拿到的东西。
- 关掉页面再打开，故事就没了——而"事件日志"在一个文字 RPG 里就是**存档的主要内容之一**。

**最小修改方案**

加一张表 + 在出口处写一行：

```sql
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id   INTEGER NOT NULL,
    seq        INTEGER NOT NULL,      -- 该世界内的回合序号
    kind       TEXT    NOT NULL,      -- narration / system / error / player
    text       TEXT    NOT NULL,      -- 给人读的
    payload    TEXT    NOT NULL DEFAULT '{}',  -- 结构化事件（与 P0-1 的 events 对齐）
    created_at TEXT    NOT NULL
);
```

- `dispatch()` 结束时写一条（它已经是所有输入的必经之路，天然是"回合"边界）；
- `capture_snapshot` 带上最近 N 条（或全部）事件，`restore_snapshot` 一并恢复；
- `GET /api/state` 返回最近事件，前端启动时把它渲染进日志 → **刷新不再丢剧情**；
- 顺手给 `worlds` 加 `seq`，就是回合数，也是未来幂等键。

改动范围：1 张表 + repository 里 2 个函数 + dispatch 挂钩 + 快照加一段。

---

## 2. P1（在"世界包"或"接 AI"之前该修）

### P1-1 文档承诺的分层，已经被自己破坏了

`docs/ARCHITECTURE.md` 写的依赖方向是 `commands → game_state → repository`，
但 `backend/commands.py` 直接调 `repository.*` **14 次**（`list_exits` / `list_items` /
`find_item` / `upsert_save` / `get_latest_save` / `set_session` …）。

**为什么是问题**：接 AI 时你最需要的是一个**唯一的收口**，在那里做权限、规则校验、
事件落库。现在有两个层都能改状态，收口不存在。

补一个精确区分（独立评审认为"依赖方向没破"，那是从另一个角度看的）：**「SQL 收口」确实没破**
——`repository` 仍是唯一写 SQL 的地方；但**「状态变更收口」从来没建立过**。
将来 AI 的写入路径如果直接调 repository，就没有任何地方能保证"AI 提议、程序裁决"。
这两件事很容易被混为一谈，而只有后者会在接 AI 时咬人。

**最小修改**：把这 14 处读操作和 save/load 都挪进 `game_state`（多数是薄封装），
让 `commands.py` 只 import `game_state` 和 `models`。纯机械改动，行为不变。
做完之后"只有 game_state 能改状态"才是一句可以被检查的话。

### P1-2 规则常量是全局的，世界包无法自带规则

`backend/config.py` 的 `/rest` 规则块（`REST_HP_GAIN` / `REST_SAN_GAIN` / `REST_MINUTES`）
被 `game_state.rest()` 直接读取。

**为什么是问题**：长期目标 4 要求世界包可独立发布，而"休息恢复多少"属于世界设定。
留着全局常量，最后会变成"全局默认 + 到处 if 世界 id"。

**最小修改**：`worlds` 加一个 `rules TEXT`（JSON，默认取 config），
`game_state.rest()` 读 `ctx.world.rules`。世界包将来只要填这个字段。很小。

### P1-3 「世界定义」和「世界实例」没分开，世界包还是代码

`backend/world_data.py` 里 `LOCATIONS / EXITS / NPCS / PLAYER_ITEMS / LOCATION_ITEMS`
是 Python 字面量。好消息：`seed_test_world()` 的形状已经是一个"装载器"了，
只要把数据源换掉。另外每个实例的 `worlds.name` 都写死 "测试世界"，
所以两局游戏在 UI 和存档列表里长得一模一样（`label` 都是"测试世界 / 林默"）。

**最小修改**：把字面量抽成 `worlds/test_world.json`（结构照搬），
`seed_test_world()` 读文件后走原来那套 insert——**入库流程一行不改**；
再给 `worlds` 加三列 `pack_id / pack_version / pack_hash`，让实例能追溯来源；
`cmd_new` 允许透传世界 id，为将来 `/new <玩家名> <世界>` 留位置。

**注意不要做的事**：不要把"定义"和"实例"拆成两套表。V0.1 把结构逐行复制进实例
（`world_data.py:74-119` 每个实例插 4 地点 + 3 出口）是**有意的**，正是它让 `/new` 不破坏旧存档。
加三个 `pack_id` 类列就够了，拆表会动摇现有全部查询。

### P1-4 存档有版本号，但没有迁移路径

`restore_snapshot` 只做 `version != SAVE_FORMAT_VERSION → raise`。
等你按 P0-3 给快照加了 events，**所有旧存档立刻变成"格式不匹配"而载不进来**。
（实时写盘保住了当前进度，丢的是所有检查点。）

**最小修改**：保留版本号，加一个 `_upgrade(snapshot)`：旧版本补默认字段后放行
（现在 restore 大量使用 `.get(..., 默认值)`，本来就有这个基础）。约 15 行。

### P1-5 在请求里调用 AI 会长时间占住数据库连接与写事务

`backend/main.py` 的每个端点是 `with database.get_conn() as conn:` 然后 `commands.dispatch()`；
`get_conn()` 在退出时才 commit，SQLite `timeout=10s`，也没开 WAL。

**为什么是问题**：如果将来把 AI 调用放进这个 `with` 里，一次 30 秒的 Provider 请求
就握着写事务 30 秒，第二个请求（另一个标签页、或前端重试）会阻塞到超时并报
`database is locked`。

**最小修改（主要是纪律，不是代码）**：定一条不变量——**绝不在 `get_conn()` 的作用域内调用 AI**。
先拿状态快照 → 关连接 → 调 AI → 再开一个短事务落库。同时把这条写进 ARCHITECTURE.md。
可选：`PRAGMA journal_mode=WAL`。

---

### P1-6 裸 `/load` 会跨世界载入，可能把玩家静默换到另一个剧本的角色身上

`commands.py:222-227` 有三段回退，**性质并不相同**：

```python
save = repository.get_latest_save(conn, world_id=current_world, slot=slot)
if save is None and slot is not None:
    save = repository.get_latest_save(conn, world_id=None, slot=slot)   # 显式存档位跨世界
if save is None and slot is None and current_world is not None:
    save = repository.get_latest_save(conn, world_id=None, slot=None)   # 裸 /load 的全局兜底
```

- 第二行（玩家**显式**给了存档位）是有意的方便，而且已被测试固定
  （`test_smoke.py` 第 14 组用 `/load 章节一` 验证"可以载回旧世界"）。
- **第三行才是隐患**：裸 `/load` 在"当前世界没有存档"时会去取**任何世界**的最新存档，
  然后 `restore_snapshot` 里的 `set_session`（`game_state.py:282`）把会话切过去。
  提示只说"已载入存档位「quicksave」"（`commands.py:237`），**不说是哪个世界、哪个玩家**。

**为什么是问题**：目标 1 是多世界，而 `saves` 的唯一键本来就是 `(world_id, slot)`
（`database.py:95`）——存档位**按世界分命名空间**。裸 `/load` 的全局兜底违反了自己定义的主键语义，
在只有一个世界时看不出，多一个世界包就会变成"静默传送"。

**最小修改**：裸 `/load` 严格限定 `current_world`，找不到就报错并列出可用存档位；
跨世界保留为**显式**行为（`/load <存档位>`），并在提示里写出世界名与玩家名。

---

### P1-7 快照存了 `max_hp` / `max_san`，恢复时却不写回

`capture_snapshot` 把这两个上限写进了快照（`game_state.py:217-218`），
但 `restore_snapshot` 调用的 `update_player_vitals` 只写 `hp / san / name`
（`game_state.py:258-264`，已核对）：**这两个字段进了快照却从不生效**。

V0.1 看不出来，因为上限只在装载时写一次、之后没人改。一旦世界包规则能改上限（见 P1-2），
`/load` 就会和快照静默漂移：你以为回滚到"上限 20"的检查点，实际拿到的还是当前行的上限。

**最小修改**：`restore_snapshot` 把 `max_hp` / `max_san` 一起写回（约 3 行）。

---

### P1-8 没有 schema 迁移机制，加列会让已有数据库停在旧结构

`init_db` 只做 `executescript(SCHEMA_SQL)`，建表全是 `CREATE TABLE IF NOT EXISTS`
（`database.py:17-96`、`121-125`）。**已有的 `data/game.db` 不会因为建表语句变了而更新**：
加一张新表可以（`IF NOT EXISTS` 会建），但给 `worlds` **加列**不会生效。

玩家手里已经有 V0.1 的 `game.db`。P0-3 要给 `worlds` 加 `seq`、P1-3 要加 `pack_id`，
如果只改 `SCHEMA_SQL`，老库会缺列，然后在运行时炸出 `no such column`。

**最小修改**：`init_db` 里加一个极小迁移步骤——读 `PRAGMA user_version`，
按版本号顺序执行 ALTER 语句，再回写 `user_version`。不需要框架，约 20 行。
**这是"以后再还更贵"里最便宜的一笔。**

---

### P1-9 物品只有「名字」当身份，而快照里连名字都不存

`items` 表只有 `name / description / quantity`（`database.py:54-63`）；
`find_item` / `find_exit` 都是"精确匹配 → 否则子串包含 → 返回第一个命中"
（`repository.py:211-224`、`101-114`）；`take_item` 遇到同名物品会**合并数量并删掉地点那一行**
（`game_state.py:177-192`）。而快照导出物品时只取 `id / owner_kind / owner_id / quantity`
（`repository.py:244-250`）——**`name` 和 `description` 根本不在快照里**。

V0.1 成立，因为所有物品都在装载时定义好、之后只会换归属。等 AI 能生成道具，
就会出现同名不同物（三把钥匙、两张纸条）：`/take 钥匙` 拿哪把不确定；
AI 新造的同名物品会被并进老堆叠、丢掉自己的身份；而快照无法描述一件 AI 生成的物品长什么样。

**最小修改**：`items` 加 `def_id TEXT`（世界包里的定义 key）与 `origin TEXT`（pack / ai / loot）；
只合并 `def_id` 相同的物品；有歧义时报错而不是静默取第一个；快照带上 `name / description / def_id`。
这一步和 P0-2、P1-3 是同一刀。

---

## 3. P2（记下来，先不做）

| # | 问题 | 证据 / 说明 |
| --- | --- | --- |
| P2-1 | `repository.list_locations` 从未被调用（死代码） | `repository.py:70`，全仓库无引用。**这是我自己违反了你"不要创建没有实际用途的代码"的要求**，建议删掉或等世界包时再用 |
| P2-2 | `saves.label` 只写不读 | `commands.py:209` 写入，全仓库无人读取 |
| P2-3 | `/rest` 没有成本，规则没有牙齿 | grep 证明 `current_time` 只被**显示**和**递增**，没有任何规则消费它 → 可以无限休息回满血 |
| P2-4 | 没有回合 / 序号概念 | `worlds` 无 `seq` 列。AI 叙事需要"第几回合"当上下文锚点和幂等键（P0-3 会顺带解决） |
| P2-5 | NPC 状态是自由文本 `status TEXT` | AI 扮演 NPC 需要结构化状态（态度 / 情绪 / 已知信息）；越晚迁移越贵 |
| P2-6 | 重复提交无幂等保护 | 前端 `setBusy` 只防双击；网络重试可能让 `/rest` 生效两次 |
| P2-7 | 跨世界的存档无法区分 | 都存在 `quicksave`，名字都是"测试世界 / 林默"；`/load` 的提示也不说是哪个世界 / 玩家 |
| P2-8 | `get_context` 静默降级 | 会话指向缺失数据时返回 `None`，表现为"没有游戏"，会掩盖数据损坏（`game_state.py:55-60`） |
| P2-9 | 同一秒的两次保存无法区分 | `_now()` 只有秒级精度，两局存档的提示时间戳一样 |
| P2-10 | `ok` 的语义不可靠 | `ok = not any(kind == error)`（`commands.py:282-284`）。`cmd_go` 先 `move_player` 改状态、再 `get_context`，后者失败时返回 error → **状态已改但 `ok=False`**（`commands.py:171-175`）。有了 P0-1 的 events 后，应改用"是否存在已提交变更" |
| P2-11 | 日志条目类型是前后端各写一半的散装协议 | 后端只产出 `narration / system / error`（`commands.py:22-31`），前端自己发明了 `player`（`app.js` 回显指令时用）。类型集合没有单一权威 |
| P2-12 | 前端没有超时与取消 | `api()` 是裸 `fetch`，没有 `AbortController` / 超时；`busy` 期间输入框被禁用（`app.js:215-243`）。接 AI 后一次 20–30 秒的等待会让界面看起来像死了 |

---

## 4. 这个骨架哪里是对的（别改坏）

- **`repository` 是唯一写 SQL 的地方**：这条守住了，换数据库/做世界包导入时才不会失控。
- **实时写盘 + 检查点双轨**：不 `/save` 也不丢进度，`/save` 提供回滚点。这个组合对本地单人游戏是合理的，
  不要退回"只在 /save 时写盘"。
- **`/new` 只追加、不删除**：每局一套世界实例，旧存档永远可载入。做多周目时会省很多事。
- **世界时间用整数分钟**：避开了日期库和时区，`format_world_time()` 是单点，将来要真实日历只改一个函数。
- **依赖只有 fastapi + uvicorn，测试只用标准库**：88 项测试不需要 pytest、不需要联网（`test_smoke.py`），
  这是很值钱的属性。
- **单文件数据库**：`data/game.db` 复制走就是完整备份。
- 前端用 `textContent` 写所有来自服务器的文本，玩家名不会注入 HTML。

---

## 5. 如果只允许做 3 件事

1. **P0-3 事件表 + 快照带历史** —— 叙事是 AI 的上下文，也是存档的主要部分。不做这一步，AI 接入无从谈起。
2. **P0-1 结构化 events** —— 让 AI 有东西可叙事，同时把测试从"中文措辞"里解放出来。
   它和 P0-3 是同一件事的两面（事件先结构化了，才值得存进表里）。
3. **P0-2 让快照成为权威回滚** —— 改动最小（约 20 行），但必须在"能创建实体的功能"之前做。

顺序建议：**3 → 1 → 2**（先花 20 行堵住会污染存档的洞，再做事件表，最后拆叙事层）。
P1-1（分层收口）建议和 P0-1 一起做，因为两者都只动 `commands.py` / `game_state.py`。

独立评审给的顺序是 **1 → 2 → 3**（先加 events 通道，再落库，最后修快照），
理由是 events 通道成本最低且是所有 AI 工作的前置。两者只差第 1 步和第 3 步谁先，
实质分歧不大：**P0-2 只有约 20 行，且是唯一"不修就会让你写坏存档"的一条**，所以放在最前。
如果你打算很快就接 AI，按评审的顺序（1 → 2 → 3）也完全合理。

---

## 附：本报告的验证方式

| 结论 | 怎么验证的 |
| --- | --- |
| 指令返回的是中文句子 | 读 `commands.py` 全部处理函数 + `main.py` 转发 |
| 测试绑定中文措辞 | 统计 `tests/` 中 `check()` 总数与依赖 `texts(...)` 的条数：58→20、30→12 |
| `/load` 不是权威回滚 | 写一次性脚本跑两个实验（存档后新建 / 存档后删除），见 P0-2 输出 |
| 没有历史持久化 | `database.py` 建表 SQL 无 events 表；`capture_snapshot` 字段清单 |
| 分层已被破坏 | grep `repository\.` in `commands.py` → 14 处 |
| 世界时间无人消费 | grep `current_time` → 全部是显示 / 递增 / 存取，无规则读取 |
| `/rest` 无成本 | 同上（没有任何地方读时间来限制休息） |
| 死代码 / 死列 | grep `list_locations`（仅定义处）、`label`（仅写入处） |
| `/docs` 没有被静态挂载遮蔽 | 实测 `/docs`、`/openapi.json`、`/api/health` 均 200 |
| 快照存了 `max_hp/max_san` 却不恢复 | 对照 `game_state.py:217-218`（存）与 `258-264`（只写 hp/san/name） |
| 裸 `/load` 会跨世界 | 读 `commands.py:222-227` 三段回退，第三段是 `world_id=None, slot=None` |
| `game_session` 被钉成单行 | `database.py:75-80` 的 `CHECK (id = 1)` + `repository.py:315-317` 硬编码 `WHERE id = 1` |

### 独立评审

为降低"作者给自己找理由"的风险，另派一个**不知情**的 subagent 通读全部代码做对抗性评审。
它独立给出的 P0 与本报告一致（指令把状态变更和叙事焊死、历史不落库、快照只增不减），
并额外找出了本报告的 P1-6 / P1-7 / P1-8 / P1-9，以及 P2-10 / P2-11 / P2-12。

一处需要更正的措辞：它认为"依赖方向没破"。这个判断只在"SQL 收口"这个意义上成立
（`repository` 确实仍独占 SQL），但 `commands.py` 直接调 `repository` 14 次是事实，
所以**状态变更的收口并未建立**。本报告 P1-1 已按这个区分重写。
它还把 `/load` 的两条跨世界回退当成同一条，实际被测试固定的是**显式存档位**那条（P1-6 已澄清）。
