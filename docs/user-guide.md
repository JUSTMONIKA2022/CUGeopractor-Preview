# 行至大地·Geopractor 使用指南（User Guide）

> 版本：v1.0
> 配套：`docs/api-protocol.md`（程序联动协议）
> 面向：首次接触本项目的同学 / 老师，目标——**5 分钟内学会并完成第一个查询任务**。

---

## 1. 这是什么

「行至大地·Geopractor」是一个**本地部署、密钥自配**的校园信息 Agent：

- **自己掌控**：模型 API、校园账号、Cookie 全部存在你本机，不上传任何第三方；数据不出本机。
- **两种用法**（这是本产品区别于普通聊天机器人的核心）：
  1. **自然语言**：像聊天一样提问，Agent 自动调用各渠道工具（官网/门户/贴吧/知乎/B站/教务）去查、去交叉验证；
  2. **命令系统**：输入 `/cache_*` 等命令，**不调 LLM、秒级返回**缓存好的固定功能，并自动打开办理网址——写操作始终由你自己完成，Agent 只给入口。

---

## 2. 快速开始（5 分钟上手）

### 2.1 安装依赖

```powershell
pip install -e .          # 安装本体
pip install -e ".[render]"  # 可选：贴吧/教务等需要 Playwright 真实浏览器渲染时安装
```

### 2.2 首次配置模型（必做）

```powershell
geopractor configure
```

按提示填写（密钥会加密存本机，不落明文）：

| 项 | 说明 | 示例 |
|---|---|---|
| 模型服务 Base URL | OpenAI 兼容接口 | `https://api.deepseek.com/v1` |
| 模型名称 | 你订阅的模型 | `deepseek-chat` |
| API 密钥 | 可留空（保持旧值） | `sk-xxx` |

验证配置：

```powershell
geopractor status
```

### 2.3 准备校园登录态（可选，门户/教务工具需要）

```powershell
geopractor session-login     # 弹出浏览器，登录一次地大统一认证门户
```

之后 Agent 自动复用登录态访问门户只读服务与教务（课表/成绩/考试/学籍/培养方案）。

### 2.4 启动服务

两种形态任选：

```powershell
geopractor chat      # 终端交互式对话（推荐，命令系统最完整）
geopractor serve     # Web 界面（默认 http://127.0.0.1:8080）
```

---

## 3. 使用形态总览

| 形态 | 入口 | 适合场景 | 说明 |
|---|---|---|---|
| CLI | `geopractor chat` | 终端重度用户 | 命令系统最完整：缓存直查/定时任务/综合调研 |
| Web | `geopractor serve` | 轻量问答 | 对话窗 + 命令速查；`/` 开头同样走命令系统 |
| API | HTTP 接口 | 与其他程序/软件联动 | 见 `docs/api-protocol.md` |

三种形态共用同一套核心（配置/密钥/工具/缓存），行为一致。

---

## 4. CLI 详解

### 4.1 顶层命令（`geopractor <命令>`）

| 命令 | 作用 | 示例 |
|---|---|---|
| `configure` | 交互式配置 LLM（Base URL/模型/密钥） | `geopractor configure` |
| `chat` | 进入交互式对话（命令系统 + 自然语言） | `geopractor chat` |
| `index` | 重建知识库索引（扫描 `data/knowledge/` 文档） | `geopractor index` |
| `status` | 查看配置与知识库状态 | `geopractor status` |
| `serve` | 启动本地 Web UI（默认 127.0.0.1:8080） | `geopractor serve` |
| `session-login` | 浏览器登录地大认证门户，登录态持久保存 | `geopractor session-login` |

### 4.2 chat 内命令全表（以 `/` 开头）

进入 `geopractor chat` 后：

**缓存命令（不调 LLM，秒级返回）**

