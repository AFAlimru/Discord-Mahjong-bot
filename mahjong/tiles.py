# Suzume Tsuk — Discord 日本麻將機器人
# Copyright (C) 2026  AFAlimru
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
"""牌面自訂表情：把牌渲染成上傳到 Discord 的自訂表情（若已建立），否則退回 unicode。

牌風（skin）：
    預設風格讀 `tile_emojis.json`（mj_*），黑色風格讀 `tile_emojis_black.json`（mjb_*），
    皆由 `tools/build_and_upload_emojis.py [black]` 產生。格式：
        { "1m": "<:mj_1m:123456789>", "7z": "<:mj_7z:...>", ... }
    以 contextvar 決定「當前渲染用的牌風」：私人面板渲染前呼叫 `set_skin(玩家偏好)`，
    公開畫面用預設。黑色缺圖（或檔案不存在）時自動退回預設，再退回 unicode。
"""
from __future__ import annotations
import contextvars
import json
import os
import re
from typing import Iterable, Optional, Union

from .engine import Tile, Suit

_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")
_DIR = os.path.dirname(__file__)

SKINS = {"default": "tile_emojis.json", "black": "tile_emojis_black.json"}

# skin → {"code": {tile.code: 表情}, "uni": {str(Tile): 表情}}
_MAPS: dict[str, dict[str, dict[str, str]]] = {}

_skin_var: contextvars.ContextVar[str] = contextvars.ContextVar("tile_skin", default="default")


