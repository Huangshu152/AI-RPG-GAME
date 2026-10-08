"""AI RPG Engine 后端包。

模块划分（V0.1）：
    config      常量与路径
    database    SQLite 连接与建表
    models      领域模型
    repository  数据访问层（唯一写 SQL 的地方）
    world_data  内置测试世界的种子数据（未来 World Pack 的雏形）
    game_state  状态聚合与存档快照
    commands    指令解析与执行（未来 AI 之外的另一半）
    schemas     HTTP 请求/响应模型
    main        FastAPI 应用与路由
"""

__all__ = ["config", "database", "models", "repository", "world_data", "game_state", "commands", "schemas", "main"]
