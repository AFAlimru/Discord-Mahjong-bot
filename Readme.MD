# 🀄 Discord 日本麻將機器人

> 🤖 本專案以 **vibe coding**（AI 輔助開發）方式完成。

在 Discord 上遊玩完整規則的日本麻將（立直麻將）。支援四麻／三麻、東風戰／半莊戰、AI 對局、觀戰模式，並以單一面板訊息持續編輯（不洗版）。

> 規則細節（役種、符點、精算、振聽等）請見 **[Rules guide.MD](Rules%20guide.MD)**。本檔只說明專案結構與使用方式。

---

## ✨ 功能總覽

- **完整計分**：42 役 + 七對子／國士、副露減飜、無役不可和、符數與點數（標準精算）。
- **情境役**：立直／一發／兩立直、海底／河底、嶺上開花、槍槓、天和／地和。
- **振聽**：捨牌／同巡／立直振聽，全程強制。
- **三麻**：108 張牌、拔北（北寶牌）、禁止吃牌。
- **多局對戰**：東風戰／半莊戰、連莊／本場、立直棒、流局聽牌罰符、擊飛、賽末順位馬點精算。
- **途中流局**：九種九牌、四風連打、四家立直、四槓散了、三家和（莊家連莊、本場 +1）。
- **加槓／搶槓、暗槓／大明槓**。
- **AI 玩家**：純內建啟發式（無外接 API），三麻會自動拔北。
- **觀戰模式**：`/mahjong watch` 全 AI 自動對局、公開手牌。
- **公平性**：採麻雀一番街公開方案 — 密碼學安全洗牌、牌山編碼 + SHA-256，可逐局自行驗證。
- **穩定性**：機器人重啟後可回復未結算的對局（玩家自行選擇繼續或結算），對局不會卡死。
- **戰績**：每場結束記錄順位、得失、自摸／榮和／放銃等數據（分三麻／四麻）。
- **多語系**：繁體中文／日文，`/language` 切換個人語言；公開共享訊息（大廳／觀戰）附「🌐 翻譯」按鈕。
- **體驗**：單一面板訊息持續編輯、即時倒數、聽牌／可立直等敏感資訊只在玩家自己的私人討論串、和牌逐項揭曉役種的儀式感。

---

## 🗂️ 檔案結構

```
Suzume Tsuk/
├── run.py              # 啟動入口：建立 bot/tree、事件處理，python run.py
├── mahjong/            # 主套件
│   ├── commands.py     # /mahjong 指令群組與大廳 LobbyView；register(tree) 掛上指令
│   ├── flow.py         # 對局流程：討論串/反應/單局/多局/和牌儀式/開局
│   ├── ui.py           # Discord 按鈕與視圖、操作說明文字
│   ├── render.py       # 文字版面渲染（牌桌/手牌面板/牌河/點數…）
│   ├── rules.py        # 純遊戲邏輯：牌工具/AI/和牌評估/聽牌振聽
│   ├── engine.py       # 牌 / 牌山(SHA-256) / 牌局狀態 / 和牌型 / 聽牌
│   ├── scoring.py      # 役種判定 + 符數 + 點數
│   ├── settlement.py   # 對局結算 + 金流馬點精算
│   ├── views.py        # 設定面板（戰長/擊飛/三麻/規則/數值）
│   ├── db.py           # SQLite 持久化（games / players / stats / game_log / game_records）
│   ├── config.py       # 環境變數與常數
│   ├── state.py        # 跨模組共享的記憶體狀態
│   └── rooms.py        # 房間編號（房間#0001）
├── Rules guide.MD      # 規則速查（役種、符點、精算、流程）
├── Readme.MD           # 本檔
├── requirements.txt    # 依賴
├── .env                # DISCORD_TOKEN 等
└── mahjong.db          # SQLite 資料庫（執行後產生）
```

### 模組職責

| 模組 | 內容 |
|------|------|
| `run.py` | `bot`/`tree`、`on_message`/`on_ready`、入口 `run()`（啟動檔，位於專案根目錄） |
| `commands.py` | `/mahjong start/join/end/status/watch/stats/help`、`/language`、大廳 LobbyView；`register(tree)` 掛上指令 |
| `flow.py` | 討論串建立/清理、反應收集、輪到行動、單/多局迴圈、和牌儀式、`launch_game` |
| `rules.py` | 牌工具、AI、`evaluate_win`、`tenpai_advice`/`is_furiten`（純邏輯，不依賴 discord） |
| `engine.py` | `Tile`/`GameState`/`PlayerState`、牌山生成與編碼、`is_complete`/`is_tenpai`/待牌 |
| `scoring.py` | `score_hand()` → 役、飜、符、點；`WinContext` 帶入立直/寶牌/規則旗標 |
| `settlement.py` | `apply_ron/tsumo/ryuukyoku`、`advance_after_*`、`final_standings`（馬點） |
| `rooms.py` | 房間編號配發、進行中房間追蹤 |
| `db.py` | 牌局、玩家、戰績、牌譜的 CRUD |

---

