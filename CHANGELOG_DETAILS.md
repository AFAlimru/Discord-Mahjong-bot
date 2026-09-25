# 更新紀錄——詳細版（Changelog Details）

開發過程的完整紀錄（含實作說明、檔案與函式層級的細節）。
簡易版請看 [CHANGELOG.md](CHANGELOG.md)。

## [0.7.2] - 2026-09-25

### 新增
- **語音房「與電腦開始」按鈕**：`voice.CpuStartButton`——掛在語音房設定面板（`handle_voice_update`
  建面板時 `sv.add_item(CpuStartButton(vc.id, lang))`）。按下取房內在座且未在別局的真人為 `free`，
  呼叫 `_start_voice_game(vc, free, fill_ai=True)`。`_start_voice_game` 新增 `fill_ai` 參數：
  依 `_room_configs[gid]["max_players"]` 用 `config.AI_NAMES` 補滿剩餘座位（`is_bot=True`）。
  文案 `voice.cpu_btn`（zh_tw／ja／en）。
- **音效改「直接播檔（FFmpeg）＋資料夾語音包」**：整檔重寫 `sfx.py`，捨棄 Soundboard
  （移除 `send_sound`／`upload_pack`／`remove_pack`／`_cache`／`_scope`／`SOUND_GUILD_ID`／`story_` scope）。
  - **檔案解析**：`_resolve(pack, name)` 先找 `SOUNDS_DIR/<pack>/<name>.<ext>`、再退根目錄；副檔名試
    `AUDIO_EXTS`（mp3/ogg/opus/wav/m4a/flac/…）。`list_packs()`＝子資料夾；`pack_coverage(pack)` 對
    `ALL_SOUND_NAMES`（事件 9＋階級 7＋役名 51＝67）算齊全度／缺少清單。
  - **播放**：每台伺服器一條 `asyncio.Queue`＋背景 `_consume` 依序播（`play()` 只 `_enqueue` 即返回、
    不擋遊戲流程；閒置 `CONSUMER_IDLE` 收工）。`_play_path` 用 `discord.FFmpegPCMAudio`＋
    `client.play(after=…)`＋`asyncio.Event` 等播完。`_ensure_client` 回 `VoiceClient`
    （`connect(self_deaf=False, self_mute=False)`，順帶解 error 50167）。`leave()` 取消消費者＋清佇列＋斷線。
  - **語音包選擇**：`db.get_voice_pack`／`set_voice_pack`（`guild_settings.voice_pack` 欄，per-guild）；
    指令 `commands.cmd_setup_voice`（`/setup voice [pack] [clear]`，需 `manage_guild`／admin）列出／切換／
    顯示齊全度。**移除** `/setup sounds`、`/setup sounds_remove`。
  - `sfx.load(bot)` 改為 `shutil.which("ffmpeg")` 設 `_ready`、`_ensure_opus()` 載入 libopus
    （送語音編碼必需；預設未載入會靜默無聲，經典雷）、列語音包（不再抓音效板）。
  - `sfx.join(vc)`＝進語音待命（`voice._start_voice_game` 開局呼叫）。
- **打牌／碰／吃／槓＋事件音效觸發**：`flow.play_hand_t` 暗槓／加槓／大明槓／碰／吃五處各
  `await sfx.play(gid,"kan"/"pon"/"chi")`；打牌（人類與 AI 捨牌各一處）`discard`；立直宣言 `riichi`、
  開局 `start`、自摸／榮和 `tsumo`/`ron`、流局沿用。事件音效名列 `sfx.EVENT_SOUNDS`。