| 命令 | 作用 |
|---|---|
| `/cache_search` | 列出全部缓存渠道与状态（[已缓存] / [未生成] 首次访问自动生成） |
| `/cache_<渠道>` | 列出该渠道缓存的功能清单（如 `/cache_ifmweb`） |
| `/cache_<渠道> <关键词\|序号>` | 定位功能，显示详情并**自动打开办理网址** |
| `/cache_<渠道>_<key>` | 直达某功能（如 `/cache_ifmweb_pwps` 直达勤工助学） |
| `/..` 或 `/返回` | **返回上一层导航**：功能层 → 渠道列表 → 渠道总览 |
| `/back` | **回到上一次交互历史**（向前一层，逐轮回看） |
| `/forward` | **向后一层**：向最新方向前进一轮 |
| `/new` | **回到最新楼层**（历史只隐藏不删除） |
| `/cache_refresh [渠道]` | 强制刷新缓存（不填渠道=全部） |

**LLM 与调研命令**

| 命令 | 作用 |
|---|---|
| `/llm <问题>` | 切到 LLM 查询；若之前在缓存层级，会自动携带当前缓存路径上下文 |
| `/research <主题>` | 综合调研（**会调用 LLM**，消耗模型 token，需已配置模型）：由 LLM 自主调用多个渠道工具（官网/门户/贴吧/知乎/B站）逐渠道搜集 → 交叉验证 → 输出结构化报告 |

**实时命令（不调 LLM、不走缓存，直接实时查询数据源）**

| 命令 | 作用 |
|---|---|
| `/live` | 列出全部实时命令 |
| `/live_news [关键词]` | 官网实时检索（通知公告/学术动态/地大要闻） |
| `/live_nav [关键词]` | **官网机构导航**：学院/职能部门及官网链接（如 `/live_nav 自动化` → 人工智能与自动化学院） |
| `/live_college <学院> [关键词]` | **学院网站检索**：实时抓取学院官网全部内容栏目（通知公告/学院新闻/学术动态/招生工作/党建工作等）的列表标题与链接，可按关键词过滤（如 `/live_college 自动化 招生`；内置 40 个学院，支持简称匹配） |
| `/live_zhihu <关键词>` / `/live_zhihu_global <关键词>` | 知乎站内 / 全网实时搜索 |
| `/live_bilibili <关键词>` / `/live_tieba <关键词>` / `/live_xhs <关键词>` | B站 / 贴吧 / 小红书实时搜索 |
| `/live_catalog [关键词]` | 门户网上厅服务目录（实时） |
| `/live_service [关键词]` | 南望厅服务事项（实时） |
| `/live_process` / `/live_todo` / `/live_finished` / `/live_notices` | 我发起的流程 / 待办 / 已办 / 待阅通知（实时） |
| `/live_room` | 自习室课表（实时）：自动把课表**图片**下载到 `data/exports/live_room/` 并打开第一张（实测打通：列表 → 详情 → sys-attach 图片下载；同一张图按地址**去重**，不会下载出多份一模一样；多张时输入 **`/next`** 查看下一张） |
| `/live_profile` | 门户账户信息（实时） |
| `/live_course [学期]` / `/live_grade [学期]` / `/live_exam [学期]` | 教务课表 / 成绩 / 考试（实时，支持学期；如 `/live_grade 2025-2026-2` = 2025-2026学年第2学期，或 `/live_grade 上学期`） |
| `/live_student` / `/live_plan` | 教务学籍 / 培养方案（实时；`/live_plan` 输出**概要 + 97 门课程完整明细**（按类别分组），自动导出 `培养方案.txt`（可读）+ `培养方案.json`（课程明细原文件）并打开可读版；**官方 PDF**：教务对学生无直接下载接口，输出末尾附「浏览器打开培养方案页 → 打印 → 另存为 PDF」引导） |

**定时与杂项**

