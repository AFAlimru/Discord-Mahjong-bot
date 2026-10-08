[中文](Readme.MD) | **English**

# 🀄 Suzume Tsuk — Discord Riichi Mahjong Bot

> 🤖 This project was built with **vibe coding** (AI-assisted development).

Play full-rules Japanese (Riichi) mahjong right inside Discord. Supports 4-player and 3-player (sanma), East-only and Hanchan games, automatic voice-channel matchmaking, cross-server ranked and casual matchmaking, and CPU opponents. Tiles are shown as **custom emojis**, and you play by pressing **buttons**.

## 🔗 Links

- Support server: <https://discord.gg/QNJMBhDrcj>
- Website: <https://suzume-tsuk-mahjong.yubingserver.top/>
- Source code: <https://github.com/AFAlimru/Discord-Mahjong-bot>
- Invite the bot: [Add it to your server](https://discord.com/oauth2/authorize?client_id=1508777411901259826&permissions=8524499709721680&integration_type=0&scope=bot)

> For rule details (yaku, fu, scoring, furiten, etc.) see **[Rules guide.MD](Rules%20guide.MD)** (Chinese). This file only covers the project layout and how to use it.

---

## ✨ Features

- **Custom tile emojis + button play**: tiles are shown as uploaded Discord application emojis. On your turn every tile in your hand is a clickable button, and chi / pon / kan / ron / tsumo / riichi / kita have buttons too. The **black tile skin** unlocks at the 日全食 (Total Eclipse) rank (`/mahjong skin`).
- **Full scoring**: 42 yaku plus seven pairs / thirteen orphans, reduced han for open hands, no win without yaku, fu and points (standard settlement).
- **Situational yaku**: riichi / ippatsu / double riichi, haitei / houtei, rinshan kaihou, chankan, tenhou / chiihou.
- **House rules**: kuikae forbidden (default), **open riichi** (2 han + revealed hand; dealing into it pays yakuman), **abortive draws** on/off, mixed / full Tenhou rules.
- **Furiten**: discard / temporary / riichi furiten (based on the full discard history, including called tiles), with live warnings on your panel for furiten / no yaku / partial no yaku.
- **Sanma**: 108 tiles, kita (north dora), no chi.
- **Full matches**: dealer repeats / honba, riichi sticks, noten penalties, busting, final placement uma.
- **Abortive draws**: nine terminals, four winds, four riichi, four kans, triple ron (can be turned off in house rules; nagashi mangan still counts).
- **Double ron**, added kan / chankan, closed kan / open kan.
- **CPU opponents**: they discard by shanten and tile acceptance, fold when someone declares riichi, declare riichi when tenpai, and don't make reckless calls.
- **Voice matchmaking**: join the "🔊 Create Friendly Match" voice channel and a 4-player voice room opens automatically. The room settings panel is posted in the voice room's text chat, and the game starts once it's full (3 players can start sanma right away, or press "🤖 Start with CPU" to fill the empty seats). The voice room is deleted when the game ends.
- **Character voices**: each player picks their own voice pack, so pon, riichi, wins, and yaku names are called out loud (sound files are not included in the source; see below for how to make a pack).
- **Category-based lobby**: `/setup create` (or the "🏗️ Create lobby" button in the setup guide) creates a "Suzume Mahjong" category with a read-only button lobby, a 💬 text channel (for opening rooms and chatting), and a 🔊 matchmaking voice channel. Game channels are created inside the category and deleted automatically when the game ends.
- **Hand panel in DMs**: press "📩 Hand via DM" in the waiting room to get your hand panel in DMs for that game.
- **Ranked (cross-server)**: `/mahjong rank` for global matchmaking (**only with players of a similar rank**); ranked games are played in DMs. Lunar-eclipse themed ranks (新人 Newcomer → … → 血月 Blood Moon, with levels 1–3 in every tier except the top) plus an R rating.
- **Casual matchmaking**: `/mahjong match` gathers players across servers; unranked.
- **Play against CPUs**: DM the bot `/mahjong start` and CPUs fill the table.
- **AFK handling**: players who keep timing out are switched to auto-discard (tsumogiri) and can take back control with one click; the game ends if every human is AFK.
- **Game logs & replays**: `/mahjong history` lets you flip through your games one by one and open a replay directly; `/mahjong replay <room code>` plays the game back step by step (hands, win ceremony, switchable point of view). Room codes are 5 characters (e.g. `K7Q2M`).
- **Stats & tasks**: `/mahjong stats`, `/mahjong profile`, daily check-in and activity points; more detailed stats on the website.
- **Fairness**: cryptographically secure shuffling, wall encoding + SHA-256 (`/mahjong fairness`), verifiable game by game.
- **Reliability**: channel games are restored after a restart; DM games end on restart.
- **Languages**: Traditional Chinese / Japanese / English, switch with `/language`; slash command descriptions follow your Discord client language.

---

## 🗂️ Project Layout

```
Suzume Tsuk/
├── run.py                 # Entry point: creates the bot/tree, events (on_ready/on_message/on_guild_join)
├── mahjong/               # Main package
│   ├── commands.py        # Slash commands (/mahjong, /setup, /help, /language), lobby & guide panels
│   ├── flow.py            # Game flow: channels/threads/DMs, reactions, hands/matches, win ceremony, game start
│   ├── voice.py           # Voice matchmaking: Friendly Match voice → auto room → start when full
│   ├── sfx.py             # Character voices: direct playback via FFmpeg, per-player voice packs
│   ├── ai.py              # CPU opponents (shanten, tile acceptance, defense, riichi & call decisions)
│   ├── matchmaking.py     # Ranked/casual global queues (cross-server, similar-rank matching, timeouts)
│   ├── rating.py          # Rank ladder (lunar-eclipse theme, levels 1–3 per tier) + R rating
│   ├── replay.py          # Rebuilds step-by-step replays from game logs
│   ├── tiles.py           # Custom tile emoji rendering (skins; falls back to Unicode if not uploaded)
│   ├── render.py          # Text layout (table / hand panel / discards / scores…)
│   ├── rules.py           # Pure game logic: tile helpers / win evaluation / tenpai & furiten
│   ├── engine.py          # Tiles / wall (SHA-256) / game state / winning shapes / tenpai
│   ├── scoring.py         # Yaku detection + fu + points
│   ├── settlement.py      # Hand settlement + final uma
│   ├── ui.py / views.py   # Buttons, views, room settings panel
│   ├── perms.py           # Bot permission requirements and checks
│   ├── db.py              # SQLite persistence
│   ├── i18n.py            # Language loading and lookup
│   ├── numfmt.py          # Compact numbers (1.2k, 3.4M)
│   ├── config.py          # Environment variables and constants
│   ├── state.py           # Shared in-memory state
│   ├── rooms.py           # Room numbers (shown as 5-character codes)
│   ├── language/          # zh_tw / ja / en language files
│   └── web/               # Website & admin dashboard (gitignored, not published)
├── assets/sounds/         # Voice packs (one subfolder per pack; sound files are not committed)
├── tools/
│   └── build_and_upload_emojis.py   # One-off: uploads tile emojis, generates tile_emojis*.json
├── Rules guide.MD         # Rules reference (Chinese)
├── CHANGELOG.md           # Changelog (summary, Chinese)
├── CHANGELOG_DETAILS.md   # Changelog (development details, Chinese)
├── requirements.txt
├── .env                   # DISCORD_TOKEN etc. (not committed)
└── mahjong.db             # SQLite database (created at runtime, not committed)
```

---

## 🚀 Installation

Running in a **virtual environment** is recommended. Requires **Python 3.10+**.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
#    Windows (PowerShell):  .venv\Scripts\Activate.ps1
#    macOS / Linux:         source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up .env (fill in your token; see .env.example for every field)
cp .env.example .env        # Windows: copy .env.example .env

# 4. Start (the database is initialised automatically on startup)
python run.py
```

> ⚠️ Before the first start, go to the [Discord Developer Portal](https://discord.com/developers/applications/) →
> your application → **Bot → Privileged Gateway Intents** and enable **Message Content Intent**.

### Upload tile emojis (optional, but strongly recommended)

Tile emojis belong to **your own bot application**, so upload them once with your token (without them the bot falls back to Unicode tiles):

```bash
pip install pymupdf pillow "discord.py>=2.4"
python tools/build_and_upload_emojis.py          # 38 white tiles
python tools/build_and_upload_emojis.py black    # 38 black tiles (rank reward skin)
```

> The generated `mahjong/tile_emojis*.json` files **belong to that bot and must not be copied between deployments** (already gitignored).

### Character voices (optional)

- The host needs **FFmpeg** installed (on PATH). Without FFmpeg, voices are disabled automatically and games are unaffected.
- Every subfolder of `assets/sounds/` is a voice pack (folder name = pack name), and file name = sound name (e.g. `riichi.mp3`, `立直.ogg`).
- Players pick their own pack with the lobby's "🎚️ Voice" button. Each action uses the voice of whoever did it; CPUs and AFK players stay silent.

---

## 🕹️ Commands

| Command | Description |
|------|------|
| `/mahjong start` | Open a room (play with friends in a channel; in DMs = play against CPUs) |
| `/mahjong join` | Join this channel's room; in DMs = cross-server casual matchmaking |
| `/mahjong rank` | Ranked global matchmaking (played in DMs) |
| `/mahjong match` | Casual matchmaking (unranked) |
| `/mahjong end` | Force-end a game (the host ends it directly; others need the host's approval) |
| `/mahjong status` · `/mahjong fairness` | Current game status · wall commitment (fairness check) |
| `/mahjong stats` · `/mahjong profile` | Stats · profile card |
| `/mahjong history` · `/mahjong replay <room code>` | Game log (flip with ◀ ▶, replay directly) · replay |
| `/mahjong daily` · `/mahjong tasks` | Daily check-in · tasks |
| `/mahjong yaku` · `/mahjong rankinfo` | Yaku list · rank guide (pt needed per level and pt per placement) |
| `/mahjong skin` · `/mahjong notify` | Tile skin · queue notifications |
| `/setup create` · `/setup channel` | Create the mahjong category (lobby + text channel + matchmaking voice) · set the play channel |
| `/setup lang` · `/setup clear_room` | Server main language · clear leftover games / waiting rooms |
| `/help` · `/language` | Help · language |

> `/setup` commands require the **Manage Server** permission. When the bot joins a server it creates an **admin-only** "setup guide" channel — just follow it to get started.

---

## ⚙️ Room Settings

The room panel lets you adjust: **length** (East-only / Hanchan), **busting**, **sanma**, **rules** (mixed / Tenhou), **kuikae** (forbidden / allowed), **open riichi** (off / on), **abortive draws** (on / off), **thinking time**, and **starting points**.

---

## 📜 License

This project is licensed under the **GNU General Public License v3.0 (GPLv3)**. See [LICENSE](LICENSE) for the full terms
and <https://www.gnu.org/licenses/> for details. Derivative works must also be released under GPLv3 (copyleft).

```
Suzume Tsuk — Discord Riichi Mahjong Bot
Copyright (C) 2026  AFAlimru
This program is free software: you can redistribute it and/or modify it
under the terms of the GNU GPL v3 (or any later version).
It comes with ABSOLUTELY NO WARRANTY. See the GNU General Public License for details.
```

---

**Have fun! 🀄✨**
