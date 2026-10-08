"""DeepSeekProvider：第一个真实 AI Provider。

    AIContext + user_input
        → Prompt Builder
        → DeepSeek HTTP API（一次请求，不重试）
        → 提取文本 → JSON parse → 信封校验
        → AIResponse

**边界**：它读 `user_input` 与只读的 `AIContext`，构造 prompt、发 HTTP、解析输出。
它**不碰** repository / SQLite / GameState，也不执行任何 Action。
它只"提出"，能不能做由 Action Engine 决定。

**HTTP 实现**：用标准库 `urllib`，不引入 openai SDK。原因有两个：
    1. 本版本明确要求"一次请求、失败直接返回"，而 openai SDK 默认会自动重试；
    2. 无论用哪个 SDK，timeout / 4xx / 5xx / 空响应都要映射成本项目的 ProviderError，
       SDK 省不掉这段代码。
HTTP 调用被隔离成可替换的 `transport`，单元测试注入假 transport，不联网。
"""

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config
from . import prompt as prompt_builder
from . import schema as ai_schema
from .errors import (
    KIND_AUTH,
    KIND_CONNECTION,
    KIND_EMPTY,
    KIND_HTTP,
    KIND_INVALID_JSON,
    KIND_MISSING_KEY,
    KIND_SCHEMA,
    KIND_TIMEOUT,
    KIND_TRUNCATED,
    ProviderError,
    redact,
)
from .provider import AIContext, AIResponse

# transport(url, headers, payload_dict, timeout) -> (status_code, body_text)
Transport = Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, str]]


