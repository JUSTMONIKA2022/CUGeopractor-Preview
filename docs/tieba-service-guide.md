# 贴吧数据服务：用户侧部署指南（BYO 模式）

> 本项目（行至大地·CUGeopractor）**不包含**任何第三方爬虫代码、签名生成或逆向实现。
> 贴吧网页版为 CSR 单页应用 + 动态风控（「百度安全验证」JS 挑战），纯 HTTP / Playwright
> 渲染均可能被拦。本项目通过"用户自配"方式接入：
> **由用户自行部署一个贴吧数据服务**（推荐开源项目 [Dilettante258/Tieba-API-SCF](https://github.com/Dilettante258/Tieba-API-SCF)，
> Hono + tieba.js，支持 Docker / Node / Cloudflare Worker），
> 项目仅作为通用 HTTP 客户端调用其 `/forum/thread`（最新帖列表）与
> `/forum/search`（吧内关键词搜索，SSE 流式）接口。服务由用户自行部署、自行维护，风险自担。

## 一、总体结构

```
┌─────────────────────────┐        HTTP        ┌──────────────────────────────┐
│ CUGeopractor (本机 Agent)  │ ── /forum/thread ─▶│ 用户自部署的贴吧数据服务       │
│ tieba_search 工具        │ ── /forum/search ─▶│  Tieba-API-SCF（内置 BDUSS）  │
│                         │ ◀──── 约定 JSON ────│                              │
│                         │ ◀── SSE 流式结果 ───│                              │
└─────────────────────────┘                    └──────────────────────────────┘
```

- **数据服务**：用户自行部署 Tieba-API-SCF，负责 BDUSS 登录态与贴吧客户端协议请求；
- **本项目**：只配置 `TIEBA_API_BASE`，以 HTTP GET 调用 `/forum/thread`（最新帖列表）
  与 `/forum/search`（吧内关键词搜索）并解析约定返回——**对底层运行时
  （Docker/Node/Worker）无感知**，代码零改动。

## 二、部署步骤（三选一）

### 1. 准备 BDUSS（登录态凭据）

Tieba-API-SCF 需要贴吧登录态 `BDUSS` 才能走客户端协议取数。获取方式：

- 浏览器登录贴吧后，F12 → Application → Cookies → 找到 `BDUSS` 字段复制（推荐，凭据仅存本机）；
- 或使用项目 README 提供的在线工具：https://bduss.nest.moe/ 。

> ⚠️ BDUSS 等同你贴吧账号的登录凭证，**只存本机环境变量，绝不提交仓库或对外分享**。

### 2. 方式一：Docker 直接运行（推荐，一行启动）

```bash
# 国内阿里云镜像源（-p 宿主机端口:容器端口；端口可自行改，避开被占用端口）
docker run --rm -d -p 8916:8916 -e BDUSS=你的BDUSS -e PORT=8916 \
  crpi-visd77fbydeujidg.cn-hangzhou.personal.cr.aliyuncs.com/dilettante/tieba-api:latest

# 或国外 GHCR 镜像源
docker run --rm -d -p 8916:8916 -e BDUSS=你的BDUSS -e PORT=8916 \
  ghcr.io/dilettante258/tieba-api-scf:latest
```

启动后默认地址 `http://localhost:8916`。也可从仓库源码本地构建：

```bash
git clone https://github.com/Dilettante258/Tieba-API-SCF.git
docker build -t tieba-api:node .
docker run --rm -d -p 8916:8916 -e BDUSS=你的BDUSS -e PORT=8916 tieba-api:node
```

> 端口说明：服务默认监听 `8000`，通过环境变量 `PORT` 自定义（本机统一使用 **8916**，
> 避开 8000/8080 等热门端口降低冲突概率）；Docker 部署需同时改 `-p` 映射与 `-e PORT`。

### 3. 方式二：Node 直接运行

从仓库 [Releases](https://github.com/Dilettante258/Tieba-API-SCF/releases) 下载
`api-node-<sha>.tar.gz`：

```bash
mkdir -p api-node && tar -xzf api-node-<sha>.tar.gz -C api-node
BDUSS=你的BDUSS node ./api-node/index.js
```

> ⚠️ **Node 版本要求 ≥ 20**：该服务打包的 undici 依赖全局 `File` 对象（Node 20+ 才有），
> 系统 Node 18 会启动报错 `ReferenceError: File is not defined`。
> 不想升级系统 Node 时，可用 `npx` 拉取临时 Node 20 运行（本机已验证）：
>
> ```powershell
> $env:BDUSS="你的BDUSS"
> $env:npm_config_registry="https://registry.npmmirror.com"   # 国内加速
> $env:npm_config_cache="<项目内缓存目录>"                    # 避免沙箱/权限限制 npm 缓存
> npx -y node@20 ./index.js
> ```
>
> **本机已提供一键启动脚本**：`tools/tieba-api/start-tieba.ps1`（该目录已被
> `.gitignore` 排除，不入库）。用法 `powershell -ExecutionPolicy Bypass -File .\start-tieba.ps1`；
> BDUSS 按「环境变量 → 本目录 `.bduss` 文件 → 交互输入」三级加载（首次输入自动落盘
> `.bduss`，之后免粘贴）；端口默认 8000，**可通过环境变量 `TIEBA_PORT` 改端口**
> （本机统一 `TIEBA_PORT=8916`），已被占用时自动提示"已在运行"不重复启动。
> **修复**：① 去除文件头多重 BOM（此前 PowerShell 5 会把首行注释当命令报错，
> 导致"# 不是命令"启动失败）；② 增加 npx 路径探测（PATH 找不到时退回
> `C:\Program Files\nodejs\npx.cmd` 等常见目录，解决"找不到 npx"）；③ 端口参数化
> （`TIEBA_PORT` 覆盖默认 8000，检查/启动/提示一致）。

### 4. 方式三：Cloudflare Worker

仓库 `wrangler.jsonc` 已含 Worker 部署配置，按仓库说明设置 `BDUSS` 环境变量后
`wrangler deploy` 即可；无服务器、免运维，但受 Worker 请求配额限制。

## 三、配置 CUGeopractor

在 `.env`（仅本机，不入库）中设置：

```ini
# 自部署服务地址（端口须与 start-tieba.ps1 的 TIEBA_PORT 一致；本机 8916）
TIEBA_API_BASE=http://127.0.0.1:8916
```

> 也可先用公开实例 `https://cf.eztb.org` 体验（第三方维护，仅建议测试用）。
> 配置后 `tieba_search` 自动走外部服务模式；未配置或调用失败时降级到
> HTTP 抓取 → Playwright 渲染 链路（可能被反爬拦截）。

### 验证

```bash
# 应返回 200 且 threadList 非空
curl "http://127.0.0.1:8916/forum/thread?fname=中国地质大学武汉&page=1&rn=5"
```

## 四、接口约定（本项目侧解析规则）

### 4.1 最新帖列表 `/forum/thread`（列表兜底）

| 项 | 约定 |
|---|---|
| 请求 | `GET {TIEBA_API_BASE}/forum/thread?fname=<贴吧名，不带"吧"后缀>&page=1&rn=<条数>` |
| 成功 | `{"forum": {...}, "threadList": [{"tid": "帖子ID", "title": "标题", ...}, ...]}` |
| 失败 | HTTP 非 200 / 网络异常 → 本项目返回空并自动降级，不中断其他渠道 |

本项目只读取 `threadList` 中的 `tid` 与 `title`，拼成
`https://tieba.baidu.com/p/{tid}` 干净链接（来源引用用），其余字段忽略。

### 4.2 吧内关键词搜索 `/forum/search`（首选）

> 背景：`/forum/thread` 只返回**最新帖列表**，配合本地标题过滤只能命中近期帖子，
> 搜不到历史帖（用户手动在贴吧搜索为全库搜索）。Tieba-API-SCF v3 提供
> `/forum/search`：服务端内置 BDUSS 按关键词扫描吧内帖子（count 张）并以 **SSE**
> 流式回传命中结果——本项目 `tieba_search` 现**首选**该接口，失败再降级列表过滤。

| 项 | 约定 |
|---|---|
| 请求 | `GET {TIEBA_API_BASE}/forum/search?fname=<贴吧名>&keywords=<关键词>&count=100&depth=first&sort=1` |
| 返回 | `text/event-stream`，事件逐行 `data: <json>`：`{"type":"threads","count":N}` 起始 → 若干 `{"type":"progress"}` → `{"type":"match","posts":[{"tid","threadTitle","content",...}]}` 命中 → `{"type":"done","stats":{...}}` 结束 |
| 失败 | HTTP 非 200 / 超时（本项目给 120s）→ 返回空并降级到 4.1 列表 + 网页抓取 |

本项目解析规则：收集所有 `match` 事件的 `posts`，按 `tid` 去重；标题取 `threadTitle`，
为空时退回 `content` 首行；拼成 `https://tieba.baidu.com/p/{tid}` 干净链接。

## 五、注意事项与法律风险

1. **风险分层**：本项目代码零签名、零逆向、零绕过（低风险）；用户自部署服务的
   行为与维护责任在用户侧（中风险，自担）；公开实例 `cf.eztb.org` 为第三方维护，
   凭据/行为不可控（中风险，仅测试用）。
2. **许可证提醒**：Tieba-API-SCF 仓库**未标注开源许可证**（无 LICENSE 文件），
   代码公开但版权默认归作者——**仅限自用部署，不得再分发/内置到本项目仓库**。
3. **BDUSS 敏感**：等同账号登录态，只存本机环境变量；服务端口建议只监听
   `127.0.0.1`，勿暴露公网。
4. **仅查公开帖子**：本项目只检索"中国地质大学武汉吧"等公开帖子标题，不批量抓取、
   不采集他人私密信息，遵守被访问平台服务条款。
5. **低频使用**：本项目已内置限速+熔断，但请勿高频调用或扩大抓取范围，以免影响
   目标服务与自身账号。