## 🚀 安裝與啟動

建議在**虛擬環境**中執行（避免污染系統 Python）。需要 **Python 3.10+**。

```bash
# 1. 建立並啟用虛擬環境
python -m venv .venv
#    Windows (PowerShell):  .venv\Scripts\Activate.ps1
#    Windows (CMD):         .venv\Scripts\activate.bat
#    macOS / Linux:         source .venv/bin/activate

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 設定 .env（複製範本後填入你的 token；各欄位說明見 .env.example）
cp .env.example .env        # Windows: copy .env.example .env

# 4. 啟動（資料庫會在啟動時自動初始化）
python run.py
```

> ⚠️ 首次啟動前，請到 [Discord Developer Portal](https://discord.com/developers/applications/) →
> 你的應用 → **Bot → Privileged Gateway Intents** 開啟 **Message Content Intent**，
> 否則登入會出現 `PrivilegedIntentsRequired` 錯誤。

---

## 🕹️ 指令

| 指令 | 說明 |
|------|------|
| `/mahjong start` | 開房（設定戰長／擊飛／三麻／規則／數值），用按鈕或 `/mahjong join` 加入 |
| `/mahjong join [host]` | 加入此頻道的房間 |
| `/mahjong watch [half] [sanma]` | 觀戰模式：全 AI、公開手牌 |
| `/mahjong status` | 查看當前牌局 |
| `/mahjong end` | 房主強制結束 |
| `/mahjong stats` | 個人統計 |
| `/help`、`/mahjong help` | 出牌輸入說明 |

對局中所有操作都在**討論串裡**完成：輪到你時直接在自己的私人討論串**打字出牌**，吃／碰／槓／榮和／自摸／立直／拔北用面板**按鈕**。

出牌輸入：`1m`~`9m`/`1p`~`9p`/`1s`~`9s`（大小寫皆可）、紅寶 `0m`/`0p`/`0s`、字牌 `e/s/w/n`＝東南西北、`r`=中、`wh`=白、`g`=發（也可直接打中文）。

---

## ⚙️ 房間設定

開房「⚙️ 數值設定」與切換按鈕可調整：

- **戰長**：東風戰 / 半莊戰
- **擊飛**：負分即結束（預設開啟）
- **三麻模式**
- **規則**：預設（含雙倍役滿；四麻一律 20-10 馬）/ 天鳳式（役滿一律單倍、不同役滿仍可複合；順位馬依天鳳：四麻東風 10-5、半莊 20-10，三麻 20-0-20）
- **思考秒數**：5~300
- **起始點數**：0~1000000（四麻預設 25000、三麻 35000；返點＝原點＋5000）

---

## 🗄️ 資料庫（SQLite）

五張表：`games`（牌局狀態 + 牌山編碼 + 房間設定）、`players`（座位/分數）、`stats`（累計戰績）、`game_log`（牌譜）、`game_records`（每場每人的戰績：順位／得失／各項次數，分三麻／四麻）。
牌局結束後牌山編碼存於 `games.game_data`，「🔐 公平性驗證」按鈕即由此讀取供校驗。

---

## 🛣️ 後續規劃（Roadmap）

### 0.5 — 里程碑 🎯
- **網頁面板完善**：個人頁／牌譜／戰績更完整，可在網站上回顧對局。
- **等級／段位系統**：以積分（類 Tenhou Rt）與段位呈現長期實力與進度。
- **段位賽**：影響積分／段位的排位對局模式。
- **更豐富的數據**：和了率／放銃率／立直率／平均打點／副露率等進階統計。
- **排行榜**：跨玩家排名，網站上可查。

### 之後
- **更多語言**：已內建繁中／日文／英文（文字抽離為 `mahjong/language/*.json`）；歡迎以新語言檔擴充。
- **資料庫**：目前為 SQLite；視規模再評估其他方案（例如 PostgreSQL）或加上遷移工具。
- 遊玩仍在 Discord，網頁只用於**顯示**牌局／戰績／牌譜／排行榜。

---

## 📜 授權（License）

本專案採用 **GNU General Public License v3.0（GPLv3）**，完整條款見 [LICENSE](LICENSE)。

```
Suzume Tsuk — Discord 日本麻將機器人
Copyright (C) 2026  AFAlimru

本程式為自由軟體：你可以依據自由軟體基金會發佈的 GNU 通用公共授權條款
（GPL 第三版，或你選擇的任何更新版本）重新散布及／或修改本程式。

本程式希望能對你有幫助，但不提供任何擔保；甚至不含適售性或特定用途
適用性的默示擔保。詳見 GNU 通用公共授權條款。

你應已隨本程式收到一份 GNU 通用公共授權條款副本；
若無，請見 <https://www.gnu.org/licenses/>。
```

簡單說，GPLv3 允許任何人自由使用、修改、散布本專案，但**衍生作品也必須以 GPLv3 開源**（copyleft）。完整授權說明：[https://www.gnu.org/licenses/](https://www.gnu.org/licenses/)。

---

**祝遊戲愉快！🀄✨**
