"""Provider 层的统一错误类型。

所有真实模型调用失败都必须变成 `ProviderError`，**绝不让底层异常**
（HTTPError / URLError / socket.timeout / json 解析错误）直接穿透到 HTTP 层。

错误信息里**绝不能出现 API Key**：构造消息时统一过一遍 `redact()`。
"""

from typing import Any, Iterable

# 错误分类，方便调用方（Turn Runtime / 前端）区分处理
KIND_MISSING_KEY = "missing_api_key"
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_AUTH = "auth"
KIND_HTTP = "http_error"
KIND_EMPTY = "empty_response"
KIND_INVALID_JSON = "invalid_json"
KIND_TRUNCATED = "truncated"
KIND_SCHEMA = "schema"


class ProviderError(Exception):
    """Provider 无法完成这一次生成。`kind` 是稳定的分类字符串。"""

    def __init__(self, message: str, kind: str = "provider_error"):
        super().__init__(message)
        self.kind = kind
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 直接继承语义
        return self.message


def redact(text: Any, secrets: Iterable[str]) -> str:
    """把文本里出现的密钥替换掉。任何要往外抛的字符串都应该先过这里。"""
    result = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 6:
            result = result.replace(str(secret), "***")
    return result