def _load() -> None:
    global _MAPS
    _MAPS = {}
    combos = [
        (Suit.MAN, range(1, 10)), (Suit.PIN, range(1, 10)),
        (Suit.SOU, range(1, 10)), (Suit.WIND, range(1, 5)),
        (Suit.DRAGON, range(1, 4)),
    ]
    for skin, fname in SKINS.items():
        by_code: dict[str, str] = {}
        try:
            with open(os.path.join(_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                by_code = {str(k): str(v) for k, v in data.items()}
        except Exception:
            by_code = {}
        by_uni: dict[str, str] = {}
        for suit, rng in combos:   # unicode → 表情（回放／日誌裡存的是 str(Tile)）
            for v in rng:
                reds = (False, True) if (v == 5 and suit in (Suit.MAN, Suit.PIN, Suit.SOU)) else (False,)
                for red in reds:
                    t = Tile(suit, v, red)
                    e = by_code.get(t.code)
                    if e:
                        by_uni[str(t)] = e
        _MAPS[skin] = {"code": by_code, "uni": by_uni}


_load()


def _cur() -> dict[str, dict[str, str]]:
    """當前牌風的對照表；該風缺整組時退回預設。"""
    m = _MAPS.get(_skin_var.get())
    if not m or not m["code"]:
        m = _MAPS.get("default", {"code": {}, "uni": {}})
    return m


def _get_code(code: str) -> Optional[str]:
    """取牌 code 的表情：當前牌風缺這張時退回預設風。"""
    m = _cur()
    e = m["code"].get(code)
    if e:
        return e
    return _MAPS.get("default", {}).get("code", {}).get(code)


def set_skin(skin: str | None) -> None:
    """設定當前（本協程脈絡）渲染用的牌風；None／未知值＝預設。"""
    _skin_var.set(skin if skin in SKINS else "default")


def available_skins() -> list[str]:
    """已載入（json 存在且非空）的牌風清單。"""
    return [s for s, m in _MAPS.items() if m["code"]]


def enabled() -> bool:
    """是否已載入自訂表情（預設風）。"""
    return bool(_MAPS.get("default", {}).get("code"))


def reload() -> None:
    """重新載入 tile_emojis*.json（上傳後免重啟即可套用）。"""
    _load()


def of(tile: Union[Tile, str]) -> str:
    """單張牌的顯示字串：有自訂表情用表情，否則退回 unicode。

    tile 可為 Tile 物件，或已是 str(Tile) 的 unicode 字串（回放／日誌）。
    """
    if isinstance(tile, Tile):
        return _get_code(tile.code) or str(tile)
    m = _cur()
    e = m["uni"].get(tile)
    if e:
        return e
    return _MAPS.get("default", {}).get("uni", {}).get(tile, tile)


def render(tiles: Iterable, sep: str = "") -> str:
    """把一串牌渲染成顯示字串。自訂表情本身留白，預設無間隔。"""
    return sep.join(of(t) for t in tiles)


_PURE_TILES = re.compile(r"^[\U0001F000-\U0001F02B🔴\s|｜｜+＋—-]+$")


def is_tiles(s: str) -> bool:
    """字串是否純由牌面 unicode（含紅5 🔴 前綴）與分隔符組成。"""
    return bool(s) and bool(_PURE_TILES.match(s))


_MELD_NAME = re.compile(r"(?:吃|碰|槓|暗槓|加槓|拔北)\s*:?\s*\[")


def emojify(s: str) -> str:
    """把字串中的 unicode 牌（含 🔴X 紅5）全部換成自訂表情（依當前牌風）；
    未上傳表情時原樣返回。只做逐牌替換，不會動到其他文字。
    副露的「碰:[…]」前綴一併拿掉（只顯示 […]）。"""
    if not s:
        return s
    s = _MELD_NAME.sub("[", s)
    uni = _cur()["uni"]
    if not uni:
        return s
    fallback = _MAPS.get("default", {}).get("uni", {})
    keys = set(uni) | set(fallback)
    for k in sorted(keys, key=len, reverse=True):   # 先換 🔴X 再換單字
        if k in s:
            s = s.replace(k, uni.get(k) or fallback[k])
    s = s.replace("🀫", back())   # 蓋牌（暗槓／未翻寶牌）→ 牌背表情
    return _tidy_spaces(s)


_EMO = r"<a?:\w+:\d+>"
_RE_EMO_GAP  = re.compile(rf"({_EMO})[ 　]+(?={_EMO})")   # 表情之間的空白
_RE_BEFORE   = re.compile(r"[ 　]+(?=[｜|\]])")           # ｜／] 之前的空白
_RE_AFTER    = re.compile(r"(?<=[\[｜|])[ 　]+")          # ［／｜ 之後的空白


def _tidy_spaces(s: str) -> str:
    """把表情牌之間、以及緊貼｜／［］的空白收掉（牌自帶留白）。
    句子裡單張牌前後（如「碰了 🀄 打出」）不受影響——只有兩張牌相鄰時才收。"""
    s = _RE_EMO_GAP.sub(r"\1", s)
    s = _RE_EMO_GAP.sub(r"\1", s)   # 連續多張需再掃一次
    s = _RE_BEFORE.sub("", s)
    s = _RE_AFTER.sub("", s)
    return s


def emojify_hand(s: str) -> str:
    """和牌牌型專用：轉表情後把「所有」空白清光（牌型字串只有牌／副露／｜，沒有詞句，
    可安全全收）。用於最高和了、和牌儀式、結果、回放的手牌顯示。"""
    return re.sub(r"[ 　]+", "", emojify(s))


def back() -> str:
    """牌背（暗槓兩端、未翻寶牌）：有自訂表情用表情，否則 🀫。"""
    return _get_code("back") or "🀫"


def meld(m) -> str:
    """把一個副露（Meld）渲染成顯示字串：只顯示 […]，不加「碰/吃」字樣（暗槓兩端蓋牌）。"""
    if m.meld_type == 4 and len(m.tiles) == 4:   # 暗槓：頭尾蓋牌
        body = back() + of(m.tiles[1]) + of(m.tiles[2]) + back()
    else:
        body = "".join(of(t) for t in m.tiles)
    return f"[{body}]"


def partial(tile: Union[Tile, str]) -> Optional["object"]:
    """回傳按鈕用的 discord.PartialEmoji；沒有自訂表情時回 None（呼叫端退回文字標籤）。"""
    code = tile.code if isinstance(tile, Tile) else tile
    s = _get_code(code)
    if not s:
        return None
    m = _EMOJI_RE.match(s)
    if not m:
        return None
    import discord
    return discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
