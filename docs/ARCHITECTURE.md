# 架构说明（V0.1）

## 分层

```
浏览器 (frontend/)
    │  fetch JSON
    ▼
FastAPI 路由 (backend/main.py)   ← 只收输入、调 Runtime、序列化结果
    │
    ├── 斜杠指令 ──► 指令层 (backend/commands.py)
    │                    ├── kind=read ──────► game_state 只读查询
    │                    ├── kind=gameplay ──► Action Engine ──┐
    │                    └── kind=system ────► game_state 系统级入口（new/save/load）
    │                                                          │
    └── 自然语言 ──► AI Runtime (backend/ai/)                   │
                         runtime → provider(Mock) → schema 校验 ┘
    ▼
状态聚合层 (backend/game_state.py)   ← 状态读取/组装、只读查询、快照、差分、历史
    │
    ▼
数据访问层 (backend/repository.py)   ← 全项目唯一写 SQL 的地方
    │
    ▼
SQLite (data/game.db)
```

**两条入口最终汇到同一个 Action Engine**：玩家敲 `/go 阅览室` 和
AI 提出 `{"type":"move","target":"阅览室"}`，走的是同一段
`validate() → execute() → 状态变更 → events`。不存在第二条平行的状态修改逻辑。

依赖方向向下：**读**走 `game_state`，**写**走那两个受控入口。于是：

- 换掉前端不影响后端；换掉数据库只影响 `repository.py`；
- **改状态没有第二条路**：命令层与将来的 AI 都拿不到写权限；
- 这条边界**被测试强制**，不是靠约定：`tests/test_smoke.py` 第 23 组用 AST 断言
  `commands.py` 不 import `repository` / `world_data`、没有裸 SQL 调用，
  且除 `repository.py` / `database.py` 外没有任何模块内联写 SQL；
  第 26、27 组断言两类入口没有被混为一谈。

## 各模块职责

| 文件 | 职责 |
| --- | --- |
| `backend/config.py` | 路径、规则常量（时间起点、`/rest` 数值）、日志类型字面量 |
| `backend/database.py` | 连接管理（事务型上下文）、建表 SQL |
| `backend/models.py` | 领域模型 dataclass、世界时间格式化 |
| `backend/repository.py` | 全部 SQL 与行↔对象转换 |
| `backend/world_data.py` | 内置测试世界的种子数据（世界装载器） |
| `backend/actions.py` | **Gameplay Action Engine**：Action 定义、验证、执行、事件产出 |
| `backend/ai/` | **AI Runtime**：`provider.py`（协议+只读上下文）、`schema.py`（Action/Response 校验）、`mock.py`（确定性 Mock）、`runtime.py`（Turn 编排） |
| `backend/game_state.py` | 状态读取/组装、只读查询包装、状态指纹差分、历史记录、存档快照、系统级入口（`new_game` / `save_game` / `load_game`） |
| `backend/commands.py` | 指令注册表、文本解析、结果渲染、回合落库 |
| `backend/schemas.py` | HTTP 请求模型 |
| `backend/main.py` | FastAPI 应用、4 个 JSON 接口、静态文件挂载 |
| `frontend/*` | 单页三栏 UI |

## 数据模型

- `worlds`：世界名、世界时间（从第 1 日 00:00 起的分钟数）、当前地点
- `locations` / `exits`：地点与有向出口
- `players`：名字、HP/MaxHP、SAN/MaxSAN
- `items`：`owner_kind` + `owner_id` 表示归属（玩家或地点）
- `npcs`：名字、所在地点、状态文本
- `events`：叙事历史 + 结构化事件（见下）
- `game_session`：单行，记录"当前正在玩的世界与玩家"
- `saves`：存档位，`(world_id, slot)` 唯一，`snapshot` 存 JSON 快照

### 为什么时间是整数分钟

避免引入日期库与时区问题；`format_world_time()` 负责显示成"第 N 日 HH:MM"。
将来需要真实日历（季节、节日）时，替换这一个函数即可。

## 两类受控入口：Gameplay Action 与 System Command

**真正修改游戏世界的入口只有两个**，都不允许业务逻辑散落地去写数据库：

