"""
微信857平台适配器

支持微信857平台的消息发送和基础信息查询功能。
"""
from typing import Any, TYPE_CHECKING

from astrbot.core.db import PlatformMessageHistory
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform import Platform
from astrbot.core.platform.message_session import MessageSesion
from .. import PlatformAdapter
from ...persistence.wechat857_member_cache import WeChat857MemberCache
from ....domain.value_objects import PLATFORM_CAPABILITIES, PlatformCapabilities, UnifiedMessage, MessageContent, \
    MessageContentType, UnifiedGroup, UnifiedMember

if TYPE_CHECKING:
    from astrbot.api.star import Context

from ....utils.logger import logger

PLATFORM_CAPABILITIES['wechat857'] = PlatformCapabilities(
    platform_name="wechat857",
    platform_version="wx_857",
    supports_message_history=True,
    max_message_history_days=30,
    max_message_count=1000,
    supports_message_search=False,
    supports_group_list=False,
    supports_group_info=True,
    supports_member_list=True,
    supports_member_info=True,
    supports_text_message=True,
    supports_image_message=True,
    supports_file_message=True,
    supports_reply_message=False,
    max_text_length=10922,
    supports_at_all=True,
    supports_recall=True,
    supports_edit=False,
    supports_user_avatar=True,
    supports_group_avatar=False,
    avatar_needs_api_call=True,
    avatar_sizes=(132, 139, 409),
)


