"""全局配置：路径与 V0.1 的规则常量。

这里只放常量，不放逻辑。未来接入 World Pack / 多 AI Provider 时，
这些值会被世界定义覆盖。
"""

from pathlib import Path

# ---------------- 路径 ----------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "game.db"
FRONTEND_DIR = BASE_DIR / "frontend"

# ---------------- 版本 ----------------
GAME_VERSION = "0.1.0"
# 2：快照里的物品/NPC 带上了 name / description，并加入了 events 历史。
# 3：快照加入了 memories（长期记忆）。v2 存档会自动升级（补空列表）。
SAVE_FORMAT_VERSION = 3
# ---------------- 日志 / 事件 ----------------
# 日志行的类型。event 是"机器可读的机制事实"，其余三种是给人读的叙事。
# 定义在这里是因为 database / game_state / commands 都要用同一套字面量。
KIND_NARRATION = "narration"
KIND_SYSTEM = "system"
KIND_ERROR = "error"
KIND_EVENT = "event"

# /api/state 一次性返回多少条历史日志（防止历史无限增长撑大响应）
HISTORY_LIMIT = 200

# ---------------- 世界时间 ----------------
# 世界时间用「从第 1 日 00:00 起经过的分钟数」表示，避免时区/日期库依赖。
MINUTES_PER_DAY = 24 * 60
START_TIME_MINUTES = 8 * 60  # 第 1 日 08:00

# ---------------- /rest 规则 ----------------
REST_HP_GAIN = 3
REST_SAN_GAIN = 2
REST_MINUTES = 60

# ---------------- 玩家默认属性 ----------------
DEFAULT_PLAYER_NAME = "调查员"
DEFAULT_MAX_HP = 12
DEFAULT_MAX_SAN = 50

# ---------------- 物品归属 ----------------
OWNER_PLAYER = "player"
OWNER_LOCATION = "location"

# ---------------- 存档 ----------------
DEFAULT_SLOT = "quicksave"

# ---------------- AI Runtime ----------------
# 交给 Provider 的近期事件条数上限（不是长期记忆，只是当前回合够用的窗口）
AI_RECENT_EVENTS = 20
# V0.2：一次玩家输入最多执行一个 Gameplay Action。
# Schema 层面兼容 action 数组，但上限由 Turn Runtime 把关，
# 避免提前引入事务编排 / 动作规划。
AI_MAX_ACTIONS_PER_TURN = 1

# ---------------- AI Provider ----------------
# 默认 Provider。真实 API **必须显式启用**（AI_PROVIDER=deepseek），
# 避免"机器上恰好装了 Key 就自动开始烧钱 / 测试自动联网"。
DEFAULT_AI_PROVIDER = "mock"

# DeepSeek 默认值。全部可以被环境变量覆盖 ——
# 业务代码里不出现模型名，也不出现 if model == ... 这种分支。
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_TIMEOUT = 30.0
# 成本控制：限制输出长度。800 tokens 对一回合的叙事 + 1 个动作足够。
DEEPSEEK_DEFAULT_MAX_TOKENS = 800
# JSON 结构化输出需要稳定，不要调太高
DEEPSEEK_DEFAULT_TEMPERATURE = 0.7

# ---------------- Memory ----------------
# 四级重要度。语义见 docs/ARCHITECTURE.md：
#   S 永久重大事实 / A 重要剧情与关系 / B 重要场景信息 / C 短期次要（默认不进上下文）
MEMORY_IMPORTANCE_LEVELS = ("S", "A", "B", "C")
# 排序权重：数字越小越优先
MEMORY_IMPORTANCE_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
# scope：这条记忆是"关于谁/什么"的。npc 用 "npc:<名字>"（**不是 id**）。
MEMORY_SCOPES = ("player", "world")
MEMORY_NPC_SCOPE_PREFIX = "npc:"
# knowledge_scope：谁有资格知道。AI 默认只收到 player 的（防剧透）。
MEMORY_KNOWLEDGE_SCOPES = ("player", "world", "npc", "secret")
MEMORY_AI_KNOWLEDGE_SCOPE = "player"
# category：闭集合，打错字会被拒绝
MEMORY_CATEGORIES = ("world", "discovery", "relationship", "quest", "death", "general")
# 进 AIContext 的条数上限（不许无限增长）
MEMORY_CONTEXT_BUDGET = 20
# 单条内容长度上限，防止记忆无限膨胀
MEMORY_MAX_CONTENT = 500
# 默认不进 AIContext 的重要度（C 是短期信息）
MEMORY_CONTEXT_MIN_IMPORTANCE = ("S", "A", "B")