- **語音房面板**：`views.RoomSettingsView` 加 `show_confirm` 參數（False → `remove_item(self.confirm)`）；
  `voice.handle_voice_update` 建面板時 `show_confirm=False` 並 `add_item(VoicePackSelect(vc.id,lang))`；
  開局 `_start_voice_game` 由 `msg.edit(view=None)` 改 `msg.delete()`（面板整則消失）。
  `voice.VoicePackSelect`（Select，選項＝`sfx.list_packs()`＋預設，`row` 可指定）callback 寫 `db.set_voice_pack`；
  `commands.cmd_setup_voice` 的 `pack` 加 autocomplete（`_voice_pack_autocomplete`）。
  `commands.LobbyPanel` 另加常駐鈕 `hub:voice`（`voice_btn`）：開 ephemeral 視窗放 `VoicePackSelect`，
  讓大廳也能切語音（persistent view 仍 `is_persistent`）。選單首項＝**🔇 關閉語音**（value＝`sfx.VOICE_OFF`
  →`db.voice_pack="__off__"`）。**出聲改 opt-in**：`sfx._guild_muted()`＝沒選（None）或 `VOICE_OFF` 皆靜音，
  `play`／`play_win`／`join` 遇之即略過（選了有效語音包才播；包內缺檔才退根目錄後備）。
  `/setup voice` 未選顯示「未選（不出聲）」、關閉顯示「🔇 關閉語音」，皆跳過齊全度。
- **`/setup rooms` 後台清理**：`flow.list_rooms(guild_id)` 彙整 `_games`／`_lobbies`／`_waiting`／
  `rooms.all_meta()`（新增）→ 每房 room_no／status／channel／humans／ai／age_s（age 取 `RoomMeta.created_at`）。
  `commands.cmd_setup_rooms`（manage_guild／admin）不帶參數列出；`delete=all/waiting/房號` 逐一 `flow.force_end`
  （沿用既有完整拆除：關 DB、停迴圈、刪大廳訊息、貼房間已關閉、刪討論串／頻道、離開語音）。
- **和了語音（逐役唸名＋打點階級）**：`sfx.tier_sound(name)` 把 `ScoreResult.name` 對應到階級名
  （空＝`han`；含「役滿」＝`yakuman`，`累計役滿`＝`kazoe`；三倍滿→倍滿→跳滿→滿貫 依序判，
  `流局滿貫`＝`mangan`）。役名清單 `sfx.YAKU_SOUNDS`（frozenset，51 名，與 scoring.py 一致，供
  `pack_coverage` 用；播放僅憑檔案是否存在）。`sfx.play_win(gid, result)`（背景任務）`WIN_INTRO_GAP`
  後逐一 `play(gid, 役名)` 唸 `result.yakuman`／`result.yaku`，最後播階級——實際依佇列序播。
  `flow` 單一和牌分支在自摸／榮和音效後 `asyncio.create_task(sfx.play_win(gid, result))`；
  多家榮和分支補上 `ron` 音效＋取最高 `points` 的 result 觸發。

### 變更
- **立直後可拔北（剛摸到的那張）**：
  - `rules.has_kita_drawn(player)`：新判斷——`player.drawn_tile` 是否為北（`Suit.WIND` value 4）。
    立直前就在手裡的北會改變聽牌不可拔，剛摸到的北抽走後手牌組成不變故可拔。
  - `flow.play_hand_t`：`already_riichi` 分支原 `kita_ok=False` → `kita_ok = gs.is_sanma and
    has_kita_drawn(player)`；`turn_time` 由 `can_tsumo` → `can_tsumo or kita_ok`（不再縮成 3 秒）。
    AI 摸切分支 `has_kita(player)` → `has_kita_drawn(player) if player.riichi else has_kita(player)`（防呆）。
  - `flow.wait_turn_action`：`riichi_locked` 打字分支新增例外——`kita_ok` 時解析出 `("kita", …)` 放行，
    其餘打字仍擋。
- **殘留對局自動清理**：
  - 等待房逾時：`flow.sweep_stale_rooms()`（`start_room_sweeper` 每 600s，run.py on_ready 啟動）刪 `_lobbies`
    中未開打（`gid not in _games`）且 `now-created_at > WAITING_TTL`(=3h) 的房，走 `force_end`。
  - 無人在玩：`play_hand_t` 每局重置 `_hand_human_turns`／`_hand_human_timeout`，真人回合後累加；
    `match_loop_t` 每局結束更新 `_afk_streak`（整局真人回合全超時→+1，有人行動→歸零），
    `>= AFK_HANDS_TO_END`(=1) 時強制 `over=True`、發 `msg.afk_end` 後正常結束。三個 dict 於 `_cleanup` 清除。