| 类型 | 入口 | 包含 | 事件 |
| --- | --- | --- | --- |
| **Gameplay Action** | `actions.perform()` | `/go` `/take` `/rest`，以及将来 AI 的 `move` / `take` / `attack` / `talk` / `use` | 引擎按状态差分**自动**产出 |
| **System Command** | `game_state.new_game()` / `save_game()` / `load_game()` | `/new` `/save` `/load`、Snapshot / Rollback | 命令**自行声明**（`saved` / `rollback` / `session_switch`） |

这两类**刻意不统一**。如果把 `/save` `/load` 也做成 Gameplay Action，
"玩家在世界里做了什么"就会和"系统把存档写回去了"混成同一个概念——
这是明确要避免的过度抽象。所以 `actions.ActionSpec` 里没有
`mutates` / `build_events` 这类只为存档造出来的字段（第 26 组测试盯着这一点）。

### Gameplay Action 的四个阶段

| 阶段 | 谁负责 | 说明 |
| --- | --- | --- |
| **Intent** | `commands.py` 或未来的 AI | 构造 `Action(type=..., target=..., params=...)` |
| **Validation** | `ActionSpec.validate` | 只判断是否合法；不合法就**完全不碰状态** |
| **Mutation** | `ActionSpec.execute` | 只在验证通过后被调用，且包在 SAVEPOINT 里 |
| **Event** | 引擎 | 变更前后对状态指纹做差分，自动产出结构化事件 |

### 为什么验证和执行必须分开

"试算"和"提交"要能分开。AI 可以先确认一个动作是否合法；动作失败时也必须能确定
**状态一点都没变**（第 24 组测试断言：被拒绝的移动，前后状态指纹完全相同、事件为空）。

### 未来的 AI 怎么接

AI **不需要**伪造 `/go 阅览室` 这样的斜杠字符串，也拿不到数据库连接：

```python
from backend import actions, commands, game_state

ctx = game_state.get_context(conn)
commands.run_action(conn, ctx, actions.Action(type="move", target="阅览室"))
commands.run_action(conn, ctx, actions.Action(type="take", target="泛黄的笔记"))
commands.run_action(conn, ctx, actions.Action(type="rest", minutes=60))
```

`commands.run_action()` 会执行动作、产出事件、并把这一回合写进历史——
和玩家敲指令走的是**同一条路**（第 25 组测试就是直接提交 `Action` 来验证这条路径）。

`actions.action_catalog()` 返回动作清单（自带 `summary`），可以直接生成将来给 AI 的
tool / function schema。

> AI 只能构造 **Gameplay Action**。存档 / 读档属于宿主系统（`system` 指令），
> 不由 AI 触发——否则模型可以自己回滚掉刚发生的剧情。

### 加一个新动作要做什么

1. 在 `actions.py` 写 `_validate_xxx` 与 `_execute_xxx`；
2. `register(ActionSpec(name="xxx", summary="...", validate=..., execute=...))`；
3. 想让玩家也能用，就在 `commands.py` 注册一条 `kind=CMD_GAMEPLAY` 的指令指向它。

结构化事件**不需要埋点**：任何状态变更都会被差分自动捕获。

## AI Runtime（V0.2）

```
玩家自然语言
    │
    ▼
Turn Runtime (ai/runtime.py)   ← 只做编排：不写 SQL、不拼 Prompt、不改状态
    │
    ├─ build_context()         只读快照，**不含任何数据库 id**
    │                          含 world / location / player / visible_* / recent_events / **memories**
    ├─ provider.generate_turn()  只**提出** narrative + 候选 Action
    ├─ validate_ai_response()    结构校验（ai/schema.py）
    └─ actions.perform()         Action Engine：领域校验 + 唯一的状态变更
    │
    ▼
TurnResult { narrative, actions, executed, events, error }
```

完整的一回合数据流（含 Memory）：

```
Gameplay Action → State Mutation → Event → Memory Service → Persistent Memory
下一回合：
World State + Relevant Events + Relevant Memories → AIContext → Provider → AIResponse
```

### 三个模块的职责

