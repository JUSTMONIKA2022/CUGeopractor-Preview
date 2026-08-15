# Geopractor API 联动协议（程序间接入接口）

> 版本：v1.1
> 用途：允许其他程序/软件通过规定的 JSON API 协议调用 Geopractor 的能力
> （健康检查、Agent 对话、工具清单、缓存查询、命令执行、综合调研），实现程序间联动。
> 服务地址：`geopractor serve` 启动后默认 `http://127.0.0.1:8080`。

## 0. 通用约定

- 除健康检查外，所有接口均为 JSON；字符编码 UTF-8；
- **鉴权**：本机默认不鉴权（开箱即用）；若配置了 `GEOPRACTOR_API_TOKEN`，
  请求必须带 `Authorization: Bearer <token>`，否则返回 401；
- 统一响应：成功 `{"ok": true, ...}`；失败 `{"ok": false, "error": "原因"}`；
- **模型依赖**：`/api/commands`、`/api/chat`、`/api/agent/invoke`、`/api/research`
  需要已配置模型服务（`geopractor configure`），未配置返回 400 可读提示；
  `/api/health`、`/api/agent/tools`、`/api/cache*` 不依赖模型。

## 1. 接口总览

| 方法 | 路径 | 用途 | 依赖模型 |
|---|---|---|---|
| GET | `/api/health` | 健康/配置/工具状态检查 | 否 |
| GET | `/api/agent/tools` | 列出当前可用工具（连接器 + 知识检索） | 否 |
| POST | `/api/chat` | 对话（Web 页面用，简单一问一答） | 是 |
| POST | `/api/agent/invoke` | Agent 对话（按 session_id 隔离会话，返回工具清单） | 是 |
| GET | `/api/cache` | 列出全部缓存渠道状态 | 否 |
| GET | `/api/cache/{channel}` | 读取/强制刷新某渠道缓存 | 否 |
| POST | `/api/commands` | 执行 CLI 命令系统命令（返回文本输出） | 部分（仅 `/llm`、`/research`） |
| POST | `/api/research` | 综合调研（多来源交叉验证） | 是 |

## 2. 健康与配置

```
GET /api/health
```

响应：

```json
{
  "ok": true,
  "configured": true,
  "knowledge_blocks": 12,
  "connectors": ["cug_news_search", "cug_college_search", "zhihu_search", "..."]
}
```

- `configured`：是否已配置模型（false 时依赖模型的接口/命令——`/api/chat`、
  `/api/agent/invoke`、`/api/research`、`/api/commands` 的 `/llm` 与 `/research`——
  会返回 400；`/api/commands` 的纯命令不受影响）；
- `connectors`：当前已注册的渠道工具名清单（不含知识检索）。

## 3. 工具清单

```
GET /api/agent/tools
```

响应：

```json
{
  "tools": [
    {"name": "knowledge_search", "description": "在本地知识库中检索..."},
    {"name": "cug_news_search", "description": "实时检索中国地质大学（武汉）官网公开栏目..."},
    {"name": "cug_college_search", "description": "检索中国地质大学各学院官网的内容栏目..."},
    {"name": "is_office_hours", "description": "判断当前时间是否处于办公时间..."}
  ]
}
```

## 4. Agent 对话

### 4.1 简单对话（Web 用）

```
POST /api/chat
Content-Type: application/json

{"message": "本周学校有什么通知？"}
```

响应：`{"reply": "..."}`（未配置模型返回 400；LLM 错误返回 502）。

### 4.2 Agent 对话（程序接入主入口）

```
POST /api/agent/invoke
Content-Type: application/json

{"message": "我下学期的课表", "session_id": "myapp-1"}
```

响应：

```json
{"ok": true, "reply": "...", "session_id": "myapp-1", "tools": ["cug_course", "..."]}
```

- `session_id`：会话隔离键（相同 id 共享对话历史；不传默认独立会话）；
- `tools`：当前可用工具清单（调用方可据此判断 Agent 能力）。

## 5. 缓存查询

### 5.1 列出全部缓存渠道

```
GET /api/cache
```

响应：

```json
{
  "ok": true,
  "channels": [
    {"channel": "ifmweb", "name": "信息门户", "desc": "...", "cached": true,
     "updated": 1786550924, "error": null, "section_count": 75, "command": "/cache_ifmweb"}
  ]
}
```

### 5.2 读取/刷新某渠道缓存

```
GET /api/cache/{channel}?refresh=0
```

- `refresh=1` 时强制重新生成缓存后返回；
- 渠道：`ofcweb`（官网）、`ifmweb`（信息门户）、`tieba`（贴吧）、
  `zhihu`（知乎）、`bilibili`（B站）、`jwgl`（教务）；未知渠道返回 404。

响应（统一缓存 schema）：