### 修正
- **語音局無法結束／房主不明**：`voice._start_voice_game` 開局訊息改附 `flow.EndGameButton`＋房主標示
  （`voice.host`，host＝`members[0]`＝東家／起家）；`_thread_game[vc.id] = gid` 登記語音頻道，
  讓語音房文字區的 `/end` 找得到對局。`flow._cleanup` 清理時一併 `_thread_game.pop(vc.id, None)`。

## [0.7.1] - 2026-07-24

### 修正
- **副露手「幻影聽牌」（重大）**：`rules.hand_waits()`／`tenpai_advice()` 原本把副露牌
  （`m.tiles[:3]`）併回手牌湊 14 張再丟給 `engine.is_complete()`——等於允許副露拆開重組，
  算出實際不成立的待牌；這些待牌再被 `_wait_flags()` 的 `evaluate_win()`（副露固定、正確）
  判為和不了 → 進聽提示整排「無役」（回報案例：副露清一色）。**同一套待牌也餵給榮和判定
  （`flow` 兩處 `in hand_waits(p)`）與流局聽牌費（`tenpai_seats`）**，故影響不只顯示。
  - `engine.is_complete()` 泛化：`len % 3 == 2` 且 ≤14 皆可判（2/5/8/11 張＝副露佔幾組面子
    就少判幾組）；七對子／國士檢查僅在 14 張時執行。門清行為不變（附回歸測試）。
  - `hand_waits()`／`tenpai_advice()` 改用**純手牌**判定，副露不再併入。
- **顯示自風不輪轉**：計分自風一直正確（`rules.build_ctx` 相對莊家），但顯示用固定 `p.seat`。
  `render._wind()` 增加 `gs` 參數改算 `(seat - dealer_seat) % n`，牌桌／牌河／點數／動態
  ／最終順位 7 處與 `flow` 的「輪到你」動態全部套用。
- **拔北按鈕牌面**：🀀（東）→ 🀃（北）。
- **語音房收尾**：對局結束（含 `/end`）直接刪語音配對房（`voice.cleanup_room()`），不等人走光。
- **三麻語音房**：三麻開局時 `user_limit` 改 3。

### 變更
- **強制結束改房主同意制**：核心抽成 `flow.force_end()`；公開面常駐 `EndGameButton`
  （房主按＝直接結束；其他玩家按或 `/end` ＝發 `end_request_view` 請求，房主按同意/繼續）。
- **語音房設定面板**：開房時把 `RoomSettingsView` 發在語音房文字區；滿員開局取當下值
  （`voice._config_from_settings()`），開局鎖面板；設定選三麻＝3 人即滿員。
  `RoomSettingsView.confirm` 改為**確認後刪除面板訊息**（ephemeral 走 `delete_original_response`）。

## [0.7.0] - 2026-07-24

### 新增
- **類別制大廳**：`/setup create` 建類別（`hub.category_name`）＋唯讀大廳（打字即刪，
  `run.on_message` 查 `state._lobby_channels` 快取）＋💬文字頻道（自動 `set_play_channel`）
  ＋🔊配對語音；id 存 `guild_settings`（`category_id`/`lobby_channel_id`/`hub_voice_id`）。
- **對局頻道化**：`flow.setup_channels()`——伺服器有類別時公開房＋私人手牌頻道
  （overwrites 僅本人＋bot）開在類別下；結束 `_finish_channels()` 發結束訊息＋刪除按鈕、
  5 分鐘自動刪；無類別沿用討論串。`_deletable_surfaces()` 統一清理（略過 DM）。
- **逐場 DM**：大廳「📩 DM」按鈕寫入 `_room_configs[gid]["dm_uids"]`，開局時該玩家面板走 DM。
- **語音配對**：`mahjong/voice.py`＋`run.on_voice_state_update`——進 hub 語音→開 4 人房→
  滿員自動開局；3 人「三人先開（三麻）」按鈕；空房自刪。
