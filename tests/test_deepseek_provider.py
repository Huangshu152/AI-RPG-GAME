"""DeepSeekProvider 单元测试。

**一次网络请求都不会发。** 全部通过注入假 transport（mock HTTP）来驱动，
所以测试不依赖网络、不花钱、结果确定。

    python tests\\test_deepseek_provider.py
"""

import ast
import contextlib
import dataclasses
import json
import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from backend import actions, config  # noqa: E402
from backend.ai import deepseek as deepseek_module  # noqa: E402
from backend.ai import get_provider, provider_catalog  # noqa: E402
from backend.ai import prompt as prompt_builder  # noqa: E402
from backend.ai import schema as ai_schema  # noqa: E402
from backend.ai.deepseek import DeepSeekProvider  # noqa: E402
from backend.ai.errors import (  # noqa: E402
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
)
from backend.ai.provider import AIContext  # noqa: E402

PASSED = 0
FAILED = 0

# 一个明显的哨兵值：任何错误信息里出现它都算泄漏
FAKE_KEY = "sk-TESTKEY-must-never-leak-0123456789"


def check(label, condition, extra=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [PASS] %s" % label)
    else:
        FAILED += 1
        print("  [FAIL] %s%s" % (label, ("  -> " + repr(extra)) if extra != "" else ""))


@contextlib.contextmanager
def env(**kwargs):
    saved = {key: os.environ.get(key) for key in kwargs}
    try:
        for key, value in kwargs.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FakeTransport:
    """假 HTTP transport：记录请求、返回预设响应或抛预设异常。"""

    def __init__(self, status=200, body="", exc=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


def api_body(content, finish_reason="stop", usage=None):
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
        },
        ensure_ascii=False,
    )


def sample_context():
    return AIContext(
        world={"name": "测试世界", "time": "第 1 日 08:00", "time_minutes": 480},
        current_location={
            "name": "灰塔图书馆·大厅",
            "description": "高耸的书架沿墙盘旋而上。",
            "exits": [{"label": "阅览室", "to": "阅览室"}],
        },
        player={
            "name": "甲",
            "hp": 12,
            "max_hp": 12,
            "san": 50,
            "max_san": 50,
            "items": [],
        },
        visible_npcs=[{"name": "阿黛尔", "status": "值班中"}],
        visible_items=[{"name": "黄铜钥匙", "description": "旧的", "quantity": 1}],
        recent_events=[{"seq": 1, "kind": "narration", "text": "你走进大厅。"}],
    )


def make_provider(**overrides):
    kwargs = {
        "api_key": FAKE_KEY,
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "timeout": 30.0,
        "max_tokens": 800,
        "temperature": 0.7,
    }
    kwargs.update(overrides)
    return DeepSeekProvider(**kwargs)


def call(provider, text="我走进阅览室"):
    return provider.generate_turn(text, sample_context())


def expect_error(provider, kind, label, text="我走进阅览室"):
    """调用并断言抛出指定 kind 的 ProviderError，且不泄漏 Key。"""
    try:
        result = call(provider, text)
    except ProviderError as exc:
        leaked = FAKE_KEY in str(exc)
        check(
            "%s → ProviderError(%s)" % (label, kind),
            exc.kind == kind and not leaked,
            "kind=%s leaked=%s" % (exc.kind, leaked),
        )
        return
    except Exception as exc:  # 别的异常穿透出来也是失败
        check("%s → ProviderError(%s)" % (label, kind), False, "%s: %s" % (type(exc).__name__, exc))
        return
    check("%s → ProviderError(%s)" % (label, kind), False, "没有抛错，返回了 %r" % (result,))


