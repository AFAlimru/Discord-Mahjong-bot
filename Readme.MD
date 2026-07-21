# 🀄 Discord 日本麻將機器人（Suzume Tsuk）

> 🤖 本專案以 **vibe coding**（AI 輔助開發）方式完成。

在 Discord 上遊玩完整規則的日本麻將（立直麻將）。支援四麻／三麻、東風戰／半莊戰、跨伺服器段位賽與隨機匹配、跟電腦打，牌面以**自訂表情**呈現、出牌用**按鈕**操作。

## 🔗 連結

- 支援伺服器：<https://discord.gg/QNJMBhDrcj>
- 網站：<https://suzume-tsuk-mahjong.yubingserver.top/>
- 專案原始碼：<https://github.com/AFAlimru/Discord-Mahjong-bot>
- 邀請機器人：[點此加入你的伺服器](https://discord.com/oauth2/authorize?client_id=1508777411901259826&permissions=8524499709721680&integration_type=0&scope=bot)

> 規則細節（役種、符點、精算、振聽等）請見 **[Rules guide.MD](Rules%20guide.MD)**。本檔只說明專案結構與使用方式。

---

## ✨ 功能總覽

- **牌面自訂表情 + 按鈕出牌**：牌以上傳的 Discord 應用程式表情顯示；輪到你時每張手牌是一顆可點的按鈕，吃／碰／槓／榮和／自摸／立直／拔北也用按鈕。**黑色牌風**達段位「日全食」解鎖（`/mahjong skin`）。
- **完整計分**：42 役 + 七對子／國士、副露減飜、無役不可和、符數與點數（標準精算）。
- **情境役**：立直／一發／兩立直、海底／河底、嶺上開花、槍槓、天和／地和。
- **房規開關**：食替禁止（預設）、**開立直**（2 飜＋亮牌，放銃＝役滿）、混合式／完全天鳳式。
- **振聽**：捨牌／同巡／立直振聽（用完整捨牌史，被鳴走的牌也算），面板即時警示振聽／無役／部分無役。
- **三麻**：108 張牌、拔北（北寶牌）、禁止吃牌。
- **多局對戰**：連莊／本場、立直棒、流局聽牌罰符、擊飛、賽末順位馬點精算。
- **途中流局**：九種九牌、四風連打、四家立直、四槓散了、三家和。
- **雙榮（ダブロン）**、加槓／搶槓、暗槓／大明槓。
- **段位賽（跨伺服器）**：`/mahjong rank` 全域匹配（**段位相近才配對**），對局走私訊；天鳳風段位（新人→…→血月）與 R 值。
- **隨機匹配（休閒）**：`/mahjong match` 跨伺服器湊人、不計段位；私訊裡 `/mahjong join` 也可。
- **跟電腦打**：私訊機器人 `/mahjong start` 直接 AI 補滿開打。
- **遊戲大廳**：`/setup create` 建立唯讀按鈕面板（段位／休閒／簽到／個資／倉庫／牌譜／回放／役種／語言／公告／網頁）。
- **牌譜與回放**：`/mahjong replay <房號>` 像對局中逐格播放（含手牌、和牌儀式、視角切換）。
- **戰績與任務**：`/mahjong stats`、`/mahjong profile`、每日簽到與活躍度。
- **公平性**：密碼學安全洗牌、牌山編碼 + SHA-256，可逐局自行驗證。
- **穩定性**：頻道對局重啟後可回復；DM 對局重啟則結束。
- **多語系**：繁體中文／日文／英文，`/language` 切換；斜線指令說明依 Discord 介面語言在地化。

---

## 🗂️ 檔案結構

```
Suzume Tsuk/
├── run.py                 # 啟動入口：建立 bot/tree、事件（on_ready/on_message/on_guild_join）
├── mahjong/               # 主套件
│   ├── commands.py        # 斜線指令（/mahjong、/setup、/help、/language）與大廳／指南面板
│   ├── flow.py            # 對局流程：討論串/DM、反應、單/多局、和牌儀式、開局與配對開局
│   ├── matchmaking.py     # 段位／休閒全域匹配佇列（跨伺服器、同段配對、逾時清掃）
│   ├── rating.py          # 段位階梯（月蝕主題）＋ R 值計算
│   ├── replay.py          # 由牌譜重建逐格回放
│   ├── tiles.py           # 牌面自訂表情渲染（含牌風切換，未上傳時退回 Unicode）
│   ├── render.py          # 文字版面渲染（牌桌/手牌面板/牌河/點數…）
│   ├── rules.py           # 純遊戲邏輯：牌工具/AI/和牌評估/聽牌振聽
│   ├── engine.py          # 牌 / 牌山(SHA-256) / 牌局狀態 / 和牌型 / 聽牌
│   ├── scoring.py         # 役種判定 + 符數 + 點數
│   ├── settlement.py      # 對局結算 + 金流馬點精算
│   ├── ui.py / views.py   # 按鈕、視圖、房間設定面板
│   ├── db.py              # SQLite 持久化
│   ├── i18n.py            # 多語系載入與查詢
│   ├── config.py          # 環境變數與常數
│   ├── state.py           # 跨模組共享的記憶體狀態
│   ├── rooms.py           # 房間編號（房間#0001）
│   ├── language/          # zh_tw / ja / en 語言檔
│   └── web/               # 網站與後台（gitignored，不隨原始碼公開）
├── tools/
│   └── build_and_upload_emojis.py   # 一次性：上傳牌面表情、產出 tile_emojis*.json
├── Rules guide.MD         # 規則速查
├── CHANGELOG.md           # 更新日誌（簡易版）
├── CHANGELOG_DETAILS.md   # 更新日誌（開發細節）
├── requirements.txt
├── .env                   # DISCORD_TOKEN 等（不提交）
└── mahjong.db             # SQLite 資料庫（執行後產生，不提交）
```

---

## 🚀 安裝與啟動

建議在**虛擬環境**中執行。需要 **Python 3.10+**。

```bash
# 1. 建立並啟用虛擬環境
python -m venv .venv
#    Windows (PowerShell):  .venv\Scripts\Activate.ps1
#    macOS / Linux:         source .venv/bin/activate

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 設定 .env（填入你的 token；各欄位說明見 .env.example）
cp .env.example .env        # Windows: copy .env.example .env

# 4. 啟動（資料庫會在啟動時自動初始化）
python run.py
```

> ⚠️ 首次啟動前，請到 [Discord Developer Portal](https://discord.com/developers/applications/) →
> 你的應用 → **Bot → Privileged Gateway Intents** 開啟 **Message Content Intent**。

### 上傳牌面表情（可選，但強烈建議）

牌面表情是綁在**你自己的機器人應用**上的，需用你的 token 上傳一次（未上傳則自動退回 Unicode 牌）：

```bash
pip install pymupdf pillow "discord.py>=2.4"
python tools/build_and_upload_emojis.py          # 白色 38 張
python tools/build_and_upload_emojis.py black    # 黑色 38 張（段位獎勵牌風）
```

> 產出的 `mahjong/tile_emojis*.json` **綁各自的機器人、不可在部署間互拷**（已 gitignore）。

---

## 🕹️ 主要指令

| 指令 | 說明 |
|------|------|
| `/mahjong start` | 開房（頻道內與朋友玩；私訊機器人＝跟電腦打） |
| `/mahjong join` | 加入此頻道房間；私訊裡＝跨伺服器隨機匹配 |
| `/mahjong rank` | 段位賽全域匹配（對局走私訊） |
| `/mahjong match` | 休閒隨機匹配（不計段位） |
| `/mahjong end` | 房主強制結束 |
| `/mahjong stats` · `/mahjong profile` | 統計 · 個人資訊卡 |
| `/mahjong history` · `/mahjong replay <房號>` | 牌譜列表 · 回放 |
| `/mahjong daily` · `/mahjong tasks` | 每日簽到 · 任務 |
| `/mahjong yaku` · `/mahjong rankinfo` | 役種表 · 段位說明 |
| `/mahjong skin` · `/mahjong notify` | 牌風切換 · 排隊通知開關 |
| `/setup create` · `/setup channel` | 建遊戲大廳 · 指定遊玩頻道（管理員） |
| `/help` · `/language` | 使用說明 · 語言 |

> 加入伺服器時會自動建立一個**僅管理員可見**的「設定指南」頻道，照著做即可開始。

---

## ⚙️ 房間設定

開房面板可調整：**戰長**（東風／半莊）、**擊飛**、**三麻**、**規則**（混合式／天鳳式）、**食替**（禁止／允許）、**開立直**（關／開）、**思考秒數**、**起始點數**。

---

## 📜 授權（License）

本專案採用 **GNU General Public License v3.0（GPLv3）**，完整條款見 [LICENSE](LICENSE)、
授權說明見 <https://www.gnu.org/licenses/>。衍生作品也必須以 GPLv3 開源（copyleft）。

```
Suzume Tsuk — Discord 日本麻將機器人
Copyright (C) 2026  AFAlimru
本程式為自由軟體，依 GNU GPL v3（或更新版本）散布及修改；
不提供任何擔保。詳見 GNU 通用公共授權條款。
```

---

**祝遊戲愉快！🀄✨**
