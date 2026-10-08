/* AI RPG Engine V0.1 —— 前端逻辑
 *
 * 只做三件事：取状态、渲染、发指令。
 * 所有来自服务器的文本都用 textContent 写入，避免玩家名/描述注入 HTML。
 */
"use strict";

var el = function (id) { return document.getElementById(id); };

var history = [];        // 已发送的指令
var historyIndex = -1;   // 上下键浏览用
var busy = false;

/* ---------------- 工具 ---------------- */
function node(tag, className, text) {
  var n = document.createElement(tag);
  if (className) { n.className = className; }
  if (text !== undefined && text !== null) { n.textContent = String(text); }
  return n;
}

function clampPercent(value, max) {
  var m = Number(max);
  if (!isFinite(m) || m <= 0) { return 0; }
  var pct = (Number(value) / m) * 100;
  if (!isFinite(pct)) { return 0; }
  return Math.max(0, Math.min(100, pct));
}

function api(path, options) {
  return fetch(path, options).then(function (res) {
    if (!res.ok) {
      return res.text().then(function (body) {
        throw new Error("HTTP " + res.status + (body ? " " + body.slice(0, 200) : ""));
      });
    }
    return res.json();
  });
}

/* ---------------- 日志 ---------------- */
var KIND_LABEL = {
  narration: "叙事",
  system: "系统",
  error: "错误",
  player: "你"
};

function appendLog(entries) {
  var log = el("log");
  entries.forEach(function (entry) {
    var kind = entry.kind || "narration";
    var wrap = node("div", "entry " + kind);
    wrap.appendChild(node("div", "who", KIND_LABEL[kind] || "叙事"));
    wrap.appendChild(node("div", "entry-body", entry.text));
    log.appendChild(wrap);
  });
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  el("log").textContent = "";
}

// 用服务器上的历史重建日志。刷新页面、载入存档回滚剧情时都要走这里，
// 否则界面会留着"未来"的对话，而状态已经回到过去了。
function renderHistory(history) {
  if (!history || !history.length) { return false; }
  appendLog(history.map(function (h) {
    return { kind: h.kind, text: h.text };
  }));
  return true;
}

function refreshLog() {
  return api("/api/state").then(function (fresh) {
    clearLog();
    render(fresh);
    if (!renderHistory(fresh.history)) {
      appendLog([{ kind: "system", text: "（暂无剧情记录）" }]);
    }
  }).catch(function (err) {
    appendLog([{ kind: "error", text: "重建日志失败：" + err.message }]);
  });
}

/* ---------------- 渲染 ---------------- */
function statRow(label, value, max, cls) {
  var row = node("div", "stat");
  row.appendChild(node("span", "stat-label", label));

  var bar = node("div", "bar");
  var fill = node("div", "bar-fill " + cls);
  fill.style.width = clampPercent(value, max) + "%";
  bar.appendChild(fill);
  row.appendChild(bar);

  row.appendChild(node("span", "stat-value", value + " / " + max));
  return row;
}

function itemList(items) {
  var ul = node("ul", "item-list");
  items.forEach(function (it) {
    var li = node("li");
    li.appendChild(node("span", null, it.name));
    if (it.quantity > 1) { li.appendChild(node("span", "qty", " ×" + it.quantity)); }
    if (it.description) { li.appendChild(node("div", "muted small", it.description)); }
    ul.appendChild(li);
  });
  return ul;
}

function renderPlayer(player) {
  var box = el("player-card");
  box.textContent = "";
  box.classList.toggle("empty", !player);

  if (!player) {
    box.textContent = "还没有玩家。请在下方创建新游戏。";
    return;
  }

  box.appendChild(node("div", "player-name", player.name));
  box.appendChild(statRow("HP", player.hp, player.max_hp, "hp"));
  box.appendChild(statRow("SAN", player.san, player.max_san, "san"));

  box.appendChild(node("div", "sub-title", "物品（" + player.items.length + "）"));
  if (player.items.length === 0) {
    box.appendChild(node("div", "muted", "（空）"));
  } else {
    box.appendChild(itemList(player.items));
  }
}