| 命令 | 作用 |
|---|---|
| `/cron list` | 查看定时任务（含已执行次数 / 完成状态） |
| `/cron add <渠道> <分钟> [次数]` | 添加定时刷新（如 `/cron add ifmweb 30` 不限次数；`/cron add tieba 60 5` 只执行 5 次；持久化到 `data/cache/cron.json`） |
| `/cron remove <id>` | 删除任务 |
| `/cron stop` | 停止调度（退出 CLI 时自动停止） |
| `/course [学期]` | **直达查询教务课表**（不调 LLM，复用登录会话；如 `/course 上学期`）。每次查询自动与上次缓存课表**对比**，换课/调课会明确提示差异 |
| `/next_course` | **下一节课**：基于结构化课表 + 时间编排推算（当前教学周/星期/节次 → 倒计时），显示"下一节课：课程 周X 第X-X节（HH:MM–HH:MM）@地点 距开始还有 X 分钟" |
| `/next` | **查看下一张自习室课表图片**：`/live_room` 下载多张课表图片后，逐张翻看（`/live_room` 只打开第一张） |
| `/schedule` | **课表预设方案配置**：内置两套校级标准时间表——**南望山校区**（夏/冬按日期自动切换：夏季 5/1–9/30、冬季 10/1–次年4/30，各 10 节课含 30 分钟大课间/午休差异）与**未来城校区**（无季节区分，12 节课）。查看 `/schedule`；切换 `/schedule campus 南望山\|未来城`；修改单节时间 `/schedule set period <节次> <HH:MM-HH:MM> [夏\|冬]`；恢复默认 `/schedule reset [夏\|冬]`；第一周周一 `/schedule set first_week_monday YYYY-MM-DD`（编排必需）。是 `/next_course` 的编排依据 |
| `/office_hours` | **当前是否办公时间**：依据当前校区方案的办公时间表判断（如南望山夏 上午08:00–12:00/下午14:30–17:30），显示当前时间与时段 |
| `/configure` | **管理 LLM 多方案**：`/configure` 查看｜`/configure add <名字>` 新增｜`/configure use <名字>` 切换（立即生效）｜`/configure remove <名字>` 删除｜`/configure show [名字]` 查看详情。多套 Base URL/模型/密钥分别存储，密钥按方案加密、不落明文 |
| `/login`（别名 `/session-login`） | **在 chat 内完成门户/教务登录**（等价 `geopractor session-login`）：弹出浏览器登录一次，登录态持久保存，之后自动复用 |
| `/help [主题]` | 帮助总览 / 分级帮助（`/help cache`、`/help llm`、`/help research`、`/help cron`、`/help course`、`/help api`） |
| `/clear` | 隐藏当前屏幕 + 清空 LLM 会话上下文（**交互历史保留**，`/back` 仍可回看） |
| `/exit` 或 `/quit` | 退出 |

**渠道速查**

| 渠道名 | 命令 | 内容 |
|---|---|---|
| 学校官网 | `/cache_ofcweb` | 通知公告/学术动态/地大要闻（实时检索） |
| 信息门户 | `/cache_ifmweb` | 网上厅服务目录（75 项服务）+ 已接入只读工具 |
| 百度贴吧 | `/cache_tieba` | 中国地质大学武汉吧公开帖子（本地服务） |
| 知乎 | `/cache_zhihu` | 地大相关内容（站内搜索） |
| B站 | `/cache_bilibili` | 地大相关内容（公开搜索） |
| 教务系统 | `/cache_jwgl` | 我的课表/成绩/考试/学籍/培养方案（需登录） |

### 4.3 实战示例：查"信息门户能不能办勤工助学"

