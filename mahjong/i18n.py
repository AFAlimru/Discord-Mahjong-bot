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
"""多語系（i18n）：載入 language/*.json，t() 取字串；per-user 語言（DB 持久化）。

新增語言：在 language/ 放一個 <code>.json（例如 en.json），鍵照 zh_tw.json 翻譯即可，
缺鍵會自動退回繁中（zh_tw）母本。"""
from __future__ import annotations
import json
import os

DEFAULT = "zh_tw"
_LANG_DIR = os.path.join(os.path.dirname(__file__), "language")
_cache: dict[str, dict] = {}        # code -> {key: template}
_user_cache: dict[str, str] = {}    # user_id -> code


def _load() -> None:
    if _cache:
        return
    try:
        files = sorted(os.listdir(_LANG_DIR))
    except FileNotFoundError:
        files = []
    for fn in files:
        if fn.endswith(".json"):
            try:
                with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                    _cache[fn[:-5]] = json.load(f)
            except Exception as e:
                print(f"[i18n] 載入 {fn} 失敗：{e}")
    if DEFAULT not in _cache:
        _cache.setdefault(DEFAULT, {})


def available() -> list[str]:
    """可用語言代碼，母本（zh_tw）排第一。"""
    _load()
    return sorted(_cache.keys(), key=lambda c: (c != DEFAULT, c))


def lang_name(code: str) -> str:
    _load()
    return _cache.get(code, {}).get("_name", code)


def translate_label(default_lang: str | None = None) -> str:
    """🌐 翻譯按鈕文字：列出「當前顯示語言以外」的其他語言
    （內容已是某語言時，就不需要翻成同一種）。例：預設繁中 → 「🌐 Translate / 翻訳」。"""
    default_lang = default_lang or DEFAULT
    _load()
    words = []
    for code in available():
        if code == default_lang:
            continue
        w = _cache.get(code, {}).get("language.button_translate", code).replace("🌐", "").strip()
        words.append(w)
    return "🌐 " + " / ".join(words) if words else "🌐"


def t(key: str, lang: str | None = None, **kw) -> str:
    """取翻譯字串；找不到鍵或語言則退回繁中母本，再退回鍵本身。支援 {name} 之類的格式化。"""
    _load()
    lang = lang or DEFAULT
    s = _cache.get(lang, {}).get(key)
    if s is None:
        s = _cache.get(DEFAULT, {}).get(key, key)
    if kw:
        from . import tiles as _tiles   # 延後匯入避免套件初始化順序問題
        kw = {k: (_tiles.of(v) if isinstance(v, _tiles.Tile) else v) for k, v in kw.items()}
        try:
            s = s.format(**kw)
        except Exception:
            pass
    return s


def raw(key: str, lang: str) -> str | None:
    """取「指定語言」的翻譯；缺鍵回 None（不退回母本）。指令描述翻譯用。"""
    _load()
    return _cache.get(lang, {}).get(key)


def yaku(name: str, lang: str | None = None) -> str:
    """役種／滿貫等級名稱翻譯（母本直接回原名；其他語言查 yaku.<原名>，缺則回原名）。
    處理「役牌中」這類前綴：先整體查，再退回前綴+牌名分別查。"""
    lang = lang or DEFAULT
    if lang == DEFAULT:
        return name
    _load()
    d = _cache.get(lang, {})
    if f"yaku.{name}" in d:
        return d[f"yaku.{name}"]
    return name


_guild_cache: dict[str, str | None] = {}   # guild_id -> code 或 None（無設定）


def detect_locale(locale) -> str:
    """把 Discord 的 preferred_locale 對應到支援語言碼（無對應回母本）。"""
    loc = str(locale or "")
    if loc.startswith("ja"):
        return "ja"
    if loc.startswith("en"):
        return "en"
    if loc.startswith(("zh", "zh-TW", "zh-CN")):
        return "zh_tw"
    return DEFAULT


def guild_lang(guild_id) -> str | None:
    """該伺服器主要語言（未設定回 None）。快取 + DB。"""
    if guild_id is None:
        return None
    gid = str(guild_id)
    if gid in _guild_cache:
        return _guild_cache[gid]
    from . import db
    try:
        gl = db.get_guild_lang(gid)
    except Exception:
        gl = None
    if gl not in available():
        gl = None
    _guild_cache[gid] = gl
    return gl


def set_guild_lang(guild_id, lang: str | None) -> None:
    _guild_cache[str(guild_id)] = lang
    from . import db
    try:
        db.set_guild_lang(str(guild_id), lang)
    except Exception as e:
        print(f"[i18n] 儲存伺服器語言失敗：{e}")


def get_user_lang(user_id, guild_id=None) -> str:
    """玩家語言：個人設定優先；無個人設定則用伺服器主要語言，再無則母本。快取 + DB。"""
    uid = str(user_id)
    if uid in _user_cache:
        return _user_cache[uid]
    from . import db
    try:
        lang = db.get_user_lang(uid)
    except Exception:
        lang = None
    if lang in available():
        _user_cache[uid] = lang         # 只快取「使用者明確設定」
        return lang
    return guild_lang(guild_id) or DEFAULT   # 伺服器語言可能變動，不快取到個人


def set_user_lang(user_id, lang: str) -> None:
    uid = str(user_id)
    _user_cache[uid] = lang
    from . import db
    try:
        db.set_user_lang(uid, lang)
    except Exception as e:
        print(f"[i18n] 儲存語言失敗：{e}")
