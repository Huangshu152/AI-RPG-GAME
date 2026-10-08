"""SQLite 连接管理与建表。

设计取舍：
- 不用 ORM。表结构简单，直接 SQL 更透明，也少一个依赖。
- 每次操作开一个新连接（连接很便宜），由 repository 层负责事务边界。
- game_session 只有一行（id = 1），代表当前正在进行的游戏。
  本地单人 V0.1 不需要多会话。
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS worlds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    current_time        INTEGER NOT NULL DEFAULT 0,
    current_location_id INTEGER
);

CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id         INTEGER NOT NULL,
    from_location_id INTEGER NOT NULL,
    to_location_id   INTEGER NOT NULL,
    label            TEXT    NOT NULL,
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE,
    FOREIGN KEY (from_location_id) REFERENCES locations (id) ON DELETE CASCADE,
    FOREIGN KEY (to_location_id) REFERENCES locations (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS players (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    hp       INTEGER NOT NULL,
    max_hp   INTEGER NOT NULL,
    san      INTEGER NOT NULL,
    max_san  INTEGER NOT NULL,
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    quantity    INTEGER NOT NULL DEFAULT 1,
    owner_kind  TEXT    NOT NULL,
    owner_id    INTEGER,
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS npcs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    location_id INTEGER,
    status      TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE,
    FOREIGN KEY (location_id) REFERENCES locations (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS game_session (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    world_id       INTEGER,
    player_id      INTEGER,
    updated_at     TEXT
);

CREATE TABLE IF NOT EXISTS saves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id   INTEGER NOT NULL,
    player_id  INTEGER NOT NULL,
    slot       TEXT    NOT NULL,
    label      TEXT    NOT NULL DEFAULT '',
    snapshot   TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

-- 叙事历史 + 结构化事件。kind = 'event' 的行是机器事实（payload 里是 JSON），
-- 其余 kind 是给人读的日志行。两者共用 seq（回合号）以便对齐。
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id   INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    text       TEXT    NOT NULL DEFAULT '',
    payload    TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL,
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE
);

-- 结构化长期记忆。**不是** World State 的副本，也不替代它：
-- 只存"从当前状态推导不出来的、对未来剧情重要的历史"。
CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id         INTEGER NOT NULL,
    scope            TEXT    NOT NULL,            -- player / world / npc:<名字>
    knowledge_scope  TEXT    NOT NULL,            -- player / world / npc / secret（谁有资格知道）
    importance       TEXT    NOT NULL,            -- S / A / B / C
    category         TEXT    NOT NULL,
    content          TEXT    NOT NULL,
    subjects         TEXT    NOT NULL DEFAULT '[]',  -- JSON 数组，只放名字，不放 id
    source_event_id  INTEGER,                     -- 来自哪一次真实事件（可空）
    created_turn     INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT    NOT NULL,
    FOREIGN KEY (world_id) REFERENCES worlds (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_locations_world ON locations (world_id);
CREATE INDEX IF NOT EXISTS idx_items_owner     ON items (owner_kind, owner_id);
CREATE INDEX IF NOT EXISTS idx_npcs_world      ON npcs (world_id);
CREATE INDEX IF NOT EXISTS idx_events_world    ON events (world_id, seq);
CREATE INDEX IF NOT EXISTS idx_memories_world  ON memories (world_id, importance, created_turn);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saves_slot ON saves (world_id, slot);
"""


def connect() -> sqlite3.Connection:
    """打开一个数据库连接（调用方负责关闭）。"""
    conn = sqlite3.connect(config.DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """事务型连接：正常结束提交，出错回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """确保数据目录存在并建好所有表。启动时调用一次。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