| 文件 | 职责 | 不得做 |
| --- | --- | --- |
| `backend/ai/provider.py` | `AIContext`（只读上下文）、`AIResponse`（原始提案）、`AIProvider` 协议 | 不碰状态、不碰数据库 |
| `backend/ai/schema.py` | Action Schema 与 Response Schema 的**结构校验** | 不做领域校验、不执行动作 |
| `backend/ai/mock.py` | `MockAIProvider`：本地、无网络、确定性 | 不改状态、不判断目标是否存在 |
| `backend/memory.py` | **Memory Service**：记忆校验、创建/查询/停用、事件摄取、上下文选择、存档对齐 | 不改 World State、不执行 Action |
| `backend/ai/prompt.py` | Prompt Builder（动作清单来自 Action Engine 注册表） | 不接触状态、不含数据库 id |
| `backend/ai/deepseek.py` | `DeepSeekProvider`：真实 API 调用与输出解析 | 不碰 repository / 不执行 Action |
| `backend/ai/errors.py` | `ProviderError` + 密钥脱敏 | — |
| `backend/ai/runtime.py` | Turn Runtime：编排 | 不直接调用任何状态变更函数 |

### 校验分两层，这是关键

| 层 | 在哪 | 管什么 | 例子 |
| --- | --- | --- | --- |
| **结构校验** | `ai/schema.py` | type 认不认识、字段合不合法、参数能不能解释、类型对不对 | `{"type":"fireball"}` → 拒绝 |
| **领域校验** | `actions.ActionSpec.validate` | 这个出口通不通、这里有没有这件东西 | `move("月亮")` → 拒绝 |

所以 `move("月亮")` 能通过结构校验（形状是对的），再由 Action Engine 拒绝。
**不能因为 AI 说了"去那里"就强行执行。**

### Action Schema（模型无关）

```json
{"type": "move", "target": "阅览室", "arguments": {}}
{"type": "take", "target": "泛黄的笔记", "arguments": {}}
{"type": "rest", "target": null, "arguments": {"minutes": 60}}
```

- 认识的 type 直接取自 `actions.ACTIONS` 注册表（不另立一份名单，避免漂移）；
- 目前只有 `move` / `take` / `rest`；**没有** `attack` / `talk` / `use_item` 之类未实现的动作；
- 多一个字段（例如 `id`）、多一个参数、参数类型不对，都会被拒绝；
- `bool` 不算 `int`（`minutes: true` 会被拒绝）。

### Provider 的边界是结构性的，不是靠自觉

`AIContext` 是普通 dict / list 的值拷贝，**不含 `conn`、不含 id**。
Provider 拿不到数据库句柄，因此"不能改状态"不是纪律要求，而是它做不到。
架构测试（`tests/test_ai_runtime.py` 第 15、16 组）用 AST 断言
`backend/ai/` 下没有模块 import `repository`、没有内联 SQL、没有 `conn.execute`，
且 Provider / Schema / Mock 连 `game_state` 都不 import。

### 两个 Provider

| Provider | 联网 | 用途 |
| --- | --- | --- |
| `mock`（默认） | 否 | 本地、确定性、规则匹配。用来验证通道，也让测试不依赖网络 |
| `deepseek` | **是** | 真实 API。必须显式 `AI_PROVIDER=deepseek` 才启用 |

`MockAIProvider` 每句叙事都带 `（Mock AI）` 前缀，避免被误认为 Keeper。
它只做固定规则的动词匹配（休息 → 拿起 → 前往），识别不了就
**只给叙事、`actions = []`**，绝不伪造动作。它提出的目标存不存在，
一律交给 Action Engine 判定——`去月亮` 也会被如实提出，然后被拒绝。

`DeepSeekProvider` 走 `Prompt Builder → HTTP（一次请求，不重试）→ JSON parse →
信封校验 → AIResponse`。它把所有失败转成 `ProviderError`（超时 / 4xx / 5xx / 空响应 /
非法 JSON / 截断 / 结构不符），**不让底层异常穿透到 HTTP 层**，并且错误信息里
永远不含 API Key。HTTP 调用被隔离成可注入的 `transport`，所以单元测试不联网。

