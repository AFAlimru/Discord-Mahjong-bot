# Suzume Tsuk — Discord 日本麻將機器人
# Copyright (C) 2026  AFAlimru
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.  You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""多語言大廳：一台伺服器可以有好幾個大廳（/setup create 建的一套），每種語言一個。

- 從哪個大廳（它的類別、大廳、開房頻道、配對語音、類別內的對局頻道）開房，就用那個大廳的語言。
- 伺服器只有一個大廳（或頻道不屬於任何大廳）時，用伺服器主要語言。
"""
from __future__ import annotations

from . import db


def all_hubs(guild_id) -> list[dict]:
    if guild_id is None:
        return []
    try:
        return db.get_hubs(str(guild_id))
    except Exception:
        return []


def for_lang(guild_id, lang: str) -> dict | None:
    """這個語言的大廳（沒有＝None）。"""
    return next((h for h in all_hubs(guild_id) if h.get("lang") == lang), None)


def by_id(guild_id, hub_id) -> dict | None:
    return next((h for h in all_hubs(guild_id) if h.get("id") == hub_id), None)


def of_channel(guild_id, channel, hubs: list[dict] | None = None) -> dict | None:
    """頻道屬於哪個大廳：是它的大廳／開房頻道／配對語音，或在它的類別裡（討論串看母頻道）。"""
    if channel is None:
        return None
    hubs = all_hubs(guild_id) if hubs is None else hubs
    if not hubs:
        return None
    ids  = {str(getattr(channel, "id", ""))}
    cats = {str(getattr(channel, "category_id", "") or "")}
    parent = getattr(channel, "parent", None)          # 討論串 → 看它的母頻道
    if parent is not None:
        ids.add(str(getattr(parent, "id", "")))
        cats.add(str(getattr(parent, "category_id", "") or ""))
    cats.discard("")
    for h in hubs:
        own = {h.get("lobby_channel_id"), h.get("hub_voice_id"), h.get("play_channel_id")}
        if ids & {x for x in own if x} or (h.get("category_id") and h["category_id"] in cats):
            return h
    return None


def lang_for(guild_id, channel=None) -> str:
    """在這個頻道該用的語言：伺服器有好幾個大廳且頻道屬於其中一個＝那個大廳的語言；否則＝伺服器主要語言。"""
    from . import i18n
    hubs = all_hubs(guild_id)
    if len(hubs) > 1:
        h = of_channel(guild_id, channel, hubs)
        if h and h.get("lang") in i18n.available():
            return h["lang"]
    return i18n.guild_lang(guild_id) or i18n.DEFAULT


def lobby_ids(guild_id) -> set[str]:
    """所有大廳頻道的 ID（大廳打字即刪用）。"""
    return {h["lobby_channel_id"] for h in all_hubs(guild_id) if h.get("lobby_channel_id")}


def play_ids(guild_id) -> set[str]:
    """所有大廳的開房文字頻道 ID。"""
    return {h["play_channel_id"] for h in all_hubs(guild_id) if h.get("play_channel_id")}