def urllib_transport(
    url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float
) -> Tuple[int, str]:
    """默认 transport：一个 POST，一次，不重试。

    HTTP 错误、超时、连接失败都**原样抛出**，由 Provider 统一翻译成 ProviderError。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


class DeepSeekProvider:
    """DeepSeek（OpenAI-compatible）Provider。

    所有配置都可以从环境变量读取，也都可以在构造时显式传入（测试用）。
    **代码里不出现模型名分支**：换模型只改配置。
    """

    name = "deepseek"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        transport: Optional[Transport] = None,
    ):
        self.api_key = _pick(api_key, "DEEPSEEK_API_KEY", "")
        self.model = _pick(model, "DEEPSEEK_MODEL", config.DEEPSEEK_DEFAULT_MODEL)
        self.base_url = _pick(
            base_url, "DEEPSEEK_BASE_URL", config.DEEPSEEK_DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = float(
            _pick(timeout, "DEEPSEEK_TIMEOUT", config.DEEPSEEK_DEFAULT_TIMEOUT)
        )
        self.max_tokens = int(
            _pick(max_tokens, "DEEPSEEK_MAX_TOKENS", config.DEEPSEEK_DEFAULT_MAX_TOKENS)
        )
        self.temperature = float(
            _pick(temperature, "DEEPSEEK_TEMPERATURE", config.DEEPSEEK_DEFAULT_TEMPERATURE)
        )
        self._transport: Transport = transport or urllib_transport
        # 最近一次调用的用量，给手动联调脚本看成本用
        self.last_usage: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def endpoint(self) -> str:
        return self.base_url + "/chat/completions"

    def build_payload(self, user_input: str, context: AIContext) -> Dict[str, Any]:
        """构造请求体。单独抽出来是为了能在测试里断言请求形状（不发网络）。"""
        return {
            "model": self.model,
            "messages": prompt_builder.build_messages(user_input, context),
            # 官方 JSON Output：要求模型只吐一个 JSON 对象
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # 本版本不支持流式
            "stream": False,
        }

    # ------------------------------------------------------------------
    def generate_turn(self, user_input: str, context: AIContext) -> AIResponse:
        if not self.api_key:
            raise ProviderError(
                "没有配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入 Key，"
                "或改用 AI_PROVIDER=mock。",
                KIND_MISSING_KEY,
            )

        payload = self.build_payload(user_input, context)
        headers = {
            "Content-Type": "application/json",
            # key 只在这里出现，且从不写进任何异常信息
            "Authorization": "Bearer %s" % self.api_key,
            "Accept": "application/json",
        }

        status, body_text = self._post(headers, payload)
        data = self._parse_http_body(body_text, status)
        self.last_usage = data.get("usage") if isinstance(data, dict) else None
        content = self._extract_content(data)
        raw = self._parse_json_content(content)

        try:
            return ai_schema.parse_ai_response_payload(raw)
        except ai_schema.SchemaError as exc:
            # 信封结构不对：如实报错，**不自行修复**
            raise ProviderError(
                "模型输出的 JSON 不符合 AI Response 结构：%s" % redact(exc, [self.api_key]),
                KIND_SCHEMA,
            )

    # ------------------------------------------------------------------
    # 下面是四个小步骤，各自只做一件事，方便单独测试
    # ------------------------------------------------------------------
    def _post(self, headers: Dict[str, str], payload: Dict[str, Any]) -> Tuple[int, str]:
        try:
            return self._transport(self.endpoint(), headers, payload, self.timeout)
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类，必须先捕获
            detail = self._http_error_detail(exc)
            if exc.code in (401, 403):
                raise ProviderError(
                    "DeepSeek 拒绝了这次认证（HTTP %d）。请检查 DEEPSEEK_API_KEY 是否有效。%s"
                    % (exc.code, detail),
                    KIND_AUTH,
                )
            raise ProviderError(
                "DeepSeek 返回 HTTP %d。%s" % (exc.code, detail), KIND_HTTP
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise ProviderError(
                    "调用 DeepSeek 超时（%.0f 秒）。本版本不重试，请稍后再试或调大 DEEPSEEK_TIMEOUT。"
                    % self.timeout,
                    KIND_TIMEOUT,
                )
            raise ProviderError(
                "无法连接 DeepSeek：%s" % redact(reason, [self.api_key]), KIND_CONNECTION
            )
        except (TimeoutError, socket.timeout):
            raise ProviderError(
                "调用 DeepSeek 超时（%.0f 秒）。本版本不重试，请稍后再试或调大 DEEPSEEK_TIMEOUT。"
                % self.timeout,
                KIND_TIMEOUT,
            )
        except OSError as exc:
            raise ProviderError(
                "调用 DeepSeek 失败：%s" % redact(exc, [self.api_key]), KIND_CONNECTION
            )

    def _http_error_detail(self, exc: urllib.error.HTTPError) -> str:
        """尽量从错误响应体里取一句有用的说明（但绝不泄漏 Key）。"""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            return ""
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return redact(raw[:200], [self.api_key])
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or error.get("type") or ""
        else:
            message = parsed.get("message", "") if isinstance(parsed, dict) else ""
        return redact(message, [self.api_key])

    def _parse_http_body(self, body_text: str, status: int) -> Dict[str, Any]:
        if not body_text or not body_text.strip():
            raise ProviderError(
                "DeepSeek 返回了空响应（HTTP %d）。" % status, KIND_EMPTY
            )
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError:
            raise ProviderError(
                "DeepSeek 的响应不是合法 JSON（HTTP %d）：%s"
                % (status, redact(body_text[:200], [self.api_key])),
                KIND_INVALID_JSON,
            )
        if not isinstance(data, dict):
            raise ProviderError("DeepSeek 的响应不是 JSON 对象。", KIND_INVALID_JSON)
        return data

    def _extract_content(self, data: Dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("DeepSeek 没有返回任何 choices。", KIND_EMPTY)

        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            # 截断的 JSON 不要猜，直接报错
            raise ProviderError(
                "模型输出被长度上限截断（finish_reason=length）。"
                "已保留截断内容不解析；可调大 DEEPSEEK_MAX_TOKENS 或缩短上下文。",
                KIND_TRUNCATED,
            )

        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("模型返回了空内容。", KIND_EMPTY)
        return content

    def _parse_json_content(self, content: str) -> Any:
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "模型输出的不是合法 JSON：%s。原文开头：%s"
                % (exc, redact(content[:200], [self.api_key])),
                KIND_INVALID_JSON,
            )


def _pick(explicit: Any, env_key: str, default: Any) -> Any:
    """显式参数 > 环境变量 > 默认值。"""
    if explicit is not None:
        return explicit
    value = os.environ.get(env_key)
    if value is None or str(value).strip() == "":
        return default
    return value