### 换真实模型只改一处

`backend/ai/__init__.py` 的 `get_provider()` —— 全项目**唯一**出现
`mock / deepseek` 分支的地方。业务层不会出现 `if provider == "deepseek": ...`。
加一个新 Provider = 实现 `AIProvider` 协议 + 在 `SUPPORTED_PROVIDERS` 登记一行。

模型名同样不硬编码：全部走 `DEEPSEEK_MODEL` 等环境变量。

## 事件与历史

`events` 表里同时放两类行，它们共用同一个 `seq`（回合号）：

- **叙事行**（`kind` = `narration` / `system` / `error`）：给人读的句子。
  前端左中右三栏底部的「事件日志」用的就是它，`GET /api/state` 的 `history` 字段也是它。
- **结构化事件行**（`kind` = `event`，`payload` 是 JSON）：机器可读的机制事实，例如
  `{"type":"player_change","field":"hp","from":5,"to":8}`、
  `{"type":"time_advance","minutes":60}`、`{"type":"item_moved",...}`。

### 结构化事件是"差分"出来的，不是手写的

`commands.dispatch()` 在调用 handler 前后各拍一张 `game_state.capture_fingerprint()`，
再由 `diff_fingerprints()` 把差异翻译成事件。于是：

- 事件**按定义**就等于"这一回合实际变了什么"，不可能和真实状态漂移（手写的事件会漂移）；
- **新增指令不需要手动埋点**：任何状态变更都会自动出现在事件里；
- 将来 AI Keeper 消费的是它，而不是去正则解析中文句子。

叙事措辞会改，事件结构不会——所以断言事件比断言中文稳。

### 历史会跟着存档一起回滚

快照包含该世界的全部历史。`/load` 会：

1. 把状态写回检查点；
2. **截断**存档之后产生的剧情（`DELETE ... WHERE id NOT IN 快照`）；
3. 把快照里有、当前缺的历史补回来。

所以载入存档后不会出现"状态是过去的、对话是未来的"这种错位。
前端在收到 `rollback` / `session_switch` 事件时会用 `GET /api/state` 的 `history`
重建整个日志，刷新页面同样能读回剧情。

## Memory（V0.3）

三种信息必须分开，**不能混为一谈**：

| 种类 | 例子 | 权威性 |
| --- | --- | --- |
| **World State** | `Player HP = 8` | **最高** |
| **Event Log** | `玩家被狼人攻击，受到 4 点伤害` | 中 |
| **Memory** | `玩家曾被狼人重伤，因此对狼人保持警惕` | 最低 |

冲突时永远 **World State > Event > Memory**。Memory **永远不能反向修改游戏世界**。
System Prompt 里也写明了这一点：记忆与当前状态冲突时，一律以当前状态为准。

### 什么才配进 Memory

判断标准只有一条：**能不能从当前 World State 直接推导出来？**

| 事件 | 记不记 | 为什么 |
| --- | --- | --- |
| `move` 到**首次**到达的地点 | ✅ B 级 discovery | "去过哪里"是历史，当前状态读不出来 |
| `item_moved`（拾取物品） | ❌ | 背包里就能读到，那属于 World State |
| `npc_status`（状态变化） | ❌ | 当前状态里就能读到 |
| `time_advance` | ❌ | 没有长期价值 |
| `player_change` / `saved` / `rollback` | ❌ | 同上 |

宁可少记，也不要把 World State 抄一遍。**V0.3 只有这一条自动规则**，
而且是确定性的——**没有 LLM 参与**，也不会让 AI 决定记什么、改重要度或删记忆。

### Memory Service 是唯一写入口

`backend/memory.py` 提供 `create` / `get` / `query` / `deactivate`，
外加事件摄取（`ingest_events`）、上下文选择（`select_for_context`）和存档对齐
（`snapshot_rows` / `reconcile_snapshot`）。

`commands.py`、`ai/*`、前端、甚至 `game_state.py` 的存档代码都**不允许**直接碰
`memories` 表——架构测试（`tests/test_memory.py` 第 13、14 组）盯着这一点。
Provider 拿到的只是 `AIContext.memories` 的**值拷贝**，改它不影响数据库。

