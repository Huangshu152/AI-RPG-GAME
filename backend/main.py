"""FastAPI 应用入口。

- 启动时建表（幂等），因此「关掉程序再启动」能直接读回状态。
- 路由**只做三件事**：收输入、调 Runtime、序列化结果。不执行 Action、不写数据库。
- /api/* 是后端接口，其余路径由 frontend/ 静态文件提供。
"""

from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import commands, config, database, game_state
from .ai import default_provider, get_provider, provider_catalog
from .ai import runtime as turn_runtime
from .commands import CommandResult
from .envfile import load_env_file
from .schemas import CommandRequest, NewGameRequest, TurnRequest

# 加载 .env（已存在的环境变量优先）。这是应用启动步骤，只做一次——
# 放在这里而不是 get_provider() 里，是为了让单元测试能和机器上的 .env 隔离开。
load_env_file()

# 配置选定的默认 AI Provider（默认 mock）。换模型只改配置，
# 业务层不会出现 if provider == "deepseek" 这种分支。
PROVIDER = default_provider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="AI RPG Engine",
    version=config.GAME_VERSION,
    description="最小可运行 RPG Runtime（本地 SQLite + 可替换的 AI Provider）",
    lifespan=lifespan,
)


def _response(conn, result: CommandResult) -> Dict[str, Any]:
    return {
        "ok": result.ok,
        "messages": result.messages,
        # 机器可读的机制事实（结构化事件）。叙事措辞会变，这些不会——
        # AI Keeper 消费它来叙述"这一回合实际发生了什么"。
        "events": result.events,
        "state": game_state.build_state(conn),
    }


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": config.GAME_VERSION,
        "provider": PROVIDER.name,
        "provider_configured": PROVIDER.is_configured(),
        "providers": provider_catalog(),
    }


@app.get("/api/state")
def read_state() -> Dict[str, Any]:
    """当前完整游戏状态 + 最近的历史日志（刷新页面后剧情不丢）。

    没有进行中的游戏时 has_game=False。
    """
    with database.get_conn() as conn:
        return game_state.build_state(conn, include_history=True)


@app.post("/api/game/new")
def new_game(payload: NewGameRequest) -> Dict[str, Any]:
    """创建新游戏（新世界实例 + 新玩家）。"""
    name = (payload.player_name or "").strip()
    with database.get_conn() as conn:
        result = commands.dispatch(conn, "/new %s" % name if name else "/new")
        return _response(conn, result)


@app.post("/api/command")
def run_command(payload: CommandRequest) -> Dict[str, Any]:
    """执行一条玩家输入（斜杠指令；自由输入在 V0.1 只给提示）。"""
    with database.get_conn() as conn:
        result = commands.dispatch(conn, payload.text)
        return _response(conn, result)


def _turn_response(conn, result) -> Dict[str, Any]:
    body = result.to_dict()
    body["state"] = game_state.build_state(conn)
    return body


@app.post("/api/turn")
def run_turn(payload: TurnRequest) -> Dict[str, Any]:
    """自然语言回合：Player Input → AI Provider → Action Schema → Action Engine。

    路由本身不执行 Action、不碰数据库写接口，只调用 Turn Runtime 并序列化。
    """
    with database.get_conn() as conn:
        requested = (payload.provider or "").strip()
        if requested:
            # 前端显式选择 —— 这是明确的手动选择，不是自动路由
            try:
                provider = get_provider(requested)
            except ValueError as exc:
                message = str(exc)
                return _turn_response(
                    conn,
                    turn_runtime.TurnResult(
                        ok=False,
                        error=message,
                        messages=[{"kind": config.KIND_ERROR, "text": message}],
                    ),
                )
        else:
            provider = PROVIDER

        return _turn_response(conn, turn_runtime.process_turn(conn, payload.input, provider))


# 静态前端：挂在最后，前面的 /api 路由优先匹配。
app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="ui")
