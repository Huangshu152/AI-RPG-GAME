r"""手动联调脚本：用真实 DeepSeek API 跑一个回合。

**不进入默认自动测试。** 它会真的发一次网络请求、真的花钱，所以必须手动运行。

用法（在项目根目录）：

    # 1) 先准备好 .env（复制 .env.example 并填入 Key）
    copy .env.example .env

    # 2) 运行
    .\.venv\Scripts\python.exe scripts\manual_test_deepseek.py

只想跑一次、不想写 .env 的话，可以直接给环境变量：

    $env:DEEPSEEK_API_KEY="sk-xxxx"
    .\.venv\Scripts\python.exe scripts\manual_test_deepseek.py

发生了什么：
    1. 读取 DEEPSEEK_API_KEY（读不到就直接退出，不会联网）
    2. 在**系统临时目录**里建一个一次性数据库，种下测试世界
       —— 绝不碰 data/game.db
    3. 构造只读 AIContext
    4. 让 DeepSeekProvider 发**一次**请求，拿结构化 JSON
    5. 打印 model / narrative / 提议的 actions / Schema 校验结果
    6. 再把这一个回合交给 Turn Runtime，看 Action Engine 的实际裁决
    7. 删除临时数据库
"""

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

from backend import commands, config, database, game_state  # noqa: E402
from backend.ai import deepseek as deepseek_module  # noqa: E402
from backend.ai import runtime as turn_runtime  # noqa: E402
from backend.ai import schema as ai_schema  # noqa: E402
from backend.ai.errors import ProviderError  # noqa: E402
from backend.envfile import load_env_file  # noqa: E402

TEST_INPUT = "我走进阅览室"


def build_temp_world():
    """在临时目录建库 + 种下测试世界。返回 (tmp_dir, world_id)。"""
    tmp = tempfile.mkdtemp(prefix="airpg-deepseek-")
    config.DATA_DIR = pathlib.Path(tmp)
    config.DB_PATH = config.DATA_DIR / "manual.db"
    database.init_db()
    with database.get_conn() as conn:
        commands.dispatch(conn, "/new 联调测试者")
        world_id = game_state.get_context(conn).world.id
    return tmp, world_id


def main() -> int:
    print("=" * 64)
    print("DeepSeek 手动联调（会真的发一次网络请求）")
    print("=" * 64)

    load_env_file()
    provider = deepseek_module.DeepSeekProvider()

    print("model      : %s" % provider.model)
    print("endpoint   : %s" % provider.endpoint())
    print("timeout    : %.0fs   max_tokens: %d   temperature: %s"
          % (provider.timeout, provider.max_tokens, provider.temperature))

    if not provider.is_configured():
        print()
        print("[跳过] 没有读到 DEEPSEEK_API_KEY，不会联网。")
        print("       复制 .env.example 为 .env 并填入 Key，或设置环境变量后重试。")
        return 1

    tmp, world_id = build_temp_world()
    try:
        with database.get_conn() as conn:
            context = turn_runtime.build_context(conn)
            before = game_state.get_context(conn).location.name

        print()
        print("测试世界   : %s（临时数据库，不会碰 data/game.db）" % context.world["name"])
        print("当前地点   : %s" % before)
        print("玩家输入   : %s" % TEST_INPUT)
        print("-" * 64)

        try:
            response = provider.generate_turn(TEST_INPUT, context)
        except ProviderError as exc:
            print("[失败] ProviderError(%s): %s" % (exc.kind, exc))
            return 1

        print("narrative  : %s" % response.narrative)
        print("actions    : %s" % response.actions)
        if provider.last_usage:
            print("usage      : %s" % provider.last_usage)

        print("-" * 64)
        print("Schema 校验（逐条）：")
        schema_ok = True
        for raw in response.actions:
            try:
                action = ai_schema.parse_action(raw)
                print("  [通过] %s" % action.to_dict())
            except ai_schema.SchemaError as exc:
                schema_ok = False
                print("  [拒绝] %s -> %s" % (raw, exc))
        if not response.actions:
            print("  （模型没有提出任何动作 —— 这是合法结果）")

        print("-" * 64)
        print("交给 Turn Runtime（Action Engine 的实际裁决）：")
        with database.get_conn() as conn:
            result = turn_runtime.process_turn(conn, TEST_INPUT, provider)
            after = game_state.get_context(conn).location.name

        print("  ok         : %s" % result.ok)
        print("  executed   : %s" % (result.executed_action.to_dict() if result.executed_action else None))
        print("  events     : %s" % result.events)
        if result.error:
            print("  error      : %s" % result.error)
        print("  地点       : %s -> %s" % (before, after))

        print("-" * 64)
        moved = result.executed_action is not None and result.executed_action.type == "move"
        if not schema_ok:
            print("[失败] 模型输出里有不符合 Action Schema 的动作（应被拒绝，见上）。")
            return 1
        if moved and after == "阅览室":
            print("[成功] 结构化输出 → Action Engine → 真实的 GameState 变更。")
            return 0
        print("[注意] 模型这次没有提出可执行的 move（可能是叙事型回复或动作被引擎拒绝）。")
        print("       这不算联调失败：通道是通的，输出也通过了 Schema。")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
