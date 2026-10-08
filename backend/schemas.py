"""HTTP 请求模型。

字段给默认值 + 在端点里 strip，避免空白输入触发 422，
让「输入为空」这类情况由指令层给出中文提示。
"""

from pydantic import BaseModel, Field


class NewGameRequest(BaseModel):
    player_name: str = Field(default="", max_length=32)


class CommandRequest(BaseModel):
    text: str = Field(default="", max_length=500)


class TurnRequest(BaseModel):
    """自然语言回合。走 AI Runtime，而不是斜杠指令。"""

    input: str = Field(default="", max_length=500)
    # 可选：显式指定 Provider（前端的最小选择器用）。留空则用配置里的默认值。
    provider: str = Field(default="", max_length=32)
