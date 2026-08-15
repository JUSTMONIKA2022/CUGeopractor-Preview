# 行至大地-CUGeopractor-Preview：中国地质大学(武汉)个人Agent

>当前版本为预览版，部分功能可能存在不确定性与不稳定性。使用前请阅读免责声明。<br>
>本项目将在未来会继续完善、优化和拓展功能。

*本地部署、用户自配密钥的开源校园 Agent：面向高校使用者，统一检索校园公开信息与个人数据，*
*支持 **Web UI / CLI / Agent API** 三种接入形态。*<br>
*__目前仅支持中国地质大学（武汉），欢迎在本项目基础上进行对其他高校的适配。__*

## 目录

- [这是什么？](#这是什么)
- [功能特性](#功能特性)
- [免责声明(使用前必看)](#免责声明使用前必看)
- [快速开始](#快速开始)
- [使用方式](#使用方式)
  - [CLI](#cli)
  - [Web UI](#web-ui)
  - [Agent API](#agent-api)
  - [知识库（RAG）](#知识库rag)
- [架构](#架构)
- [目录结构](#目录结构)
- [文档](#文档)
- [许可证](#许可证)

___

## 这是什么？

一款本地部署、用户自配的校园agent。支持通过web、cli和api三种方式进行调用。面向中国地质大学(武汉)学生开发。<br>
功能包括官方信息检索、网络信息检索、本地rag检索、信息门户信息检索、教务系统信息检索等部分。允许用户进行部分业务办理，例如:<ul><li>查询成绩</li><li>查询考试信息</li><li>查询课表</li><li>查询个人学籍</li><li>查询培养方案</li></ul>
支持 */命令* 与 __自然语言__ 调用，以便快速获取目标信息而非 __慢吞吞的打开信息门户→登录→搜索教务管理→进入教务系统→查询__ <br>
CUGeopractor 以 LLM 工具调用（function calling）为核心：由用户自配的 LLM 负责理解问题，
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
- **本地 RAG 知识库**
  - 把校历、办事指南、培养方案等长期稳定资料放进 `data/knowledge/`，
    一条命令建索引，对话时自动检索相关段落并附来源引用
  - 向量化默认使用内置 ONNX 小模型（免密钥、纯本地）；也可配置外部 OpenAI 兼容 embedding 服务
- **课表与时间编排**
  - 两套校区课表预设（南望山夏/冬自动切换、未来城），支持单节修改与重置
  - 下一节课倒计时、办公时间判断
- **CLI 命令系统**：缓存查询、实时查询、课表、综合调研、定时任务、知识库检索等 20+ 命令
- **Agent API**：HTTP 接口，供其他程序/软件接入
- **本地优先**：密钥本机加密存储，凭据与数据不入库、不落仓库

## 快速开始

```bash
# 1. 安装（Python 3.11+）
pip install -e ".[dev]"

# 2. 配置
#    复制 .env.example 为 .env，填写 LLM_BASE_URL / LLM_MODEL（自配 API Key）
#    或运行 `cugeopractor configure` 交互式配置

# 3. 启动
cugeopractor chat          # 交互式 CLI
cugeopractor serve         # Web UI（http://127.0.0.1:8080）
```

访问个人校园数据（门户/教务）前，先登录一次：

```bash
cugeopractor session-login
```

登录态持久保存，之后课表/成绩/考试等查询自动复用。

## 使用方式

### CLI

```bash
cugeopractor chat
```

聊天内可直接使用 `/` 命令（无需退出会话）：

| 类别 | 命令 |
|---|---|
| 实时查询 | `/live_news`、`/live_college`、`/live_zhihu`、`/live_bilibili`、`/live_tieba`、`/live_xhs` |
| 门户/教务 | `/live_catalog`、`/live_room`、`/live_course`、`/live_grade`、`/live_exam`、`/live_plan` 等 |
| 知识库 | `/index`（重建索引）、`/knowledge <关键词>`（直接检索） |
| 课表编排 | `/schedule`、`/next_course`、`/office_hours`、`/next` |
| 缓存 | `/cache_search`、`/cache_<渠道>` |
| 其他 | `/llm <问题>`、`/research <主题>`、`/cron`、`/configure`、`/login`、`/help` |

完整命令说明见 [docs/user-guide.md](docs/user-guide.md)。

### Web UI

```bash
cugeopractor serve
```

默认监听 `127.0.0.1:8080`，提供对话窗、配置向导与「重建知识库索引」按钮。通过 `CUGEOPRACTOR_API_TOKEN` 可开启 API 鉴权。

### Agent API

供其他程序接入（健康检查、Agent 对话、工具清单、缓存查询、命令执行、综合调研）：
协议见 [docs/api-protocol.md](docs/api-protocol.md)。

### 知识库（RAG）

本地检索增强（RAG）数据源，让 LLM 在对话中命中你自备的长期稳定资料：

```bash
# 1. 把资料放进知识库目录（支持 txt / md / pdf）
#    data/knowledge/，例如校历、办事指南、培养方案
#    示例见 docs/examples/knowledge/示例知识库.md

# 2. 重建索引（新增/修改/删除文档后都需要执行一次）
cugeopractor index

# 3. 对话时自动生效：直接问即可，如「校历第一周是什么时候」
cugeopractor chat

# 4. 查看索引状态
cugeopractor status
# 输出：知识库 : N 块（目录 data/knowledge）
```

- 索引为 0 块说明目录为空或尚未执行过 `cugeopractor index`；
- 向量化模式：默认内置 ONNX 小模型（免密钥、纯本地）；如需接入外部 embedding 服务，
  在 `.env` 配置 `CUGEOPRACTOR_EMBEDDING_BASE_URL / _API_KEY / _MODEL`（见 `.env.example` 注释）。

## 架构

```
用户问题
   │
   ▼
┌────────────┐   function calling   ┌──────────────────────────┐
│   LLM      │ ◄────────────────────│  Agent 主循环             │
│（用户自配） │                      │  （工具注册表 + 会话记忆） │
└────────────┘                      └───────────┬──────────────┘
                                                │
                ┌───────────────────────────────┼───────────────────┐
                ▼                               ▼                   ▼
        ┌──────────────┐             ┌─────────────────┐   ┌────────────────┐
        │  连接器层     │             │  RAG 知识检索   │   │  课表/时间编排  │
        │  connectors/ │             │  app/rag/       │   │  app/course_…  │
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
| [docs/user-guide.md](docs/user-guide.md) | 操作指南与命令全表（含知识库 RAG 章节） |
| [docs/api-protocol.md](docs/api-protocol.md) | Agent API 联动协议 |
| [docs/tieba-service-guide.md](docs/tieba-service-guide.md) | 贴吧外部数据服务部署 |
| [docs/xhs-service-guide.md](docs/xhs-service-guide.md) | 小红书外部数据服务部署 |
| [docs/terms/disclaimer.md](docs/terms/disclaimer.md) | 免责声明 |
| [docs/terms/user-agreement.md](docs/terms/user-agreement.md) | 用户协议模板 |
| [docs/examples/knowledge/示例知识库.md](docs/examples/knowledge/示例知识库.md) | 知识库使用示例 |

## 许可证

[Apache-2.0](LICENSE)

## 免责声明(使用前必看)

- **使用本项目默认已阅读该声明。**
- 项目不预置任何 API 密钥或校园凭据；所有密钥/会话仅存用户本机，不入库、不落仓库，__但仍然存在用户本机被其他恶意程序入侵导致信息泄露的风险__。
- 校园数据仅访问"用户本人有权限"的接口；社区渠道内容来自第三方平台，仅供参考，请以官方信息为准。
- 本项目不提供任何绕过平台安全措施的能力，使用者须遵守所在学校与第三方平台的服务条款。
- 使用本项目产生的任何风险与责任由使用者自行承担。
