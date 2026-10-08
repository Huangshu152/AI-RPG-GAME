"""HTTP 接口测试：需要一个已经启动的服务（见 README「运行方法」）。

    python tests\\test_api.py
    python tests\\test_api.py http://127.0.0.1:8000

只用标准库（urllib），不需要额外装测试依赖。
注意：它会真的创建一个新游戏存进 data/game.db。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# 同 test_smoke.py：仅在被重定向时强制 UTF-8，真实控制台交给 Python 自己处理。
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASSED = 0
FAILED = 0


def check(label, condition, extra=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [PASS] %s" % label)
    else:
        FAILED += 1
        print("  [FAIL] %s%s" % (label, ("  -> " + repr(extra)) if extra != "" else ""))


def call(method, path, payload=None):
    """返回 (status, body_bytes)。HTTP 错误也返回状态码而不是抛异常。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(path, payload=None, method="GET"):
    status, raw = call(method, path, payload)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, {"_raw": raw[:200]}


def cmd(text):
    status, body = api("/api/command", {"text": text}, "POST")
    return status, body


def all_text(body):
    return "\n".join(m.get("text", "") for m in body.get("messages", []))


def main():
    print("目标服务：%s\n" % BASE)

    print("== 1. 健康检查与静态页面")
    status, body = api("/api/health")
    check("GET /api/health 返回 200", status == 200, status)
    check("版本号正确", body.get("version") == "0.1.0", body)

    status, raw = call("GET", "/")
    check("GET / 返回 200", status == 200, status)
    check("首页就是 Web UI", "AI RPG Engine".encode("utf-8") in raw, raw[:120])

    status, raw = call("GET", "/style.css")
    check("GET /style.css 返回 200", status == 200, status)
    status, raw = call("GET", "/app.js")
    check("GET /app.js 返回 200", status == 200, status)

    print("== 2. 初始状态")
    status, state = api("/api/state")
    check("GET /api/state 返回 200", status == 200, status)
    check("state 含 has_game 字段", "has_game" in state, list(state.keys()))

    print("== 3. 创建新游戏（中文名走一遍 JSON 编码）")
    status, body = api("/api/game/new", {"player_name": "接口测试员"}, "POST")
    check("POST /api/game/new 返回 200", status == 200, status)
    check("创建成功", body.get("ok") is True, all_text(body))
    check("玩家名正确回传", body["state"]["player"]["name"] == "接口测试员",
          body["state"]["player"]["name"])

    print("== 4. 各条指令")
    status, body = cmd("/look")
    check("/look 成功", status == 200 and body.get("ok") is True, all_text(body))
    check("/look 有地点信息", "测试世界" in str(body["state"]["world"]["name"]) or
          body["state"]["location"]["name"], body["state"]["location"])

    status, body = cmd("/status")
    check("/status 含 HP 与 SAN", "HP" in all_text(body) and "SAN" in all_text(body), all_text(body))

    status, body = cmd("/inventory")
    check("/inventory 成功", body.get("ok") is True, all_text(body))

    status, body = cmd("/go 阅览室")
    check("/go 中文出口成功", body.get("ok") is True, all_text(body))
    check("地点已切换", body["state"]["location"]["name"] == "阅览室",
          body["state"]["location"]["name"])

    status, body = cmd("/take 泛黄的笔记")
    check("/take 成功", body.get("ok") is True, all_text(body))
    check("背包里有笔记", "泛黄的笔记" in [i["name"] for i in body["state"]["player"]["items"]],
          body["state"]["player"]["items"])

    status, body = cmd("/rest")
    check("/rest 成功", body.get("ok") is True, all_text(body))

    print("== 5. 保存与载入")
    status, body = cmd("/save")
    check("/save 成功", body.get("ok") is True, all_text(body))
    saved_location = body["state"]["location"]["name"]
    saved_hp = body["state"]["player"]["hp"]

    cmd("/go 向下的楼梯")
    _, moved = cmd("/look")
    check("移动到了新地点", moved["state"]["location"]["name"] == "地下档案室",
          moved["state"]["location"]["name"])

    status, body = cmd("/load")
    check("/load 成功", body.get("ok") is True, all_text(body))
    check("载入后回到保存时的地点", body["state"]["location"]["name"] == saved_location,
          body["state"]["location"]["name"])
    check("载入后 HP 一致", body["state"]["player"]["hp"] == saved_hp,
          (body["state"]["player"]["hp"], saved_hp))

    print("== 6. 异常与边界")
    status, body = cmd("我推开门")
    check("自由输入不报错并提示未接入 AI", body.get("ok") is True and "AI" in all_text(body),
          all_text(body))

    status, body = cmd("/不存在")
    check("未知指令 ok=False", body.get("ok") is False, body.get("ok"))

    status, body = cmd("")
    check("空输入被拒绝", body.get("ok") is False, body)

    status, body = api("/api/command", {"text": "x" * 501}, "POST")
    check("超长输入返回 422", status == 422, status)

    status, body = api("/api/state")
    check("最终状态仍是完整游戏", body.get("has_game") is True, body.get("has_game"))

    print("== 7. 自然语言回合 POST /api/turn")
    # 全部显式指定 provider=mock：自动测试绝不能因为机器上配了真实 Key
    # 就悄悄发起付费请求。
    def turn(text, provider="mock"):
        return api("/api/turn", {"input": text, "provider": provider}, "POST")

    status, body = api("/api/game/new", {"player_name": "回合接口测试员"}, "POST")
    check("先建一局新游戏（回到大厅）", body.get("ok") is True, all_text(body))
    check("起点是大厅", body["state"]["location"]["name"] == "灰塔图书馆·大厅",
          body["state"]["location"]["name"])

    status, body = turn("我走进阅览室")
    check("POST /api/turn 返回 200", status == 200, status)
    check("响应含 narrative", isinstance(body.get("narrative"), str) and bool(body["narrative"]),
          body.get("narrative"))
    check("响应含 actions（1 个）",
          isinstance(body.get("actions"), list) and len(body["actions"]) == 1, body.get("actions"))
    check("actions 是 move 阅览室",
          body["actions"][0]["type"] == "move" and body["actions"][0]["target"] == "阅览室",
          body.get("actions"))
    check("响应含 executed", (body.get("executed") or {}).get("type") == "move", body.get("executed"))
    check("响应含 events（move）",
          any(e["type"] == "move" for e in body.get("events", [])), body.get("events"))
    check("响应含 state，且地点已变",
          body["state"]["location"]["name"] == "阅览室", body["state"]["location"]["name"])

    status, body = turn("拿起泛黄的笔记")
    check("自然语言 take 真的进了背包",
          "泛黄的笔记" in [i["name"] for i in body["state"]["player"]["items"]],
          body["state"]["player"]["items"])

    status, body = turn("休息")
    check("自然语言 rest 推进了世界时间",
          any(e["type"] == "time_advance" for e in body.get("events", [])), body.get("events"))

    status, body = turn("今天天气不错")
    check("无法识别 → actions 为空数组", body.get("actions") == [], body.get("actions"))
    check("无法识别 → 仍然有 narrative", bool(body.get("narrative")), body.get("narrative"))
    check("无法识别 → 没有事件、没有 executed",
          body.get("events") == [] and body.get("executed") is None, body.get("events"))

    _, state_before_illegal = api("/api/state")
    status, body = turn("去月亮")
    check("非法目标被 Action Engine 拒绝", body.get("ok") is False, body.get("ok"))
    check("非法目标仍如实报告提出的动作",
          len(body.get("actions", [])) == 1, body.get("actions"))
    check("非法目标不产生任何事件", body.get("events") == [], body.get("events"))
    check("非法目标没有 executed", body.get("executed") is None, body.get("executed"))
    _, state_after_illegal = api("/api/state")
    check("非法目标后地点完全没变",
          state_after_illegal["location"]["name"] == state_before_illegal["location"]["name"],
          state_after_illegal["location"]["name"])

    status, body = turn("")
    check("空输入被拒绝", body.get("ok") is False, body)

    status, body = api("/api/turn", {"input": "x" * 501}, "POST")
    check("超长输入返回 422", status == 422, status)

    status, body = api("/api/health")
    check("health 报告当前 Provider（受机器配置影响，只要求是受支持的名字）",
          body.get("provider") in {"mock", "deepseek"}, body)

    status, body = api("/api/command", {"text": "/look"}, "POST")
    check("斜杠指令仍然正常（回归）", body.get("ok") is True, all_text(body))

    print("== 8. Provider 选择与配置")
    status, health = api("/api/health")
    check("health 报告 provider_configured", isinstance(health.get("provider_configured"), bool), health)
    check("health 列出两个 Provider",
          {p["name"] for p in health.get("providers", [])} == {"mock", "deepseek"},
          health.get("providers"))
    check("默认 Provider 是受支持的名字",
          health.get("provider") in {"mock", "deepseek"}, health.get("provider"))
    check("每个 Provider 都带 available 标记",
          all(isinstance(p.get("available"), bool) for p in health.get("providers", [])),
          health.get("providers"))

    # 用"休息"而不是"去阅览室"：它和当前地点无关，不会受前面几节移动的影响
    status, body = api("/api/turn", {"input": "休息", "provider": "mock"}, "POST")
    check("可以显式指定 mock", body.get("ok") is True, body.get("error"))
    check("确实用的是 mock（叙事带 Mock AI 标记）",
          "Mock AI" in (body.get("narrative") or ""), body.get("narrative"))

    status, body = api("/api/turn", {"input": "随便说点什么", "provider": "不存在的provider"}, "POST")
    check("未知 Provider 返回干净的错误而不是 500",
          status == 200 and body.get("ok") is False, (status, body.get("ok")))
    check("错误信息可读", "未知的 AI Provider" in (body.get("error") or ""), body.get("error"))
    check("未知 Provider 时 state 仍然返回", body.get("state") is not None)

    deepseek_state = [p["available"] for p in health["providers"] if p["name"] == "deepseek"]
    if deepseek_state == [False]:
        # 本机没有 Key，正好可以验证"配置缺失 → 可读错误，且不崩溃、不产生事件"
        status, body = api("/api/turn", {"input": "我走进阅览室", "provider": "deepseek"}, "POST")
        check("未配置 Key 时选 deepseek 得到可读错误",
              body.get("ok") is False and "DEEPSEEK_API_KEY" in (body.get("error") or ""),
              body.get("error"))
        check("未配置 Key 时不产生任何事件", body.get("events") == [], body.get("events"))
        check("未配置 Key 时没有 executed", body.get("executed") is None, body.get("executed"))
    else:
        # 本机配了 Key —— 不在这里发真实请求（会自动测试不该花钱）
        check("（跳过真实 deepseek 调用：本机已配置 Key，避免自动测试花钱）", True)
        check("（同上）", True)
        check("（同上）", True)

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (PASSED, FAILED))
    print("=" * 46)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
