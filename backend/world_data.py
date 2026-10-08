"""内置测试世界：种下 World / Location / NPC / Item。

这个文件是未来「World Pack」的雏形：
目前用 Python 代码描述世界，将来会替换成 JSON 世界包 + 同样的入库流程。
seed_test_world() 是唯一对外入口。
"""

import sqlite3
from typing import Tuple

from . import config, repository

# ---------------- 世界内容定义 ----------------
# 地点：key -> (名称, 描述)
LOCATIONS = {
    "hall": (
        "灰塔图书馆·大厅",
        "高耸的书架沿墙盘旋而上，尘埃在穹顶漏下的光柱里缓慢翻滚。"
        "借阅台后的座钟走得很慢，却从不停止。",
    ),
    "reading": (
        "阅览室",
        "长桌两侧摆着一排绿色台灯，只有一盏是亮着的。"
        "空气里有旧纸和墨水的味道，安静得能听见自己的呼吸。",
    ),
    "street": (
        "图书馆前的街道",
        "雨刚停，石板路反着灰白的天光。街上没什么人，"
        "远处传来电车驶过的声音。",
    ),
    "archive": (
        "地下档案室",
        "楼梯尽头是一扇没有把手的铁门，推开后是成排的铁皮柜。"
        "这里的温度明显更低，灯光昏黄。",
    ),
}

# 出口：(起点, 终点, 标签)
EXITS = [
    ("hall", "reading", "阅览室"),
    ("hall", "street", "大门"),
    ("reading", "archive", "向下的楼梯"),
]

# 起始地点：玩家在哪里、世界从哪里开始
START_LOCATION = "hall"

# NPC：(名称, 所在地点, 基础状态)
NPCS = [
    ("图书管理员 阿黛尔", "hall", "值班中，正在整理借阅卡"),
    ("沉默的读者", "reading", "专注地读一本书，头也不抬"),
    ("门房 老周", "street", "靠在门边抽烟"),
]

# 玩家初始物品：(名称, 描述, 数量)
PLAYER_ITEMS = [
    ("手电筒", "金属外壳，电量看起来还够用。", 1),
    ("绷带", "干净的纱布卷，可以应急处理伤口。", 2),
]

# 地点物品：(所在地点, 名称, 描述, 数量)
LOCATION_ITEMS = [
    ("hall", "老旧的黄铜钥匙", "钥匙柄上刻着一个模糊的塔形图案。", 1),
    ("reading", "泛黄的笔记", "某位读者留下的手写笔记，字迹潦草。", 1),
    ("archive", "铁皮抽屉里的档案袋", "封口处贴着褪色的封条。", 1),
]


def seed_test_world(conn: sqlite3.Connection, player_name: str) -> Tuple[int, int]:
    """创建一个全新的测试世界和玩家，返回 (world_id, player_id)。

    每次调用都会新建一套数据，不覆盖已有世界，因此旧存档始终可载入。
    """
    world_id = repository.insert_world(conn, "测试世界", config.START_TIME_MINUTES)

    # 地点
    location_ids = {}
    for key, (name, description) in LOCATIONS.items():
        location_ids[key] = repository.insert_location(conn, world_id, name, description)

    # 出口
    for from_key, to_key, label in EXITS:
        repository.insert_exit(
            conn, world_id, location_ids[from_key], location_ids[to_key], label
        )

    # NPC
    for name, loc_key, status in NPCS:
        repository.insert_npc(conn, world_id, name, location_ids[loc_key], status)

    # 玩家
    player_id = repository.insert_player(
        conn,
        world_id=world_id,
        name=player_name,
        hp=config.DEFAULT_MAX_HP,
        max_hp=config.DEFAULT_MAX_HP,
        san=config.DEFAULT_MAX_SAN,
        max_san=config.DEFAULT_MAX_SAN,
    )

    # 物品
    for name, description, quantity in PLAYER_ITEMS:
        repository.insert_item(
            conn, world_id, name, description, quantity, config.OWNER_PLAYER, player_id
        )
    for loc_key, name, description, quantity in LOCATION_ITEMS:
        repository.insert_item(
            conn,
            world_id,
            name,
            description,
            quantity,
            config.OWNER_LOCATION,
            location_ids[loc_key],
        )

    # 世界起点
    repository.update_world_location(conn, world_id, location_ids[START_LOCATION])
    return world_id, player_id