- **全域音效框架**：`mahjong/sfx.py`——家伺服器（`SOUND_GUILD_ID`）soundboard 跨伺服器代發；
  `story_` 前綴限家伺服器；掛點：開局／立直／自摸／榮和；`/setup sounds` 批次上傳；
  需 discord.py≥2.5＋PyNaCl，不足時靜默停用。

## [0.6.0] - 2026-07-07

### 新增
- **牌面改用自訂表情符號**：不再用不易辨識的 Unicode 麻將字元，改上傳一套牌圖成 Discord
  **應用程式表情**（綁機器人本身、所有伺服器通用），手牌／牌河／副露／寶牌／和牌牌型皆以表情渲染。
  - `mahjong/tiles.py`：載入 `mahjong/tile_emojis.json`（`牌code → <:mj_xx:id>`）做渲染；
    未上傳時自動退回 Unicode，不影響對局。提供 `of()`／`render()`／`back()`／`meld()`／`partial()`（按鈕用），
    並支援回放／日誌裡的 Unicode 牌字串反查。
  - `tools/build_and_upload_emojis.py`：一鍵工具——下載 FluffyStuff/riichi-mahjong-tiles（CC0）牌胚＋牌面，
    疊圖光柵化成 256×256 透明 PNG，上傳成應用程式表情，產出 `tile_emojis.json`。
    支援 PyMuPDF（免系統 Cairo、Windows 友善）或 cairosvg 兩種光柵化後端；可重跑（先刪舊 `mj_*`）。
- **按鈕出牌**：輪到自己時，手牌**每一張都做成可點的出牌鈕**（牌面表情），第 0～2 列放牌、第 3 列放
  動作（拔北／暗槓／加槓／立直／九種／自摸）、第 4 列放資訊鈕（看牌河／點數／副露／動態／說明）。
  - 剛摸到的牌用藍色鈕標示、置於最後；立直後鎖手僅留摸切鈕。**打字出牌仍保留**為備援。

### 變更
- **立直宣言全按鈕化**：按「立直」後不再要求打字——**可宣言的牌亮綠色**（打出後仍聽牌），
  其餘手牌鈕與動作鈕（拔北／暗槓／加槓／九種／自摸）**一律停用**；再按一次「立直」取消並全部恢復。
  提示文字（`prompt.riichi_declared`／`prompt.discard`，三語）同步改為按鈕說法。
- **Unicode 牌全面替換為表情**：
  - `i18n.t()` 集中處理——翻譯參數若是 `Tile` 物件自動轉表情（涵蓋出牌／碰吃槓／搶槓等所有動態與提示）。
  - `feed_text()`：牌譜裡存的純牌面字串參數（Unicode）自動轉表情；模板裡裝飾用的 🀄／🀫 不受影響。
  - 進聽提示「打X→聽Y」（`rules.tenpai_note_text`）、聽牌待牌、和牌儀式手牌、結果牌型
    （`result_body`）、**回放**（牌河／副露／手牌／寶牌／和牌儀式）、`/mahjong profile` 最高和了皆改表情。
  - `tiles.emojify()`：把字串中的 Unicode 牌（含 🔴 紅5、🀫 蓋牌→牌背）整批換成表情，供牌譜類字串用。

### 變更（第二輪回饋）
- **出牌鈕依花色分列**：萬／條／餅／字各起一列（一列最多 5 顆、超過自動換列；花色太散超過 4 列
  時退回緊排）。動作鈕接在牌列下一列、資訊鈕再下一列（版面滿時該回合先略過資訊鈕，回合結束恢復）。
- **副露只顯示 `[…]`**：拿掉「碰:/吃:/槓:」字樣（`tiles.meld()`；牌譜舊字串由 `emojify()` 同步剝除；
  回放的副露標記同步改為 `[牌]`）。