function renderScene(state) {
  var box = el("scene");
  box.textContent = "";

  if (!state.has_game || !state.location) {
    box.className = "card empty";
    box.textContent = state.has_game ? "当前地点未知。" : "还没有进行中的游戏。";
    return;
  }

  box.className = "card";
  box.appendChild(node("div", "player-name", state.location.name));
  box.appendChild(node("div", null, state.location.description));

  if (state.location.exits.length > 0) {
    var labels = state.location.exits.map(function (e) { return e.label; });
    box.appendChild(node("div", "muted small exit-line", "出口：" + labels.join("、")));
  }
}

function renderLocation(state) {
  var box = el("location-card");
  box.textContent = "";

  if (!state.has_game || !state.location) {
    box.classList.add("empty");
    box.textContent = "--";
    return;
  }

  box.classList.remove("empty");
  box.appendChild(node("div", "player-name", state.location.name));
  box.appendChild(node("div", "muted small", "世界时间：" + state.world.time));

  if (state.location.exits.length === 0) {
    box.appendChild(node("div", "muted small", "没有出口"));
  } else {
    state.location.exits.forEach(function (e) {
      box.appendChild(node("div", "small", "→ " + e.label + "：" + e.target_name));
    });
  }
}

function renderNpcs(state) {
  var box = el("npc-card");
  box.textContent = "";

  var here = state.has_game
    ? state.npcs.filter(function (n) { return n.here; })
    : [];

  if (here.length === 0) {
    box.classList.add("empty");
    box.textContent = state.has_game ? "这里没有别人。" : "--";
    return;
  }

  box.classList.remove("empty");
  here.forEach(function (n) {
    var row = node("div");
    row.appendChild(node("div", null, n.name));
    row.appendChild(node("div", "muted small", n.status));
    box.appendChild(row);
  });
}

function renderItems(state) {
  var box = el("item-card");
  box.textContent = "";

  var items = (state.has_game && state.location) ? state.location.items : [];

  if (items.length === 0) {
    box.classList.add("empty");
    box.textContent = state.has_game ? "这里没有可拾取的物品。" : "--";
    return;
  }

  box.classList.remove("empty");
  box.appendChild(itemList(items));
}

function render(state) {
  el("world-name").textContent = state.has_game ? state.world.name : "尚未载入世界";
  el("world-time").textContent = state.has_game ? state.world.time : "--";
  renderPlayer(state.has_game ? state.player : null);
  renderScene(state);
  renderLocation(state);
  renderNpcs(state);
  renderItems(state);
}

/* ---------------- 交互 ---------------- */
function setConn(ok, text) {
  var c = el("conn");
  c.className = "conn " + (ok ? "ok" : "bad");
  c.textContent = text;
}

function setBusy(value) {
  busy = value;
  // busy 时发送按钮变成「取消」，而不是禁用 —— 真实模型可能要等十几秒，
  // 玩家必须能中途放弃。
  el("send").textContent = value ? "取消" : "发送";
  el("btn-new").disabled = value;
}

// ---------------- 请求生命周期（超时 / 取消） ----------------
var activeController = null;
var activeTimer = null;

function endRequest() {
  if (activeTimer) {
    clearTimeout(activeTimer);
    activeTimer = null;
  }
  activeController = null;
  setBusy(false);
}

function cancelActiveRequest() {
  if (activeController) {
    activeController.abort();
  }
}

// 真实模型比 Mock 慢得多：给自然语言回合更长的超时，斜杠指令仍然要快
var TIMEOUT_TURN_MS = 60000;
var TIMEOUT_COMMAND_MS = 15000;

function loadProviders() {
  // 最小选择器：从 /api/health 拿可用 Provider，不做模型管理 UI
  api("/api/health").then(function (health) {
    var select = el("provider");
    if (!select || !health.providers) { return; }
    select.textContent = "";
    health.providers.forEach(function (p) {
      var option = document.createElement("option");
      option.value = p.name;
      option.textContent = p.available ? p.name : p.name + "（未配置）";
      option.disabled = !p.available && !p.active;
      option.selected = !!p.active;
      select.appendChild(option);
    });
  }).catch(function () {
    // 选择器不是关键路径，拉不到就保持空着
  });
}

function handleResult(result) {
  // rollback / session_switch：状态被整体换掉了，日志必须按服务器上的历史重建，
  // 不能只往后追加。这两类事件来自后端的结构化 events 通道。
  var reset = (result.events || []).some(function (e) {
    return e.type === "rollback" || e.type === "session_switch";
  });

  if (reset) {
    if (result.state) { render(result.state); }
    refreshLog();
    return;
  }

  appendLog(result.messages || []);
  if (result.state) { render(result.state); }
}