```text
你 > /cache_search
  （返回：信息门户（/cache_ifmweb）：✅ 已缓存｜更新于 2026-08-13 00:58）

你 > /cache_ifmweb
  （返回：「信息门户」当前缓存的功能（75 项）：[1] 学生资助 …[5] 勤工助学（/cache_ifmweb 5 或 /cache_ifmweb_pwps）…）

你 > /cache_ifmweb 5        ← 或直接 /cache_ifmweb_pwps
  （返回：【勤工助学】说明 + 网址，并自动在浏览器打开办理入口）

你 > /llm 勤工助学怎么申请   ← 任意层级可切 LLM
  （LLM 结合你正在看的 /cache_ifmweb_pwps 上下文回答）
```

关键设计：**带网址的功能命令只负责打开浏览器，具体填写/提交由你自己完成**——写操作的红线不碰。

### 4.4 实战示例：综合调研与定时任务

```text
你 > /research 地大宿舍条件
  （Agent 从官网/门户/贴吧/知乎/B站搜集信息，交叉验证后输出结构化报告，
   社区来源会标注『该信息来自社区，仅供参考』）

你 > /cron add ifmweb 30
  （每 30 分钟后台刷新信息门户缓存，之后再 /cache_ifmweb 就是最新数据）
你 > /cron add tieba 60 5
  （每 60 分钟刷新贴吧缓存，只执行 5 次后标记完成）
```

### 4.5 实战示例：查询过往课表/成绩（按学期）

```text
你 > /course 2025-2026-2          ← 直达 2025-2026 学年第 2 学期课表（不调 LLM）
你 > /course 上学期             ← 相对当前学期
你 > 查询上学期课表             ← 自然语言同样支持（自动带上学期参数）
你 > 2025-2026-2 成绩怎么样      ← 成绩/考试接口同样支持按学期
```

> 说明：课表/成绩/考试连接器支持学期参数化（`semester_params`），
> 不再只能查默认学期——"agent 查不了过往课表"的问题已修复。

### 4.6 实战示例：实时命令（不调 LLM）

```text
你 > /live                          ← 列出全部实时命令
你 > /live_news 放假                 ← 官网实时检索
你 > /live_catalog 勤工助学           ← 门户服务目录实时查询
你 > /live_grade 2025-2026-2         ← 教务成绩实时查询（=2025-2026学年第2学期；也可用 /live_grade 上学期）
你 > /live_todo                      ← 我的待办实时查询
```

> 三种查询方式对照：
> - **自然语言**（直接输入）：LLM 自主决定工具 → 慢但灵活、可交叉验证
> - **缓存命令**（`/cache_*`）：秒回但数据最多 30 分钟前生成
> - **实时命令**（`/live_*`）：直接查数据源、不用 LLM → 快且最新，适合明确需求

---

## 5. Web UI 使用

启动 `geopractor serve` 后浏览器打开 `http://127.0.0.1:8080`：

1. **对话**：输入框直接提问；**以 `/` 开头同样走命令系统**（如 `/cache_search`），与 CLI 行为一致；
2. **命令速查**：页面下方「命令速查」按钮，随时查看命令列表；
3. **设置**：右上角「设置」弹窗填写 Base URL/模型/密钥（与 `geopractor configure` 等价）；
4. **重建知识库索引**：放入新文档到 `data/knowledge/` 后点击，把文档向量化导入知识库。

### 5.1 知识库（RAG）使用

知识库是**本地检索增强**数据源：把长期稳定的资料（校历、办事指南、培养方案等）放入 `data/knowledge/`（支持 txt / md / pdf），索引后对话时 LLM 自动检索相关内容并附来源引用。

1. **放文档**：复制示例 `docs/examples/knowledge/示例知识库.md` 到 `data/knowledge/`（或放入自己的资料）；
2. **建索引**：CLI 执行 `geopractor index`，或点击 Web 的「重建知识库索引」；索引为 0 块说明目录为空或尚未索引；
3. **看状态**：`geopractor status` 显示知识库块数；对话中问到资料内内容即自动命中。

**向量化模式**（`geopractor index` 会打印当前模式）：

