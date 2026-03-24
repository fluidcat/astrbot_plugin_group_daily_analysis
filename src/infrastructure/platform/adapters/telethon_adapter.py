"""
Telethon 平台适配器

支持 Telegram 客户端协议 (MTProto) 的消息获取、发送和群组管理功能。

通过 Telethon 库直接访问 Telegram 客户端协议。
"""

import asyncio
import base64
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Any

from ....domain.value_objects.platform_capabilities import (
    TELEGRAM_CAPABILITIES,
    PlatformCapabilities, TELETHON_CAPABILITIES,
)
from ....domain.value_objects.unified_group import UnifiedGroup, UnifiedMember
from ....domain.value_objects.unified_message import (
    MessageContent,
    MessageContentType,
    UnifiedMessage,
)
from ....utils.logger import logger
from ..base import PlatformAdapter

if TYPE_CHECKING:
    from astrbot.api.star import Context

# Telethon 依赖
try:
    from telethon import TelegramClient
    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, Chat, Channel
    from telethon.errors import (
        ChatWriteForbiddenError,
        PeerIdInvalidError,
        UserBannedInChannelError,
        FloodWaitError,
    )

    TELETHON_AVAILABLE = True
except ImportError:
    TelegramClient = None
    TELETHON_AVAILABLE = False