- **動態版面**：「🀫 上家：… 打出」與牌面合併成一行並放大（表情可與文字同行）；「📜 最新動態」整行放大。
- **鳴牌後打出合併成一句**：碰／吃／槓之後的打牌，動態顯示「X 碰了 Y 的「A」，打出「B」（手切），輪到 Z」
  （新鍵 `feed.call_discard`＋`term.pon/chi/kan`，三語；AI 與真人皆適用；回放同步認得此鍵）。
- **回放和牌儀式清空牌桌**：結算／儀式畫格不再殘留該局的手牌與牌河，只顯示儀式內容（`clear` 畫格）。

### 變更（第四輪回饋）
- **完整牌河獨立成一則訊息**：私人討論串／DM 改成兩則訊息——上面一則是**場況＋完整牌河**，
  下面一則是手牌面板（動態＋手牌＋按鈕）。牌河訊息在每筆動作後即時更新（內容沒變就不重編輯，
  省 API）；輪到自己時再刷新一次（牌山數即時）；每局開始重發一組；只有逼近 2000 字上限才逐步
  縮到最近 N 張。「看牌河」按鈕移除（畫面上就有）。
- **場況移到整個畫面最上方（牌河訊息頂部）**，並改用含**立直棒（供託）**的完整場況行
  （局／本場／立直棒／牌山＋寶牌）；手牌面板不再重複顯示場況。

### 新增（第六輪）
- **黑色牌風（段位獎勵）**：上傳第二套「黑底」牌面表情（FluffyStuff Black、`mjb_*`、38 張，
  `tile_emojis_black.json`），達到段位頂點「**血月**」解鎖；機器人擁有者直接可用（測試）。
  - `/mahjong skin`：切換 預設（白）／黑色（血月限定）；選了黑色但未解鎖會提示條件。
  - 牌風只套用在**自己的私人畫面**（牌河訊息、手牌面板、出牌鈕）；公開牌桌／回放維持預設。
    以 contextvar 實作（`tiles.set_skin`），黑色缺圖自動退回預設再退回 Unicode。
  - 上傳工具支援風格參數：`python tools/build_and_upload_emojis.py black`；只清除自己風格的舊表情；
    GitHub raw 429 限流加了重試與下載間隔。
  - `user_prefs` 新增 `skin` 欄（舊庫自動補欄）。
- **段位改名（月蝕主題）**：新人 → 新月 → 殘月 → 上弦月 → 下弦月 → 月偏食 → 日全食 → **血月**
  （原：新人／新星／星群／星雀／星月／上弦月／下弦月／月魂）。資料庫存索引，舊資料不受影響；
  降段起點同為第 4 階（上弦月）。

### 新增（規則）
- **對開立直放銃＝役滿（房規）**：開立直亮牌後，其他玩家仍放銃（含被搶槓）→ 以**役滿**計
  （役種顯示「開立直」役滿）。**豁免**：放銃者自身已立直（鎖手自動摸切、無從迴避）則不升級，
  照一般開立直 2 飜計。自摸照常 2 飜。適用一般型／七對／國士等所有牌型路徑。
- **食替禁止（預設）＋房間開關**：碰／吃之後的那一打，不能打出剛鳴的牌（現物）；兩面吃另有
  **筋食替**限制（如 4m5m 吃 3m 後不能立刻打 6m；嵌張吃只禁現物）。被禁的出牌鈕直接**停用**、
  打字會提示 `msg.kuikae`；逾時自動打與 AI 都會避開。房間設定新增「🚫 食替：禁止／允許」開關
  （預設禁止；段位賽固定禁止）。
- **開立直（房間開關）**：房間設定開啟後，能立直時會多一顆「開立直」鈕（同宣言流程，可切換／取消）。
  開立直＝**2 飜**（取代立直；兩立直＋開＝3 飜），宣告後**亮出手牌**給所有人看（牌河訊息與公開牌桌
  顯示【開立直】標籤＋「👁 亮牌：…」，只亮牌面、不標待牌）。裏寶牌照常。段位賽不開放。
- 黑色牌風解鎖點從「血月」**提前到「日全食」**（`rating.BLACK_SKIN_IDX`）。