- **默认**：ChromaDB 内置本地 ONNX 小模型——免密钥、数据不出本机，首次索引自动下载模型权重，开箱即用；
- **可选**：在 `.env` 配置 `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`，切换为 OpenAI 兼容 `/embeddings` 外部接口（更强模型或远程向量化）。

---

## 6. Agent 内置工具清单（自然语言自动调用）

| 类别 | 工具 | 说明 |
|---|---|---|
| 知识库 | `knowledge_search` | 你放入 `data/knowledge/` 的本地文档 |
| 官网 | `cug_news_search` | 通知公告/学术动态/地大要闻实时检索（TTL 缓存+条件请求） |
| 官网 | `cug_college_search` | 学院网站栏目检索（通知/新闻/动态等，返回标题+链接，支持关键词） |
| 知乎 | `zhihu_search` / `zhihu_global_search` | 站内 / 全网检索 |
| B站 | `bilibili_search` | 公开搜索 |
| 贴吧 | `tieba_search` | 外部服务→HTTP→Playwright 渲染降级链 |
| 小红书 | `xiaohongshu_search` | 需用户自带 Cookie（BYO 模式） |
| 门户·只读 | `portal_service_catalog` 网上厅完整服务目录 | 全量 75 项服务，关键词过滤 |
| 门户·只读 | `portal_my_processes` 我发起的流程 | 含流程详情链接（用户自行查看） |
| 门户·只读 | `portal_study_room_timetable` 自习室课表 | 返回课表文档列表 |
| 门户·只读 | `portal_service_items` 南望厅服务事项 | 公开页，免登录 |
| 门户·只读 | `portal_todo_tasks` / `portal_finished_tasks` | 我的待办 / 已办 |
| 门户·只读 | `portal_personal_info` / `portal_pending_notices` | 账户信息 / 待阅通知 |
| 教务 | `cug_course` / `cug_grade` / `cug_exam` / `cug_student_info` / `cug_training_plan` | 课表/成绩/考试/学籍/培养方案（需 `session-login`） |
| 时间编排 | `is_office_hours` | 当前是否办公时间（依据当前校区方案办公时间表；问「现在是办公时间吗」时调用） |

> 设计红线：**只读**。Agent 不提交任何申请、不发起审批；需要办理时给出入口链接，由你自己操作。

---

## 7. 与其他程序联动（API）

适合：让别的软件/脚本调用 Geopractor 的能力（详见 `docs/api-protocol.md`）：

| 接口 | 方法 | 作用 |
|---|---|---|
| `/api/cache` | GET | 渠道缓存状态列表 |
| `/api/cache/{channel}` | GET | 读取某渠道缓存（`?refresh=1` 强制刷新） |
| `/api/commands` | POST | 执行命令（`{"command":"/cache_search"}`） |
| `/api/research` | POST | 综合调研（`{"message":"..."}`） |
| `/api/agent/invoke` | POST | 自然语言对话（按 session_id 隔离会话） |
| `/api/health` | GET | 健康/配置检查 |

配置 `GEOPRACTOR_API_TOKEN` 后，请求头需带 `Authorization: Bearer <token>`。

---

## 8. 常见问题（FAQ）

**Q0：CLI 输出里的彩色前缀（[INFO]/[ERROR]/[WARN]/[GEO]）是什么意思？**
每次输入后 CLI 会清空旧输出、只保留本次结果（保持面板干净）；前缀颜色区分信息类型：**蓝色 `[INFO]`**=程序提示、**红色 `[ERROR]`**=错误、**黄色 `[WARN]`**=警告、**绿色 `[GEO]`**=CLI 返回的结果内容。颜色仅影响终端显示，Web/API 返回的仍是原始文本。

**Q1：`geopractor chat` 提示未配置模型？**
运行 `geopractor configure` 填写 Base URL/模型/密钥，再用 `geopractor status` 确认。

