# 行至大地·Geopractor

> 本地部署、用户自配密钥的开源校园 Agent：面向高校社区，统一检索校园公开信息与个人数据，
> 支持 **Web UI / CLI / Agent API** 三种接入形态。

Geopractor 以 LLM 工具调用（function calling）为核心：由用户自配的 LLM 负责理解问题，
通过内置连接器访问校园信息源（官网、学院网站、信息门户、教务系统及社区渠道），
并整理输出带来源的答案。项目不预置任何厂商密钥，所有凭据仅存用户本机。

## 功能特性

- **多源信息检索**
  - 官网通知公告 / 学术动态 / 地大要闻（实时检索 + 缓存）
  - 学院网站栏目检索（40 个学院官网，通知/新闻/动态等）
  - 社区渠道：知乎（官方 OpenAPI，站内 + 全网）、B 站、贴吧、小红书
- **个人校园数据（需登录态）**
  - 信息门户：办事服务目录、待办/已办、通知、个人资料、自习室课表
  - 教务系统：课表、成绩、考试安排、学籍信息、培养方案
- **课表与时间编排**
  - 两套校区课表预设（南望山夏/冬自动切换、未来城），支持单节修改与重置
  - 下一节课倒计时、办公时间判断
- **CLI 命令系统**：缓存查询、实时查询、课表、综合调研、定时任务等 20+ 命令
- **Agent API**：HTTP 接口，供其他程序/软件接入
- **本地优先**：密钥本机加密存储，凭据与数据不入库、不落仓库

## 快速开始

```bash
# 1. 安装（Python 3.11+）
pip install -e ".[dev]"

# 2. 配置
#    复制 .env.example 为 .env，填写 LLM_BASE_URL / LLM_MODEL（自配 API Key）
#    或运行 `geopractor configure` 交互式配置

# 3. 启动
geopractor chat          # 交互式 CLI
geopractor serve         # Web UI（http://127.0.0.1:8080）
```

访问个人校园数据（门户/教务）前，先登录一次：

```bash
geopractor session-login
```

登录态持久保存，之后课表/成绩/考试等查询自动复用。

## 使用方式

### CLI

```bash
geopractor chat
```

聊天内可直接使用 `/` 命令（无需退出会话）：

| 类别 | 命令 |
|---|---|
| 实时查询 | `/live_news`、`/live_college`、`/live_zhihu`、`/live_bilibili`、`/live_tieba`、`/live_xhs` |
| 门户/教务 | `/live_catalog`、`/live_room`、`/live_course`、`/live_grade`、`/live_exam`、`/live_plan` 等 |
| 课表编排 | `/schedule`、`/next_course`、`/office_hours`、`/next` |
| 缓存 | `/cache_search`、`/cache_<渠道>` |
| 其他 | `/llm <问题>`、`/research <主题>`、`/cron`、`/configure`、`/login`、`/help` |

完整命令说明见 [docs/user-guide.md](docs/user-guide.md)。

### Web UI

```bash
geopractor serve
```

默认监听 `127.0.0.1:8080`，提供对话窗与配置向导。通过 `GEOPRACTOR_API_TOKEN` 可开启 API 鉴权。

### Agent API

供其他程序接入（健康检查、Agent 对话、工具清单、缓存查询、命令执行、综合调研）：
协议见 [docs/api-protocol.md](docs/api-protocol.md)。

## 架构

```
用户问题
   │
   ▼
┌────────────┐   function calling   ┌──────────────────────────┐
│   LLM      │ ◄──────────────────── │  Agent 主循环             │
│（用户自配） │                      │  （工具注册表 + 会话记忆） │
└────────────┘                      └───────────┬──────────────┘
                                                │
                ┌───────────────────────────────┼───────────────────┐
                ▼                               ▼                   ▼
        ┌──────────────┐             ┌─────────────────┐   ┌────────────────┐
        │  连接器层     │             │  RAG 知识检索    │   │  课表/时间编排  │
        │  connectors/ │             │  app/rag/        │   │  app/course_…  │
        └──────────────┘             └─────────────────┘   └────────────────┘
```

- `app/`：配置、密钥加密、限速熔断、LLM 客户端、RAG、Agent、Web 服务、CLI
- `connectors/`：各信息源连接器（HTTP 公开渠道 + 会话型个人数据渠道 + 社区渠道），
  统一注册为 Agent 白名单工具
- 三形态（Web UI / CLI / Agent API）共用同一套核心代码

## 目录结构

```
.
├── app/                  # 核心代码（配置/密钥/LLM/RAG/Agent/Web/CLI）
├── connectors/           # 信息源连接器（官网/学院/门户/教务/社区）
├── tests/                # 单元测试
├── docs/                 # 用户文档（使用指南 / API 协议 / 部署指南 / 协议模板）
├── .env.example          # 环境变量占位模板
├── pyproject.toml        # 项目元数据与依赖
├── Dockerfile            # 容器化部署
└── LICENSE               # Apache-2.0
```

## 文档

| 文档 | 说明 |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | 操作指南与命令全表 |
| [docs/api-protocol.md](docs/api-protocol.md) | Agent API 联动协议 |
| [docs/tieba-service-guide.md](docs/tieba-service-guide.md) | 贴吧外部数据服务部署 |
| [docs/xhs-service-guide.md](docs/xhs-service-guide.md) | 小红书外部数据服务部署 |
| [docs/terms/disclaimer.md](docs/terms/disclaimer.md) | 免责声明 |
| [docs/terms/user-agreement.md](docs/terms/user-agreement.md) | 用户协议模板 |

## 许可证

[Apache-2.0](LICENSE)

## 免责声明

- 项目不预置任何 API 密钥或校园凭据；所有密钥/会话仅存用户本机，不入库、不落仓库。
- 校园数据仅访问"用户本人有权限"的接口；社区渠道内容来自第三方平台，仅供参考，请以官方信息为准。
- 本项目不提供任何绕过平台安全措施的能力，使用者须遵守所在学校与第三方平台的服务条款。
- 使用本项目产生的任何风险与责任由使用者自行承担。