function sendCommand(text) {
  if (busy) { return; }
  var value = (text || "").trim();
  if (!value) { return; }

  appendLog([{ kind: "player", text: value }]);
  history.push(value);
  historyIndex = history.length;
  setBusy(true);

  // 斜杠开头走 Command System；自然语言走 AI 回合（Turn Runtime → Action Engine）
  var isCommand = value.charAt(0) === "/";
  var path = isCommand ? "/api/command" : "/api/turn";
  var body = isCommand ? { text: value } : { input: value };

  if (!isCommand) {
    // 把玩家选中的 Provider 一起带上（留空则用服务端配置的默认值）
    var picked = el("provider");
    if (picked && picked.value) { body.provider = picked.value; }
  }

  activeController = new AbortController();
  var limit = isCommand ? TIMEOUT_COMMAND_MS : TIMEOUT_TURN_MS;
  var timedOut = false;
  activeTimer = setTimeout(function () {
    timedOut = true;
    cancelActiveRequest();
  }, limit);

  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: activeController.signal
  }).then(function (result) {
    setConn(true, "已连接");
    handleResult(result);
  }).catch(function (err) {
    if (err && err.name === "AbortError") {
      // 超时和手动取消要分开说，否则玩家不知道发生了什么
      appendLog([{
        kind: "system",
        text: timedOut
          ? "请求超时（" + Math.round(limit / 1000) + " 秒），已取消。"
          : "已取消这次请求。"
      }]);
      return;
    }
    setConn(false, "连接失败");
    appendLog([{ kind: "error", text: "请求失败：" + (err && err.message ? err.message : err) }]);
  }).then(endRequest);
}

function createNewGame() {
  if (busy) { return; }
  var name = el("new-name").value.trim();
  setBusy(true);

  api("/api/game/new", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ player_name: name })
  }).then(function (result) {
    setConn(true, "已连接");
    // 不用额外补日志：后端已经把这一回合写进历史，
    // 若切换了世界（session_switch）handleResult 会用服务器历史重建整个日志。
    handleResult(result);
    el("new-name").value = "";
  }).catch(function (err) {
    setConn(false, "连接失败");
    appendLog([{ kind: "error", text: "创建失败：" + err.message }]);
  }).then(function () {
    setBusy(false);
  });
}

function boot() {
  loadProviders();
  api("/api/state").then(function (state) {
    setConn(true, "已连接");
    render(state);
    if (state.has_game) {
      var restored = renderHistory(state.history);
      appendLog([{
        kind: "system",
        text: (restored ? "（以上是已保存的剧情记录）" : "已读回上次的游戏进度") +
              "（" + state.player.name + " @ " + state.world.name +
              "）。输入 /help 查看指令，/load 可载入存档。"
      }]);
    } else {
      appendLog([{
        kind: "system",
        text: "欢迎使用 AI RPG Engine V0.1。请先在左侧输入玩家名称并点击「创建新游戏」。"
      }]);
    }
  }).catch(function (err) {
    setConn(false, "连接失败");
    appendLog([{ kind: "error", text: "无法连接后端：" + err.message }]);
  });
}

el("send").addEventListener("click", function () {
  // busy 时按钮是「取消」
  if (busy) {
    cancelActiveRequest();
    return;
  }
  var input = el("cmd");
  sendCommand(input.value);
  input.value = "";
});

el("cmd").addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") {
    ev.preventDefault();
    el("send").click();
  } else if (ev.key === "ArrowUp") {
    if (historyIndex > 0) {
      historyIndex -= 1;
      ev.target.value = history[historyIndex];
    }
    ev.preventDefault();
  } else if (ev.key === "ArrowDown") {
    if (historyIndex < history.length - 1) {
      historyIndex += 1;
      ev.target.value = history[historyIndex];
    } else {
      historyIndex = history.length;
      ev.target.value = "";
    }
    ev.preventDefault();
  }
});

el("btn-new").addEventListener("click", createNewGame);

el("new-name").addEventListener("keydown", function (ev) {
  if (ev.key === "Enter") {
    ev.preventDefault();
    createNewGame();
  }
});

boot();
