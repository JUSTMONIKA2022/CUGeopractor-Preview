# 小红书数据服务：用户侧部署指南（BYO 模式）

> 本项目（行至大地·Geopractor）**不包含**任何第三方爬虫代码、签名生成或逆向实现。
> 小红书强反爬（x-s/x-t 签名 + 登录态 + 账号级风控），本项目通过"用户自配"方式接入：
> **由用户自行部署一个小红书数据服务**（任选其一，如开源的 [ReaJason/xhs](https://github.com/ReaJason/xhs)
> 或 [Xiaohongshutools](https://hub.openclaw.ai/chocomintx/skills/xiaohongshutools)），
> 项目仅作为通用 HTTP 客户端调用其 `/search` 接口。服务由用户自行部署、自行维护，风险自担。

## 一、总体结构

```
┌─────────────────────────┐        HTTP        ┌──────────────────────────────┐
│ Geopractor (本机 Agent)  │ ──── /search ────▶ │ 用户自部署的小红书数据服务      │
│ xhs_search 工具          │ ◀──── 约定 JSON ─── │  方案一：xhs 库 + Docker 签名 │
└─────────────────────────┘                    │  方案二：Xiaohongshutools      │
                                               └──────────────────────────────┘
```

- **数据服务**：用户自行部署（方案一或方案二），负责登录态/签名与小红书数据请求；
- **薄封装**：用户侧写的一个极简 HTTP 服务，暴露 `/search` 并按约定格式返回；
- **本项目**：只配置 `XHS_API_BASE`，以 HTTP GET 调用 `/search` 并解析约定 JSON——
  **对底层是 xhs 还是 Xiaohongshutools 无感知**（同一协议，代码零改动）。

## 二、部署步骤

### 1. 启动签名服务（Docker）

```bash
docker run -it -d -p 5005:5005 reajason/xhs-api:latest
```

> 说明：该服务基于 Playwright 模拟浏览器生成签名，启动时打印 `a1` 值；
> 建议把下面 Cookie 中的 `a1` 与该服务保持一致，避免签名错误。

### 2. 准备小号 Cookie

在你自己的浏览器登录小红书（**建议使用干净小号**，主号易触发账号级风控 `code=300011`），
F12 → Application → Cookies，复制 `a1`、`web_session`、`webId` 三个字段。

### 3. 方案一：xhs 库（ReaJason/xhs）

以下为**用户侧示例**，不属于本项目代码，请放在你自己的环境运行：

```python
# 用户侧薄封装示例：user_xhs_service.py（自行部署，风险自担）
from flask import Flask, request, jsonify
import requests
from xhs import XhsClient

app = Flask(__name__)

# 步骤 1 启动的签名服务地址
SIGN_SERVER = "http://127.0.0.1:5005"
# 步骤 2 获取的小号 Cookie（含 a1/web_session/webId）
COOKIE = "a1=...; web_session=...; webId=..."

def sign(uri, data=None, a1="", web_session=""):
    """把签名请求转发给 Docker 签名服务，返回 {x-s, x-t}"""
    resp = requests.post(f"{SIGN_SERVER}/sign",
                         json={"uri": uri, "data": data, "a1": a1, "web_session": web_session},
                         timeout=10)
    return resp.json()

@app.route("/search")
def search():
    """供 Geopractor 调用的数据接口（约定格式）"""
    keyword = request.args.get("keyword", "")
    count = int(request.args.get("count", 8))
    client = XhsClient(cookie=COOKIE, sign=sign)
    notes = client.get_note_by_keyword(keyword)  # xhs 库的笔记搜索方法
    items = []
    for note in notes[:count]:
        items.append({
            "title": note.title,
            "desc": note.desc,
            "url": f"https://www.xiaohongshu.com/explore/{note.note_id}",
        })
    return jsonify({"code": 0, "data": {"items": items}})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5100)
```

```bash
pip install xhs flask requests
python user_xhs_service.py   # 监听 127.0.0.1:5100
```

### 4. 方案二：Xiaohongshutools（RedCrack）

[Xiaohongshutools](https://hub.openclaw.ai/chocomintx/skills/xiaohongshutools) 是
基于 RedCrack 的小红书数据工具（自动处理 x-s/x-t 签名，支持游客模式与 `web_session`
登录态）。安装方式：

```bash
# 任选一种安装（OpenClaw CLI 或 npx）
openclaw skills install @chocomintx/xiaohongshutools
# 或
npx clawhub@latest install xiaohongshutools
```

安装后同样写一个极简 HTTP 薄封装（**用户侧示例**，不属于本项目代码），暴露同一套
`/search` 约定接口：

```python
# 用户侧薄封装示例：user_xhs_redfox_service.py（自行部署，风险自担）
import asyncio
from flask import Flask, request, jsonify
# 按 Xiaohongshutools 实际安装路径调整 import（ClawHub 工作区）
from request.web.xhs_session import create_xhs_session

app = Flask(__name__)
# 你的小号 web_session（游客模式可留空，但搜索能力受限）
WEB_SESSION = "你的小号 web_session 或留空"

async def _search(keyword, count):
    xhs = await create_xhs_session(proxy=None, web_session=WEB_SESSION)
    try:
        res = await xhs.apis.note.search_notes(keyword)
        data = await res.json()
        return data
    finally:
        await xhs.close_session()

@app.route("/search")
def search():
    """供 Geopractor 调用的数据接口（约定格式）"""
    keyword = request.args.get("keyword", "")
    count = int(request.args.get("count", 8))
    data = asyncio.run(_search(keyword, count))
    items = []
    for item in (data.get("data", {}).get("items", []) or [])[:count]:
        note_card = item.get("note_card", {}) or {}
        note_id = item.get("id", "") or note_card.get("note_id", "")
        items.append({
            "title": note_card.get("display_title", "") or note_card.get("desc", "")[:40],
            "desc": (note_card.get("desc", "") or "").replace("\n", " ").strip(),
            "url": f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else "",
        })
    return jsonify({"code": 0, "data": {"items": items}})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5100)
```

```bash
pip install flask aiohttp loguru pycryptodome getuseragent
python user_xhs_redfox_service.py   # 监听 127.0.0.1:5100
```

> 两种方案对 Geopractor 完全等价（同一 `/search` 约定协议），按需选择即可；
> 方案一依赖 Playwright 签名服务（较重），方案二为纯 Python（较轻但属逆向实现）。

### 5. 配置 Geopractor

在 `.env`（仅本机，不入库）中设置：

```ini
XHS_API_BASE=http://127.0.0.1:5100
```

配置后 `xhs_search` 工具即自动走外部服务模式；未配置时回退到
`XHS_COOKIE` 直连模式或给出配置指引。

## 三、接口约定（本项目侧解析规则）

| 项 | 约定 |
|---|---|
| 请求 | `GET {XHS_API_BASE}/search?keyword=关键词&count=条数` |
| 成功 | `{"code": 0, "data": {"items": [{"title","desc","url"}, ...]}}` |
| 失败 | `{"code": <非0>, "message": "错误说明"}` |

## 四、注意事项

1. **账号风控**：小号也可能被风控（`300011`/`461`），需低频使用（本项目已内置限速+熔断）；
2. **a1 一致性**：Cookie 中的 `a1` 应与签名服务一致，否则签名报错；
3. **服务隔离**：xhs 服务与签名服务均为**用户自行部署**，其行为与维护责任在用户侧；
4. **仅供公开数据检索**：不做批量抓取、不采集他人私密信息，遵守被访问平台服务条款。
