"""Вход юзербота через @jsjeigiejwhnewbot (делегирует в tracker_bot)."""

from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from tracker import TRACKER_VERSION, data_dir
from tracker_bot import ChannelStore, ControlBot, channel_file_path


async def bot_login_wizard(cfg) -> TelegramClient:
    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()
    store = ChannelStore(channel_file_path(data_dir()))
    bot = ControlBot(
        cfg.bot_token,
        client,
        cfg.session_file,
        store,
        tracker_version=TRACKER_VERSION,
    )
    await bot.start()
    await bot.wait_login()
    return client