class Wx857Adapter(PlatformAdapter):
    """
    微信857平台适配器

    实现了统一的消息获取、发送和群组管理接口。
    """

    def __init__(self, bot_instance: Any, config: dict | None = None):
        super().__init__(bot_instance, config)
        self._context: Context | None = None
        self._platform: Platform | None = None

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

        # 成员缓存（1小时 TTL）
        self._member_cache = WeChat857MemberCache(ttl=3600)

    def set_context(self, context: "Context") -> None:
        """
        设置 AstrBot 上下文

        用于访问 message_history_manager 等核心服务。
        """
        self._context = context
        self._platform = context.get_platform_inst(self._platform_id)

    async def batch_get_avatar_urls(
        self, user_ids: list[str], size: int = 100
    ) -> dict[str, str | None]:
        """
        批量获取头像
        """
        if not user_ids:
            return {}

        # 通过 user_id 获取成员信息
        result = {}
        for uid in user_ids:
            member_data = await self._member_cache.get_members(user_id=uid)
            if member_data:
                result[uid] = member_data[0].avatar_url
            else:
                result[uid] = None

        return result

    def _init_capabilities(self) -> PlatformCapabilities:
        return PLATFORM_CAPABILITIES['wechat857']

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

        从 message_history_manager 读取存储的消息
        """
        if not self._context:
            logger.warning("[WeChat857] 未设置 context，无法获取消息历史")
            return []

        try:
            from datetime import datetime, timedelta, timezone

            history_mgr = self._context.message_history_manager

            before_id_int: int | None = None
            if before_id:
                try:
                    before_id_int = int(before_id)
                except (TypeError, ValueError):
                    pass

            if since_ts and since_ts > 0:
                # 统一使用 UTC 以兼容数据库记录的时间存储
                cutoff_time = datetime.fromtimestamp(since_ts, timezone.utc)
            else:
                cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
            target_count = max(1, int(max_count))
            page_size = target_count
            current_page = 1

            messages: list[UnifiedMessage] = []

            while len(messages) < target_count:
                history_records = await history_mgr.get(
                    platform_id=self._platform_id,
                    user_id=group_id,
                    page=current_page,
                    page_size=page_size,
                )
                if not history_records:
                    if current_page == 1:
                        logger.info(
                            f"[WeChat857] 群 {group_id} 没有存储的消息。"
                            f"提示：消息需要通过拦截器实时存储。"
                        )
                    break

                # 先用当前页已有的有效昵称预热缓存，减少额外 API 请求
                for record in history_records:
                    sender_id = str(getattr(record, "sender_id", "") or "").strip()
                    sender_name = str(getattr(record, "sender_name", "") or "").strip()
                    if sender_id and not self._is_placeholder_sender_name(
                        sender_name, sender_id
                    ):
                        pass  # 预热缓存（暂时不需要）

                oldest_record_time: datetime | None = None
                for record in history_records:
                    if before_id_int is not None:
                        try:
                            if int(record.id) >= before_id_int:
                                continue
                        except (TypeError, ValueError):
                            pass

                    record_time = getattr(record, "created_at", None)
                    if not record_time:
                        continue
                    if record_time.tzinfo is None:
                        record_time = record_time.replace(tzinfo=timezone.utc)
                    if oldest_record_time is None or record_time < oldest_record_time:
                        oldest_record_time = record_time
                    if record_time < cutoff_time:
                        continue

                    msg = self._convert_history_record(record, group_id)
                    if not msg:
                        continue

                    # 过滤机器人自己的消息
                    if self.bot_user_id and msg.sender_id == self.bot_user_id:
                        continue
                    if msg.sender_id in self.bot_self_ids:
                        continue

                    messages.append(msg)

                # 当前页完整处理后已足够，停止继续翻更旧页面。
                if len(messages) >= target_count:
                    break

                # 下一页一定更旧，若当前页最旧记录已越过时间窗口则可提前停止
                if oldest_record_time and oldest_record_time < cutoff_time:
                    break
                if len(history_records) < page_size:
                    break
                current_page += 1

            messages.sort(key=lambda m: m.timestamp)
            if len(messages) > target_count:
                messages = messages[-target_count:]

            logger.info(
                f"[WeChat857] 从数据库获取群 {group_id} 的消息: "
                f"{len(messages)} 条"
            )

            # 预热群成员信息
            await self.get_member_list(group_id)
            return messages

        except Exception as e:
            logger.error(f"[WeChat857] 获取消息历史失败: {e}")
            return []

    @staticmethod
    def _is_placeholder_sender_name(name: str | None, sender_id: str) -> bool:
        """判断 sender_name 是否属于占位值。"""
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

    def _convert_history_record(
        self, record: PlatformMessageHistory, group_id: str
    ) -> UnifiedMessage | None:
        """
        将数据库记录转换为 UnifiedMessage
        """
        try:
            content = record.content
            if not content:
                return None

            # 提取消息内容
            message_parts = content.get("message", [])
            text_content = ""
            contents = []

            for part in message_parts:
                if isinstance(part, dict):
                    part_type = part.get("type", "")
                    if part_type == "plain" or part_type == "text":
                        text = part.get("text", "")
                        text_content += text
                        contents.append(
                            MessageContent(
                                type=MessageContentType.TEXT,
                                text=text,
                            )
                        )
                    elif part_type == "image":
                        contents.append(
                            MessageContent(
                                type=MessageContentType.IMAGE,
                                url=part.get("url", "")
                                or part.get("attachment_id", ""),
                            )
                        )
                    elif part_type == "at":
                        target_id = (
                            part.get("target_id", "")
                            or part.get("qq", "")
                            or part.get("at_user_id", "")
                        )
                        contents.append(
                            MessageContent(
                                type=MessageContentType.AT,
                                at_user_id=str(target_id),
                            )
                        )

            if not contents:
                contents.append(
                    MessageContent(
                        type=MessageContentType.TEXT,
                        text=text_content,
                    )
                )

            sender_id = str(record.sender_id or "")
            sender_name = str(record.sender_name or "").strip() or sender_id or "Unknown"

            return UnifiedMessage(
                message_id=str(record.id),
                sender_id=sender_id,
                sender_name=sender_name,
                sender_card=None,
                group_id=group_id,
                text_content=text_content,
                contents=tuple(contents),
                timestamp=int(record.created_at.timestamp()),
                platform="wechat857",
                reply_to_id=None,
            )

        except Exception as e:
            logger.debug(f"[WeChat857] 转换历史记录失败: {e}")
            return None

    def convert_to_raw_format(self, messages: list[UnifiedMessage]) -> list[dict]:
        """忽略"""
        pass

    # ==================== IMessageSender 实现 ====================

    async def send_text(
        self, group_id: str, text: str, reply_to: str | None = None
    ) -> bool:
        mt = 'GroupMessage' if '@chatroom' in group_id else 'FriendMessage'
        ms = MessageSesion.from_str(f"{self._platform_id}:{mt}:{group_id}")
        await self._platform.send_by_session(ms, MessageChain().message(text))
        return True

    async def send_image(
        self, group_id: str, image_path: str, caption: str = ""
    ) -> bool:
        mt = 'GroupMessage' if '@chatroom' in group_id else 'FriendMessage'
        ms = MessageSesion.from_str(f"{self._platform_id}:{mt}:{group_id}")
        chain = MessageChain().url_image(image_path) if image_path.startswith(
            ("http://", "https://")) else MessageChain().file_image(image_path)
        await self._platform.send_by_session(ms, chain)
        return True

    async def send_forward_msg(self, group_id: str, nodes: list[dict]) -> bool:
        """忽略"""
        pass

    async def send_file(self, group_id: str, file_path: str, filename: str | None = None) -> bool:
        """忽略"""
        pass

    # ==================== IGroupInfoRepository 实现 ====================

    async def get_group_info(self, group_id: str) -> UnifiedGroup | None:
        """
        获取群组信息
        """
        info = await self.bot.get_chatroom_info(group_id)
        if not info:
            return None

        # 转换为 UnifiedGroup
        return UnifiedGroup(
            group_id=info.get('UserName', {}).get("string", ''),
            group_name=info.get('NickName', {}).get("string", ''),
            member_count=info.get('NewChatroomData', {}).get('MemberCount', 0),
            owner_id=info.get('ChatroomOwner',""),
            platform="wechat857",
        )

    async def get_group_list(self) -> list[str]:
        """
        获取群组列表
        """
        gids = (await self._plugin_instance.wechat857_message_processing_service.get_cache_group()).get(self._platform_id, {})
        return list(gids.keys()) if gids else []

    async def get_member_list(self, group_id: str) -> list[UnifiedMember]:
        """
        获取群组成员列表

        策略：
        1. 调用 API 获取群成员列表
        2. 返回转换后的成员列表

        注意：调用此方法会自动缓存群成员信息
        """
        if await self._member_cache.get_members(group_id) is not None:
            return await self._member_cache.get_members(group_id)
        try:
            # 调用 API 获取群成员
            raw_members = await self.bot.get_chatroom_member_list(group_id)

            result = []
            for raw_member in raw_members:
                # 转换为 UnifiedMember
                unified_member = UnifiedMember(
                    user_id=raw_member.get('UserName'),
                    nickname=raw_member.get('NickName'),
                    card=raw_member.get('DisplayName', raw_member.get('NickName')),
                    avatar_url=raw_member.get('SmallHeadImgUrl'),
                    role="admin" if raw_member.get('ChatroomMemberFlag', 0) > 100 else "member",
                )
                if unified_member:
                    result.append(unified_member)
                    await self._member_cache.add_members(unified_member, group_id)

            return result
        except Exception as e:
            logger.error(f"[WeChat857] 获取成员列表失败: {e}")
            return []

    async def get_member_info(self, group_id: str, user_id: str) -> UnifiedMember | None:
        """
        获取单个成员详情

        支持两种查询模式：
        - 模式1：group_id + user_id → 查询指定群的指定成员
        - 模式2：仅 user_id → 直接查询全局唯一的成员信息
        """
        ms = await self._member_cache.get_members(group_id, user_id)
        return ms[0] if ms else None


    # ==================== IAvatarRepository 实现 ====================

    async def get_user_avatar_url(self, user_id: str, size: int = 100) -> str | None:
        """
        获取用户头像

        需要提供群组 ID 才能获取头像
        """
        gid: str | None = None
        info = await self.get_member_info(gid, user_id)
        if info:
            return info.avatar_url
        return None

    async def get_user_avatar_data(
        self, user_id: str, size: int = 100
    ) -> str | None:
        """暂不提供 Base64 转换服务，优先使用 CDN 链接"""
        return None

    async def get_group_avatar_url(self, group_id: str, size: int = 100) -> str | None:
        """获取群头像"""
        info = await self.bot.get_chatroom_info(group_id)
        if info:
            return info.get('SmallHeadImgUrl')
        return None

    async def set_reaction(
        self, group_id: str, message_id: str, emoji: str | int, is_add: bool = True
    ) -> bool:
        mapping = {289: "🔍", 424: "📊", 124: "✅"}
        return await self.send_text(group_id=group_id, text=emoji)