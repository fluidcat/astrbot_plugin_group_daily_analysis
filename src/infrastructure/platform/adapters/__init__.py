# 平台适配器
from .discord_adapter import DiscordAdapter
from .onebot_adapter import OneBotAdapter
from .telethon_adapter import TelethonAdapter
from .wx857_adapter import Wx857Adapter

__all__ = ["OneBotAdapter", "DiscordAdapter", "TelethonAdapter", "Wx857Adapter"]