### 變更
- **移除 `/mahjong watch` 觀戰指令**（測試用途已結束）；觀戰渲染基礎（open_hand/watch 牌桌）保留。
- **拔北鈕**：輪到你且手上有北（三麻）時，出牌畫面多一顆「🀀 拔北」綠色鈕（常駐整回合，
  回合本身已有倒數）；**牌照樣能直接打**、其他動作不受影響。選拔北立即執行並補摸
  （打字 `!n` 仍可用）。

### 新增
- **DM 跟電腦打**：在與機器人的私訊裡 `/mahjong start`，選完房間設定後**直接 AI 補滿開打**
  （不開大廳、不列入段位），全程走段位賽的 DM 管線（`flow.launch_dm_game` + `setup_dms`，
  開場白 `dm.start`）。`/mahjong end` 在 DM 也能結束自己的對局（以玩家對照找房）。
  同一人同時只能一場 DM 對局。

### 修正
- **反應提示（碰／吃／槓／榮和窗口）的牌還是 Unicode**：`collect_reactions_t` 的「X 打出」牌面與
  吃的組合改用表情渲染，並依觀看者牌風；搶槓窗口同步套牌風。
- **反應窗口期間「上家打出」顯示上一張**：`gs.pending_discard` 原在動態發出「之後」才更新，
  面板掛的是舊牌——改成打牌當下（人類與 AI）先更新再發動態。
- **DM 對局 `/mahjong end` 誤報「⚠️ 有討論串刪不掉」**：DM 局沒有討論串（private 是 DM 頻道，
  不能刪也不用刪），結束流程改成 `is_dm` 時跳過刪除與權限警告（「房間已關閉」照樣會貼）。
- **在 DM 開房會炸**（`channel.guild` 為 None → `AttributeError`）：起因是 DM 頻道沒有 guild、
  又沒有討論串可開。`/mahjong start` 在 DM 改走上述「跟電腦打」路徑；`/mahjong watch` 在 DM
  仍會擋下並提示（`msg.guild_only`，三語）。

### 變更（第五輪回饋）
- **振聽／無役警示**：聽牌但振聽或無役時，手牌面板會像【已立直】一樣顯眼標註——
  「⚠️ 振聽中（不可榮和）」「⚠️ 無役（目前和不了牌）」「⚠️ 榮和無役（僅可自摸）」（三語，只有本人看得到）。
  於打牌後、同巡放過榮和（含搶槓）時即時更新（`rules.wait_status` + `player.warn_tags`）。
- **和牌儀式的寶牌行不再標「寶牌／裏寶牌」字樣**：直接顯示 `[牌...]　[牌...]`（對局與回放一致）。
- **換局會刪掉上一局的牌河訊息與手牌面板**，討論串不再堆疊舊局畫面。

### 修正（第三輪回饋）
- **輪到自己卻沒有出牌鈕（重大）**：表情牌在訊息裡每張近 30 字元，中盤私人面板（場況＋全員牌河＋
  動態＋手牌）會超過 Discord 2000 字上限，`refresh()` 編輯失敗又被吞掉——按鈕永遠掛不上去。修法：
  - `make_hand_panel` 超過 1950 字自動降級（先捨牌河區、再捨動態區），確保面板一定更新得了；
  - 牌河全面截尾（超出加 `…` 前綴、總數照標）：私人面板每家 10 張、看牌河按鈕 15 張、
    公開牌桌 12 張（原 18）、回放 12 張。
- **動態裡的牌不再帶「」**：`feed.discard`／`feed.riichi_tsumogiri`／`feed.call_discard`（三語）
  拿掉牌旁邊的引號，表情牌本身已夠醒目。

### 修正
- **牌圖右下角黑斑與符號溢出**：FluffyStuff 的 `Front.svg` 自帶右下陰影、且牌面符號畫到滿版
  （紅5條等會貼齊牌邊）——改成**自畫滿版圓角牌底**（底色取樣原素材）＋面符號縮 88% 置中，
  已重新上傳全部 38 張表情。
- 上傳工具在 Windows 主控台（cp950）印 ✓ 會炸 `UnicodeEncodeError`：改強制 stdout UTF-8。
