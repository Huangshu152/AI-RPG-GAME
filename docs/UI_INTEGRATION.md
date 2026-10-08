# UI Vertical Slice Integration Notes

这是一版纯前端 Vertical Slice UI，目标是让当前 Runtime 看起来像一个可以试玩的 AI RPG，而不是后台管理页面。

## 当前兼容策略

UI 可以直接跑在旧 V0.1 后端上：

- `/api/state`
- `/api/game/new`
- `/api/command`

自然语言输入会优先尝试 V0.2/V0.3 的：

- `POST /api/turn` body `{"input": "...", "provider": "mock|deepseek"}`

如果服务端返回 404/405，则自动回退到 `/api/command`，因此这份 UI 在尚未接入 AI 的旧 Runtime 里也不会直接崩掉。

## V0.2/V0.3 接线点

### Theme Name

顶部的“主题名”目前先用 browser localStorage 保存，代码位置：

- `frontend/app.js`
- `currentTopicName()`
- `setTopicName()`

后端有正式的 `theme_name` 字段/API 后，应改成后端持久化，并保证它和 Save/Load 属于同一存档语义。

如果状态中已有：

```json
{"theme_name": "灰塔之下"}
```

UI 会优先显示服务器值。

### Memories

如果 `/api/state` 或相应结果带有：

```json
{"memories": [{"importance":"B","category":"discovery","content":"...","created_turn":2}]}
```

UI 会自动渲染 Memory 面板。

UI 不读取 `id`、`world_id`、`source_event_id` 等内部字段。

### Events

`/api/turn` 若返回 `events`，UI 会显示最近事件；如果只有 `executed.type`，UI 会生成一个最小事件项。

### Provider

顶部 Provider 选择器当前提供：

- `mock`
- `deepseek`

`sendTurn()` 会在请求体带上当前 provider。

### Turn Result

UI 兼容当前 V0.2/V0.3 回合响应中的：

- `narrative`
- `actions`
- `executed`
- `events`
- `messages`
- `error`
- `state`

## 原则

前端不能自行修改：

- HP / SAN
- inventory
- location
- NPC state
- world time
- events
- memory

所有 Gameplay State Mutation 必须继续通过后端 Runtime / Action Engine。