**Q2：门户/教务工具提示"会话失效"？**
运行 `geopractor session-login`（或在 chat 内直接输入 `/login`）在浏览器重新登录一次。教务会话短效（约 20~60 分钟），门户为长效会话；Agent 会自动复用与保活，失效时按提示重新登录即可。

**Q3：`/cache_*` 返回"生成缓存失败"或渠道为空？**
多为渠道临时不可用（如贴吧风控、未登录）。用 `/cache_refresh <渠道>` 重试；贴吧可配置本机 Tieba-API-SCF 服务（见 `docs/tieba-service-guide.md`）提高成功率。

**Q4：命令输错了？**
CLI 会给出相近命令建议（如输入 `/cache_ifweb` 提示你是不是想输入 `/cache_ifmweb`）。也可随时 `/help` 或 `/help <主题>` 看示例。

**Q5：缓存多久更新一次？**
默认 30 分钟 TTL；可手动 `/cache_refresh` 或 `/cron add <渠道> <分钟>` 定时刷新。

**Q6：Web 端能用命令系统吗？**
能。输入以 `/` 开头的文本即走 `/api/commands`，与 CLI 行为一致。

**Q7：LLM 对话历史保留几轮？**
每次调用 LLM 时注入的对话历史为**最近 10 条消息**（约 5 轮问答，`agent/_build_messages` 中 `history()[-10:]`）。超出部分不会被送进模型上下文，避免上下文膨胀；可用 `/clear` 清空当前会话上下文重开。

**Q8：知乎接口用的什么？**
知乎走**官方 OpenAPI**（`developer.zhihu.com` 开放平台），无需模拟登录/抓页面；两个工具：`zhihu_search`（站内搜索）与 `zhihu_global_search`（全网搜索）。

**Q9：怎么用"下一节课"和课表对比？**
- 先 `geopractor session-login` 登录教务 → `/course` 查询一次课表（自动缓存结构化快照）；
- `/schedule set first_week_monday YYYY-MM-DD` 配置第一周周一等编排参数；
- 之后随时 `/next_course` 查看下一节课与倒计时；
- 每次 `/course`/`/live_course` 或问 LLM"查课表"都会自动与上次缓存课表对比，换课/调课会明确提示差异（新增/取消/地点变动），避免按旧课表上课。

**Q10：`/configure` 怎么切换模型？**
`/configure list` 查看已存方案；`/configure add deepseek` 交互录入新方案；`/configure use deepseek` 切换（当前会话立即生效，无需重启）。多套 Base URL/模型/密钥分别存储，密钥按方案加密、不落明文。

**Q11：怎么导航到学院/办公室官网？**
输入 `/live_nav [关键词]`（不调 LLM、实时抓官网组织机构页）：无关键词输出学院/部门全量分组清单（学院 40 + 部门 43），带关键词按名称过滤。例：`/live_nav 自动化` →「人工智能与自动化学院（http://au.cug.edu.cn/）」；`/live_nav 图书馆` → 部门链接。自然语言提问（如"自动化学院官网是什么"）时 Agent 也会自动调用 `cug_navigation` 工具。

**Q12：`/back`、`/forward`、`/new` 有什么区别？**
三者都是**回看交互历史**（历史只隐藏不删除）：`/back` **向前一层**（往历史深处走）、`/forward` **向后一层**（逐层往最新方向走，与 `/back` 对称）、`/new` **直接回到最新楼层**（无论看过几轮历史，一步跳回当前）。缓存导航层级的回退用 `/..` 或 `/返回`，与历史回看不冲突。

---

## 9. 责任与边界（重要）

- 本工具为**本地个人使用**，所有校园账号/密钥/凭据仅存你本机；
- Agent **只读**：查询/整理/给入口；申请、审批、缴费等**写操作请自行在官方系统完成**；
- 社区来源（贴吧/知乎/B站/小红书）信息仅供参考，请以官方发布为准；
- 请勿使用本工具批量抓取、越权访问他人数据。
