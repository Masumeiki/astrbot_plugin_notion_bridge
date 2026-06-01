# Masumeiki/astrbot_plugin_notion_bridge

> **从「只读同步」到「全量管理」——用 API 控制 Notion，就像操作本地文件一样自由**
>
> 递归读取、全文搜索、LLM 查询只是起点。现在你可以**创建页面、编辑内容、管理数据库、全局搜索、维护评论**，把你的 Notion 变成 AstrBot 的「外置大脑」。

---

## 📑 目录

1. [功能概览](#-功能概览)
2. [安装方法](#-安装方法)
3. [配置指南（必读）](#-配置指南必读)
4. [指令全集](#-指令全集)
5. [实战：本地 Markdown → Notion Wiki](#-实战本地-markdown--notion-wiki)
6. [技术要点与踩坑记录](#-技术要点与踩坑记录)
7. [LLM 工具](#-llm-工具)
8. [文件结构](#-文件结构)
9. [更新日志](#-更新日志)

---

## ✨ 功能概览

### 📖 读取（v1.0 已有）

| 功能 | 说明 |
|------|------|
| 🔄 **递归页面读取** | 自动读取页面下所有子页面（child_page + mention 引用） |
| 📥 **本地缓存** | 同步后缓存为 JSON，离线可查 |
| 🔍 **关键词搜索** | 在缓存内容中快速检索 |
| 🧠 **LLM 自动查询** | LLM Tool + 上下文注入双模式 |
| 📚 **知识库同步** | 写入 AstrBot 知识库，支持 RAG 检索 |
| 🌐 **Webhook 实时同步** | （可选）页面变更自动同步 |
| ⏰ **定时自动同步** | 按间隔自动检查更新 |

### ✍️ 写入（v2.0 新增）

| 功能 | 指令/工具 | 说明 |
|------|-----------|------|
| 📄 **创建页面** | `/notion page create` | 带内容、图标、markdown 正文 |
| ✏️ **更新页面** | `/notion page update` | 改标题、图标、回收站状态 |
| 🗑️ **删除页面** | `/notion page delete` | 移入回收站 |
| 📦 **追加内容** | `/notion block add` | 支持标题/列表/待办/引用/表格 |
| ✂️ **编辑块** | `/notion block edit` | 修改段落内容 |
| 🗑️ **删除块** | `/notion block delete` | 删除指定块 |
| 🗄️ **查询数据库** | `/notion db query` | 支持按关键词筛选 |
| 🗄️ **创建数据库** | `/notion db create` | 支持 11 种自定义字段类型 |
| 🔍 **全局搜索** | `/notion find` | 跨所有页面和数据库搜索 |
| 👥 **用户管理** | `/notion user list/me` | 列出用户、查看 Bot 信息 |
| 💬 **评论管理** | `/notion comment list/add` | 查看/添加页面评论 |
| 🤖 **LLM 自动管理** | 5 个 LLM Tool | AI 可自动创建/搜索/追加内容 |

---

## 📥 安装方法

### 方式一：插件市场

打开 AstrBot WebUI → **插件管理** → **插件市场** → 搜索 `notion_bridge` → 安装

### 方式二：手动安装

将插件目录放入 `data/plugins/` 下，在 WebUI 中重载插件即可。

---

## 🔥 配置指南（必读）

### 3.1 获取 Notion API Token

1. 打开 [https://www.notion.so/my-integrations](https://www.notion.so/my-integrations)
2. 点击 **"+ New integration"**
3. 填写名称（如 `AstrBot 助手`），选择工作空间
4. 点击 **Submit**，复制 **Internal Integration Secret**（`ntn_` 开头）
5. 填入插件配置的 `notion_token` 字段

> ❗ Token 只显示一次，丢失需重新创建。

### 3.2 获取根页面 ID

1. 打开根页面 → 右上角 **···** → **共享** → **发布**
2. 复制生成的共享链接
3. 从链接中提取 **32 位 hex**：

```
https://workspace.notion.site/页面标题-0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d
                                       └────────────────────────────────┘
                                       取最后一个 / 后的 32 位 hex，去掉连字符
```

### 3.3 授权集成

根页面右上角 **···** → **Add connections** → 选择你的集成 → Confirm

> ✅ 子页面自动继承权限。独立引用页面需单独授权。

---

## 🎯 指令全集

### 📄 页面管理

| 指令 | 功能 |
|------|------|
| `/notion page create <parent_id> <标题> [内容] [图标]` | 创建页面（内容支持 markdown） |
| `/notion page update <page_id> <标题>` | 更新页面标题/图标 |
| `/notion page delete <page_id>` | 删除页面 |

### 📦 内容管理

| 指令 | 功能 |
|------|------|
| `/notion block info <block_id>` | 获取块信息 |
| `/notion block add <page_id> <内容>` | 追加内容（自动识别标题/列表/待办等） |
| `/notion block edit <block_id> <内容>` | 编辑段落块 |
| `/notion block delete <block_id>` | 删除块 |

### 🗄️ 数据库管理

| 指令 | 功能 |
|------|------|
| `/notion db info <database_id>` | 查看数据库信息 |
| `/notion db query <database_id> [关键词]` | 查询数据库条目 |
| `/notion db create <parent_id> <标题> [fields]` | 创建数据库（fields 格式：`名称:title,价格:number`） |

### 🔍 搜索

| 指令 | 功能 |
|------|------|
| `/notion find <关键词>` | 全局搜索所有页面和数据库 |
| `/notion search <关键词>` | 在本地缓存中搜索 |

### 👤 用户

| 指令 | 功能 |
|------|------|
| `/notion user list` | 列出工作空间用户 |
| `/notion user me` | 查看当前 Bot 信息 |

### 💬 评论

| 指令 | 功能 |
|------|------|
| `/notion comment list <page_id>` | 查看页面评论 |
| `/notion comment add <page_id> <内容>` | 添加评论 |

### 🔄 同步（v1.0 已有）

| 指令 | 功能 |
|------|------|
| `/notion sync` | 手动同步 Notion → 本地缓存 |
| `/notion status` | 查看同步状态和页面目录树 |
| `/notion inject` | 查看注入 LLM 的设定上下文 |
| `/notion kb bind [名称]` | 绑定知识库 |
| `/notion kb sync` | 写入知识库 |
| `/notion kb list` | 列出知识库 |
| `/notion webhook create <URL>` | 创建 Webhook 订阅 |
| `/notion webhook list` | 查看订阅 |
| `/notion webhook delete <id>` | 删除订阅 |

---

## 🎯 实战：本地 Markdown → Notion Wiki

以下是将本地 `.md` 设定文件迁移到 Notion 并 Wiki 化的完整流程，同时也是本插件的典型使用场景。

### 场景

你有 17 个世界构建文档存放在本地目录：

```
设定/
├── 世界底层/  世界名称.md · 世界年龄.md · 世界气候.md · 魔力潮汐.md · 魔网.md · 元素生命.md
├── 地理/      世界地理总览.md · 东大陆.md · 西大陆.md · 中央群岛.md
├── 魔法体系/  魔力潮汐.md · 魔网.md · 施法原理.md · 元素生命.md
├── 思路打开点/ 世界地理与气候.md · 世界本源与魔网.md · 施法原理初探.md
└── _目录.md   完整索引
```

### 第一步：上传分类页

```bash
# 在根页面下创建分类页面，带上 emoji 图标
/notion page create <根页面ID> "世界底层" "" "🌍"
/notion page create <根页面ID> "地理" "" "🗺️"
/notion page create <根页面ID> "魔法体系" "" "🔮"
# 每个分类页都带上描述和分隔线
```

### 第二步：上传内容页

```bash
# 把每个 .md 文件作为子页面写入对应分类
/notion page create <世界底层ID> "世界名称" "成文内容…" "🏷️"
/notion page create <世界底层ID> "魔力潮汐" "## 阶段\n\n| 阶段 | 表现 |\n|------|------|\n| 涨潮 | 魔法能量充盈 |\n| 退潮 | 魔法能量稀薄 |" "🌊"
```

### 第三步：Wiki 化（添加内部链接）

使用 `/notion block add` 或 LLM 工具，在文本中引用其他页面时添加 Notion 内部链接。例如，在「元素生命」页面中追加：

```
随着魔网形成，世间渐渐充盈了魔力。在魔网的节点处，魔力凝聚出第一批意识——元素生命诞生了。
```

→ 点击「魔网」跳转到魔网页面，点击「元素生命」跳转到当前页面。

### 第四步：重建主页索引

在设定集主页添加「精彩设定速览」「目录速览」「按分类浏览」等区域，所有分类名、页面名都带上 Notion 内部链接。

### 最终效果

```
咕咕鱼的设定集主页
├─ 精彩设定速览        ← 点击「紫金币」「魔网」直接跳转
├─ 目录速览             ← callout 中每个页面名都可点击
├─ 按分类浏览
├─ 📄 世界底层          ← 子页面，包含6个内容页
├─ 📄 地理              ← 子页面，包含4个内容页
├─ 📄 魔法体系          ← 子页面，包含5个内容页
├─ 📄 思路打开点        ← 子页面，包含3个内容页
├─ 📄 设定目录          ← 完整目录表格，含内部链接
└─ 📄 全页面            ← 所有未来新建页面的容器
```

每个蓝色文字都是可点击的内部链接，形成完整的 Wiki 网络。

---

## 💡 技术要点与踩坑记录

> 以下全部来自实战经验，记录了从 Notion API 集成到批量操作的全链路技术细节。

### 📌 1. Notion API 速率限制：3 req/s

**现象**：批量操作（如上传 17 个页面）时，前几个成功，后面开始返回 429 或超时。
**原因**：Notion API 限制每个集成**每秒最多 3 次请求**（滚动窗口）。
**解决**：插件内置滑动窗口限流器 `RateLimiter(max_calls=3, period=1.0)`，所有 API 调用自动排队。

**实现原理：**
```python
class RateLimiter:
    """滑动窗口：记录过去 1 秒内的请求时间戳，超过 3 个则等待"""
    async def acquire(self):
        now = time.monotonic()
        self.timestamps = [t for t in self.timestamps if now - t < 1.0]
        if len(self.timestamps) >= 3:
            await asyncio.sleep(1.0 - (now - self.timestamps[0]))
        self.timestamps.append(now)
```

**批量操作耗时估算：**
```
1 个页面重建 ≈ 25 次 API 调用
15 个页面 × 25 = 375 次
375 次 ÷ 3 req/s ≈ 125 秒  ← 这是纯 API 耗时，加上网络延迟更多
```

### 📌 2. Notion 页面 ID 的坑

**现象**：点击链接打开浏览器而不是 Notion 内部跳转。
**原因**：用了 `slug 路径` 格式 `https://www.notion.so/地理/世界地理总览.md` 而不是 `page_id` 格式。

**正确做法：**
```
❌ https://www.notion.so/地理/世界地理总览.md        ← slug格式，外部链接
✅ https://www.notion.so/370fc1761e4481cca53dcdfac536743b  ← page_id格式，内部跳转
```

**page_id 必须是完整的 32 位 hex（UUID 去掉连字符）**，少一位都不行。

**获取正确 page_id 的方法：**
```python
# 用 Notion Search API
resp = await client.post("https://api.notion.com/v1/search", json={
    "query": "页面标题",
    "filter": {"value": "page", "property": "object"}
})
page_id = resp.json()["results"][0]["id"].replace("-", "")
```

### 📌 3. Markdown 格式不会自动渲染

**现象**：使用插件上传 markdown 内容后，`**粗体**` `|表格|` `[链接](url)` 在 Notion 中显示为纯文本。

**原因**：Notion API 的 rich_text 不会自动解析 markdown 标记。每个格式都需要用**原生对象**表达：

```python
# ❌ markdown 字符串，不会被渲染
"**角色潜力**：资源封锁线"

# ✅ Notion 原生 rich_text 格式
[
    {"type": "text", "text": {"content": "角色潜力"}, "annotations": {"bold": True}},
    {"type": "text", "text": {"content": "：资源封锁线"}}
]
```

**解决方案：**
- **粗体** → `annotations: {"bold": true}`
- **链接** → `text.link: {"url": "https://..."}`
- **表格** → `table` + `table_row` block 类型（需在创建时指定，不能从 paragraph 转换）
- **数字列表** → `numbered_list_item` block 类型

### 📌 4. Block 类型创建后不可变更

**现象**：想将一段以 `|` 分隔的文本改为 Notion 原生表格，发现改不了。

**原因**：Notion API 中 `PATCH /v1/blocks/{id}` **只能修改**块内的 `rich_text` 内容，**不能改变**块类型（paragraph → table）。

**解决方案**：需要删除旧块（`DELETE /v1/blocks/{id}`），再追加新块（`PATCH /v1/blocks/{parent_id}/children`）。

### 📌 5. PATCH 修改链接需深拷贝

**现象**：批量修复链接 URL 时，检测不到变化，修复不生效。

**原因**：Python 中 `dict` 是引用类型，原地修改后用于比较的两个列表指向相同对象：
```python
# ❌ 原地修改，比较永远相等
new_rt = rt  # 相同引用
for t in new_rt:
    t["text"]["link"]["url"] = new_url  # rt 也被改了！
new_rt == rt  # → True，因为指向同一组对象
```

**解决方案**：使用 `copy.deepcopy()` 创建独立副本：
```python
import copy
new_rt = copy.deepcopy(rt)  # 独立副本
```

### 📌 6. HTTP Client 复用

**现象**：频繁创建 `httpx.AsyncClient()` 导致连接开销大。

**解决方案**：在插件 `__init__` 中创建一次共享 Client，`terminate` 时关闭：
```python
self._http = httpx.AsyncClient(timeout=60)  # 共享
# ...
async def terminate(self):
    await self._http.aclose()
```

### 📌 7. 页面链接的两种形式：Mention 与 Child Page

Notion 中一个页面出现在另一个页面下有**两种方式**：

| 方式 | API 块类型 | 特点 |
|------|-----------|------|
| **子页面** | `child_page` | 在父页面中自动出现，继承权限 |
| **提及链接** | `mention → page` | 独立页面，需单独授权，通过 `@` 引用 |

插件 v2.0 支持两种形式的递归读取。如果使用 mention 引用，务必在目标页面**单独添加集成权限**。

### 📌 8. 定时同步与 Webhook 的选择

| 方式 | 优点 | 缺点 |
|------|------|------|
| **定时轮询**（默认） | 无需公网，零配置 | 最长 30 分钟延迟 |
| **Webhook** | 实时同步 | 需要公网 HTTPS |

如果没有公网 HTTPS，推荐使用 **Cloudflare Tunnel**（免费）或直接使用定时轮询。

---

## 🤖 LLM 工具

插件注册了以下 LLM 工具，AI 可自动调用：

| 工具名 | 功能 |
|--------|------|
| `query_notion_world_setting` | 查询设定集中的内容（按关键词） |
| `notion_create_page` | 在 Notion 中创建新页面 |
| `notion_query_database` | 查询 Notion 数据库条目 |
| `notion_search` | 全局搜索 Notion 页面和数据库 |
| `notion_append_content` | 向已有页面追加内容 |

配合上下文注入（`auto_inject_context`），AI 可在对话时自动参考设定内容。

---

## 📁 文件结构

```
astrbot_plugin_notion_bridge/
├── metadata.yaml          # 插件元数据
├── main.py                # 插件主代码（~1670 行）
├── _conf_schema.json      # WebUI 配置面板
├── requirements.txt       # 依赖：httpx
└── README.md              # 本文件

# 运行时缓存：
data/notion_bridge_cache/
├── notion_content.json    # 同步后的完整缓存
└── notion_meta.json       # 同步元信息
```

---

## 📝 更新日志

### v2.0.1 (2026-06-01) Agent Loop 修复与稳定性改进

- 🐛 **修复 LLM 工具调用后 Agent Loop 提前结束**：所有 Notion 工具在 `yield event.plain_result()` 后增加 `return` 值，避免框架检测到 `resp is None` 后结束 Agent Loop，LLM 现在能连续调用多个工具
- 🐛 **修复 `notion_search` 工具死代码导致 NameError**：`return` 后不可达代码导致 `summary` 变量未定义，搜索有结果时报错
- 🐛 **修复 `notion_query_database` 工具的 yield 缺失与重复行**：查询结果未通过 `yield event.plain_result()` 发送给用户，且标题行重复拼接
- 🐛 **限制 `query_notion_world_setting` 输出长度**：加 MAX_OUTPUT=2500 限制，防止搜索结果撑爆上下文触发压缩
- 🐛 **修复 `notion_create_page` / `notion_append_content` 无返回值**：Agent Loop 二阶段任务（如先查后建）可正常继续

### v2.0.0 (2026-05-30) 文档管理增强版

- ✨ **增删改查全套 API**：Pages（create/update/delete）、Blocks（info/add/edit/delete）、Databases（info/query/create/update）
- ✨ **全局搜索**：`/notion find` 搜索所有页面和数据库
- ✨ **用户管理**：`/notion user list/me`
- ✨ **评论管理**：`/notion comment list/add`
- ✨ **4 个 LLM 工具**：`notion_create_page` / `notion_query_database` / `notion_search` / `notion_append_content`
- ✨ **速率限制器**：`RateLimiter` 滑动窗口，严格遵循 3 req/s
- 🔧 **共享 HTTP Client**：连接复用，`terminate` 时优雅关闭
- 🔧 **UUID 校验**：命令层校验页面 ID 格式，提前拦截无效输入
- 🔧 **数字列表支持**：`1. xxx` / `1) xxx` 自动识别为 `numbered_list_item`
- 🔧 **数据库自定义字段**：11 种字段类型（title/rich_text/number/select/date/checkbox 等）
- 🔧 **超时从 30s 提升到 60s**：给 Notion API 更充裕的响应时间

### v1.1.0 (2026-05-29) 更名为 Notion Bridge

- ✨ 知识库同步、Webhook 实时同步、mention 引用支持
- 🐛 修复指令冲突、换行符问题
- 📝 完善配置指引，总结常见问题

### v1.0 (2026-05-29)

- ✨ 初始版本：递归读取、缓存、搜索、LLM 查询、定时同步