```json
{
  "ok": true,
  "data": {
    "channel": "ifmweb",
    "name": "信息门户",
    "updated": 1786550924,
    "error": null,
    "sections": [
      {"key": "pwps", "name": "勤工助学", "url": "https://i.cug.edu.cn/...",
       "desc": "分类：教学管理｜部门：本科生院｜咨询：027-...", "items": []}
    ]
  }
}
```

- `url` 非空：该功能有外部办理入口（外部程序可直接打开/跳转）；
- `items` 非空：该功能为列表型（如官网栏目新闻、贴吧帖子），条目含 `name/url/desc`。

## 6. 命令执行

执行 CLI 命令系统的任意命令，返回文本输出（与 CLI 交互效果一致）。
CLI 与 Web `/api/commands` 共用同一分发入口（`dispatch_command`）。

```
POST /api/commands
Content-Type: application/json

{"command": "/live_college 自动化 招生", "session_id": "myapp-1"}
```

响应：

```json
{"ok": true, "output": "学院网站检索「人工智能与自动化学院」...", "session_id": "myapp-1"}
```

**支持命令**（与 CLI 完全一致，命令可省略前导 `/`）：

- 缓存：`/cache_search`、`/cache_<渠道>`、`/cache_<渠道> <关键词|序号>`、
  `/cache_refresh [渠道]`、`/..`（返回上层缓存导航）；
- 实时查询（不调 LLM，直接查数据源）：
  - 公开/社区：`/live_news <关键词>`、`/live_nav [关键词]`（官网机构导航）、
    `/live_college <学院> [关键词]`（学院网站检索）、`/live_zhihu`、`/live_zhihu_global`、
    `/live_bilibili`、`/live_tieba`、`/live_xhs`；
  - 门户只读：`/live_catalog`、`/live_service`、`/live_process`、`/live_room`（自习室课表，
    自动下载图片到 `data/exports/live_room/`）、`/live_todo`、`/live_finished`、
    `/live_notices`、`/live_profile`；
  - 教务：`/live_course [学期]`、`/live_grade [学期]`、`/live_exam [学期]`、
    `/live_student`、`/live_plan`（概要+97 门课程明细，导出 txt/json，附官方 PDF 另存引导）；
- 编排与办公：`/next_course`（下一节课倒计时）、`/next`（查看下一张自习室课表图片）、
  `/schedule`（课表预设方案：南望山夏冬自动切换/未来城，可切换/改单节/reset）、
  `/office_hours`（当前是否办公时间）；
- 其他：`/llm <问题>`（切 LLM 查询）、`/research <主题>`、`/cron list|add|remove`、
  `/course [学期]`（直达教务课表）、`/configure`（管理 LLM 方案）、
  `/login`（/session-login，会话内浏览器登录）、`/help [主题]`。

> 注意：涉及教务/门户的命令（`/live_*`、`/course` 等）需要登录态
> （`geopractor session-login` 或 `/login` 登录过一次）；未登录返回可读提示。
>
> 模型门槛（放宽，与 CLI 对齐）：未配置模型服务时，`/llm`、`/research`
> 返回 400；其余纯命令（缓存/实时/课表/办公时间/编排等）可直接执行，无需模型。

## 7. 综合调研

多来源（官网/门户/学院/贴吧/知乎/B站）交叉搜集并输出结构化调研报告。

```
POST /api/research
Content-Type: application/json

{"message": "地大宿舍条件", "session_id": "myapp-1"}
```

响应：

```json
{"ok": true, "reply": "【调研报告】\n...", "session_id": "myapp-1"}
```

## 8. 联动示例（curl）

```bash
# 健康检查
curl http://127.0.0.1:8080/api/health
# 工具清单
curl http://127.0.0.1:8080/api/agent/tools
# 列缓存渠道
curl http://127.0.0.1:8080/api/cache
# 读门户缓存（含服务入口 URL）
curl "http://127.0.0.1:8080/api/cache/ifmweb"
# 执行命令（实时查询学院网站检索）
curl -X POST http://127.0.0.1:8080/api/commands \
  -H "Content-Type: application/json" \
  -d '{"command": "live_college 自动化 招生"}'
# 执行命令（查看课表预设方案）
curl -X POST http://127.0.0.1:8080/api/commands \
  -H "Content-Type: application/json" \
  -d '{"command": "/schedule"}'
# Agent 对话（程序接入）
curl -X POST http://127.0.0.1:8080/api/agent/invoke \
  -H "Content-Type: application/json" \
  -d '{"message": "自动化学院最近有什么通知？", "session_id": "myapp-1"}'
# 综合调研
curl -X POST http://127.0.0.1:8080/api/research \
  -H "Content-Type: application/json" -d '{"message": "地大宿舍条件"}'
# 带鉴权（配置了 GEOPRACTOR_API_TOKEN 时所有接口均需）
curl -H "Authorization: Bearer <token>" http://127.0.0.1:8080/api/cache
```
