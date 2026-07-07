# Suzume Tsuk — Discord 日本麻將機器人
# Copyright (C) 2026  AFAlimru
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
"""一次性工具：建立麻將牌的自訂表情並上傳到 Discord「應用程式表情」。

流程：
  1. 從 FluffyStuff/riichi-mahjong-tiles（CC0）下載牌胚 Front.svg 與各牌面 SVG。
  2. 用 cairosvg 光柵化，Pillow 把「面」疊到「牌胚」上，置中貼進 256×256 透明方框。
  3. 以 bot token 上傳成應用程式表情（名稱 mj_<code>），在所有伺服器都能用。
  4. 產出 mahjong/tile_emojis.json（{ "1m": "<:mj_1m:id>", ... }）供機器人載入。

用法（在專案根目錄，已啟用 venv）：
    pip install cairosvg pillow          # 若尚未安裝（Linux 另需系統 libcairo2）
    python tools/build_and_upload_emojis.py

需要 discord.py >= 2.4（應用程式表情 API）。可重複執行：會先刪掉舊的 mj_* 再重建。
"""
from __future__ import annotations

import io
import json
import os
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

# ── 風格：預設 Regular（mj_*）；`python tools/build_and_upload_emojis.py black`
#    上傳黑色風（Black 資料夾、mjb_*、寫出 tile_emojis_black.json，作段位獎勵牌風）──
STYLE = (sys.argv[1].lower() if len(sys.argv) > 1 else "regular")
if STYLE not in ("regular", "black"):
    sys.exit(f"未知風格：{STYLE}（可用 regular / black）")
FOLDER   = "Black" if STYLE == "black" else "Regular"
PREFIX   = "mjb_" if STYLE == "black" else "mj_"
OUT_JSON = "tile_emojis_black.json" if STYLE == "black" else "tile_emojis.json"

# ── FluffyStuff 檔名對照：tile.code → 牌面 SVG 檔名（None＝牌本身就是完整圖）──
BASE = f"https://raw.githubusercontent.com/FluffyStuff/riichi-mahjong-tiles/master/{FOLDER}/"
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
    for attempt in range(4):   # GitHub raw 偶爾 429 限流：重試 + 退避
        try:
            with urllib.request.urlopen(BASE + name + ".svg", timeout=30) as r:
                data = r.read()
            break
        except Exception as e:
            if attempt == 3:
                raise
            wait = 10 * (attempt + 1)
            print(f"    下載失敗（{e}），{wait}s 後重試 …")
            time.sleep(wait)
    time.sleep(0.4)   # 放慢一點，避免觸發限流
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


def build_png(code: str) -> bytes:
    """回傳該牌的 256×256 透明 PNG bytes。"""
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
    # 縮到高 256、置中貼進 256×256 透明方框（維持 3:4 比例，寬約 192）
    th = 256
    tw = round(W * th / H)
    tile = tile.resize((tw, th), Image.LANCZOS)
    canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    canvas.paste(tile, ((256 - tw) // 2, 0), tile)
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


class Uploader(discord.Client):
    async def on_ready(self):
        try:
            print(f"已登入：{self.user}（app id {self.application_id}）")
            print("預先產生牌圖 …")
            pngs = {code: build_png(code) for code in list(FACE) + list(FULL)}

            print(f"清除舊的 {PREFIX}* 應用程式表情 …")
            existing = await self.fetch_application_emojis()
            for em in existing:
                # 只清本風格：regular 清 mj_*（排除 mjb_*），black 清 mjb_*
                if PREFIX == "mj_":
                    hit = em.name.startswith("mj_") and not em.name.startswith("mjb_")
                else:
                    hit = em.name.startswith(PREFIX)
                if hit:
                    await em.delete()

            mapping: dict[str, str] = {}
            print("上傳中 …")
            for code, data in pngs.items():
                em = await self.create_application_emoji(name=f"{PREFIX}{code}", image=data)
                mapping[code] = f"<:{em.name}:{em.id}>"
                print(f"  ✓ {code} → {mapping[code]}")

            out = os.path.join(_ROOT, "mahjong", OUT_JSON)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            print(f"\n完成，共 {len(mapping)} 張。已寫出 {out}")
            print("重啟機器人（或呼叫 tiles.reload()）即可套用。")
        finally:
            await self.close()


def main():
    if not config.TOKEN:
        sys.exit("找不到 DISCORD_TOKEN，請確認 .env。")
    Uploader(intents=discord.Intents.none()).run(config.TOKEN)


if __name__ == "__main__":
    main()