### 检索是确定性的

不相似度、不 Embedding、不向量库：

1. 只给 `knowledge_scope == player` 的（防剧透，见下）
2. **C 级默认不进上下文**（短期信息）
3. 排序：**S → A → B**；同级按回合从近到远；再同则按 id 倒序（**保证裁剪稳定**）
4. 截断到 `MEMORY_CONTEXT_BUDGET`（默认 20 条），不允许无限增长

### 玩家知识 vs 世界真相

`knowledge_scope` 预留了区分能力：`player` / `world` / `npc` / `secret`。

**AI 默认只收到 `player`**——即使重要度是 S 也不能突破这条隔离
（宁可不说，也不要剧透）。被隔离的记忆仍然存在库里，数据模型保留了区分能力，
将来接完整知识系统时不用改表结构。

### 记忆随存档一起回滚

`memories` 进快照，`/load` 按快照对齐，**删除"存档之后才产生"的记忆**，
补回"快照里有而当前缺"的记忆。所以不会出现
`World rollback ✅ / Memory 留在未来 ❌` 的时间线污染。

老的 v2 快照会**自动升级**：v2 产生于 Memory 功能存在之前，所以"那个时刻没有任何记忆"
是事实而不是猜测，补一个空列表即可。这样老存档仍然能载入。

## 三个关键设计决定

**1. 实时写盘 + 显式存档并存**

每条改变状态的指令都立刻写进 SQLite，所以关掉程序、甚至断电都不会丢进度。
`/save` 额外把一个 JSON 快照写进 `saves` 表，`/load` 可以把快照写回数据库，
相当于"检查点 / 回滚"。两种机制解决不同问题，不重复。

**2. `/new` 不删除任何数据**

每次开新游戏会新建一整套世界实例（新的 world/location/npc/item 行），
旧存档依然可以载入。将来要做多周目、多角色时会省很多事。

**3. 快照是权威的（authoritative）**

`restore_snapshot()` 不是"能改就改"，而是以快照为准对齐整个世界实例：

| 情况 | 处理 |
| --- | --- |
| 当前有、快照里没有的 物品 / NPC / 历史 | **删除**（否则 `/load` 会留下"未来"的东西） |
| 快照里有、当前缺的 | 按原 id **补回**（否则被删的东西永久丢失） |
| 两边都有 | 更新归属 / 数量 / 状态 |

快照因此完整包含 `name` / `description` 等字段（重建一行所需的一切）。
这条是接 AI 的前置条件：AI 一旦能生成物品和 NPC，"只增不减"的还原就会变成存档漏洞。

失败时不再静默跳过 `continue`，而是在 `/load` 的提示里报告回滚摘要
（删了几件、补回几件、截断多少剧情）。

## 未来扩展点

| 未来功能 | 现在应该动哪里 |
| --- | --- |
| 接入真实 AI Keeper | 已经有了 AI Runtime（`backend/ai/`）。把 `default_provider()` 从 `MockAIProvider` 换成真实 Provider 即可——它只需要实现 `generate_turn(user_input, context) -> AIResponse`，返回 narrative + 候选 Action。**AI 拿不到数据库，也不能触发存档/读档。** |
| 多 AI Provider | 新增 `backend/providers/`，定义 `complete(prompt) -> str` 接口；`commands.py` 只依赖接口。 |
| World Pack 导入导出 | `world_data.seed_test_world()` 已经是一个"世界装载器"的形状：把 `LOCATIONS/EXITS/NPCS/ITEMS` 从 Python 常量换成读 JSON，入库流程不变。 |
| 战斗 / 技能 / 物品效果 | 在 `game_state.py` 增加变更函数，在 `commands.py` 注册新指令。事件会自动产生（差分），不需要额外埋点。 |
| 多人 / 多会话 | `game_session` 目前单行；改成每会话一行，`get_context()` 接受 session_id。 |

## V0.1 明确不做的事

AI API、向量库 / RAG、Redis、Docker、ORM、用户系统、前端框架、实时推送（WebSocket）。
