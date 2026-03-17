"""
微信857消息处理服务

负责处理接收到的微信消息事件：
1. 解析消息内容（文本、图片、@提及等）
2. 解析发送者信息
3. 存储消息历史
"""
import asyncio
import re
from collections import Counter

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.core import logger
from astrbot.core.star import Star


class WeChat857MessageProcessingService:
    """
    微信857消息处理服务

    负责处理微信消息事件并存储到 message_history_manager。
    """
    RECORD_GROUP_KV_KEY = f"wechat857_record_group"

    def __init__(self, context: Context, plugin_instance: Star):
        self.context = context
        self.plugin_instance = plugin_instance
        self.platform_group_cache = dict()
        self._lock = asyncio.Lock()

    async def process_message(self, event: AstrMessageEvent) -> None:
        """
        处理并在历史记录中存储消息。

        Args:
            event: AstrBot 消息事件

        Raises:
            ValueError: 当必要数据无法获取时
            RuntimeError: 当消息内容为空时
        """
        # 1. 获取群组 ID（必需）
        group_id = self._get_group_id_from_event(event)
        platform_id = event.get_platform_id()
        if not group_id:
            raise ValueError("无法获取群组 ID，拒绝存储消息")

        # 2. 获取发送者 ID（必需）
        sender_id = event.get_sender_id()
        if not sender_id:
            raise ValueError(f"群 {group_id}: 无法获取发送者 ID，拒绝存储消息")
        sender_id = str(sender_id)

        # 3. 获取发送者名称（昵称优先）
        sender_name = self._resolve_sender_name(event, sender_id)

        # 4. 提取消息内容
        message_parts = self._extract_message_parts(event)
        if not message_parts:
            raise RuntimeError(
                f"群 {group_id}: 消息内容为空 (sender={sender_name})，拒绝存储"
            )

        # 5. 提取事件消息 ID
        msg_obj = getattr(event, "message_obj", None)
        event_message_id = str(getattr(msg_obj, "message_id", "") or "")

        # 6. 存储到 message_history_manager
        await self.context.message_history_manager.insert(
            platform_id=platform_id,
            user_id=group_id,
            content={"type": "user", "message": message_parts},
            sender_id=sender_id,
            sender_name=sender_name,
        )

        logger.debug(
            f"[WeChat857] 已缓存群 {group_id} 的消息 (发送者: {sender_name})"
        )

        # 缓存有消息存储的群
        _cache = (await self.get_cache_group()).get(platform_id)
        if not _cache or group_id not in _cache:
            async with self._lock:
                self.platform_group_cache.setdefault(platform_id, {})[group_id] = {}
                await self.plugin_instance.put_kv_data(self.RECORD_GROUP_KV_KEY, self.platform_group_cache)

    async def get_cache_group(self) -> dict:
        if not self.platform_group_cache:
            async with self._lock:
                gs = await self.plugin_instance.get_kv_data(self.RECORD_GROUP_KV_KEY, {})
                self.platform_group_cache.update(gs)

        return self.platform_group_cache

    def _get_group_id_from_event(self, event: AstrMessageEvent) -> str | None:
        """从消息事件中安全获取群组 ID"""
        try:
            group_id = event.get_group_id()
            return group_id if group_id else None
        except Exception:
            return None

    def _resolve_sender_name(self, event: AstrMessageEvent, sender_id: str) -> str:
        """解析发送者展示名"""
        msg_obj = getattr(event, "message_obj", None)
        raw_message = getattr(msg_obj, "raw_message", None)
        raw_msg_obj = getattr(raw_message, "message", raw_message)
        from_user = getattr(raw_msg_obj, "from_user", None)

        candidates: list[str | None] = []

        # 微信特有的字段
        if from_user is not None:
            candidates.extend([
                getattr(from_user, "NickName", None),
                getattr(from_user, "RemarkName", None),
            ])

        # 兼容 AstrBot 标准字段
        candidates.append(event.get_sender_name())

        if msg_obj is not None:
            candidates.append(getattr(msg_obj, "nickname", None))

        # 如果 from_user 有更多字段，也添加进去
        if from_user is not None:
            candidates.extend([
                getattr(from_user, "RemarkName", None),
                getattr(from_user, "RemarkPyinitial", None),
                getattr(from_user, "RemarkQuanPin", None),
            ])

        for candidate in candidates:
            name = str(candidate or "").strip()
            if not self._is_placeholder_sender_name(name, sender_id):
                return name

        return sender_id

    def _extract_message_parts(self, event: AstrMessageEvent) -> list[dict]:
        """从事件中提取消息内容"""
        message_parts = []
        message = event.message_obj

        # 收集 @ 标记
        pending_mentions: Counter[str] = Counter()

        if message and hasattr(message, "message"):
            for seg in message.message:
                if not hasattr(seg, "type"):
                    continue
                if seg.type not in ("At", "at"):
                    continue

                target = getattr(seg, "target", None) or getattr(seg, "user_id", None)
                if target is None and hasattr(seg, "data"):
                    target = seg.data.get("user_id") or seg.data.get("target")

                target_str = str(target or "").strip()
                if target_str:
                    pending_mentions[target_str] += 1

                display_name = str(getattr(seg, "name", "") or "").strip()
                if display_name and display_name != target_str:
                    pending_mentions[display_name] += 1

        if message and hasattr(message, "message"):
            for seg in message.message:
                if not hasattr(seg, "type"):
                    continue

                seg_type = seg.type

                # 文本消息
                if seg_type in ("Plain", "text"):
                    text = getattr(seg, "text", None)
                    if text is None and hasattr(seg, "data"):
                        text = seg.data.get("text")
                    if text:
                        text = self._strip_known_mentions(text, pending_mentions)
                        message_parts.append({"type": "plain", "text": text})

                # 图片消息
                elif seg_type in ("Image", "image"):
                    url = getattr(seg, "url", None) or (
                        seg.data.get("url") if hasattr(seg, "data") else None
                    )
                    if url:
                        message_parts.append({"type": "image", "url": url})

                # @提及
                elif seg_type in ("At", "at"):
                    target = getattr(seg, "target", None) or getattr(seg, "user_id", None)
                    if target is None and hasattr(seg, "data"):
                        target = seg.data.get("user_id") or seg.data.get("target")
                    if target:
                        message_parts.append({
                            "type": "at",
                            "target_id": str(target),
                            "name": str(getattr(seg, "name", "") or ""),
                        })

        if not message_parts and event.message_str:
            message_parts.append({"type": "plain", "text": event.message_str})

        # 清理空文本段
        message_parts = [
            part
            for part in message_parts
            if not (part.get("type") == "plain" and not str(part.get("text", "")).strip())
        ]

        return message_parts

    @staticmethod
    def _strip_known_mentions(text: str, pending_mentions: Counter[str]) -> str:
        """从文本中移除已识别的 @ 提及"""
        cleaned = str(text)
        if not cleaned or not pending_mentions:
            return cleaned.strip()

        for mention, remaining in list(pending_mentions.items()):
            if not mention or remaining <= 0:
                continue

            pattern = re.compile(rf"(?<!\w)@{re.escape(mention)}(?!\w)")
            removed = 0
            while removed < remaining:
                cleaned, subn = pattern.subn("", cleaned, count=1)
                if subn == 0:
                    break
                removed += 1

            if removed > 0:
                pending_mentions[mention] -= removed
                if pending_mentions[mention] <= 0:
                    pending_mentions.pop(mention, None)

        return re.sub(r"\s{2,}", " ", cleaned).strip()

    @staticmethod
    def _is_placeholder_sender_name(name: str | None, sender_id: str) -> bool:
        """判断 sender_name 是否为占位值"""
        if not name:
            return True
        normalized = str(name).strip()
        if not normalized:
            return True
        if normalized.lower() in {"unknown", "none", "null", "nil", "undefined"}:
            return True
        if sender_id and normalized == str(sender_id).strip():
            return True
        return False
