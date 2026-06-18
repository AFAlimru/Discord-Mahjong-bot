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


def t(key: str, lang: str | None = None, **kw) -> str:
    """取翻譯字串；找不到鍵或語言則退回繁中母本，再退回鍵本身。支援 {name} 之類的格式化。"""
    _load()
    lang = lang or DEFAULT
    s = _cache.get(lang, {}).get(key)
    if s is None:
        s = _cache.get(DEFAULT, {}).get(key, key)
    if kw:
        try:
            s = s.format(**kw)
        except Exception:
            pass
    return s


def get_user_lang(user_id) -> str:
    """玩家語言（無設定回母本）。快取 + DB。"""
    uid = str(user_id)
    if uid in _user_cache:
        return _user_cache[uid]
    from . import db
    try:
        lang = db.get_user_lang(uid)
    except Exception:
        lang = None
    if lang not in available():
        lang = DEFAULT
    _user_cache[uid] = lang
    return lang


def set_user_lang(user_id, lang: str) -> None:
    uid = str(user_id)
    _user_cache[uid] = lang
    from . import db
    try:
        db.set_user_lang(uid, lang)
    except Exception as e:
        print(f"[i18n] 儲存語言失敗：{e}")