class TelethonAdapter(PlatformAdapter):
    """
    Telethon 平台适配器

    实现 PlatformAdapter 接口，支持：
    - 消息获取（通过 Telethon MTProto API）
    - 消息发送（文本、图片、文件）
    - 群组信息获取
    - 成员信息查询
    - 媒体下载

    工作原理：
    - 使用 Telethon 库直接连接 Telegram 服务器
    - 通过 API ID 和 API Hash 认证
    - 支持会话持久化，无需重复登录
    """

    def __init__(self, bot_instance: Any, config: dict | None = None):
        super().__init__(bot_instance, config)
        self._cached_client: TelegramClient | None = None
        self._context: Context | None = None

        # 机器人自身 ID（用于消息过滤）
        self.bot_user_id = str(config.get("bot_user_id", "")) if config else ""

        # 尝试从配置获取 bot self ids 列表
        self.bot_self_ids: list[str] = []
        if config:
            ids = config.get("bot_self_ids", [])
            self.bot_self_ids = [str(i) for i in ids] if ids else []
            self._plugin_instance = config.get("plugin_instance")
        else:
            self._plugin_instance = None
        self._platform_id = str(config.get("platform_id", "")).strip() if config else ""

    def set_context(self, context: "Context") -> None:
        """
        设置 AstrBot 上下文

        用于访问核心服务（可选）。
        """
        self._context = context

    def _init_capabilities(self) -> PlatformCapabilities:
        """返回 Telegram 平台能力声明"""
        return TELETHON_CAPABILITIES

    @property
    def _telegram_client(self) -> TelegramClient | None:
        """
        懒加载获取 Telethon 客户端

        如果 bot_instance 就是 TelethonClient，直接使用。
        否则尝试从 bot 实例中提取。
        """
        if self._cached_client:
            return self._cached_client

        if not TELETHON_AVAILABLE:
            logger.warning("Telethon 库未安装，Telethon 适配器不可用")
            raise RuntimeError("Telethon library not installed")

        # 路径 A：bot 本身就是 TelegramClient
        if isinstance(self.bot, TelegramClient):
            self._cached_client = self.bot
            return self._cached_client

        # 路径 B：bot 有 client 属性且是 TelegramClient
        if hasattr(self.bot, "client") and isinstance(self.bot.client, TelegramClient):
            self._cached_client = self.bot.client
            return self._cached_client

        raise RuntimeError(
            f"无法从 bot_instance 获取 Telethon 客户端。"
            f"bot_type: {type(self.bot).__name__}"
        )

    # ==================== IMessageRepository 实现 ====================

    async def fetch_messages(
        self,
        group_id: str,
        days: int = 1,
        max_count: int = 100,
        before_id: str | None = None,
        since_ts: int | None = None,
    ) -> list[UnifiedMessage]:
        """
        获取历史消息

        从 Telethon 拉取群组历史消息记录。

        Args:
            group_id: 群组/频道/用户 ID
            days: 查询天数范围
            max_count: 最大拉取消息数量上限
            before_id: 锚点消息 ID（从该消息之前开始）
            since_ts: 从指定时间戳开始拉取（Unix timestamp）

        Returns:
            list[UnifiedMessage]: 统一格式的消息对象列表
        """
        if not TELETHON_AVAILABLE:
            logger.error("Telethon 库未安装")
            return []

        try:
            client = self._telegram_client

            # 获取实体（群组/频道/用户）
            entity = await self._get_entity(group_id)

            # 确定时间范围
            if since_ts and since_ts > 0:
                start_date = datetime.fromtimestamp(since_ts, timezone.utc)
            else:
                start_date = datetime.now(timezone.utc) - timedelta(days=days)

            messages = []
            offset_date = start_date  # 用于分页

            logger.info(
                f"[Telethon] 开始拉取群 {group_id} 的消息: "
                f"起始时间 {start_date.strftime('%Y-%m-%d %H:%M:%S')}, "
                f"上限 {max_count} 条"
            )

            last_id = 0
            while len(messages) < max_count:
                fetched = False
                # 分页拉取消息
                async for msg in client.iter_messages(
                    entity,
                    limit=max_count - len(messages),
                    offset_date=offset_date,
                    min_id=last_id,
                    reverse=True,  # 从最旧的消息开始
                ):
                    # 时间过滤
                    if msg.date < start_date:
                        continue

                    # 跳过机器人自己的消息
                    if self.bot_user_id and str(msg.sender_id) == self.bot_user_id:
                        continue
                    if str(msg.sender_id) in self.bot_self_ids:
                        continue

                    # 转换为统一格式
                    unified = self._convert_message(msg, group_id)
                    if unified:
                        messages.append(unified)

                    # 更新分页锚点
                    last_id = msg.id
                    fetched = True

                    # 达到数量上限
                    if len(messages) >= max_count:
                        break

                # 如果没有获取到消息，说明已到达历史尽头
                if not fetched:
                    break

            logger.info(f"[Telethon] 拉取完成，共获取 {len(messages)} 条消息")
            return messages

        except FloodWaitError as e:
            logger.warning(f"[Telethon] 遇到限流错误，等待 {e.seconds} 秒后重试...")
            await asyncio.sleep(e.seconds)
            return await self.fetch_messages(group_id, days, max_count, before_id, since_ts)
        except Exception as e:
            logger.error(f"[Telethon] 获取消息历史失败: {e}", exc_info=True)
            return []

    async def _get_entity(self, group_id: str) -> Any:
        """
        获取 Telegram 实体

        Args:
            group_id: 群组/频道/用户 ID

        Returns:
            Entity 对象（Chat, Channel 或 User）
        """
        client = self._telegram_client
        try:
            # 尝试通过 ID 获取
            return await client.get_entity(int(group_id))
        except (TypeError, ValueError):
            pass

        # 尝试通过用户名获取（@username）
        if group_id.startswith("@"):
            return await client.get_entity(group_id)

        raise ValueError(f"无法找到实体: {group_id}")

    def _convert_message(self, raw_msg: Any, group_id: str) -> UnifiedMessage | None:
        """
        将 Telethon Message 转换为 UnifiedMessage

        Args:
            raw_msg: Telethon Message 对象
            group_id: 群组 ID

        Returns:
            UnifiedMessage 对象
        """
        try:
            # 提取文本内容
            text_content = raw_msg.text or ""
            contents = []

            # 添加文本内容
            if text_content:
                contents.append(
                    MessageContent(
                        type=MessageContentType.TEXT,
                        text=text_content,
                    )
                )

            # 处理媒体
            if raw_msg.media:
                media = self._convert_media(raw_msg.media)
                if media:
                    contents.append(media)

            # 处理回复
            reply_to_id = None
            if raw_msg.reply_to_msg_id:
                reply_to_id = str(raw_msg.reply_to_msg_id)

            # 获取发送者信息
            sender_id = str(raw_msg.sender_id) if raw_msg.sender_id else ""
            sender_name = ""

            if raw_msg.sender:
                # 优先使用全名
                if raw_msg.sender.first_name or raw_msg.sender.last_name:
                    name_parts = []
                    if raw_msg.sender.first_name:
                        name_parts.append(raw_msg.sender.first_name)
                    if raw_msg.sender.last_name:
                        name_parts.append(raw_msg.sender.last_name)
                    sender_name = " ".join(name_parts)
                elif raw_msg.sender.username:
                    sender_name = raw_msg.sender.username
                else:
                    sender_name = raw_msg.sender.first_name or ""

            return UnifiedMessage(
                message_id=str(raw_msg.id),
                sender_id=sender_id,
                sender_name=sender_name,
                sender_card=None,
                group_id=group_id,
                text_content=text_content,
                contents=tuple(contents),
                timestamp=int(raw_msg.date.timestamp()) if raw_msg.date else 0,
                platform="telethon",
                reply_to_id=reply_to_id,
            )

        except Exception as e:
            logger.debug(f"[Telethon] 消息转换失败: {e}")
            return None

    def _convert_media(self, media: Any) -> MessageContent | None:
        text = ""
        if isinstance(media, MessageMediaPhoto):
            text = "[图片]"
        elif isinstance(media, MessageMediaDocument):
            if doc := media.document:
                mime_type = doc.mime_type or ""
                if mime_type.startswith("video"):
                    text="[视频]"
                elif mime_type.startswith("audio"):
                    text="[语音]"
                else:
                    text="[文档]"

        if text:
            return MessageContent(type=MessageContentType.TEXT, text=text)
        return None

    def _convert_media1(self, media: Any) -> MessageContent | None:
        """
        转换媒体类型

        Args:
            media: Telethon MessageMedia 对象

        Returns:
            MessageContent 对象
        """
        try:
            if isinstance(media, MessageMediaPhoto):
                # 如果有文件路径，优先使用
                if hasattr(media, "photo") and media.photo:
                    file = media.photo
                    if file.local_path:
                        return MessageContent(
                            type=MessageContentType.IMAGE,
                            url=f"file://{file.local_path}",
                        )

                    # 否则使用 URL
                    if file.web_placeholder and file.web_placeholder.url:
                        return MessageContent(
                            type=MessageContentType.IMAGE,
                            url=file.web_placeholder.url,
                        )

            elif isinstance(media, MessageMediaDocument):
                doc = media.document
                if not doc:
                    return None

                mime_type = doc.mime_type or ""

                # 视频
                if mime_type.startswith("video"):
                    url = getattr(media, "document", None)
                    if url and hasattr(url, "local_path") and url.local_path:
                        return MessageContent(
                            type=MessageContentType.VIDEO,
                            url=f"file://{url.local_path}",
                        )

                # 音频
                elif mime_type.startswith("audio"):
                    url = getattr(media, "document", None)
                    if url and hasattr(url, "local_path") and url.local_path:
                        return MessageContent(
                            type=MessageContentType.VOICE,
                            url=f"file://{url.local_path}",
                        )

                # 文档
                else:
                    url = getattr(media, "document", None)
                    if url and hasattr(url, "local_path") and url.local_path:
                        return MessageContent(
                            type=MessageContentType.FILE,
                            url=f"file://{url.local_path}",
                        )

        except Exception as e:
            logger.debug(f"[Telethon] 媒体转换失败: {e}")

        return None

    def convert_to_raw_format(self, messages: list[UnifiedMessage]) -> list[dict]:
        """
        将统一消息格式转换为 OneBot 兼容格式

        用于向后兼容现有分析逻辑。
        """
        result = []
        for msg in messages:
            raw = {
                "message_id": msg.message_id,
                "group_id": msg.group_id,
                "time": msg.timestamp,
                "sender": {
                    "user_id": msg.sender_id,
                    "nickname": msg.sender_name,
                    "card": msg.sender_card or "",
                },
                "message": [],
                "user_id": msg.sender_id,
            }

            # 转换消息内容
            for content in msg.contents:
                if content.type == MessageContentType.TEXT:
                    raw["message"].append(
                        {"type": "text", "data": {"text": content.text or ""}}
                    )
                elif content.type == MessageContentType.IMAGE:
                    raw["message"].append(
                        {"type": "image", "data": {"url": content.url or ""}}
                    )
                elif content.type == MessageContentType.AT:
                    raw["message"].append(
                        {"type": "at", "data": {"qq": content.at_user_id or ""}}
                    )
                elif content.type == MessageContentType.VIDEO:
                    raw["message"].append(
                        {"type": "video", "data": {"url": content.url or ""}}
                    )
                elif content.type == MessageContentType.VOICE:
                    raw["message"].append(
                        {"type": "record", "data": {"url": content.url or ""}}
                    )
                elif content.type == MessageContentType.FILE:
                    raw["message"].append(
                        {"type": "file", "data": {"url": content.url or ""}}
                    )

            result.append(raw)

        return result

    # ==================== IMessageSender 实现 ====================

    async def send_text(
        self,
        group_id: str,
        text: str,
        reply_to: str | None = None,
    ) -> bool:
        """发送文本消息"""
        client = self._telegram_client
        if not client:
            logger.error("[Telethon] 客户端未初始化，无法发送文本")
            return False

        try:
            entity = await self._get_entity(group_id)

            kwargs = {"entity": entity, "text": text}
            if reply_to:
                kwargs["reply_to"] = int(reply_to)

            await client.send_message(**kwargs)
            return True
        except (ChatWriteForbiddenError, UserBannedInChannelError) as e:
            logger.error(f"[Telethon] 无权限发送消息: {e}")
            return False
        except FloodWaitError as e:
            logger.warning(f"[Telethon] 遇到限流错误，等待 {e.seconds} 秒...")
            await asyncio.sleep(e.seconds)
            return await self.send_text(group_id, text, reply_to)
        except Exception as e:
            logger.error(f"[Telethon] 发送文本失败: {e}")
            return False

    async def send_image(
        self,
        group_id: str,
        image_path: str,
        caption: str = "",
    ) -> bool:
        """发送图片消息"""
        client = self._telegram_client
        if not client:
            logger.error("[Telethon] 客户端未初始化，无法发送图片")
            return False

        try:
            entity = await self._get_entity(group_id)
            file_obj: Any = None
            is_temp_obj = False

            kwargs: dict[str, Any] = {"entity": entity}
            if caption:
                kwargs["caption"] = caption

            # 1. 统一处理输入源 (Base64 / URL / Local File)
            if image_path.startswith("base64://"):
                data = base64.b64decode(image_path[len("base64://") :])
                file_obj = BytesIO(data)
                is_temp_obj = True
            elif image_path.startswith("data:"):
                parts = image_path.split(",", 1)
                if len(parts) == 2:
                    data = base64.b64decode(parts[1])
                    file_obj = BytesIO(data)
                    is_temp_obj = True
            elif image_path.startswith(("http://", "https://")):
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            image_path, timeout=aiohttp.ClientTimeout(total=30)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                file_obj = BytesIO(data)
                                is_temp_obj = True
                            else:
                                file_obj = image_path
                except Exception as e:
                    logger.warning(f"[Telethon] 下载图片失败: {e}")
                    file_obj = image_path
            else:
                # 本地文件
                if os.path.exists(image_path):
                    file_obj = open(image_path, "rb")
                    is_temp_obj = True
                else:
                    file_obj = image_path

            # 2. 发送图片
            if file_obj and hasattr(file_obj, "read"):
                await client.send_file(file=file_obj, **{k: v for k, v in kwargs.items() if k != "entity"})
            else:
                await client.send_message(file=image_path, **kwargs)

            return True

        except Exception as e:
            logger.error(f"[Telethon] 发送图片失败: {e}")
            return False

    async def send_file(
        self,
        group_id: str,
        file_path: str,
        filename: str | None = None,
    ) -> bool:
        """发送文件消息"""
        client = self._telegram_client
        if not client:
            logger.error("[Telethon] 客户端未初始化，无法发送文件")
            return False

        try:
            entity = await self._get_entity(group_id)

            # 1. 统一处理输入源 (Base64 / Local File)
            if file_path.startswith("base64://"):
                data = base64.b64decode(file_path[len("base64://") :])
                file_obj = BytesIO(data)
                is_temp_obj = True
                if not filename:
                    filename = "file.pdf"
            elif file_path.startswith("data:"):
                parts = file_path.split(",", 1)
                if len(parts) == 2:
                    data = base64.b64decode(parts[1])
                    file_obj = BytesIO(data)
                    is_temp_obj = True
                    if not filename:
                        filename = "file.pdf"
            elif os.path.isfile(file_path):
                file_obj = open(file_path, "rb")
                is_temp_obj = True
                if not filename:
                    filename = os.path.basename(file_path)
            else:
                # 可能是 URL
                file_obj = file_path
                if not filename:
                    filename = "file"

            # 2. 发送文件
            if file_obj and hasattr(file_obj, "read"):
                await client.send_file(
                    file=file_obj, filename=filename, force_document=True
                )
            else:
                await client.send_message(file=file_path, force_document=True)

            return True
        except Exception as e:
            logger.error(f"[Telethon] 发送文件失败: {e}")
            return False

    async def send_forward_msg(self, group_id: str, nodes: list[dict]) -> bool:
        """
        发送合并转发消息

        Telethon 不支持原生转发消息链，转换为格式化文本发送。
        """
        if not nodes:
            return True

        lines = ["📊 **分析报告**\n"]
        for node in nodes:
            data = node.get("data", node)
            name = data.get("name", "AstrBot")
            content = data.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for seg in content:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        text_parts.append(seg.get("data", {}).get("text", ""))
                content = "".join(text_parts)
            lines.append(f"**[{name}]**\n{content}\n")

        full_text = "\n".join(lines)

        if len(full_text) > 4096:
            parts = [
                full_text[i : i + 4000] for i in range(0, len(full_text), 4000)
            ]
            for part in parts:
                if not await self.send_text(group_id, part):
                    return False
            return True
        else:
            return await self.send_text(group_id, full_text)

    # ==================== IGroupInfoRepository 实现 ====================

    async def get_group_list(self) -> list[str]:
        """
        获取群组列表

        通过 Telethon 获取所有对话列表。
        """
        if not TELETHON_AVAILABLE:
            logger.error("Telethon 库未安装")
            return []

        try:
            client = self._telegram_client
            group_ids = []

            async for dialog in client.iter_dialogs():
                # 过滤群组和频道（不包括个人）
                if dialog.is_group or dialog.is_channel:
                    group_ids.append(str(dialog.entity.id))

            logger.info(f"[Telethon] 获取到 {len(group_ids)} 个群组/频道")
            return group_ids

        except Exception as e:
            logger.error(f"[Telethon] 获取群列表失败: {e}")
            return []

    async def get_group_info(self, group_id: str) -> UnifiedGroup | None:
        """获取群组信息"""
        if not TELETHON_AVAILABLE:
            return None

        try:
            client = self._telegram_client
            entity = await self._get_entity(group_id)

            member_count = 0
            group_name = "Unknown"

            if isinstance(entity, Channel):
                # 频道
                member_count = entity.participants_count or 0
                group_name = entity.title or "Unknown"
            elif isinstance(entity, Chat):
                # 群组
                member_count = entity.participants_count or 0
                group_name = entity.title or "Unknown"

            return UnifiedGroup(
                group_id=str(entity.id),
                group_name=group_name,
                member_count=member_count,
                description="",
                platform="telethon",
            )

        except Exception as e:
            logger.debug(f"[Telethon] 获取群信息失败: {e}")
            return None

    async def get_member_list(self, group_id: str) -> list[UnifiedMember]:
        """
        获取成员列表

        通过 Telethon 获取群组成员。
        """
        if not TELETHON_AVAILABLE:
            return []

        try:
            client = self._telegram_client
            entity = await self._get_entity(group_id)

            members = []
            async for member in client.iter_participants(entity, aggressive=True):
                # 跳过机器人自己
                if self.bot_user_id and str(member.id) == self.bot_user_id:
                    continue

                role = "member"
                if member.admin_rights:
                    role = "admin"
                elif member.creator:
                    role = "owner"

                members.append(
                    UnifiedMember(
                        user_id=str(member.id),
                        nickname=member.first_name or "",
                        card=member.username or "",
                        role=role,
                    )
                )

            return members

        except Exception as e:
            logger.debug(f"[Telethon] 获取成员列表失败: {e}")
            return []

    async def get_member_info(
        self,
        group_id: str,
        user_id: str,
    ) -> UnifiedMember | None:
        """获取成员信息"""
        if not TELETHON_AVAILABLE:
            return None

        try:
            client = self._telegram_client
            entity = await self._get_entity(group_id)
            member = await client.get_entity(int(user_id))

            role = "member"
            if member.admin_rights:
                role = "admin"
            elif member.creator:
                role = "owner"

            return UnifiedMember(
                user_id=str(member.id),
                nickname=member.first_name or "",
                card=member.username or "",
                role=role,
            )
        except Exception as e:
            logger.debug(f"[Telethon] 获取成员信息失败: {e}")
            return None

    # ==================== IAvatarRepository 实现 ====================

    async def get_user_avatar_url(
        self,
        user_id: str,
        size: int = 100,
    ) -> str | None:
        """获取用户头像 URL"""
        if not TELETHON_AVAILABLE:
            return None

        try:
            client = self._telegram_client
            user = await client.get_entity(int(user_id))

            if user and user.photo:
                # 获取最大尺寸的头像
                photo = user.photo.big
                if photo.local_path:
                    return f"file://{photo.local_path}"

                if photo.web_placeholder and photo.web_placeholder.url:
                    return photo.web_placeholder.url

            return None
        except Exception as e:
            logger.debug(f"[Telethon] 获取用户头像失败: {e}")
            return None

    async def get_user_avatar_data(
        self,
        user_id: str,
        size: int = 100,
    ) -> str | None:
        """获取头像的 Base64 数据"""
        url = await self.get_user_avatar_url(user_id, size)
        if not url:
            return None

        try:
            if url.startswith("file://"):
                file_path = url[7:]
                if os.path.exists(file_path):
                    with open(file_path, "rb") as f:
                        data = f.read()
                        b64 = base64.b64encode(data).decode("utf-8")
                        return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.debug(f"[Telethon] 获取头像 Base64 失败: {e}")
            return None

        return None

    async def get_group_avatar_url(
        self,
        group_id: str,
        size: int = 100,
    ) -> str | None:
        """获取群组头像 URL"""
        if not TELETHON_AVAILABLE:
            return None

        try:
            client = self._telegram_client
            entity = await self._get_entity(group_id)

            if entity and entity.photo:
                photo = entity.photo.big
                if photo.local_path:
                    return f"file://{photo.local_path}"

                if photo.web_placeholder and photo.web_placeholder.url:
                    return photo.web_placeholder.url

            return None
        except Exception as e:
            logger.debug(f"[Telethon] 获取群头像失败: {e}")
            return None

    async def batch_get_avatar_urls(
        self,
        user_ids: list[str],
        size: int = 100,
    ) -> dict[str, str | None]:
        """批量获取头像 URL"""
        if not user_ids:
            return {}

        # 适度并发，避免串行等待过久
        semaphore = asyncio.Semaphore(8)

        async def _fetch_avatar(uid: str) -> tuple[str, str | None]:
            async with semaphore:
                try:
                    return uid, await self.get_user_avatar_url(uid, size)
                except Exception as e:
                    logger.debug(f"[Telethon] 批量获取头像失败 uid={uid}: {e}")
                    return uid, None

        pairs = await asyncio.gather(*(_fetch_avatar(uid) for uid in user_ids))
        return dict(pairs)

    async def set_reaction(
        self, group_id: str, message_id: str, emoji: str | int, is_add: bool = True
    ) -> bool:
        """
        Telethon 实现消息回应。
        """
        if not TELETHON_AVAILABLE:
            return False

        try:
            client = self._telegram_client
            entity = await self._get_entity(group_id)

            message = await client.get_messages(entity, ids=int(message_id))
            if not message:
                return False

            # 映射表情 ID
            emoji_to_use = str(emoji)
            if str(emoji) == "🔍":
                emoji_to_use = "🔍"
            elif str(emoji) == "📊":
                emoji_to_use = "📊"
            elif str(emoji) == "✅":
                emoji_to_use = "✅"

            if is_add:
                await message.add_reaction(emoji_to_use)
            else:
                await message.remove_reaction(emoji_to_use)
            return True
        except Exception as e:
            logger.debug(f"[Telethon] set_reaction 失败: {e}")
            return False
