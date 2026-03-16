"""
微信群成员缓存

提供缓存机制，避免频繁调用 get_chatroom_member_list。
使用 cachetools 的 TTLCache 实现。

缓存结构：
- 外层 key: group_id
- 内层 key: user_id (UserName) - 全局唯一，不受群组影响
- value: member_data

使用场景：
1. 通过 group_id 获取群的成员信息并缓存
2. 通过 user_id 获取全局唯一的成员信息
3. 支持两种查询模式：
   - 模式1：get_members(adapter, group_id, user_id) - 查询指定群的指定成员
   - 模式2：get_members(adapter, user_id) - 查询全局唯一的成员信息

边界条件：
- group_id 和 user_id 不能同时为空
- 可以只提供 group_id（返回该群所有成员 ID）
- 可以只提供 user_id（返回该全局成员的信息）
"""

from cachetools import TTLCache
from astrbot.core import logger

from ...domain.value_objects import UnifiedMember

class WeChat857MemberCache:
    """
    微信群成员缓存

    缓存群组成员信息，避免频繁调用 API。
    TTL 默认为 1 小时。
    """

    def __init__(self, ttl: int = 3600):
        # {group_id: {user_id: member_data}}
        self._cache: TTLCache = TTLCache(maxsize=10240, ttl=ttl)

    async def add_members(self, user_info: UnifiedMember, group_id: str = None) -> None:
        # {group_id: {user_id:{user_info}}}
        if group_id:
            group_cache_key = f"wechat:group:{group_id}"
            self._cache[group_cache_key] = self._cache.get(group_cache_key) or dict()
            self._cache[group_cache_key][user_info.user_id] = user_info

        # {user_id: [user_info]}
        members_cache_key = f"wechat:member:{user_info.user_id}"
        self._cache[members_cache_key] = user_info

    async def get_members(self, group_id: str = None, user_id: str = None) -> list[UnifiedMember] | None:
        """
        获取群组成员信息
        """
        # 检查边界条件
        if group_id is None and user_id is None:
            logger.warning("[WeChat857MemberCache] group_id 和 user_id 不能同时为空")
            return None

        # 模式2：仅提供 group_id（user_id 为空）→ 返回该群所有成员的 ID
        group_cache_key = f"wechat:group:{group_id}"
        group_members = (self._cache.get(group_cache_key) or dict()) if group_id else dict()
        if group_members and not user_id:
            return list(group_members.values())

        if group_members.get(user_id):
            return [group_members.get(user_id)]
        elif m := self._cache.get(f"wechat:member:{user_id}"):
            return [m]
        else:
            return None

    async def invalidate(self, group_id: str) -> None:
        """失效指定群组的缓存"""
        cache_key = f"wechat:members:{group_id}"
        if cache_key in self._cache:
            del self._cache[cache_key]

    async def invalidate_all(self) -> None:
        """清空所有缓存"""
        self._cache.clear()
