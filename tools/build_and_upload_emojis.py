# Suzume Tsuk — Discord 日本麻將機器人
# Copyright (C) 2026  AFAlimru
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
"""建立麻將牌的自訂表情並上傳到 Discord「應用程式表情」（直立牌＋橫放牌）。

流程：
  1. 從 FluffyStuff/riichi-mahjong-tiles（CC0）下載牌胚 Front.svg 與各牌面 SVG。
  2. 用 cairosvg／PyMuPDF 光柵化，Pillow 把「面」疊到「牌胚」上，置中貼進 256×256 透明方框。
  3. 每張牌另做一張「橫放」版（逆時針轉 90°、貼底置中，與直立牌排在一起時落在同一條底線），
     名稱 mj_<code>_s（黑色風 mjb_<code>_s），JSON 鍵 <code>_s；立直宣言牌與副露裡別人打的那張用。
  4. 以 bot token 上傳成應用程式表情，在所有伺服器都能用，並合併寫出
     mahjong/tile_emojis.json（{ "1m": "<:mj_1m:id>", "1m_s": "<:mj_1m_s:id>", ... }）。

用法（在專案根目錄，已啟用 venv）：
    pip install cairosvg pillow          # 若尚未安裝（Linux 另需系統 libcairo2）
    python tools/build_and_upload_emojis.py [regular|black] [選項]

預設是「安全增量」：只上傳應用程式上還沒有的表情，絕不刪除既有表情，
再把「既有＋新增」合併寫回 JSON（既有條目原封不動），所以線上機器人用的 ID 不會失效。
上傳前會檢查本機 JSON 與應用程式上同名表情的 ID 一致（換了應用程式或 JSON 過期就中止）。

選項：
    --force          略過 ID 一致性檢查（確定要照目前應用程式的狀態合併時才用）
    --rebuild        舊的全部重建：刪掉該風格全部表情再重新上傳。ID 會全部改變，
                     之後必須把新的 tile_emojis*.json 重新部署到伺服器，否則線上牌面會壞掉
    --dry-run [目錄] 不登入、不上傳，只把全部 PNG（直立＋橫放）產到目錄供預覽
                     （預設 tools/.tile_preview/<風格>/）

需要 discord.py >= 2.4（應用程式表情 API）。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request

# Windows 主控台預設 cp950，印 ✓ 之類字元會炸；強制 UTF-8（印不出就以 ? 代替）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 專案匯入（讀 token）────────────────────────────────────────
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from mahjong import config  # noqa: E402

# ── 相依套件檢查 ──────────────────────────────────────────────
try:
    from PIL import Image  # type: ignore
    import discord  # type: ignore
except ImportError as e:
    sys.exit(f"缺少相依套件：{e.name}。請先 `pip install pillow discord.py`。")

# SVG 光柵化後端：優先 PyMuPDF（pip 直接裝、免系統 Cairo，Windows 友善），
# 其次 cairosvg（Linux 上 `apt install libcairo2` 即可）。
_BACKEND = None
try:
    import fitz  # PyMuPDF  # type: ignore
    _BACKEND = "pymupdf"
except ImportError:
    try:
        import cairosvg  # type: ignore
        _BACKEND = "cairosvg"
    except ImportError:
        sys.exit("找不到 SVG 光柵化後端。請 `pip install pymupdf`（推薦，免 Cairo）"
                 "或 `pip install cairosvg`（Linux 另需 libcairo2）。")

# ── 參數：風格（預設 regular → mj_*；black → Black 資料夾、mjb_*、tile_emojis_black.json，
#    作段位獎勵牌風）與上傳模式 ──
_ap = argparse.ArgumentParser(description="建立並上傳麻將牌應用程式表情（直立＋橫放）")
_ap.add_argument("style", nargs="?", default="regular", choices=("regular", "black"))
_ap.add_argument("--force", action="store_true", help="略過本機 JSON 與應用程式表情的 ID 一致性檢查")
_ap.add_argument("--rebuild", action="store_true", help="刪除該風格全部表情後重建（ID 會變，需重新部署 JSON）")
_ap.add_argument("--dry-run", nargs="?", const=True, default=None, metavar="DIR",
                 help="不登入，只把 PNG 產到資料夾預覽（預設 tools/.tile_preview/<風格>/）")
ARGS = _ap.parse_args()
STYLE = ARGS.style
FOLDER   = "Black" if STYLE == "black" else "Regular"
PREFIX   = "mjb_" if STYLE == "black" else "mj_"
OUT_JSON = "tile_emojis_black.json" if STYLE == "black" else "tile_emojis.json"

# ── FluffyStuff 檔名對照：tile.code → 牌面 SVG 檔名（None＝牌本身就是完整圖）──
# 下載來源：jsDelivr CDN 為主（不會像 GitHub raw 對整個 IP 限流），raw 備援
MIRRORS = [
    f"https://cdn.jsdelivr.net/gh/FluffyStuff/riichi-mahjong-tiles@master/{FOLDER}/",
    f"https://raw.githubusercontent.com/FluffyStuff/riichi-mahjong-tiles/master/{FOLDER}/",
]
FRONT = "Front"  # 牌胚

FACE = {}
for i in range(1, 10):
    FACE[f"{i}m"] = f"Man{i}"
    FACE[f"{i}p"] = f"Pin{i}"
    FACE[f"{i}s"] = f"Sou{i}"
FACE["0m"] = "Man5-Dora"   # 紅5萬
FACE["0p"] = "Pin5-Dora"   # 紅5筒
FACE["0s"] = "Sou5-Dora"   # 紅5條
FACE["1z"] = "Ton"         # 東
FACE["2z"] = "Nan"         # 南
FACE["3z"] = "Shaa"        # 西
FACE["4z"] = "Pei"         # 北
FACE["5z"] = "Haku"        # 白
FACE["6z"] = "Hatsu"       # 發
FACE["7z"] = "Chun"        # 中
# 牌背（回放／暗槓用），本身即完整圖，不需疊牌胚
FULL = {"back": "Back"}

_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tile_cache", FOLDER)


def _fetch_svg(name: str) -> bytes:
    os.makedirs(_CACHE, exist_ok=True)
    path = os.path.join(_CACHE, name + ".svg")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    print(f"  下載 {name}.svg …")
    import time
    data = None
    last_err = None
    for attempt in range(4):
        for base in MIRRORS:   # 每輪輪流試各鏡像
            try:
                with urllib.request.urlopen(base + name + ".svg", timeout=30) as r:
                    data = r.read()
                break
            except Exception as e:
                last_err = e
        if data is not None:
            break
        wait = 5 * (attempt + 1)
        print(f"    下載失敗（{last_err}），{wait}s 後重試 …")
        time.sleep(wait)
    if data is None:
        raise RuntimeError(f"下載 {name}.svg 失敗：{last_err}")
    time.sleep(0.2)   # 放慢一點，避免觸發限流
    with open(path, "wb") as f:
        f.write(data)
    return data


def _raster(svg: bytes, w: int, h: int) -> "Image.Image":
    """把 SVG bytes 光柵化成 w×h 的 RGBA 圖（透明背景）。"""
    if _BACKEND == "pymupdf":
        doc = fitz.open(stream=svg, filetype="svg")
        page = doc[0]
        zoom_x = w / page.rect.width
        zoom_y = h / page.rect.height
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom_x, zoom_y), alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        doc.close()
        return img
    png = cairosvg.svg2png(bytestring=svg, output_width=w, output_height=h)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _body_color(name: str) -> tuple:
    """取樣原 SVG 中央的顏色當牌底色（Front=米白、Back=紅）。"""
    img = _raster(_fetch_svg(name), 300, 400)
    r, g, b, _ = img.getpixel((150, 200))
    return (r, g, b, 255)


def _tile_body(w: int, h: int, fill: tuple) -> "Image.Image":
    """自畫滿版圓角牌底：FluffyStuff 的 Front/Back 自帶右下陰影、且牌面符號會
    略溢出牌胚，縮成表情會看到右下角黑斑——改成自己畫就沒這些問題。"""
    from PIL import ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = round(min(w, h) * 0.18)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=fill,
                        outline=(0, 0, 0, 45), width=3)
    return img


def _tile_image(code: str) -> "Image.Image":
    """該牌直立的 tw×256 透明圖（3:4 比例，高 256）。"""
    W, H = 300, 400  # FluffyStuff 原生 viewBox
    if code in FULL:
        tile = _tile_body(W, H, _body_color(FULL[code]))
    else:
        tile = _tile_body(W, H, _body_color(FRONT))
        # 面符號縮 88% 置中：原素材的面畫到滿版，直接疊會貼齊牌邊（如紅5條）
        face = _raster(_fetch_svg(FACE[code]), W, H)
        fw, fh = round(W * 0.88), round(H * 0.88)
        face = face.resize((fw, fh), Image.LANCZOS)
        pad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pad.paste(face, ((W - fw) // 2, (H - fh) // 2), face)
        tile = Image.alpha_composite(tile, pad)
    th = 256
    tw = round(W * th / H)
    return tile.resize((tw, th), Image.LANCZOS)


def build_png(code: str, sideways: bool = False) -> bytes:
    """回傳該牌的 256×256 透明 PNG bytes。
    直立：置中貼進方框（寬約 192）。橫放：逆時針轉 90°，貼底、水平置中——
    與直立牌排成一列時，橫牌落在同一條底線上，像真的橫放的麻將牌。"""
    tile = _tile_image(code)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    if sideways:
        tile = tile.rotate(90, expand=True)            # PIL：逆時針
        canvas.paste(tile, ((256 - tile.width) // 2, 256 - tile.height), tile)
    else:
        canvas.paste(tile, ((256 - tile.width) // 2, 0), tile)
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def build_all() -> dict:
    """全部要上傳的圖：{ 鍵: PNG }。直立（含牌背）＋ 每張牌面的橫放版（<code>_s，不含牌背）。"""
    pngs = {code: build_png(code) for code in list(FACE) + list(FULL)}
    for code in FACE:
        pngs[f"{code}_s"] = build_png(code, sideways=True)
    return pngs


def _is_mine(name: str) -> bool:
    """該應用程式表情是否屬於本風格（regular 只算 mj_*，排除 mjb_*）。"""
    if PREFIX == "mj_":
        return name.startswith("mj_") and not name.startswith("mjb_")
    return name.startswith(PREFIX)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}


_ID_RE = re.compile(r"<a?:\w+:(\d+)>")


def _id_of(s: str):
    m = _ID_RE.match(s)
    return m.group(1) if m else None


class Uploader(discord.Client):
    def __init__(self, pngs, **kw):
        super().__init__(**kw)
        self.pngs = pngs
        self.ok = False

    async def on_ready(self):
        try:
            print(f"已登入：{self.user}（app id {self.application_id}）")
            pngs = self.pngs
            out = os.path.join(_ROOT, "mahjong", OUT_JSON)
            existing = [em for em in await self.fetch_application_emojis() if _is_mine(em.name)]
            by_name = {em.name: em for em in existing}

            if ARGS.rebuild:
                print("!! --rebuild：將刪除並重建本風格全部表情，ID 會全部改變。")
                print(f"!! 完成後務必把新的 {OUT_JSON} 重新部署到伺服器，否則線上機器人的牌面會壞掉。")
                for em in existing:
                    await em.delete()
                by_name = {}
                mapping: dict = {}
            else:
                local = _load_json(out)
                # 安全檢查：本機 JSON 與應用程式上的表情 ID 必須一致，否則多半是換了應用程式或 JSON 過期
                bad = []
                for code, val in local.items():
                    if code.endswith("_s"):
                        continue
                    em = by_name.get(PREFIX + code)
                    if em is None or _id_of(val) != str(em.id):
                        bad.append(code)
                if bad and not ARGS.force:
                    print(f"\n中止：{OUT_JSON} 與這個應用程式上的表情 ID 對不上"
                          f"（{len(bad)} 張，例如 {', '.join(bad[:5])}）。")
                    print("可能是用了不同的應用程式 token，或 JSON 已過期。什麼都沒有上傳。")
                    print("確定要照目前應用程式的狀態處理就加 --force；要整批重建請用 --rebuild。")
                    return
                # 以應用程式上實際存在的表情為準，疊在本機 JSON 上（既有條目原封不動）
                mapping = dict(local)
                for em in existing:
                    mapping.setdefault(em.name[len(PREFIX):], f"<:{em.name}:{em.id}>")

            todo = [(c, d) for c, d in pngs.items() if f"{PREFIX}{c}" not in by_name]
            print(f"已存在 {len(pngs) - len(todo)} 張，需上傳 {len(todo)} 張 …")
            for code, data in todo:
                em = await self.create_application_emoji(name=f"{PREFIX}{code}", image=data)
                mapping[code] = f"<:{em.name}:{em.id}>"
                print(f"  ✓ {code} → {mapping[code]}")

            with open(out, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            print(f"\n完成，JSON 共 {len(mapping)} 筆（新增 {len(todo)}）。已寫出 {out}")
            print("重啟機器人（或呼叫 tiles.reload()）即可套用；新增的橫牌表情記得把 JSON 一併部署到伺服器。")
            self.ok = True
        finally:
            await self.close()


def main():
    if ARGS.dry_run is not None:
        d = ARGS.dry_run if isinstance(ARGS.dry_run, str) else os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".tile_preview", STYLE)
        os.makedirs(d, exist_ok=True)
        print(f"預覽模式：產生牌圖到 {d}（不登入、不上傳）…")
        for key, data in build_all().items():
            with open(os.path.join(d, f"{PREFIX}{key}.png"), "wb") as f:
                f.write(data)
        print("完成。")
        return
    if not config.TOKEN:
        sys.exit("找不到 DISCORD_TOKEN，請確認 .env。")
    # 先在登入前把圖全部產好（下載＋光柵化都是同步操作，放進事件圈會堵住心跳）
    print("預先產生牌圖 …")
    pngs = build_all()
    print(f"完成 {len(pngs)} 張，開始登入 …")
    Uploader(pngs, intents=discord.Intents.none()).run(config.TOKEN)


if __name__ == "__main__":
    main()
