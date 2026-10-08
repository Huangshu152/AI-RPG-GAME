"""极小的 `.env` 读取器。

不引入 python-dotenv：这里只需要 `KEY=VALUE`、`#` 注释、可选引号三种情况，
而且必须遵守一条安全规则——**已经存在的环境变量优先，永远不会被 .env 覆盖**。

真实 Key 只放在 `.env`（已被 .gitignore 忽略），绝不进源码 / 测试 / 仓库 / 前端。
"""

import os
from pathlib import Path
from typing import Dict, Optional

from . import config

ENV_PATH = config.BASE_DIR / ".env"


def parse_env_text(text: str) -> Dict[str, str]:
    """把 .env 文本解析成字典。不支持的写法直接忽略，不做任何展开。"""
    result: Dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 只去掉成对的引号，不做变量展开 / 转义
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_env_file(path: Optional[Path] = None) -> int:
    """把 .env 里**尚未设置**的变量写进 os.environ，返回写入条数。

    文件不存在时静默返回 0 —— 没有 .env 是完全正常的状态。
    """
    target = path or ENV_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0

    loaded = 0
    for key, value in parse_env_text(text).items():
        if key in os.environ:  # 真实环境变量优先
            continue
        os.environ[key] = value
        loaded += 1
    return loaded
