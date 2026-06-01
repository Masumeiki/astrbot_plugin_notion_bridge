"""
AstrBot Notion 设定集同步插件 v2.0
===================================
递归读取 Notion 页面树（含所有子页面），缓存到本地，
支持关键词搜索、LLM 自动查询、Notion Webhook 实时推送。
"""

import hashlib
import hmac
import json
import os
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"

TEXT_BLOCK_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do",
    "toggle", "quote", "callout", "code", "equation",
}

BLOCK_LABELS = {
    "heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
    "bulleted_list_item": "• ", "numbered_list_item": "1. ",
    "to_do": "[ ] ", "quote": "> ", "callout": "📌 ",
    "code": "```\n",
}


class RateLimiter:
    """滑动窗口速率限制器 —— Notion API 限制 3 req/s"""
    def __init__(self, max_calls: int = 3, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self.timestamps = [t for t in self.timestamps if now - t < self.period]
            if len(self.timestamps) >= self.max_calls:
                wait = self.period - (now - self.timestamps[0])
                if wait > 0:
                    await asyncio.sleep(wait)
                now = time.monotonic()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
            self.timestamps.append(now)


class NotionSyncPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.notion_token = config.get("notion_token", "")
        self.root_page_id = config.get("root_page_id", "").replace("-", "")
        self.sync_interval = config.get("sync_interval_minutes", 10)
        self.auto_inject = config.get("auto_inject_context", True)
        self.webhook_verification_token = config.get("webhook_verification_token", "")
        # 知识库同步配置
        self.sync_to_kb = config.get("sync_to_kb", False)
        self.kb_name = config.get("kb_name", "咕咕鱼的设定集")
        self.kb_chunk_size = config.get("kb_chunk_size", 512)
        self._last_kb_doc_id: str | None = None  # 跟踪上次写入的文档ID

        self.cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "data",
            "notion_bridge_cache"
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "notion_content.json")
        self.meta_file = os.path.join(self.cache_dir, "notion_meta.json")

        self.cached_content: dict[str, str] = {}
        self.page_tree: list[dict] = []
        self.last_sync_time: Optional[str] = None
        self._sync_lock = asyncio.Lock()
        self._webhook_subscription_id: Optional[str] = None
        self._http = httpx.AsyncClient(timeout=60)
        self._rate_limiter = RateLimiter()

        self._register_webhook_api()

        if self.notion_token and self.root_page_id:
            asyncio.create_task(self._auto_sync_loop())

    # ── Webhook API 端点 ──

    def _register_webhook_api(self):
        try:
            self.context.register_web_api(
                "/notion/webhook", self._handle_webhook, ["POST"],
                "接收 Notion Webhook 事件推送"
            )
            logger.info("✅ Notion Webhook 端点已注册: POST /notion/webhook")
        except Exception as e:
            logger.warning(f"注册 Webhook 端点失败: {e}")

    async def _handle_webhook(self):
        from astrbot.dashboard.server import request
        body_bytes = await request.get_data()
        body_str = body_bytes.decode("utf-8")
        headers = dict(request.headers)
        logger.debug(f"📩 收到 Notion Webhook: {body_str[:200]}...")

        try:
            payload = json.loads(body_str)
        except json.JSONDecodeError:
            return {"ok": False, "error": "无效的 JSON"}, 400

        # 验证订阅请求
        if payload.get("type") == "verification":
            challenge = payload.get("challenge")
            if challenge:
                logger.info("✅ Notion Webhook 订阅验证通过！")
                return {"challenge": challenge}, 200
            return {"ok": False}, 400

        # 签名验证
        if self.webhook_verification_token:
            signature = headers.get("X-Notion-Signature", "")
            if not self._verify_signature(body_str, signature):
                logger.warning("⚠️ Webhook 签名验证失败")
                return {"ok": False, "error": "签名验证失败"}, 401

        event_type = payload.get("type", "")
        logger.info(f"📢 Notion 事件: {event_type}")

        if event_type in ("page.content_updated", "page.updated", "page.created"):
            entity = payload.get("entity", {})
            page_id = entity.get("id", "").replace("-", "")
            logger.info(f"🔄 检测到页面更新 [{page_id}]，自动同步中...")
            asyncio.create_task(self._webhook_triggered_sync(page_id))
            return {"ok": True, "message": "已触发同步"}, 200

        if event_type == "page.deleted":
            logger.info("🗑️ 页面被删除，触发同步")
            asyncio.create_task(self._webhook_triggered_sync(""))
            return {"ok": True}, 200

        return {"ok": True}, 200

    def _verify_signature(self, body: str, signature_header: str) -> bool:
        if not signature_header.startswith("sha256="):
            return False
        received_sig = signature_header.split("sha256=")[-1]
        expected_sig = hmac.new(
            self.webhook_verification_token.encode("utf-8"),
            body.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(received_sig, expected_sig)

    async def _webhook_triggered_sync(self, changed_page_id: str):
        await asyncio.sleep(2)
        result = await self.sync_all()
        if result.get("ok"):
            logger.info(f"✅ Webhook 触发同步完成")
            await self._notify_admin(f"📢 Notion 设定集已自动更新（共 {result['page_count']} 个页面）")
        else:
            logger.warning(f"⚠️ Webhook 触发同步失败: {result.get('error')}")

    async def _notify_admin(self, message: str):
        try:
            umo = self.config.get("admin_notify_umo", "")
            if umo:
                from astrbot.api.message_components import Plain
                from astrbot.api.event import MessageChain
                chain = MessageChain().message(message)
                await self.context.send_message(umo, chain)
        except Exception:
            pass

    # ── Webhook 订阅管理 ──

    async def _create_webhook_subscription(self, webhook_url: str) -> dict:
        resp = await self._http.post(
            f"{NOTION_API}/webhooks", headers=self._headers(),
            json={
                "url": webhook_url,
                "events": [
                    "page.content_updated", "page.updated",
                    "page.created", "page.deleted",
                ],
            },
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            self._webhook_subscription_id = data.get("id", "")
            vt = data.get("verification_token", "")
            if vt:
                self.webhook_verification_token = vt
                self.config["webhook_verification_token"] = vt
                self.config.save_config()
            return {"ok": True, "data": data}
        return {"ok": False, "error": resp.text}

    async def _list_webhook_subscriptions(self) -> list[dict]:
        resp = await self._http.get(f"{NOTION_API}/webhooks", headers=self._headers())
        if resp.status_code == 200:
            return resp.json().get("results", [])
        return []

    async def _delete_webhook_subscription(self, subscription_id: str) -> bool:
        resp = await self._http.delete(
            f"{NOTION_API}/webhooks/{subscription_id}", headers=self._headers()
        )
        return resp.status_code in (200, 204)

    # ── Notion API 请求 ──

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str) -> dict:
        await self._rate_limiter.acquire()
        resp = await self._http.get(f"{NOTION_API}{path}", headers=self._headers())
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"Notion API 请求失败 [{resp.status_code}]: {resp.text[:200]}")
        return {}

    async def _get_page_info(self, page_id: str) -> dict:
        return await self._get(f"/pages/{page_id}")

    async def _get_block_children(self, block_id: str, cursor: str = None) -> tuple[list[dict], Optional[str]]:
        params = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            params += f"&start_cursor={cursor}"
        data = await self._get(params)
        return data.get("results", []), data.get("next_cursor")

    # ── HTTP 辅助方法 ──

    async def _post(self, path: str, json_data: dict = None) -> dict:
        await self._rate_limiter.acquire()
        resp = await self._http.post(f"{NOTION_API}{path}", headers=self._headers(), json=json_data or {})
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error(f"Notion POST 失败 [{resp.status_code}]: {resp.text[:300]}")
        return {"ok": False, "error": resp.text, "status": resp.status_code}

    async def _patch(self, path: str, json_data: dict = None) -> dict:
        await self._rate_limiter.acquire()
        resp = await self._http.patch(f"{NOTION_API}{path}", headers=self._headers(), json=json_data or {})
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error(f"Notion PATCH 失败 [{resp.status_code}]: {resp.text[:300]}")
        return {"ok": False, "error": resp.text, "status": resp.status_code}

    async def _delete_req(self, path: str) -> dict:
        await self._rate_limiter.acquire()
        resp = await self._http.delete(f"{NOTION_API}{path}", headers=self._headers())
        if resp.status_code in (200, 204):
            return resp.json() if resp.content else {"ok": True}
        logger.error(f"Notion DELETE 失败 [{resp.status_code}]: {resp.text[:300]}")
        return {"ok": False, "error": resp.text, "status": resp.status_code}

    # ── 文本提取 ──

    def _extract_rich_text(self, rich_text: list) -> str:
        return "".join(t.get("plain_text", "") for t in rich_text)

    def _extract_block_text(self, block: dict) -> str:
        block_type = block.get("type", "")
        if block_type not in TEXT_BLOCK_TYPES:
            return ""
        bdata = block.get(block_type, {})
        rt = bdata.get("rich_text", [])
        if block_type == "code":
            lang = bdata.get("language", "")
            text = self._extract_rich_text(rt)
            return f"```{lang}\n{text}\n```\n"
        text = self._extract_rich_text(rt)
        prefix = BLOCK_LABELS.get(block_type, "")
        return f"{prefix}{text}\n"

    # ── 递归读取页面树（支持 child_page + mention 引用）──

    async def _read_page_recursive(
        self, page_id: str, depth: int = 0, visited: set = None
    ) -> tuple[str, list[dict]]:
        if visited is None:
            visited = set()
        if page_id in visited:
            return "", []
        visited.add(page_id)

        indent = "  " * depth
        md_parts = []
        tree_node = {"id": page_id, "title": "", "url": "",
                     "children": [], "last_edited": ""}
        all_nodes = [tree_node]

        page_info = await self._get_page_info(page_id)
        if not page_info:
            return "", []

        title = ""
        for pn, pd in page_info.get("properties", {}).items():
            if pd.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in pd.get("title", []))
                break

        tree_node["title"] = title
        tree_node["url"] = page_info.get("url", "")
        tree_node["last_edited"] = page_info.get("last_edited_time", "")

        md_parts.append(f"\n{'#' * min(3, depth + 1)} {title}\n")
        md_parts.append(f"> 页面ID: `{page_id}`\n")
        if page_info.get("url"):
            md_parts.append(f"> 链接: {page_info['url']}\n")
        md_parts.append("\n")

        cursor = None
        while True:
            blocks, cursor = await self._get_block_children(page_id, cursor)
            if not blocks:
                break

            for block in blocks:
                btype = block.get("type", "")

                # ── child_page 块（Notion 内嵌子页面）──
                if btype == "child_page":
                    cid = block["id"].replace("-", "")
                    ctitle = block.get("child_page", {}).get("title", "未命名")
                    md_parts.append(f"\n{indent}---\n")
                    md_parts.append(f"{indent}**子页面: {ctitle}**\n\n")
                    child_md, child_nodes = await self._read_page_recursive(
                        cid, depth + 1, visited
                    )
                    md_parts.append(child_md)
                    tree_node["children"].append(
                        child_nodes[0] if child_nodes else {"id": cid, "title": ctitle, "url": "", "children": [], "last_edited": ""}
                    )
                    all_nodes.extend(child_nodes)
                    continue

                # ── 提取 rich_text 中的 mention 页面引用 ──
                bdata = block.get(btype, {})
                rt_list = bdata.get("rich_text", [])
                for rt in rt_list:
                    if rt.get("type") == "mention":
                        ment = rt.get("mention", {})
                        if ment.get("type") == "page":
                            mid = ment["page"]["id"].replace("-", "")
                            if mid not in visited:
                                minfo = await self._get_page_info(mid)
                                mtitle = "未命名"
                                if minfo:
                                    for pn2, pd2 in minfo.get("properties", {}).items():
                                        if pd2.get("type") == "title":
                                            mtitle = "".join(
                                                t.get("plain_text", "") for t in pd2.get("title", [])
                                            )
                                            break
                                md_parts.append(f"\n{indent}---\n")
                                md_parts.append(f"{indent}**引用页面: {mtitle}**\n\n")
                                child_md, child_nodes = await self._read_page_recursive(
                                    mid, depth + 1, visited
                                )
                                md_parts.append(child_md)
                                tree_node["children"].append(
                                    child_nodes[0] if child_nodes else {"id": mid, "title": mtitle, "url": "", "children": [], "last_edited": ""}
                                )
                                all_nodes.extend(child_nodes)

                # ── 文本内容 ──
                text = self._extract_block_text(block)
                if text:
                    md_parts.append(f"{indent}{text}")

                # ── 有子块则递归（如 toggle 内部）──
                if block.get("has_children"):
                    sub_md, _ = await self._read_page_recursive(
                        block["id"], depth + 1, visited
                    )
                    if sub_md.strip():
                        md_parts.append(f"{indent}{sub_md}")

            if not cursor:
                break

        return "".join(md_parts), all_nodes

    # ── 同步 & 缓存 ──

    async def sync_all(self) -> dict:
        async with self._sync_lock:
            logger.info("🔄 开始同步 Notion 设定集...")
            if not self.notion_token:
                return {"ok": False, "error": "未配置 Notion Token"}
            if not self.root_page_id:
                return {"ok": False, "error": "未配置根页面 ID"}

            root_info = await self._get_page_info(self.root_page_id)
            if not root_info:
                return {"ok": False, "error": "无法访问根页面，请确认 Token 和页面权限"}

            try:
                full_md, tree = await self._read_page_recursive(self.root_page_id)
                content_map = {}
                for node in tree:
                    if node["title"]:
                        content_map[node["id"]] = {
                            "title": node["title"],
                            "url": node.get("url", ""),
                            "last_edited": node.get("last_edited", ""),
                        }

                cache_data = {
                    "root_page_id": self.root_page_id,
                    "last_sync": datetime.now(timezone.utc).isoformat(),
                    "full_markdown": full_md,
                    "pages": content_map,
                    "tree": tree,
                    "page_count": len(tree),
                }
                with open(self.cache_file, "w", encoding="utf-8") as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)

                meta = {"last_sync": cache_data["last_sync"], "page_count": len(tree)}
                with open(self.meta_file, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

                self.cached_content = content_map
                self.page_tree = tree
                self.last_sync_time = cache_data["last_sync"]

                logger.info(f"✅ 同步完成！共 {len(tree)} 个页面")
                
                # 如果启用了知识库同步，自动写入
                if self.sync_to_kb:
                    logger.info("📚 正在写入知识库...")
                    kb_result = await self._sync_to_knowledge_base()
                    if kb_result.get("ok"):
                        logger.info(f"✅ 知识库同步完成: {kb_result.get('uploaded', 0)} 个页面")
                    else:
                        logger.warning(f"⚠️ 知识库同步失败: {kb_result.get('error')}")
                
                return {"ok": True, "page_count": len(tree), "last_sync": cache_data["last_sync"], "tree": tree}
            except Exception as e:
                logger.error(f"❌ 同步失败: {e}")
                return {"ok": False, "error": str(e)}

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.cached_content = data.get("pages", {})
                self.page_tree = data.get("tree", [])
                self.last_sync_time = data.get("last_sync")
                return data.get("full_markdown", "")
            except Exception:
                pass
        return ""

    def _get_formatted_context(self, max_length: int = 4000) -> str:
        if not os.path.exists(self.cache_file):
            return ""
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            md = data.get("full_markdown", "")
            tree = data.get("tree", [])

            def build_toc(nodes, depth=0):
                toc = ""
                for node in nodes:
                    toc += "  " * depth + f"- {node.get('title', '未命名')}\n"
                    if node.get("children"):
                        toc += build_toc(node["children"], depth + 1)
                return toc

            toc = build_toc(tree)
            context = (
f"【设定集信息】\n\n最后同步时间: {data.get('last_sync', '未知')}\n\n页面总数: {data.get('page_count', 0)} 个页面\n\n\n【目录结构】\n{toc}\n\n"
                f"【详细内容】\n{md}\n"
            )
            if len(context) > max_length:
                context = context[:max_length] + "\n\n...(内容过长已截断)"
            return context
        except Exception:
            return ""

    # ── 知识库同步 ──

    async def _get_or_create_kb(self) -> object | None:
        """获取或创建知识库"""
        try:
            mgr = self.context.kb_manager
            # 先尝试获取
            kb = await mgr.get_kb_by_name(self.kb_name)
            if kb:
                logger.info(f"✅ 找到已有知识库: {self.kb_name}")
                return kb

            # 知识库不存在，提示用户先在 WebUI 创建
            logger.warning(f"知识库「{self.kb_name}」不存在，请先在 WebUI 创建")
            return None
        except Exception as e:
            logger.error(f"获取知识库失败: {e}")
            return None

    async def _sync_to_knowledge_base(self) -> dict:
        """将缓存的内容写入知识库"""
        if not os.path.exists(self.cache_file):
            return {"ok": False, "error": "没有缓存内容"}

        kb = await self._get_or_create_kb()
        if not kb:
            return {"ok": False, "error": f"知识库「{self.kb_name}」未创建"}

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            full_md = data.get("full_markdown", "")
            tree = data.get("tree", [])
            if not full_md.strip():
                return {"ok": False, "error": "缓存内容为空"}

            # 1. 删除知识库中所有旧文档
            old_docs = await kb.list_documents(limit=999)
            deleted = 0
            for doc in old_docs:
                try:
                    await kb.delete_document(doc.doc_id)
                    deleted += 1
                except Exception as e:
                    logger.warning(f"删除旧文档失败 {doc.doc_id}: {e}")
            if deleted:
                logger.info(f"🗑️ 已删除 {deleted} 个旧文档")

            # 2. 按页面分割内容，每个页面作为一个文档
            # 从 markdown 中提取每个页面的内容
            pages_text = {}
            lines = full_md.split("\n")
            current_page_id = None
            current_lines = []

            for line in lines:
                if line.startswith("> 页面ID: `") and line.endswith("`"):
                    # 保存上一页
                    if current_page_id and current_lines:
                        pages_text[current_page_id] = "\n".join(current_lines)
                    # 开始新页面
                    pid = line[9:-1]  # 提取页面ID
                    current_page_id = pid
                    current_lines = [line]
                else:
                    if current_page_id:
                        current_lines.append(line)

            # 保存最后一页
            if current_page_id and current_lines:
                pages_text[current_page_id] = "\n".join(current_lines)

            # 如果没找到页面分割，就把全文作为一整个文档
            if not pages_text:
                pages_text["root"] = full_md

            # 3. 上传每个页面作为独立文档
            uploaded = 0
            page_titles = {}
            for node in tree:
                page_titles[node["id"]] = node.get("title", "未命名")

            for pid, text in pages_text.items():
                if not text.strip():
                    continue
                title = page_titles.get(pid, pid)
                # 文件名
                file_name = f"notion_{title}.md"
                # 上传
                doc = await kb.upload_document(
                    file_name=file_name,
                    file_content=text.encode("utf-8"),
                    file_type="md",
                    chunk_size=self.kb_chunk_size,
                    chunk_overlap=50,
                    batch_size=32,
                    tasks_limit=3,
                )
                uploaded += 1
                logger.info(f"📄 已写入: {title} ({doc.chunk_count} 个块)")

            logger.info(f"✅ 知识库同步完成，共 {uploaded} 个页面")
            return {"ok": True, "uploaded": uploaded}

        except Exception as e:
            logger.error(f"知识库同步失败: {e}")
            return {"ok": False, "error": str(e)}

    # ── 自动同步 ──

    async def _auto_sync_loop(self):
        await asyncio.sleep(5)
        self._load_cache()
        while True:
            try:
                await asyncio.sleep(self.sync_interval * 60)
                logger.info("⏰ 定时同步触发")
                result = await self.sync_all()
                if result.get("ok"):
                    logger.info(f"✅ 自动同步完成: {result.get('page_count')} 个页面")
                else:
                    logger.warning(f"⚠️ 自动同步失败: {result.get('error')}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动同步异常: {e}")

    # ── 搜索 ──

    def search(self, keyword: str) -> list[dict]:
        if not os.path.exists(self.cache_file):
            return []
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            md = data.get("full_markdown", "")
            results = []
            lines = md.split("\n")
            for i, line in enumerate(lines):
                if keyword.lower() in line.lower():
                    page_title = "未知"
                    for j in range(i, -1, -1):
                        if lines[j].startswith("#") and not lines[j].startswith("> "):
                            page_title = lines[j].lstrip("#").strip()
                            break
                    results.append({"line": i + 1, "page": page_title, "content": line.strip()})
            seen = set()
            unique = []
            for r in results:
                key = (r["page"], r["content"])
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            return unique[:20]
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return []

    # ════════════════════════════════════════════
    #  Notion API 文档管理（增删改查全套）
    # ════════════════════════════════════════════

    # ── Rich Text 辅助 ──

    @staticmethod
    def _is_valid_uuid(text: str) -> bool:
        """校验 UUID 格式（32位hex 或 36位带连字符）"""
        import re as _re
        return bool(_re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', text.strip()))

    def _make_rich_text(self, text: str) -> list:
        """将纯文本转为 Notion rich_text 数组"""
        return [{"type": "text", "text": {"content": text}}]

    def _make_blocks_from_markdown(self, markdown: str) -> list:
        """将简单 markdown 文本转为 Notion block 数组"""
        import re as _re
        blocks = []
        for line in markdown.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("### "):
                blocks.append({"type": "heading_3", "heading_3": {"rich_text": self._make_rich_text(line[4:])}})
            elif line.startswith("## "):
                blocks.append({"type": "heading_2", "heading_2": {"rich_text": self._make_rich_text(line[3:])}})
            elif line.startswith("# "):
                blocks.append({"type": "heading_1", "heading_1": {"rich_text": self._make_rich_text(line[2:])}})
            elif line.startswith("- [ ] "):
                blocks.append({"type": "to_do", "to_do": {"rich_text": self._make_rich_text(line[6:]), "checked": False}})
            elif line.startswith("- [x] "):
                blocks.append({"type": "to_do", "to_do": {"rich_text": self._make_rich_text(line[6:]), "checked": True}})
            elif line.startswith("- "):
                blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": self._make_rich_text(line[2:])}})
            elif _re.match(r'^\d+[\.\)]\s', line):
                # 数字列表: "1. xxx" 或 "1) xxx"
                text = _re.sub(r'^\d+[\.\)]\s', '', line, count=1)
                blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": self._make_rich_text(text)}})
            elif line.startswith("> "):
                blocks.append({"type": "quote", "quote": {"rich_text": self._make_rich_text(line[2:])}})
            else:
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": self._make_rich_text(line)}})
        return blocks

    # ── Pages API ──

    async def create_page(self, parent_id: str, title: str,
                          content_markdown: str = "",
                          icon_emoji: str = None,
                          parent_type: str = "page_id") -> dict:
        """创建页面。parent_type: 'page_id' 或 'data_source_id'"""
        properties = {"title": {"title": self._make_rich_text(title)}}
        body = {
            "parent": {parent_type: parent_id},
            "properties": properties,
        }
        if icon_emoji:
            body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        if content_markdown:
            body["children"] = self._make_blocks_from_markdown(content_markdown)

        result = await self._post("/pages", body)
        if result.get("ok") is False:
            return result
        return {
            "ok": True,
            "id": result.get("id", ""),
            "url": result.get("url", ""),
            "title": title,
            "created_time": result.get("created_time", ""),
        }

    async def update_page(self, page_id: str, title: str = None,
                          icon_emoji: str = None, in_trash: bool = None) -> dict:
        """更新页面标题、图标、回收站状态"""
        body = {}
        if title is not None:
            body["properties"] = {"title": {"title": self._make_rich_text(title)}}
        if icon_emoji is not None:
            body["icon"] = {"type": "emoji", "emoji": icon_emoji}
        if in_trash is not None:
            body["in_trash"] = in_trash

        result = await self._patch(f"/pages/{page_id}", body)
        if result.get("ok") is False:
            return result
        return {"ok": True, "id": result.get("id", ""), "url": result.get("url", "")}

    async def delete_page(self, page_id: str) -> dict:
        """删除页面（移入回收站）"""
        return await self.update_page(page_id, in_trash=True)

    # ── Blocks API ──

    async def retrieve_block(self, block_id: str) -> dict:
        """获取单个块信息"""
        data = await self._get(f"/blocks/{block_id}")
        if not data:
            return {"ok": False, "error": "块不存在或无法访问"}
        return {"ok": True, "block": data}

    async def append_blocks(self, block_id: str, markdown: str,
                            position: str = "end", after_block_id: str = None) -> dict:
        """给页面追加块。position: 'end'/'start'/('after_block', after_block_id)"""
        children = self._make_blocks_from_markdown(markdown)
        if not children:
            return {"ok": False, "error": "没有可追加的内容"}

        body = {"children": children}
        if position == "start":
            body["position"] = {"type": "start"}
        elif position == "after_block" and after_block_id:
            body["position"] = {"type": "after_block", "after_block": {"id": after_block_id}}

        result = await self._patch(f"/blocks/{block_id}/children", body)
        if result.get("ok") is False:
            return result
        return {"ok": True, "count": len(children), "results": result.get("results", [])}

    async def update_block(self, block_id: str, block_data: dict) -> dict:
        """更新块内容。block_data 示例：{"paragraph": {"rich_text": [{"text": {"content": "新内容"}}]}}"""
        result = await self._patch(f"/blocks/{block_id}", block_data)
        if result.get("ok") is False:
            return result
        return {"ok": True, "block": result}

    async def delete_block(self, block_id: str) -> dict:
        """删除块（移入回收站）"""
        result = await self._delete_req(f"/blocks/{block_id}")
        return result

    # ── Databases API ──

    async def retrieve_database(self, database_id: str) -> dict:
        """获取数据库信息"""
        data = await self._get(f"/databases/{database_id}")
        if not data:
            return {"ok": False, "error": "数据库不存在或无法访问"}
        return {"ok": True, "database": data}

    async def query_database(self, database_id: str,
                             filter_obj: dict = None, sorts: list = None,
                             page_size: int = 100, start_cursor: str = None) -> dict:
        """查询数据库条目。支持 filter 和 sorts"""
        body = {"page_size": page_size}
        if filter_obj:
            body["filter"] = filter_obj
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor

        result = await self._post(f"/databases/{database_id}/query", body)
        if result.get("ok") is False:
            return result
        pages = []
        for p in result.get("results", []):
            # 提取标题
            title_text = ""
            for prop_name, prop_val in p.get("properties", {}).items():
                if prop_val.get("type") == "title":
                    title_text = "".join(t.get("plain_text", "") for t in prop_val.get("title", []))
                    break
            pages.append({
                "id": p.get("id", ""),
                "title": title_text,
                "url": p.get("url", ""),
                "created_time": p.get("created_time", ""),
            })
        return {
            "ok": True,
            "results": pages,
            "total": len(pages),
            "has_more": result.get("has_more", False),
            "next_cursor": result.get("next_cursor"),
        }

    async def create_database(self, parent_page_id: str, title: str,
                              properties: dict = None, is_inline: bool = False) -> dict:
        """创建数据库"""
        if properties is None:
            properties = {
                "Name": {"title": {}},
                "描述": {"rich_text": {}},
            }
        body = {
            "parent": {"page_id": parent_page_id},
            "title": self._make_rich_text(title),
            "properties": properties,
            "is_inline": is_inline,
        }
        result = await self._post("/databases", body)
        if result.get("ok") is False:
            return result
        return {
            "ok": True,
            "id": result.get("id", ""),
            "url": result.get("url", ""),
            "title": title,
        }

    async def update_database(self, database_id: str, title: str = None,
                              description: str = None, in_trash: bool = None,
                              is_inline: bool = None) -> dict:
        """更新数据库"""
        body = {}
        if title is not None:
            body["title"] = self._make_rich_text(title)
        if description is not None:
            body["description"] = self._make_rich_text(description)
        if in_trash is not None:
            body["in_trash"] = in_trash
        if is_inline is not None:
            body["is_inline"] = is_inline

        result = await self._patch(f"/databases/{database_id}", body)
        if result.get("ok") is False:
            return result
        return {"ok": True, "id": result.get("id", ""), "url": result.get("url", "")}

    # ── Search API ──

    async def search_notion(self, query: str,
                            filter_obj: dict = None, sort_obj: dict = None,
                            page_size: int = 20, start_cursor: str = None) -> dict:
        """全局搜索 Notion 页面和数据库"""
        body = {
            "query": query,
            "page_size": page_size,
        }
        if filter_obj:
            body["filter"] = filter_obj  # {"value": "page", "property": "object"}
        if sort_obj:
            body["sort"] = sort_obj  # {"direction": "descending", "timestamp": "last_edited_time"}
        if start_cursor:
            body["start_cursor"] = start_cursor

        result = await self._post("/search", body)
        if result.get("ok") is False:
            return result
        items = []
        for r in result.get("results", []):
            obj_type = r.get("object", "")
            title_text = ""
            if obj_type == "page":
                for pn, pv in r.get("properties", {}).items():
                    if pv.get("type") == "title":
                        title_text = "".join(t.get("plain_text", "") for t in pv.get("title", []))
                        break
            elif obj_type == "database":
                title_text = "".join(t.get("plain_text", "") for t in r.get("title", []))
            items.append({
                "id": r.get("id", ""),
                "type": obj_type,
                "title": title_text or "(无标题)",
                "url": r.get("url", ""),
            })
        return {
            "ok": True,
            "results": items,
            "total": len(items),
            "has_more": result.get("has_more", False),
            "next_cursor": result.get("next_cursor"),
        }

    # ── Users API ──

    async def list_users(self, page_size: int = 100, start_cursor: str = None) -> dict:
        """列出工作空间的所有用户"""
        path = f"/users?page_size={page_size}"
        if start_cursor:
            path += f"&start_cursor={start_cursor}"
        data = await self._get(path)
        if not data:
            return {"ok": False, "error": "获取用户列表失败"}
        users = []
        for u in data.get("results", []):
            users.append({
                "id": u.get("id", ""),
                "name": u.get("name", ""),
                "type": u.get("type", ""),
                "avatar_url": u.get("avatar_url", ""),
            })
        return {"ok": True, "users": users, "total": len(users)}

    async def retrieve_user(self, user_id: str) -> dict:
        """获取特定用户信息"""
        data = await self._get(f"/users/{user_id}")
        if not data:
            return {"ok": False, "error": "用户不存在"}
        return {"ok": True, "user": {"id": data.get("id"), "name": data.get("name"), "type": data.get("type")}}

    async def get_me(self) -> dict:
        """获取当前 bot 用户信息"""
        data = await self._get("/users/me")
        if not data:
            return {"ok": False, "error": "获取 bot 信息失败"}
        return {"ok": True, "bot": {"id": data.get("id"), "name": data.get("name"), "type": data.get("type")}}

    # ── Comments API ──

    async def list_comments(self, block_id: str = None, page_id: str = None,
                            page_size: int = 100, start_cursor: str = None) -> dict:
        """获取评论列表。需要 block_id 或 page_id"""
        path = f"/comments?page_size={page_size}"
        if block_id:
            path += f"&block_id={block_id}"
        elif page_id:
            path += f"&page_id={page_id}"
        if start_cursor:
            path += f"&start_cursor={start_cursor}"

        data = await self._get(path)
        if not data:
            return {"ok": False, "error": "获取评论失败"}
        comments = []
        for c in data.get("results", []):
            text = "".join(t.get("plain_text", "") for t in c.get("rich_text", []))
            comments.append({
                "id": c.get("id", ""),
                "text": text,
                "created_time": c.get("created_time", ""),
                "author": c.get("created_by", {}).get("id", ""),
            })
        return {"ok": True, "comments": comments, "total": len(comments)}

    async def create_comment(self, text: str,
                             parent_page_id: str = None, discussion_id: str = None) -> dict:
        """添加评论。需要 parent_page_id 或 discussion_id"""
        if not parent_page_id and not discussion_id:
            return {"ok": False, "error": "需要 parent_page_id 或 discussion_id"}
        body = {
            "rich_text": self._make_rich_text(text),
        }
        if discussion_id:
            body["discussion_id"] = discussion_id
        else:
            body["parent"] = {"page_id": parent_page_id}

        result = await self._post("/comments", body)
        if result.get("ok") is False:
            return result
        return {"ok": True, "id": result.get("id", ""), "text": text, "created_time": result.get("created_time", "")}

    async def retrieve_comment(self, comment_id: str) -> dict:
        """获取单个评论"""
        data = await self._get(f"/comments/{comment_id}")
        if not data:
            return {"ok": False, "error": "评论不存在"}
        text = "".join(t.get("plain_text", "") for t in data.get("rich_text", []))
        return {"ok": True, "comment": {"id": data.get("id"), "text": text, "created_time": data.get("created_time")}}

    # ── LLM 工具 ──

    @filter.llm_tool(name="query_notion_world_setting")
    async def query_notion_setting(self, event: AstrMessageEvent, keyword: str):
        """查询设定集中的内容。当用户询问世界观设定、角色设定、物品设定等时，使用此工具查询设定集数据库。

        Args:
            keyword(string): 搜索关键词，如"紫金币"、"炼金术"、"魔法材料"等
        """
        if not os.path.exists(self.cache_file):
            yield event.plain_result("设定集尚未同步，请先使用 /notion sync 命令同步。")
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            md = data.get("full_markdown", "")
            results = []
            lines = md.split("\n")
            for i, line in enumerate(lines):
                if keyword.lower() in line.lower():
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    results.append("\n".join(lines[start:end]))
            if not results:
                yield event.plain_result(f"在设定集中未找到与「{keyword}」相关的内容。")
                return
            seen = set()
            unique = [r for r in results if not (r in seen or seen.add(r))]
            MAX_OUTPUT = 2500
            output = f"📖 在设定集中找到 {len(unique)} 处与「{keyword}」相关的内容：\n\n"
            for idx, ctx in enumerate(unique[:8], 1):
                block = f"--- 匹配 {idx} ---\n{ctx}\n\n"
                if len(output) + len(block) > MAX_OUTPUT:
                    output += f"...还有 {len(unique) - idx + 1} 处匹配（输出过长已截断）\n"
                    break
                output += block
            yield event.plain_result(output)
            return output[:1000]
        except Exception as e:
            yield event.plain_result(f"查询设定集时出错: {e}")
            return ""

    # ── 指令 ──

    @filter.command_group("notion")
    def notion_group(self):
        pass

    @notion_group.command("sync")
    async def notion_sync(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 正在同步 Notion 设定集，请稍候...")
        result = await self.sync_all()
        if result.get("ok"):
            yield event.plain_result(
f"✅ **同步完成！**\n\n- 时间: {result['last_sync'][:19].replace('T', ' ')}\n\n- 页面: {result['page_count']} 个\n\n- 已缓存 ✅"
            )
        else:
            yield event.plain_result(f"❌ 同步失败: {result.get('error', '未知错误')}")

    @notion_group.command("search")
    async def notion_search(self, event: AstrMessageEvent, keyword: str):
        results = self.search(keyword)
        if not results:
            yield event.plain_result(f"🔍 未找到「{keyword}」相关内容。")
            return
        msg = f"🔍 **找到 {len(results)} 处相关：**\n\n"
        for r in results[:10]:
            msg += f"📄 [{r['page']}] {r['content']}\n"
        if len(results) > 10:
            msg += f"\n...还有 {len(results) - 10} 处匹配"
        yield event.plain_result(msg)

    @notion_group.command("status")
    async def notion_status(self, event: AstrMessageEvent):
        if not os.path.exists(self.cache_file):
            yield event.plain_result("📭 尚未同步，请使用 `/notion sync`")
            return
        with open(self.meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        with open(self.cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        md_len = len(data.get("full_markdown", ""))
        wh_status = "未配置 ❌"
        if self._webhook_subscription_id:
            wh_status = "已启用 ✅"
        elif self.webhook_verification_token:
            wh_status = "已配置待确认"
        msg = (
f"📊 **Notion 设定集状态**\n\n\n✅ 上次同步: {meta.get('last_sync', '未知')[:19].replace('T', ' ')}\n\n📄 页面数量: {meta.get('page_count', 0)} 个\n\n📦 内容大小: {md_len/1024:.1f} KB\n"
f"⏰ 自动同步: 每 {self.sync_interval} 分钟\n\n🌐 Webhook: {wh_status}\n\n🔌 AI 查询: {'开启 ✅' if self.auto_inject else '关闭 ❌'}\n\n"
        )
        tree = data.get("tree", [])
        if tree:
            def show_tree(nodes, d=0):
                t = ""
                for node in nodes:
                    t += "  " * d + f"- {node.get('title', '未命名')}\n"
                    if node.get("children"):
                        t += show_tree(node["children"], d + 1)
                return t
            msg += f"**📂 目录（{meta.get('page_count', 0)} 个页面）：**\n{show_tree(tree)}"
        yield event.plain_result(msg)

    @notion_group.command("inject")
    async def notion_inject(self, event: AstrMessageEvent):
        context = self._get_formatted_context(max_length=2000)
        if not context:
            yield event.plain_result("📭 还没有缓存内容。")
            return
        yield event.plain_result(f"📖 **设定上下文（前2000字）：**\n\n{context[:2000]}")

    @notion_group.group("kb")
    def notion_kb_group(self):
        pass

    @notion_kb_group.command("sync")
    async def notion_kb_sync(self, event: AstrMessageEvent):
        """手动将设定集写入知识库"""
        if not os.path.exists(self.cache_file):
            yield event.plain_result("📭 还没有缓存内容，请先 /notion sync")
            return
        yield event.plain_result("📚 正在将设定集写入知识库，请稍候...")
        result = await self._sync_to_knowledge_base()
        if result.get("ok"):
            yield event.plain_result(
f"✅ **知识库同步完成！**\n\n📄 共写入 {result['uploaded']} 个页面\n\n💡 现在可以直接问 LLM 设定相关的问题了~"
            )
        else:
            yield event.plain_result(
                f"❌ 知识库同步失败: {result.get('error')}\n\n💡 请先在 WebUI 创建名为「{self.kb_name}」的知识库"
            )

    @notion_kb_group.command("list")
    async def notion_kb_list(self, event: AstrMessageEvent):
        """列出所有可用知识库"""
        try:
            mgr = self.context.kb_manager
            kbs = []
            for kb_id, kb_helper in mgr.kb_insts.items():
                kbs.append(kb_helper.kb)
            
            if not kbs:
                yield event.plain_result("📭 还没有知识库，请先在 WebUI 创建")
                return
            
            msg = f"📋 **共有 {len(kbs)} 个知识库：**\n\n"
            for i, kb in enumerate(kbs, 1):
                name = kb.kb_name
                desc = kb.description or ""
                count = getattr(kb, "doc_count", 0)
                msg += f"{i}. **{name}** ({count} 个文档)\n"
                if desc:
                    msg += f"   {desc}\n"
            
            current = self.kb_name
            msg += f"\n📌 当前绑定: **{current}**"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"❌ 获取知识库列表失败: {e}")

    @notion_kb_group.command("bind")
    async def notion_kb_bind(self, event: AstrMessageEvent, kb_name: str = ""):
        """绑定知识库。不带参数时列出所有知识库供选择"""
        if not kb_name:
            # 列出所有知识库
            try:
                mgr = self.context.kb_manager
                kbs = []
                # 列出所有知识库实例
                for kb_id, kb_helper in mgr.kb_insts.items():
                    kbs.append(kb_helper.kb)
                
                if not kbs:
                    yield event.plain_result("📭 还没有知识库，请先在 WebUI 创建")
                    return
                
                msg = "📋 **请选择要绑定的知识库：**\n\n"
                for i, kb in enumerate(kbs, 1):
                    name = kb.kb_name
                    desc = kb.description or ""
                    count = getattr(kb, "doc_count", 0)
                    msg += f"{i}. **{name}** ({count} 文档)\n"
                    if desc:
                        msg += f"   {desc}\n"
                msg += "\n💡 发送 `/notion kb bind <名称>` 来绑定"
                yield event.plain_result(msg)
            except Exception as e:
                yield event.plain_result(f"❌ 获取知识库列表失败: {e}")
            return
        
        # 绑定指定的知识库
        self.kb_name = kb_name
        self.config["kb_name"] = kb_name
        self.config.save_config()
        yield event.plain_result(f"✅ 已绑定知识库: {kb_name}\n💡 使用 /notion kb sync 写入数据")

    @notion_group.group("webhook")
    def notion_webhook_group(self):
        pass

    @notion_webhook_group.command("create")
    async def notion_webhook_create(self, event: AstrMessageEvent, url: str):
        if not url.startswith("https://"):
            yield event.plain_result("❌ Webhook URL 必须以 https:// 开头")
            return
        yield event.plain_result(f"🔄 正在注册 Webhook...\n📮 {url}")
        result = await self._create_webhook_subscription(url)
        if result.get("ok"):
            data = result["data"]
            yield event.plain_result(
f"✅ **Webhook 创建成功！**\n\n\n📮 订阅ID: `{data.get('id', '?')}`\n\n📢 监听: page.content_updated, page.updated, page.created, page.deleted\n\n\n以后在 Notion 里改设定，机器人就自动同步啦！🎉"
            )
        else:
            yield event.plain_result(f"❌ 创建失败: {result.get('error', '?')}")

    @notion_webhook_group.command("list")
    async def notion_webhook_list(self, event: AstrMessageEvent):
        yield event.plain_result("🔄 查询中...")
        subs = await self._list_webhook_subscriptions()
        if not subs:
            yield event.plain_result("📭 没有 Webhook 订阅。")
            return
        msg = f"📋 找到 {len(subs)} 个订阅：\n\n"
        for sub in subs:
            msg += (
f"📮 `{sub.get('id', '?')}`\n\n  URL: {sub.get('url', '?')}\n\n  状态: {'活跃 ✅' if sub.get('active') else '停用 ❌'}\n\n  事件: {', '.join(sub.get('events', []))}\n\n"
            )
        yield event.plain_result(msg)

    @notion_webhook_group.command("delete")
    async def notion_webhook_delete(self, event: AstrMessageEvent, subscription_id: str):
        yield event.plain_result(f"🔄 删除中...")
        ok = await self._delete_webhook_subscription(subscription_id)
        if ok:
            self._webhook_subscription_id = None
            yield event.plain_result("✅ 已删除！")
        else:
            yield event.plain_result("❌ 删除失败")

    # ── LLM 请求钩子 ──

    @filter.on_llm_request()
    async def inject_context_to_llm(self, event: AstrMessageEvent, req):
        if not self.auto_inject:
            return
        if not os.path.exists(self.cache_file):
            return
        try:
            context = self._get_formatted_context(max_length=3000)
            if context:
                from astrbot.core.agent.message import TextPart
                req.extra_user_content_parts.append(
                    TextPart(
                        text=(
                            "<world_setting_database>\n"
                            f"{context}\n"
                            "</world_setting_database>\n\n"
                            "以上是用户设定的世界观设定集数据库。"
                            "当用户询问与世界观、角色、物品、设定等相关问题时，"
                            "请优先参考上述内容进行回答。"
                            "如果用户的问题在上述设定集中找不到答案，请如实说明。"
                        )
                    ).mark_as_temp()
                )
                logger.debug("✅ 已注入设定上下文到 LLM 请求")
        except Exception as e:
            logger.error(f"注入设定上下文失败: {e}")

    # ════════════════════════════════════════════
    #  文档管理指令（Page / Block / DB / Search / User / Comment）
    # ════════════════════════════════════════════

    # ── 指令组: /notion page ──

    @notion_group.group("page")
    def notion_page_group(self):
        pass

    @notion_page_group.command("create")
    async def notion_page_create(self, event: AstrMessageEvent,
                                  parent_id: str, title: str,
                                  content: str = "",
                                  icon: str = ""):
        """创建页面。parent_id: 父页面ID, title: 页面标题, content: 可选markdown内容, icon: 可选表情符号"""
        if not self._is_valid_uuid(parent_id):
            yield event.plain_result(f"❌ `{parent_id}` 不是有效的页面ID格式")
            return
        yield event.plain_result("📄 正在创建页面...")
        result = await self.create_page(
            parent_id=parent_id,
            title=title,
            content_markdown=content,
            icon_emoji=icon if icon else None,
        )
        if result.get("ok"):
            yield event.plain_result(
f"✅ **页面创建成功！**\n"
f"📌 标题: {title}\n"
f"🔗 链接: {result.get('url', '')}\n"
                f"🆔 ID: `{result.get('id', '')}`"
            )
        else:
            yield event.plain_result(f"❌ 创建失败: {result.get('error', '未知错误')[:200]}")

    @notion_page_group.command("update")
    async def notion_page_update(self, event: AstrMessageEvent,
                                  page_id: str, title: str = "",
                                  icon: str = ""):
        """更新页面。page_id: 页面ID, title: 新标题(不填不改), icon: 新表情符号(不填不改)"""
        kwargs = {}
        if title:
            kwargs["title"] = title
        if icon:
            kwargs["icon_emoji"] = icon
        if not kwargs:
            yield event.plain_result("⚠️ 至少需要 title 或 icon 参数")
            return

        yield event.plain_result("📝 正在更新页面...")
        result = await self.update_page(page_id, **kwargs)
        if result.get("ok"):
            yield event.plain_result(f"✅ **页面已更新！**\n🔗 {result.get('url', '')}")
        else:
            yield event.plain_result(f"❌ 更新失败: {result.get('error', '未知错误')[:200]}")

    @notion_page_group.command("delete")
    async def notion_page_delete(self, event: AstrMessageEvent, page_id: str):
        """删除页面（移入回收站）。page_id: 页面ID"""
        yield event.plain_result("🗑️ 正在删除页面...")
        result = await self.delete_page(page_id)
        if result.get("ok"):
            yield event.plain_result(f"✅ **页面已移入回收站！**\n🆔 `{page_id}`")
        else:
            yield event.plain_result(f"❌ 删除失败: {result.get('error', '未知错误')[:200]}")

    # ── 指令组: /notion block ──

    @notion_group.group("block")
    def notion_block_group(self):
        pass

    @notion_block_group.command("info")
    async def notion_block_info(self, event: AstrMessageEvent, block_id: str):
        """获取块信息。block_id: 块ID"""
        yield event.plain_result("🔍 正在查询块信息...")
        result = await self.retrieve_block(block_id)
        if result.get("ok"):
            block = result["block"]
            btype = block.get("type", "未知")
            yield event.plain_result(
f"📦 **块信息**\n"
f"🆔: `{block.get('id', '')}`\n"
f"📌 类型: {btype}\n"
                f"🔗 对象: {block.get('object', '')}"
            )
        else:
            yield event.plain_result(f"❌ 查询失败: {result.get('error', '未知错误')[:200]}")

    @notion_block_group.command("add")
    async def notion_block_add(self, event: AstrMessageEvent,
                                block_id: str, content: str):
        """添加内容到页面。block_id: 页面/块ID, content: markdown文本内容"""
        yield event.plain_result("📝 正在添加内容...")
        result = await self.append_blocks(block_id, content)
        if result.get("ok"):
            yield event.plain_result(f"✅ **已添加 {result['count']} 个块！**")
        else:
            yield event.plain_result(f"❌ 添加失败: {result.get('error', '未知错误')[:200]}")

    @notion_block_group.command("edit")
    async def notion_block_edit(self, event: AstrMessageEvent,
                                 block_id: str, content: str):
        """编辑块内容（只支持段落）。block_id: 块ID, content: 新文本"""
        yield event.plain_result("✏️ 正在更新块...")
        block_data = {"paragraph": {"rich_text": self._make_rich_text(content)}}
        result = await self.update_block(block_id, block_data)
        if result.get("ok"):
            yield event.plain_result(f"✅ **块已更新！**")
        else:
            yield event.plain_result(f"❌ 更新失败: {result.get('error', '未知错误')[:200]}")

    @notion_block_group.command("delete")
    async def notion_block_delete(self, event: AstrMessageEvent, block_id: str):
        """删除块。block_id: 块ID"""
        yield event.plain_result("🗑️ 正在删除块...")
        result = await self.delete_block(block_id)
        if result.get("ok"):
            yield event.plain_result(f"✅ **块已删除！**")
        else:
            yield event.plain_result(f"❌ 删除失败: {result.get('error', '未知错误')[:200]}")

    # ── 指令组: /notion db ──

    @notion_group.group("db")
    def notion_db_group(self):
        pass

    @notion_db_group.command("info")
    async def notion_db_info(self, event: AstrMessageEvent, database_id: str):
        """获取数据库信息。database_id: 数据库ID"""
        yield event.plain_result("🔍 正在查询数据库...")
        result = await self.retrieve_database(database_id)
        if result.get("ok"):
            db = result["database"]
            title = "".join(t.get("plain_text", "") for t in db.get("title", []))
            yield event.plain_result(
f"🗄️ **数据库信息**\n"
f"📌 标题: {title or '(无)'}\n"
f"🆔 `{db.get('id', '')}`\n"
                f"🔗 {db.get('url', '')}"
            )
        else:
            yield event.plain_result(f"❌ 查询失败: {result.get('error', '未知错误')[:200]}")

    @notion_db_group.command("query")
    async def notion_db_query(self, event: AstrMessageEvent, database_id: str, keyword: str = ""):
        """查询数据库条目。database_id: 数据库ID, keyword: 可选搜索关键词"""
        yield event.plain_result("🔍 正在查询数据库...")
        filter_obj = None
        if keyword:
            filter_obj = {
                "or": [
                    {"property": prop, "rich_text": {"contains": keyword}}
                    for prop in ["Name", "名称", "标题", "name", "title"]
                ]
            }
        result = await self.query_database(database_id, filter_obj=filter_obj)
        if result.get("ok"):
            pages = result["results"]
            if not pages:
                yield event.plain_result(f"📭 没有找到条目{'（关键词: ' + keyword + '）' if keyword else ''}")
                return
            msg = f"📋 **共 {result['total']} 条记录：**\n\n"
            for p in pages[:15]:
                msg += f"📄 {p['title']}\n  🆔 `{p['id'][:8]}...`\n"
            if len(pages) > 15:
                msg += f"\n...还有 {len(pages) - 15} 条"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 查询失败: {result.get('error', '未知错误')[:200]}")

    @notion_db_group.command("create")
    async def notion_db_create(self, event: AstrMessageEvent,
                                parent_id: str, title: str,
                                fields: str = ""):
        """创建数据库。parent_id: 父页面ID, title: 数据库名称, fields: 字段列表 格式 名称:类型,名称:类型（类型: title/rich_text/number/select/multi_select/date/checkbox/email/phone/url）"""
        if not self._is_valid_uuid(parent_id):
            yield event.plain_result(f"❌ `{parent_id}` 不是有效的页面ID格式")
            return
        yield event.plain_result("🗄️ 正在创建数据库...")
        properties = {"Name": {"title": {}}, "描述": {"rich_text": {}}}
        if fields:
            for pair in fields.split(","):
                pair = pair.strip()
                if ":" in pair:
                    name, typ = pair.split(":", 1)
                    name, typ = name.strip(), typ.strip().lower()
                    tm = {
                        "title": {"title": {}}, "文本": {"rich_text": {}}, "rich_text": {"rich_text": {}},
                        "数字": {"number": {"format": "number"}}, "number": {"number": {"format": "number"}},
                        "选择": {"select": {"options": []}}, "select": {"select": {"options": []}},
                        "多选": {"multi_select": {"options": []}}, "multi_select": {"multi_select": {"options": []}},
                        "日期": {"date": {}}, "date": {"date": {}},
                        "复选框": {"checkbox": {}}, "checkbox": {"checkbox": {}},
                        "邮箱": {"email": {}}, "email": {"email": {}},
                        "电话": {"phone_number": {}}, "phone": {"phone_number": {}},
                        "url": {"url": {}}, "链接": {"url": {}},
                    }
                    properties[name] = tm.get(typ, {"rich_text": {}})
        result = await self.create_database(parent_id, title, properties=properties)
        if result.get("ok"):
            yield event.plain_result(
f"✅ **数据库创建成功！**\n"
f"📌 名称: {title}\n"
f"🆔 `{result.get('id', '')}`\n"
                f"🔗 {result.get('url', '')}"
            )
        else:
            yield event.plain_result(f"❌ 创建失败: {result.get('error', '未知错误')[:200]}")

    # ── 指令: /notion find（全局搜索，区别于本地缓存搜索）/notion search ──

    @notion_group.command("find")
    async def notion_find(self, event: AstrMessageEvent, query: str):
        """全局搜索 Notion 页面和数据库。query: 搜索关键词"""
        yield event.plain_result(f"🔍 正在全局搜索「{query}」...")
        result = await self.search_notion(query)
        if result.get("ok"):
            items = result["results"]
            if not items:
                yield event.plain_result(f"📭 未找到与「{query}」相关的页面或数据库")
                return
            msg = f"🔍 **全局搜索「{query}」共 {result['total']} 条结果：**\n\n"
            for item in items[:10]:
                icon = "📄" if item["type"] == "page" else "🗄️"
                msg += f"{icon} **{item['title']}**\n  🆔 `{item['id']}`\n"
                if item.get("url"):
                    msg += f"  🔗 {item['url']}\n"
            if len(items) > 10:
                msg += f"\n...还有 {len(items) - 10} 条"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 搜索失败: {result.get('error', '未知错误')[:200]}")

    # ── 指令组: /notion user ──

    @notion_group.group("user")
    def notion_user_group(self):
        pass

    @notion_user_group.command("list")
    async def notion_user_list(self, event: AstrMessageEvent):
        """列出工作空间的所有用户"""
        yield event.plain_result("👥 正在获取用户列表...")
        result = await self.list_users()
        if result.get("ok"):
            users = result["users"]
            if not users:
                yield event.plain_result("📭 没有用户")
                return
            msg = f"👥 **共 {result['total']} 个用户：**\n\n"
            for u in users:
                msg += f"• {u['name']} ({u['type']})\n  🆔 `{u['id']}`\n"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 获取失败: {result.get('error', '未知错误')[:200]}")

    @notion_user_group.command("me")
    async def notion_user_me(self, event: AstrMessageEvent):
        """获取当前 bot 用户信息"""
        yield event.plain_result("🤖 正在获取 bot 信息...")
        result = await self.get_me()
        if result.get("ok"):
            bot = result["bot"]
            yield event.plain_result(
f"🤖 **Bot 信息**\n"
f"📌 名称: {bot['name']}\n"
f"🆔 `{bot['id']}`\n"
                f"📌 类型: {bot['type']}"
            )
        else:
            yield event.plain_result(f"❌ 获取失败: {result.get('error', '未知错误')[:200]}")

    # ── 指令组: /notion comment ──

    @notion_group.group("comment")
    def notion_comment_group(self):
        pass

    @notion_comment_group.command("list")
    async def notion_comment_list(self, event: AstrMessageEvent, page_id: str):
        """获取页面的评论。page_id: 页面ID"""
        yield event.plain_result("💬 正在获取评论...")
        result = await self.list_comments(page_id=page_id)
        if result.get("ok"):
            comments = result["comments"]
            if not comments:
                yield event.plain_result("📭 该页面暂无评论")
                return
            msg = f"💬 **共 {result['total']} 条评论：**\n\n"
            for c in comments:
                msg += f"• {c['text']}\n  🕐 {c.get('created_time', '')[:16]}\n"
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"❌ 获取失败: {result.get('error', '未知错误')[:200]}")

    @notion_comment_group.command("add")
    async def notion_comment_add(self, event: AstrMessageEvent, page_id: str, text: str):
        """添加评论。page_id: 页面ID, text: 评论内容"""
        yield event.plain_result("💬 正在添加评论...")
        result = await self.create_comment(text=text, parent_page_id=page_id)
        if result.get("ok"):
            yield event.plain_result(f"✅ **评论已添加！**\n💬 {result.get('text', '')}")
        else:
            yield event.plain_result(f"❌ 添加失败: {result.get('error', '未知错误')[:200]}")

    # ── LLM 工具：创建页面 ──

    @filter.llm_tool(name="notion_create_page")
    async def llm_notion_create_page(self, event: AstrMessageEvent,
                                      parent_id: str, title: str,
                                      content: str = "", icon: str = ""):
        """在Notion中创建新页面。当用户要求创建文档、笔记、设定页面时使用此工具。

        Args:
            parent_id(string): 父页面ID，新页面将创建在该父页面下
            title(string): 页面标题
            content(string): 页面内容（Markdown格式），可选
            icon(string): 页面图标（表情符号如📖🌟），可选
        """
        result = await self.create_page(parent_id, title, content, icon if icon else None)
        if result.get("ok"):
            yield event.plain_result(f"✅ 已在 Notion 创建页面「{title}」\n🔗 {result.get('url', '')}")
            return f"✅ 已在 Notion 创建页面「{title}」"
        else:
            yield event.plain_result(f"❌ 创建失败: {result.get('error', '未知错误')[:200]}")
            return f"❌ 创建失败: {result.get('error', '未知错误')[:200]}"

    @filter.llm_tool(name="notion_query_database")
    async def llm_notion_query_db(self, event: AstrMessageEvent,
                                   database_id: str, keyword: str = ""):
        """查询Notion数据库中的条目。当用户询问数据库内容、条目列表时使用此工具。

        Args:
            database_id(string): 数据库ID
            keyword(string): 搜索关键词，可选
        """
        filter_obj = None
        if keyword:
            filter_obj = {
                "or": [
                    {"property": prop, "rich_text": {"contains": keyword}}
                    for prop in ["Name", "名称", "标题", "name", "title"]
                ]
            }
        result = await self.query_database(database_id, filter_obj=filter_obj)
        if result.get("ok"):
            pages = result["results"]
            if not pages:
                yield event.plain_result(f"数据库查询完成，共 0 条结果。")
                return ""
            summary = f"数据库查询完成，共 {result['total']} 条结果：\n"
            for p in pages[:20]:
                summary += f"- {p['title']}\n"
            yield event.plain_result(summary)
            return summary[:1000]
        else:
            yield event.plain_result(f"数据库查询失败: {result.get('error', '未知错误')[:200]}")
            return ""

    @filter.llm_tool(name="notion_search")
    async def llm_notion_search(self, event: AstrMessageEvent, query: str):
        """全局搜索Notion页面和数据库。当用户想在Notion中查找内容时使用此工具。

        Args:
            query(string): 搜索关键词
        """
        result = await self.search_notion(query)
        if result.get("ok"):
            items = result["results"]
            if not items:
                yield event.plain_result(f"在 Notion 中未找到与「{query}」相关的内容。")
                return
            summary = f"在 Notion 中搜索到 {result['total']} 条结果：\n"
            for item in items[:15]:
                icon = "📄" if item["type"] == "page" else "🗄️"
                summary += f"{icon} {item['title']}\n"
            yield event.plain_result(summary)
            return summary[:1000]
        else:
            yield event.plain_result(f"搜索失败: {result.get('error', '未知错误')[:200]}")
            return ""

    @filter.llm_tool(name="notion_append_content")
    async def llm_notion_append_content(self, event: AstrMessageEvent,
                                         page_id: str, content: str):
        """向Notion页面追加内容。当用户要求向已有页面添加内容时使用此工具。

        Args:
            page_id(string): 页面ID
            content(string): 要追加的Markdown内容
        """
        result = await self.append_blocks(page_id, content)
        if result.get("ok"):
            yield event.plain_result(f"✅ 已向页面追加 {result['count']} 个块")
            return f"✅ 已向页面追加 {result['count']} 个块"
        else:
            yield event.plain_result(f"❌ 追加失败: {result.get('error', '未知错误')[:200]}")
            return f"❌ 追加失败: {result.get('error', '未知错误')[:200]}"

    async def terminate(self):
        await self._http.aclose()
        logger.info("Notion Bridge 插件 - 文档管理增强版已卸载")