def main():
    context = sample_context()

    # ================================================================
    print("== 1. 正常 JSON → AIResponse")
    good = json.dumps(
        {"narrative": "你推开了阅览室的门。", "actions": [{"type": "move", "target": "阅览室", "arguments": {}}]},
        ensure_ascii=False,
    )
    transport = FakeTransport(200, api_body(good))
    provider = make_provider(transport=transport)
    response = provider.generate_turn("我走进阅览室", context)
    check("返回 AIResponse", response.narrative == "你推开了阅览室的门。", response)
    check("actions 原样带回来", response.actions == [{"type": "move", "target": "阅览室", "arguments": {}}],
          response.actions)
    check("只发了一次请求", len(transport.calls) == 1, len(transport.calls))

    print("== 2. 请求形状（成本 / 结构化输出 / 不重试）")
    sent = transport.calls[0]
    check("URL 正确", sent["url"] == "https://api.deepseek.com/chat/completions", sent["url"])
    check("model 来自配置", sent["payload"]["model"] == "deepseek-flash", sent["payload"]["model"])
    check("要求 JSON Output",
          sent["payload"]["response_format"] == {"type": "json_object"}, sent["payload"].get("response_format"))
    check("stream=False", sent["payload"]["stream"] is False)
    check("temperature 来自配置", sent["payload"]["temperature"] == 0.7, sent["payload"]["temperature"])
    check("max_tokens 来自配置（成本控制）", sent["payload"]["max_tokens"] == 800, sent["payload"]["max_tokens"])
    check("timeout 传给了 transport", sent["timeout"] == 30.0, sent["timeout"])
    check("messages 是 system + user",
          [m["role"] for m in sent["payload"]["messages"]] == ["system", "user"],
          [m["role"] for m in sent["payload"]["messages"]])
    check("Authorization 头带 Key", sent["headers"]["Authorization"] == "Bearer " + FAKE_KEY)
    check("Key 不在请求体里", FAKE_KEY not in json.dumps(sent["payload"], ensure_ascii=False))
    check("usage 被记录（供手动联调看成本）",
          provider.last_usage == {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
          provider.last_usage)

    # ================================================================
    print("== 3. 非法 JSON → ProviderError(invalid_json)")
    expect_error(
        make_provider(transport=FakeTransport(200, api_body("这不是 JSON，我是模型随便说的"))),
        KIND_INVALID_JSON,
        "content 不是 JSON",
    )
    expect_error(
        make_provider(transport=FakeTransport(200, "<html>502 Bad Gateway</html>")),
        KIND_INVALID_JSON,
        "HTTP body 不是 JSON",
    )

    print("== 4. 空响应 → ProviderError(empty_response)")
    expect_error(make_provider(transport=FakeTransport(200, "")), KIND_EMPTY, "HTTP body 为空")
    expect_error(make_provider(transport=FakeTransport(200, api_body(""))), KIND_EMPTY, "content 为空")
    expect_error(
        make_provider(transport=FakeTransport(200, api_body("   "))), KIND_EMPTY, "content 只有空白"
    )
    expect_error(
        make_provider(transport=FakeTransport(200, json.dumps({"choices": []}))),
        KIND_EMPTY,
        "choices 为空数组",
    )
    expect_error(
        make_provider(transport=FakeTransport(200, json.dumps({"usage": {}}))),
        KIND_EMPTY,
        "没有 choices 字段",
    )

    print("== 5. 截断 → ProviderError(truncated)，不猜 JSON")
    truncated = json.dumps({"narrative": "你推开了", "actions": [{"type": "mo"}], "extra": "x"})
    expect_error(
        make_provider(transport=FakeTransport(200, api_body(truncated, finish_reason="length"))),
        KIND_TRUNCATED,
        "finish_reason=length",
    )

    print("== 6. 信封结构合法但不符合 Schema → ProviderError(schema)")
    expect_error(
        make_provider(transport=FakeTransport(200, api_body(json.dumps({"actions": []})))),
        KIND_SCHEMA,
        "缺少 narrative",
    )
    expect_error(
        make_provider(transport=FakeTransport(200, api_body(json.dumps({"narrative": 123, "actions": []})))),
        KIND_SCHEMA,
        "narrative 不是字符串",
    )
    expect_error(
        make_provider(transport=FakeTransport(200, api_body(json.dumps({"narrative": "x", "actions": "move"})))),
        KIND_SCHEMA,
        "actions 不是数组",
    )
    expect_error(
        make_provider(transport=FakeTransport(200, api_body(json.dumps([1, 2, 3])))),
        KIND_SCHEMA,
        "顶层不是对象",
    )

    print("== 7. 未知 Action：Provider 原样透传，由 Schema 层拒绝（不自行修复）")
    unknown = json.dumps(
        {"narrative": "我要放火球。", "actions": [{"type": "fireball", "target": "门"}]},
        ensure_ascii=False,
    )
    provider = make_provider(transport=FakeTransport(200, api_body(unknown)))
    response = provider.generate_turn("放火球", context)
    check("Provider 不修正、原样带出未知动作",
          response.actions == [{"type": "fireball", "target": "门"}], response.actions)
    try:
        ai_schema.validate_ai_response(response)
        check("Schema 层拒绝未知 action", False, "居然通过了")
    except ai_schema.SchemaError as exc:
        check("Schema 层拒绝未知 action", "fireball" in str(exc), str(exc))

    print("== 8. 信封多出来的键被忽略（Action 本身仍然一个字不许多）")
    extra = json.dumps({"narrative": "好的。", "actions": [], "confidence": 0.9}, ensure_ascii=False)
    response = make_provider(transport=FakeTransport(200, api_body(extra))).generate_turn("嗯", context)
    check("多余的信封字段被忽略", response.narrative == "好的。" and response.actions == [], response)
    check("action 里的多余字段仍然会被 Schema 拒绝",
          _raises(lambda: ai_schema.parse_action({"type": "move", "target": "x", "confidence": 1})))

    # ================================================================
    print("== 9. 网络 / HTTP 错误统一转成 ProviderError")
    expect_error(
        make_provider(transport=FakeTransport(exc=TimeoutError("timed out"))),
        KIND_TIMEOUT,
        "TimeoutError",
    )
    expect_error(
        make_provider(
            transport=FakeTransport(exc=urllib.error.URLError(TimeoutError("timed out")))
        ),
        KIND_TIMEOUT,
        "URLError(timeout)",
    )
    expect_error(
        make_provider(
            transport=FakeTransport(exc=urllib.error.URLError(OSError("getaddrinfo failed")))
        ),
        KIND_CONNECTION,
        "连接失败",
    )

    def http_error(code, body=""):
        return urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            code,
            "err",
            {},
            _FakeBody(body.encode("utf-8")),
        )

    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(401, json.dumps({"error": {"message": "Invalid API key"}})))),
        KIND_AUTH,
        "HTTP 401",
    )
    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(403))), KIND_AUTH, "HTTP 403"
    )
    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(400, json.dumps({"error": {"message": "bad model"}})))),
        KIND_HTTP,
        "HTTP 400",
    )
    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(429))), KIND_HTTP, "HTTP 429"
    )
    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(500))), KIND_HTTP, "HTTP 500"
    )
    expect_error(
        make_provider(transport=FakeTransport(exc=http_error(503))), KIND_HTTP, "HTTP 503"
    )

    print("== 10. HTTP 错误信息里有服务端的说明，但不含 Key")
    err = http_error(400, json.dumps({"error": {"message": "model not found: " + FAKE_KEY}}))
    try:
        call(make_provider(transport=FakeTransport(exc=err)))
        check("400 抛错", False)
    except ProviderError as exc:
        check("带上了服务端说明", "model not found" in str(exc), str(exc))
        check("服务端回显的 Key 也被抹掉", FAKE_KEY not in str(exc), str(exc))

    print("== 11. API Key 缺失")
    expect_error(make_provider(api_key=""), KIND_MISSING_KEY, "空 Key")
    check("is_configured() 反映 Key 是否存在",
          make_provider(api_key="").is_configured() is False
          and make_provider(api_key=FAKE_KEY).is_configured() is True)

    # ================================================================
    print("== 12. 配置来自环境变量，且不在业务代码里硬编码")
    with env(
        DEEPSEEK_MODEL="my-custom-model",
        DEEPSEEK_BASE_URL="https://example.invalid/v1",
        DEEPSEEK_TIMEOUT="7",
        DEEPSEEK_MAX_TOKENS="123",
        DEEPSEEK_TEMPERATURE="0.1",
        DEEPSEEK_API_KEY=FAKE_KEY,
    ):
        transport = FakeTransport(200, api_body(good))
        provider = DeepSeekProvider(transport=transport)
        provider.generate_turn("我走进阅览室", context)
        sent = transport.calls[0]
        check("model 来自 DEEPSEEK_MODEL", sent["payload"]["model"] == "my-custom-model")
        check("base_url 来自 DEEPSEEK_BASE_URL",
              sent["url"] == "https://example.invalid/v1/chat/completions", sent["url"])
        check("timeout 来自 DEEPSEEK_TIMEOUT", sent["timeout"] == 7.0, sent["timeout"])
        check("max_tokens 来自 DEEPSEEK_MAX_TOKENS", sent["payload"]["max_tokens"] == 123)
        check("temperature 来自 DEEPSEEK_TEMPERATURE", sent["payload"]["temperature"] == 0.1)
        check("api_key 来自 DEEPSEEK_API_KEY", provider.is_configured() is True)

    print("== 13. 显式参数优先于环境变量（测试可注入）")
    with env(DEEPSEEK_MODEL="env-model"):
        explicit = make_provider(model="explicit-model", transport=FakeTransport(200, api_body(good)))
        explicit.generate_turn("x", context)
        check("显式 model 覆盖环境变量", explicit.model == "explicit-model", explicit.model)

    print("== 14. default_provider：默认 mock，装了 Key 也不自动切换")
    with env(AI_PROVIDER=None, DEEPSEEK_API_KEY=FAKE_KEY):
        check("没设 AI_PROVIDER 时仍是 mock",
              get_provider(None).name == "mock", get_provider(None).name)
    with env(AI_PROVIDER="deepseek", DEEPSEEK_API_KEY=FAKE_KEY):
        check("显式设置才切到 deepseek",
              get_provider(None).name == "deepseek", get_provider(None).name)
    with env(AI_PROVIDER="MOCK"):
        check("大小写不敏感", get_provider(None).name == "mock")
    try:
        get_provider("不存在的provider")
        check("未知 Provider 名报错", False)
    except ValueError as exc:
        check("未知 Provider 名报错", "未知的 AI Provider" in str(exc), str(exc))
    check("catalog 覆盖两个 Provider",
          {item["name"] for item in provider_catalog()} == {"mock", "deepseek"}, provider_catalog())

    # ================================================================
    print("== 15. Prompt Builder")
    system_prompt = prompt_builder.build_system_prompt()
    check("只声明已注册的动作",
          all(name in system_prompt for name in actions.ACTIONS), sorted(actions.ACTIONS))
    forbidden = [name for name in ("attack", "talk", "use_item", "inspect") if name in system_prompt]
    check("不声明未实现的动作（attack/talk/use_item/inspect）", forbidden == [], forbidden)
    check("动作说明来自注册表（含 summary）",
          actions.ACTIONS["rest"].summary in system_prompt, actions.ACTIONS["rest"].summary)
    check("要求只输出 JSON", "只输出一个 JSON 对象" in system_prompt)
    check("明确禁止输出数据库 ID", "数据库 ID" in system_prompt)
    check("说明动作只是建议、引擎会校验", "建议" in system_prompt and "拒绝" in system_prompt)
    check("不清楚就返回空数组", '"actions": []' in system_prompt)
    check(
        "禁止把玩家意图替换成另一个动作（真实联调发现的问题）",
        "不要替换玩家的意图" in system_prompt and "做不到" in system_prompt,
        system_prompt[-400:],
    )
    check("给出了 rest 的参数格式", "minutes" in system_prompt)

    user_prompt = prompt_builder.build_user_prompt("我走进阅览室", context)
    check("user prompt 含玩家输入", "我走进阅览室" in user_prompt)
    check("user prompt 含当前地点", "灰塔图书馆·大厅" in user_prompt)
    check("user prompt 含可见 NPC / 物品",
          "阿黛尔" in user_prompt and "黄铜钥匙" in user_prompt)
    check("user prompt 含近期事件", "你走进大厅。" in user_prompt)
    check("user prompt 里没有任何数据库 id 键", '"id"' not in user_prompt, user_prompt[:200])

    # V0.3：长期记忆必须真的进 Prompt
    with_memories = dataclasses.replace(
        context,
        memories=[
            {"importance": "S", "category": "world", "scope": "world",
             "subjects": ["北塔"], "content": "北塔已经坍塌。", "created_turn": 3},
            {"importance": "A", "category": "relationship", "scope": "npc:阿黛尔",
             "subjects": ["阿黛尔"], "content": "阿黛尔答应帮忙调查地下档案室。",
             "created_turn": 5},
        ],
    )
    memory_prompt = prompt_builder.build_user_prompt("我去看看", with_memories)
    check("user prompt 含 memories 字段", '"memories"' in memory_prompt)
    check("记忆内容进了 Prompt",
          "北塔已经坍塌" in memory_prompt and "阿黛尔答应帮忙" in memory_prompt)
    check("记忆进 Prompt 时仍然没有数据库 id", '"id"' not in memory_prompt, memory_prompt[-300:])
    check("system prompt 说明记忆低于当前状态",
          "一律以当前状态为准" in prompt_builder.build_system_prompt())

    check("messages 结构正确",
          [m["role"] for m in prompt_builder.build_messages("x", context)] == ["system", "user"])

    # ================================================================
    print("== 16. 架构边界：DeepSeekProvider 不碰数据层")
    source_path = os.path.join(ROOT, "backend", "ai", "deepseek.py")
    with open(source_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[-1])
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
    forbidden_imports = imported & {"repository", "sqlite3", "game_state", "commands"}
    check("不 import repository / sqlite3 / game_state / commands",
          forbidden_imports == set(), sorted(forbidden_imports))
    check("没有内联 SQL",
          not any(word in source for word in ("INSERT INTO", "UPDATE ", "DELETE FROM")))
    check("没有 conn.execute 调用", "conn.execute" not in source)
    check("不执行 Action", ".perform(" not in source)
    check("HTTP 调用被隔离在 transport 里（可注入、可测）",
          "self._transport(" in source and "urllib_transport" in source)

    # 整个 ai 包也不许碰数据层（沿用 task 001 的约束）
    ai_dir = os.path.join(ROOT, "backend", "ai")
    offenders = []
    for filename in sorted(os.listdir(ai_dir)):
        if not filename.endswith(".py"):
            continue
        with open(os.path.join(ai_dir, filename), encoding="utf-8") as fh:
            body = fh.read()
        if "import repository" in body or "from .. import repository" in body:
            offenders.append(filename)
        if any(word in body for word in ("INSERT INTO", "UPDATE ", "DELETE FROM")):
            offenders.append(filename + "（内联 SQL）")
    check("backend/ai/ 整体仍然不碰 repository / 不写 SQL", offenders == [], offenders)

    print("== 17. Mock 没被删，且仍是默认")
    check("MockAIProvider 仍然存在", get_provider("mock").name == "mock")
    with env(AI_PROVIDER=None):
        check("默认仍是 mock（不会因为装了 Key 就联网）", get_provider(None).name == "mock")

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (PASSED, FAILED))
    print("=" * 46)
    return 1 if FAILED else 0


class _FakeBody:
    """给 urllib.error.HTTPError 当 fp 用（它只要求 .read() 和 .close()）。"""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, *args):
        return self._data

    def close(self):
        return None


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    sys.exit(main())
