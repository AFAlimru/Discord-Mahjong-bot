# 更新紀錄——詳細版（Changelog Details）

開發過程的完整紀錄（含實作說明、檔案與函式層級的細節）。
簡易版請看 [CHANGELOG.md](CHANGELOG.md)。

## [0.9.1] - 未發布（開發中）

### 新增
- **Discord 狀態**（新檔 `mahjong/presence.py`，`run.on_ready` 呼叫 `presence.start(bot)`）：每 60 秒算一次進行中對局（`state._games`）裡的真人人數（同一人只算一次、電腦不算），組成 `presence.playing`（「{n} 人對局中」）＋`config.WEB_BASE_URL` 的網域，用 `discord.Game` 設成「正在玩」；文字沒變就不呼叫 Discord。沒設網址或網址是本機（127.*／localhost）就只顯示人數。
- **觀戰**（新檔 `mahjong/spectate.py`）：
  - 設定：`RoomSettingsView.spectate`（預設 True）放在 `RuleDetailsView` 的 `_PANEL_FIELDS`（第 3 列，跟食替、開立直同類，直接存在面板上），房間設定鍵 `spectate`；
    `rulesets.compact(..., spectate=)` 在不開放時把「不開放觀戰」列進「與預設不同」。`commands.cmd_start`／`voice._config_from_settings` 寫進 `_room_configs`，語音房沒有面板時預設開；
    段位／匹配局（`launch_match_game`）寫 `spectate: False`，私訊對局走 `launch_dm_game` 不經過 `launch_game`，所以不登記。`spectate.enabled(config)` 另外排除 `ranked`。
  - 記錄：`launch_game`／`resume_game` 呼叫 `spectate.register(gid, channel, gs, config)`，之後每次牌桌更新（`play_hand_t.render_board`，不管公開牌桌有沒有放；換局的 `deal_next_hand` 之後）呼叫 `spectate.record(gid, gs, status)`：
    一次組好**明牌版文字**（`board_parts(gs, lang, True)` ＋狀態）與**結構化字典**（`_build_state`），存進每局的環形緩衝（`_Rec.snaps`，約 `RING_KEEP`＝300 秒，但一定留著目前延遲可見的那一張；文字完全相同就不重複存）。
    沒有人看的時候不呼叫 Discord；不開放觀戰的局只留輕量紀錄（供 `is_player`／`watchable_games(include_closed=True)`），不存快照。
    和牌／流局結果文字在 `match_loop_t` 取公開頻道那組訊息的最終版本，用 `spectate.event` 記成事件（附時間與第幾手）；最終順位也一樣；`spectate.finish(gid, gs)` 記最後一張 `finished` 快照。
  - 頻道：`spectate.ensure_channels(guild, gid)`（以 `rec.lock` 防止同時建兩次）在第一次有人要看時建 **觀戰-<房號>**（即時）與 **觀戰明牌-<房號>**，開在對局頻道所在的類別（沒有就用開房頻道的類別）；
    @everyone 可看不能發言，機器人可管理，這場每位真人玩家（含選了 DM 的）加 `view_channel=False` 的成員覆寫（拿不到 Member 就用 `discord.Object`）。頻道 id 經 `flow.save_surfaces` 存進 `surfaces.channels`，
    重啟後的 `purge_saved_surfaces` 跟其他對局頻道一起清。
  - 即時頻道：`MsgGroup`（標頭＋每家一則＋狀態，同公開牌桌的只改有變的那幾則），`record` 發現頻道存在就排程一個合併式的同步任務（`_live_loop`，連續更新只送最新的一版）；結果事件馬上貼。
  - 明牌頻道：`_publisher` 每 `TICK`＝3 秒執行 `_publish_once`：把時間 ≤ 現在 − `SPECTATE_DELAY`（180 秒）的最新快照同步上去，結果事件在它自己的時間點貼出（貼之前先把牌桌同步到那個時間點的版本，順序才對）。
    第一張快照可見之前只放「開局 3 分鐘後開始顯示」。建頻道時更早的結果不補貼。對局結束後發佈器把最終快照與結果播完就自己結束。
  - 清理：`spectate.finish`（`match_loop_t` 結尾與 `_cleanup` 都會呼叫，可重複）排程 `_closer`，在結束時間 ＋ 180 秒 ＋ `CLOSE_AFTER`（600 秒）後 `close(gid)` 刪兩個頻道並丟掉紀錄；`force_end` 直接 `await spectate.close(gid)`。
  - 入口：`/mahjong watch [房號]`（房號用 `rooms.parse`，可自動完成；不帶房號＝列出這個伺服器可觀戰的對局下拉選單）、開局公告上的按鈕（`WatchButton`，`launch_game`／`resume_game` 的公告）、大廳面板新增 `hub:watch` 按鈕（第 0 列，既有面板按「🔁 更新面板」後才會出現）。
    `request_watch` 會檢查：不在這個伺服器／已結束／不開放／申請人是這場的玩家就拒絕，其餘 defer 後建（或沿用）頻道並用 ephemeral 回兩個連結。
  - 給網站的介面（同一個行程，直接呼叫）：`watchable_games(guild_id=None, include_closed=False)`、`live_state(gid)`（沒有手牌；開立直的人手牌本來就公開，保留）、`delayed_state(gid, now=None)`（含手牌，`ready` 為 False 時附 `available_in` 秒數）、`is_player(gid, user_id)`。
    狀態是可直接轉 JSON 的字典：局／本場／立直棒／牌山、寶牌（未翻開是 `back`）、每家的風位／名字／電腦／點數／立直／`riichi_idx`／牌河（`{tile, side}`，立直牌 `side`＝True）／副露（沿用 `tiles._meld_order` 的橫放，暗槓兩端 `back`）／拔北，明牌版另有 `hand`；`results` 是結果的純文字（牌表情換成牌代碼）。
  - 其他：`open_hand`（公開牌桌亮手牌）維持原狀，跟觀戰無關。新增文字鍵 `spectate.*`、`hub.btn.watch`、`rules.det.spectate`、`rules.v.open`／`rules.v.closed`、`rules.diff.nospectate`，`help.guide` 加上指令說明，三語都有。
- **比賽**（新檔 `mahjong/tournament.py`；`db.py` 新增 `tournaments`／`tournament_entrants`／`tournament_tables`／`tournament_results` 四張表與 `tour_*` 讀寫函式、`game_record_count`；`config.py` 新增 `ADMIN_IDS`（`ADMIN_DISCORD_ID`）與 `TOURNAMENT_PLAYER_CREATE`）：
  - 資料：`tournaments` 存名稱／伺服器／頻道／主辦／開賽時間（unix 秒）／狀態（`draft`→`registration`→`running`→`finished`，或 `cancelled`）／`visibility`（`public`／`private`）／`join_mode`（`open`／`invite`）／名額／是否要報到／報到分鐘數／`rules`（規則預設、細項、戰長、思考秒數、起始點數、牌山、食替、開立直、觀戰…，鍵同房間設定）／`format`（預賽輪數、晉級人數、決賽輪數、每桌人數、自動接續、輪間隔分鐘）／`state`（排程的內部紀錄）。
    `tournament_entrants`：`registered`／`selected`／`waitlist`／`withdrawn`＋報到時間＋來源（`web`／`discord`／`manual`）＋抽選順序。`tournament_tables`：階段／輪／桌號／`gid`／座位（user_id，電腦是 `cpu`）／狀態（`pending`／`running`／`finished`／`void`）／`closed`（作廢後主辦決定不重打）。`tournament_results`：每桌每個真人的座位／順位／最終點數／精算 pt（小數 1 位）。
  - 報名與權限：`register`／`unregister`／`checkin`（開賽前 `checkin_minutes` 分鐘到開賽時間）是玩家自己的操作；邀請制（`join_mode='invite'`）玩家不能自己報，要 `add_entrant(..., by=主辦)`。會改資料的管理函式都必須帶 `by=`，內部用 `can_manage`（主辦或 `ADMIN_IDS`）檢查，`by=None` 是系統（排程、測試）；建立比賽看 `can_create`（管理員，或 `TOURNAMENT_PLAYER_CREATE` 開啟後所有人）。出錯統一丟 `TournamentError(code)`。
  - 抽選：`draw` 超過名額就用 `secrets` 隨機選（可帶 seed 重現），其餘照抽出順序候補；`trim` 把人數削到一桌人數的整數倍；`set_selection` 手動指定；開賽前可重抽。
  - 分桌：`seat_round` 隨機分桌，人數不整除時平均分配並補電腦；預賽第 2 輪起試 3000 種洗牌挑「以前同桌次數」最少的。第 1 輪開賽時沒報到的已選者換成有報到的候補（照抽選順序），再不夠就留給電腦。
  - 開桌：`start_round` 把每桌用既有的 `flow.launch_game` 開成真的對局（房間設定由比賽規則轉出，另帶 `tournament` 標記，房主設成主辦，不經等待大廳），開完在比賽頻道貼各桌名單與牌桌連結。排程器 `tournament.start(bot)`（`run.on_ready` 呼叫）每 15 秒 `tick`：到時間自動開第 1 輪；一輪全部打完就貼排名，間隔 `round_gap_minutes` 後開下一輪（預賽→決賽），全部打完結束比賽並貼最終名次。重啟後會補記「對局其實已正常打完但結果沒記」的桌。
  - 結果：`flow.match_loop_t` 結算時呼叫 `tournament.on_game_end`（`finalize_interrupted` 也會），用**比賽自己的規則**呼叫 `settlement.final_standings`（終局立直棒、馬點、同點由座位決勝都照它），只記真人；`flow._cleanup` 呼叫 `on_cleanup`，桌子還在進行就被關掉＝標成 `void`，擋住進度直到主辦用 `rerun_table`（同一批人重開）或 `void_table`（作廢不重打）處理。`bind_game` 把玩家自己開的對局綁到一桌：已打完（`game_records` 有戰績）就馬上匯入，還在打就等它打完自動記錄。
  - 排名：預賽累計 pt；同分比單場最高 pt → 那場的座位（起家起）→ 報名先後。前 `advance_n` 名進決賽，最終名次＝決賽排名，其餘接在後面照預賽排名。
  - Discord：公告用 `discord.ui.DynamicItem`（`TourButton`，custom_id `tour:<join|leave|checkin>:<比賽 id>`）做常駐按鈕，重啟後不用重掛 view；公告訊息（人數、狀態、按鈕）合併更新。`/mahjong tournament` 列出這個伺服器的比賽。新增文字鍵 `tour.*`。
  - 給網站的介面（同一個行程直接呼叫）：`create`／`update`／`publish`／`cancel`／`get`／`list_all`／`standings`／`draw`／`set_selection`／`add_entrant`／`remove_entrant`／`seat_round`／`start_round`／`void_table`／`rerun_table`／`bind_game`／`register`／`unregister`／`checkin`／`can_manage`／`can_create`。

## [0.9.0] - 2026-11-05

### 新增
- **橫放的牌**：
  - 資料：`engine.Meld` 新增 `called`（被鳴的那張，暗槓與舊存檔為 `None`）、`added`（加槓補上的那張）、`n_players`（算上家／對家／下家用，三麻 3）；
    `PlayerState` 新增 `riichi_idx`（立直宣言牌在 `discards` 的位置，預設 −1）。`Meld`／`PlayerState` 的 `to_dict`／`from_dict` 一併存取
    （`PlayerState` 之前不存副露，現在也存；舊存檔缺這些欄位照樣可讀）。flow 在吃、碰、大明槓、暗槓、加槓時寫入；真人與電腦立直、開立直、兩立直都會設 `riichi_idx`。
  - 宣言牌被鳴走：被鳴走的牌本來就會從 `discards` 移除，所以 `riichi_idx` 剛好指到「下一張捨牌」，那張就改橫放，不需要額外狀態。
  - 顯示：`tiles.side(tile)` 取橫牌表情（當前牌風 → 預設風 → 直立牌），`tiles.meld(m, owner_seat)` 依 `(from_seat − 持有者座位) % 人數` 決定橫牌位置
    （下家＝最右、上家＝最左、對家＝中間，大明槓為第二張；吃最左；加槓在同位置橫放兩張；暗槓不變；沒有 `called` 的舊資料照舊全直立）。
    `render._river_str()` 統一所有牌河（面板、討論串牌桌、牌河訊息、看牌河）並處理「只留最近 N 張」時的橫牌位置。
  - 和牌牌型：`flow.format_winning_hand` 改走 `tiles.win_hand()`，仍輸出 unicode 字串，但副露被叫的那張與和牌張前面加零寬標記 `tiles.SIDE_MARK`（U+2060），
    `emojify`／`emojify_hand` 遇到「標記＋牌」換成橫牌表情（該牌風沒有橫牌就退回直立；完全沒表情時標記清掉）。和牌儀式、公開／私人結果、雙榮、回放結算、最高和了都因此橫放；
    標記不可見，沒有標記的舊紀錄（DB 最高和了、舊回放）照舊全直立，不需改資料庫。橫牌表情名稱較長，牌型字串在 2000 字上限內仍遠低於上限（最多 14 張）。
  - 還是直立的：舊紀錄（沒有標記）、流局時的聽牌手牌顯示。
- **更安全的表情上傳腳本**：`tools/build_and_upload_emojis.py` 每個牌風從 38 張變成 75 張（直立 37＋牌背＋橫放 37）。
  橫牌是直立牌逆時針轉 90°，貼進 256×256 方框的底部、水平置中，排在直立牌旁邊時落在同一條底線上。
  預設流程改成增量：登入 → 讀取現有應用程式表情 → 檢查本機 JSON 裡每張 `mj_<牌>` 的 ID 與應用程式上的一致（對不上就中止、什麼都不上傳；`--force` 可略過）
  → 只上傳還沒有的名稱、絕不刪除 → 把「既有＋新增」合併寫回 JSON（既有條目不動，新增 `<牌>_s` 鍵）。
  舊的刪光重建改成明確的 `--rebuild`（會警告 ID 全變、伺服器的 JSON 必須重新部署）；`--dry-run [資料夾]` 不登入，只把全部 PNG 產出供預覽
  （預設 `tools/.tile_preview/<風格>/`，已 gitignore）。
- **規則預設**：新增 `mahjong/rulesets.py`，把「這桌用什麼規則」集中成一份 `RuleSet`（frozen dataclass），計分、牌山、流程、結算都讀它。
  欄位：`start`／`return_pts`／`uma`／`start_3`／`return_3`／`uma_3`、`leftover_sticks`（`"top"`／`"none"`）、`tobi`、`red_fives`／`red_fives_3`、
  `ippatsu`／`ura`／`kuitan`／`kiriage`、`kazoe`（`"yakuman"`／`"sanbaiman"`）、`double_yakuman`、`renhou`（`"none"`／`"mangan"`）、`pao`（適用的役滿名 tuple）、
  `riichi_need_1000`、`multi_ron`（`"double"`／`"triple"`／`"atamahane"`）、`abortive`（frozenset，`ABORT_ALL`＝九種九牌／四風連打／四槓散了／四家立直／三家和）、
  `nagashi`、`kan_dora`（`"immediate"`／`"tenhou"`）、`west`／`west_target`／`west_target_3`、`agari_yame`、`sanma_ok`、`label`（三語顯示名）。
  `PRESETS` 五套（數值依各組織官方規則，2026-11 查證；官方沒寫清楚的少數項目採常見慣例，註解在各欄旁）：

  | 項目 | default（預設） | tenhou（天鳳） | majsoul（雀魂） | mleague（M聯盟） | wrc（WRC） |
  |------|------|------|------|------|------|
  | 起始／返點（四麻） | 25000／30000 | 25000／30000 | 25000／25000 | 25000／30000 | 30000／30000 |
  | 順位馬（四麻） | 20/10/−10/−20 | 20/10/−10/−20 | 15/5/−5/−15 | 30/10/−10/−30 | 15/5/−5/−15 |
  | 起始／返點（三麻） | 35000／40000 | 35000／40000 | 35000／35000 | （無三麻） | （無三麻） |
  | 順位馬（三麻） | 20/0/−20 | 20/0/−20 | 15/0/−15 | — | — |
  | 赤寶牌（四麻／三麻） | 3／2 | 3／2 | 3／2 | 3／2 | 0／0 |
  | 一發／裏寶牌／喰斷 | 有 | 有 | 有 | 有 | 有 |
  | 切上滿貫 | 無 | 無 | 無 | 有 | 有 |
  | 13 飜以上 | 累計役滿 | 累計役滿 | 累計役滿 | 封頂三倍滿 | 累計役滿（13 飜＝單倍役滿點數） |
  | 雙倍役滿 | 有 | 無 | 有 | 無 | 無 |
  | 人和 | 無 | 無 | 無 | 無 | 滿貫 |
  | 包牌 | 無 | 大三元、大四喜 | 大三元、大四喜 | 大三元、大四喜、四槓子 | 大三元、大四喜、四槓子 |
  | 立直需 1000 點 | 是 | 是 | 是 | 是 | 否 |
  | 多家榮和 | double | double | triple | atamahane | atamahane |
  | 途中流局 | 全部五種 | 全部五種 | 四種（無三家和） | 無 | 無 |
  | 流局滿貫 | 有 | 有 | 有 | 無 | 無 |
  | 槓寶牌 | immediate | tenhou | tenhou | immediate | immediate |
  | 擊飛 | 有 | 有 | 有 | 無 | 無 |
  | 南入／西入（門檻四麻 30000／三麻 40000） | 無 | 有 | 有 | 無 | 無 |
  | 和了止め | 無 | 有 | 有 | 無 | 無 |
  | 終局立直棒 | 消失 | 給第一名 | 給第一名 | 給第一名 | 消失 |

  `for_mode(sanma)` 依人數取出 `start`／`return_pts`／`uma`／`red`／`west_target`；`abortive_for(sanma)` 三麻自動去掉四風連打／四家立直／三家和。
  M聯盟與 WRC `sanma_ok=False`，三麻時 `resolve` 退回 `default`。預設 `default` 的行為與 0.8.x 完全相同。
  房間設定 `_room_configs[gid]` 新增 `"rules"`（預設名稱）與 `"rule_over"`（細項改過的欄位）；`preset_key` 相容舊的 `"ruleset"`（`mixed`→default、`tenhou`→tenhou），
  `resolve(config)` 先取預設，再套 `rule_over`（只認 `OVERRIDABLE`）、面板的 `tobi`／`abortive` 開關，細項赤寶牌的有／無對三麻同步成 2／0。
  `to_dict` 供存檔與牌譜用。`flow.launch_game`／`launch_dm_game`／`voice._config_from_settings`／`/mahjong start` 的設定都寫入 `rules`／`rule_over`；
  `flow._start_points` 沒自訂起始點數時改讀規則（WRC 30000）。
- **規則細項**：`views.RuleDetailsView`（按「🔧 細項」＝`RoomSettingsView.show_details`，ephemeral）＋`_DetailButton`，每顆對應 `rulesets.OVERRIDABLE` 的一個欄位：
  `ippatsu`、`ura`、`red_fives`（有／無，三張或兩張依人數）、`kuitan`、`kiriage`、`multi_ron`（double → triple → atamahane 輪替）、`leftover_sticks`（top／none）。
  `toggle` 只在跟目前預設（`base_rules()`）不同時才寫進 `panel.rule_over`；換預設（`rules_select`）會清空 `rule_over`。改完用 `refresh_panel` 更新共用面板訊息
  （`origin` ephemeral 互動或 `panel_message`）。i18n `rules.det.*`、`rules.v.*`、`rules.multi.*`、`rules.left.*`（三語）。
- **規則摘要**：`rulesets.summary(rs, lang, sanma, selected, start_points, hanchan)` 產生 4～6 行說明（預設名稱一行，再依「點數」「寶牌與役」「流局與結束」三個標籤各一行：起始／返點／馬點、赤牌與各役開關、途中流局／擊飛／南入／多家榮和／終局立直棒；
  `rs.west` 或 `rs.agari_yame` 才多第 4 行，東風寫「南入」、半莊寫「西入」，不確定時兩個都寫；選了無三麻規則的預設又開三麻時多一行退回預設的說明）。
  房間自訂起始點數時，起始、返點、延長門檻以同樣的差額平移。`RoomSettingsView.panel_content` 把它接在開頭文字後面，開面板與每次按鈕後都刷新；語音房面板改成 `base_content=voice.settings_hint`。i18n `rules.sum.*`。
- **一炮多響三種處理**：`flow.collect_reactions_t(..., multi_ron=)` 依 `rs.multi_ron`：`atamahane` 時多人榮和只取離放銃者最近的下家，其餘人以 `("ron", uid, 被略過者 uid 清單)` 的 extra 帶回
  （不會被當成「放過」而記振聽）；`triple` 時三家和照一般多家榮和處理；`double` 時三家和依途中流局設定（`sancha_abort`）。
- **途中流局分種類**：`rs.abortive_for(is_sanma)` 決定這桌的途中流局集合，`play_hand_t` 逐項檢查（九種九牌、四風連打、四槓散了、四家立直、三家和各自獨立，`"三家和" in abortive` 傳給 `collect_reactions_t`）；
  流局滿貫另看 `rs.nagashi`。舊的面板 `abortive` 開關仍有效（關＝空集合，開而預設沒有＝`ABORT_ALL`）。
- **南入／西入與和了止め**：`settlement.west_target(gs, rs, start_points)`（依人數取門檻，自訂起始點數就平移）；`settlement.is_game_over(..., rs=, start_points=, renchan=)`：
  `rs.west` 時預定最後一局（東風戰東4、半莊南4）打完沒人達門檻就延長，延長中任何一局結束有人達標就結束，最多到延長場風的最後一局（東風戰南4、半莊西4）；擊飛先判。
  `rs.agari_yame` 時預定最後一局莊家連莊且是單獨第一、達門檻就結束。`flow` 以 `_renchan`（途中流局不算）傳入。不給 `rs` 時維持舊行為。
- **包牌**：`settlement.attach_pao`（把包牌紀錄掛到 `result.pao`，底點＝8000×倍數，`double_yakuman` 才算雙倍）、`_ron_pays`（榮和：包牌役滿的部分由責任者與放銃者各付一半，本場由責任者付）、
  `_pao_tsumo`（自摸：責任者代付其他人在該役滿的份並負擔全部本場）。`flow` 的 `pao_map` 每局重算，`_note_pao` 在碰／大明槓成立時登記
  （第 3 組三元牌＝大三元、第 4 組風牌＝大四喜、第 4 個槓＝四槓子且限大明槓），只登記 `rs.pao` 有列的役滿；`_pao` 在各條和牌路徑掛上，結果訊息多顯示誰負責支付（i18n `result.pao`）。
- **人和（WRC）**：`flow._is_renhou(p, any_call)`（閒家、尚未打過牌、場上無任何鳴牌含暗槓）；`rules.build_ctx(..., is_renhou=)` 傳給 `WinContext.is_renhou`，
  `scoring` 在 `renhou == "mangan"` 且沒有役滿時，人和固定算滿貫，唯一的役是「人和」，不與其他役／寶牌複合；`ai_can_ron` 也認人和。
- **切上滿貫與 13 飜封頂**：`WinContext` 新增 `ippatsu_on`、`ura_on`、`kuitan`、`kiriage`、`kazoe`、`renhou`、`is_renhou`，`rules.build_ctx` 從 `rs` 填入（裏寶牌關閉時 `ura_tiles` 為空、`allow_double_yakuman=rs.double_yakuman`）。
  `_finalize_points`：`kiriage` 時 base ≥ 1920（4 飜 30 符、3 飜 60 符）算滿貫；`kazoe == "sanbaiman"` 時 13 飜以上不升累計役滿，維持三倍滿。喰斷關閉時副露的斷么九不成立（暗槓視為門前）。
- **槓寶牌後翻**：`flow` 的 `_kan_dora(closed)`／`_flush_dora()`／`pending_dora`：`rs.kan_dora == "tenhou"` 時暗槓立刻翻，大明槓與加槓先記著，等這個人打牌之後（別家反應與榮和之前）才翻；
  打牌前又宣告下一個槓時先把前一個翻出來；嶺上開花、搶槓時沒打牌就不翻。`"immediate"` 維持立刻翻。
- **赤寶牌開關**：`engine.generate_wall_yonma`／`generate_wall_sanma`／`_tenhou_wall`／`_tenhou_tile` 與 `new_game` 新增 `red_fives` 參數（0＝全部不標赤），`GameState.red_fives` 存進 `to_dict`／`from_dict`，下一局沿用；
  `launch_game` 等處以 `rs.for_mode(is_sanma)["red"]` 傳入。
- **終局立直棒**：`settlement.final_standings(..., rs=)` 在 `rs.leftover_sticks == "top"` 時把桌上剩的立直棒加進第一名點數再精算，`"none"` 為消失；
  馬點／原點／返點也改讀 `rs.for_mode`，房間自訂起始點數時返點＝自訂起始＋（規則返點 − 規則起始）。不給 `rs` 維持舊行為。

### 變更
- **電腦語音預設曼波**：`config.DEFAULT_AI_VOICE`（環境變數 `DEFAULT_AI_VOICE`，預設 `AI曼波`）。`sfx._ai_pack` 依序看房主、同桌真人（要在語音房）的 `user_prefs.ai_voice`：有語音包就用；空字串＝明確選了「電腦不出聲」→ 不出聲；都沒設定過（NULL）→ 用預設語音包（`_pack_ok` 檢查資料夾存在，不存在＝不出聲）。
  `voice.VoicePackSelect`（電腦語音）選「不出聲」改存空字串（原本存 NULL，所以以前關過的人會變回預設、要再關一次），沒設定過時選單標成預設語音包。
- **牌河每家一則訊息**：
  - 渲染（`render.py`）：新增 `board_parts`（公開牌桌：`board_header_text` 標頭＋每家 `board_player_text`）、`board_status_text`（底部狀態引言，沒有狀態放 `> —` 佔位）、
    `river_parts`（私人牌河：`river_header_text` 場況標頭＋每家 `river_player_text`）。牌河一律完整（`_river_str` 不再傳 cap），不再有「…」；
    每家單則若超過 1950 字（`_MSG_LIMIT`），`_fit_block` 改用 unicode 純字牌（每張 1 字、順序完整）重組，不截斷。原本放在各家名字前的 ▶ 移到標頭一行（`_turn_line`：▶ 風位「名字」），
    這樣出牌只會動到該家那一則＋標頭，換人不必多編輯一則。`make_board_text`／`make_thread_board`／`river_panel`／`river_message_text` 保留為「整桌合成一段」的舊介面（發送路徑不再使用）。
  - 流程（`flow.py`）：新增 `MsgGroup`（`send`／`edit_jobs`／`sync`／`delete`，記下每則上次送出的內容，只編輯內容有變的）。`_threads[gid]["board_msg"]`（觀戰牌桌：標頭＋每家＋狀態，「🌐 翻譯」鈕掛在最後的狀態訊息）
    與 `["river_msg"][uid]`（標頭＋每家）都改存 `MsgGroup`；`setup_channels`／`setup_threads`／`setup_dms` 建立，`play_hand_t` 的 `_river_edits`／`render_board`、`wait_turn_action`、換局流程改用 `edit_jobs`／`sync`
    （原本的 `_river_cache` 移除，快取改在群組內）；結算時 `delete()` 逐則刪除。`ui.TranslateBoardButton` 與 `/mahjong status`（`commands.cmd_status`）改成分則私密訊息送出（首則回應、其餘 followup）。
  - 重啟接續：牌桌訊息本來就不存 ID（接續時舊頻道由 `purge_saved_surfaces` 整個刪掉、`setup_threads` 重發整組），所以不需要新的存檔欄位，舊存檔照常接續。
- **自動操作每局重置**：`match_loop_t` 在一局結算完、發下一手牌之前清掉該局 `_auto_modes` 中這個 gid 的全部項目（面板標記由 `ui.auto_marker` 即時計算，自然消失）；`auto.title` 三語改成「每局開始時重置」。
- **段位賽改用天鳳預設**：段位賽設定沿用 `"ruleset": "tenhou"`，經 `preset_key` 對應到 `tenhou` 預設，因此一併有南入／西入、和了止め、包牌（大三元、大四喜）、槓寶牌後翻、終局立直棒歸第一名。
  雙倍役滿維持無（與原本的完全天鳳式相同）。
- **天鳳四麻東風戰順位馬**：`tenhou` 預設的 `uma` 一律 20/10/−10/−20（官方規則），不再分戰長用 10/5；`settlement._pick_uma` 與 `UMA_YONMA_TONPUU` 只剩沒給 `rs` 時的舊行為使用。
- **WRC 起始點數 30000**：`rulesets.PRESETS["wrc"].start=30000`（返點同），`flow._start_points` 改讀規則。
- **「📐 規則」按鈕改成下拉選單**：`RoomSettingsView.rules_select`（`row=0`，選項由 `_sync_rules_select` 依語言重建，M聯盟／WRC 附「僅四麻」說明）取代原本的切換按鈕；
  選預設時 `_sync_toggles_from_rules` 把擊飛、途中流局按鈕同步成該預設的值（之後玩家仍可自己切）。版面見下面「設定面板整理」。
- **起家隨機**：新增 `flow._prepare_seating(gid, players_info)`，在 `launch_game`、`launch_dm_game`、`launch_match_game` 呼叫 `new_game` 之前，用 `secrets.SystemRandom().shuffle` **就地**洗牌 `_waiting[gid]`
  （名單第 0 位＝seat 0＝東家／起家）。就地洗是為了讓 `match_loop_t` 與 `deal_next_hand` 之後每一手拿到的 `players_info` 順序都跟開局一致；
  `engine.new_game` 本身不洗牌（基準測試工具要固定座位）；斷線重連（`resume_game`）用快照裡的座位，不重洗。
  房主（`_room_owners[gid]`）不變，只是不再等於東家。牌桌上各面板（`render.make_thread_board`／牌河／手牌／結果）本來就照 `gs.players`（座位）順序排，不用改，起家自然排在最上面。
  大廳等待名單（`LobbyView._content`）還是照加入順序。語音房 `_start_voice_game` 的註解同步改掉（原本寫「房主＝東家＝members[0]」）。
- **電腦名字**：移除 `config.AI_NAMES`（AI(1)～AI(4)）。新增 i18n `bot.name`（zh_tw「電腦」／ja「CPU」／en「CPU」）與 `flow.bot_name(level, lang)`（預設回 `bot.name`）；
  `LobbyView.ai_btn`、`/mahjong start` 的 DM 補電腦、`voice._start_voice_game` 補電腦都改用它（不再編號）；
  房間設定在開房後才改（語音房的面板）也對，所以 `_prepare_seating` 在開局前會依房間設定與語言把所有 `is_bot` 的 `username` 重設一次。
  電腦名字現在會重複，已檢查沒有任何東西用 `username` 當鍵：統計、段位、牌譜的 `players`、`names`、`rank_of` 都用 `user_id`；電腦本來就不寫入 `game_records`／活躍度。
- **語音房開局訊息不 @ 房主**：`voice._start_voice_game` 開局後的訊息不再附 `voice.host`（「房主＝東家 @某人｜…」，三語都已刪），改附新的 `voice.end_hint`（沒有 mention 欄位：要結束對局按下面的按鈕，非房主需房主同意）。
- **設定面板整理**（`views.RoomSettingsView`）：目標是精簡、不換行、不靠顏色猜狀態。
  - （後續調整）`panel_content()` 改成：標題 → `settings.sum.game`（`**四麻・東風戰**`）→ `settings.sum.opts2`（`思考 25 秒・一番街牌山`＋電腦難度標籤）→ `rulesets.compact` 三行（`rules.sum.c1`＝`規則 **預設**{extra}`，`extra`＝`rules.sum.fallback`（`（M聯盟沒有三麻規則）`）＋`rules.diff.head`（`：切上滿貫・…`）；`rules.sum.pts`＝`25,000 起・30,000 返`；`rules.sum.uma`＝`馬 +20/+10/-10/-20`）。拿掉小字提示（刪除 `settings.hint`／`settings.hint_voice`）。以下是第一版的說明，差異行與提示已被這條取代。
  - `panel_content()` 預設 4 行：`base_content`（只有標題 `⚙️ **房間設定**`，語音房是 `voice.settings_hint`，沒有全形冒號或破折號）→ `settings.sum.game`（`**四麻・東風戰**　思考 25 秒・一番街牌山`，面板有選用的電腦開關時接上它的標籤文字，不帶表情符號）→ `rulesets.compact`（`rules.sum.c1`：`規則 **預設**　25,000 起・30,000 返・馬 +20/+10/-10/-20`）→ 小字提示（一般面板 `settings.hint`＝「開局前都能改；調好後按「確認」」，語音房 `settings.hint_voice`＝「…滿員自動開局時用當下的設定」，依 `show_confirm` 選）。
    有跟「預設」不同的項目時，在規則行後多一行 `-#` 小字「與預設不同：…」（`rules.diff.*`）：赤寶牌、一發、裏寶牌、喰斷、切上滿貫、擊飛、途中流局、多家榮和、終局立直棒，以及面板自己的食替（允許）與開立直（開），還有南入／西入（含門檻）、和了止め；
    選了沒有三麻規則的預設又開三麻時，退回預設的說明也放在這一行（`rules.sum.fallback`，不再獨立一行）。沒有差異就不顯示這行。完整列表的 `rulesets.summary` 只留給細項視窗（`RuleDetailsView.content`）使用。
  - 按鈕版面：第 0 列規則下拉、第 1 列「對局」（`toggle_sanma`、`toggle_length`、`modify_time`、`toggle_wall`、[`toggle_ai`]）、第 2 列「規則」（`toggle_tobi`、`toggle_abortive`、`show_details`）、第 3 列 `confirm`。
    食替（`kuikae`）與開立直（`open_riichi`）移到細項視窗當一般開關（`RuleDetailsView._PANEL_FIELDS`：直接改 `panel.kuikae`／`panel.open_riichi`，不進 `rule_over`，開局時 `commands`／`voice` 照舊從面板取值）；`toggle_kuikae`、`toggle_open_riichi` 移除。
  - 按鈕標籤改短，直接顯示目前的值：`四麻`／`三麻`、`東風戰`／`半莊戰`、`數值設定`、`牌山生成：一番街`／`牌山生成：天鳳`（`settings.wall_btn`）、電腦難度（私有的標籤，`_hard_ai_label` 會去掉開頭的表情符號）；開關用 `擊飛 ✓`／`擊飛 ✗`、`途中流局 ✓`／`✗`（藍色＝開、灰色＝關，另有記號不只靠顏色）。
    按鈕與面板文字拿掉裝飾用的表情符號（規則下拉選項、確認、完成、與電腦開始、語音包下拉等），只留標題 ⚙️、下拉選單 📐、🔧 細項。
  - 開關鈕的標籤與顏色統一由 `_refresh_buttons()` 依狀態設定（開或不是預設值＝`primary` 藍、關＝`secondary` 灰；`modify_time`、`show_details` 是動作鈕，固定灰），所有 toggle 回呼都改走 `_edit(interaction)`：刷新按鈕後同時編輯訊息的文字與 view，
    原本牌山、食替、開立直、電腦開關只更新 view 不更新文字。`_sync_toggles_from_rules`（換預設、三麻退回預設）也改呼叫 `_refresh_buttons`。
  - 語音房面板：`voice.CpuStartButton` 新增 `row` 參數，`_post_settings_panel` 固定放第 3 列（語音房沒有確認鈕，所以第 3 列是空的），語音包下拉 `VoicePackSelect` 在第 4 列；
    有沒有電腦開關都在 5 列 × 5 顆的限制內。
  - i18n（三語）：新增 `settings.hint`／`settings.hint_voice`、`settings.sum.game`、`settings.wallname.*`、`rules.sum.c1`、`rules.diff.*`、`rules.det.kuikae`／`openriichi`、`rules.v.allow`／`deny`、`bot.name`、`voice.end_hint`；
    改寫 `settings.prompt`、`voice.settings_hint`、`rules.sum.name`／`l1`～`l4`／`fallback`；刪除 `voice.host`、`settings.sum.opts`、`settings.v.kuikae_*`、`settings.sanma_state`、`settings.length_*`、`settings.wall_*`、`settings.kuikae_*`、`settings.openriichi_*`。
    各語言檔裡的「——」一併改成一般標點。
- **手牌面板不再列牌名文字**：`render.make_hand_panel` 移除 `player.hand_display_with_names()` 的呼叫與牌圖下面那行牌名文字（`engine.PlayerState.hand_display_with_names` 本身保留）。

### 修正
- **紅寶牌沒算到**：`scoring._score_standard` 把拆解出的面子（`_build_groups` 以 `_key_to_tile` 重建、不帶 `red` 標記）傳給 `_dora_han`，所以門前一般型手裡的紅五都數成 0 張（七對子、國士走原本的牌，不受影響；副露的紅五也不受影響）。
  現在改用原本的牌（手牌＋和牌張＋副露前三張）計算寶牌／裏寶牌／赤寶牌；寶牌與裏寶牌張數不變（只比花色數字）。
- **三元牌的寶牌順序顛倒**：`engine.GameState.get_doras` 與 `rules._indicator_to_dora`（裏寶牌）用 `(value % 3) + 1`，在 value 1=中、2=發、3=白 的編碼下變成 白→中、發→白、中→發。改成 `(value - 2) % 3 + 1`，即標準的 白→發→中→白。
- **點數不到 1000 也能立直**：`flow.play_hand_t` 的人類路徑（立直按鈕的可用條件）與電腦路徑（`ai_riichi`）原本沒檢查點數，點數 < 1000 仍可立直而變成負分。
  現在 `rs.riichi_need_1000` 為真（除 WRC 外的預設）時，持有 < 1000 點不能立直。

## [0.8.6] - 2026-10-31

### 新增
- **和牌儀式跳過**：`flow._SkipButton`（「⏭ 跳過」，i18n `btn.skip`；有進度時 `btn.skip_n`＝n/總人數）加在和牌儀式與結果訊息上；`ResultSkip` 每手牌共用一個 `asyncio.Event`，記錄已按的真人（排除掛機），全部按過（單人對電腦＝按一次）就 `set()`。`win_ceremony(..., skip=)` 與 `_result_countdown(..., skip=)` 收到事件後快轉：剩餘語音略過、5 秒倒數結束。自摸、榮和、雙榮、荒牌流局、途中流局都有。i18n `skip.*` 為提示文字（三語）。
- **自動功能**：`state._auto_modes` 以 `(gid, uid)` 為鍵存每人在該局的開關；`ui.AutoButton`（「⚙️ 自動」）、`AUTO_OPTIONS`（自動和牌／自動拔北（三麻限定）／不吃碰槓／自動摸切）、`auto_flags`、`auto_marker`；按鈕由 `flow._hand_panel` 放進手牌面板。`play_hand_t` 的 `auto_result`：摸牌後先判自摸（自動和牌開時直接和；自動摸切開時能自摸則不摸切），再依序處理自動拔北、自動摸切；摸到的牌被食替禁止時回到一般選牌。`collect_reactions_t`／`collect_chankan_t` 加入自動路徑：自動和牌時直接榮和（含搶槓）、不吃碰槓時略過吃碰槓詢問（榮和仍詢問）。自動動作走和手動相同的路徑（音效、feed、牌譜 log），不 @ 人；`_cleanup` 清除該局的 `_auto_modes`。i18n `btn.auto`、`auto.*`（三語）。

### 變更
- **邀請連結**：`permissions=8524499709721680` → `4905462335373169`（`Readme.MD`／`Readme.en.MD`／`Readme.ja.MD`、網站 `BOT_INVITE_URL` 預設值）。新組合比舊的多：Manage Roles、Manage Server、Speak、Mute／Deafen Members、Priority Speaker、Video、Use Voice Activity、Request to Speak、Manage Webhooks、Timeout Members、Use Soundboard、Use External Sounds、Set Voice Channel Status、Use External Stickers；少了：Attach Files、Use Activities、Create Polls、Use External Apps、Pin Messages。
- **名字旁顯示拔北數**：`render._kita_tag` 新增；牌河面板與「看牌河」視圖中，名字旁原本的數字是打牌張數，現在三麻顯示拔北數（僅 > 0 時，例如「（拔北×2）」），四麻不顯示。
- **自摸／榮和語音在宣告當下播放**：`flow._act_sound` 與新的 `_act_sound_together`（背景呼叫 `sfx.say_together`）移到 `feed.tsumo`／`feed.ron`／`feed.dblron`／`feed.chankan` 之前，按鈕按下或電腦決定和牌的當下就和畫面同時播放，雙榮兩家一起播。和牌儀式拿掉自摸／榮和的 `CeremonyVoice` 開場，`together` 參數一併移除；儀式仍唸役名與點數等級，流局滿貫的「流局」開場不變。
- **語音連線逾時的提示**：`sfx._ensure_client` 的例外分支原本一律提示「安裝語音套件」；現在 `TimeoutError` 改印網路／UDP／Discord 語音服務的提示，並在下一次播放音效時重試。

### 修正
- **立直後每張打牌都顯示「立直摸切」**：`flow.play_hand_t` 原本在立直後的摸切一律走 `feed.riichi_tsumogiri`（「{name} 立直摸切 {tile}」），人類路徑還會因自動打出被判為逾時而顯示「超時自動打出」。拿掉電腦與人類兩條路徑的 `feed.riichi_tsumogiri` 分支，改走一般的 `feed.discard`（「出牌了 X（摸切）」），只有宣告立直那張仍是「宣告立直！」；人類路徑的用詞判斷改為 `elif timed and not already_riichi:`（立直後自動摸切不算逾時）。`feed.riichi_tsumogiri` 的 i18n 鍵保留，舊牌譜回放（`replay.DISCARD_KEYS`）仍能顯示。此問題在已發佈版本就存在。
- **機器人重啟後等待中的語音房不會刪除**：等人的語音房只記在記憶體，重啟後成為殘留房。新增 `voice.recover_rooms`（開機時掃描各伺服器的語音類別）：以 `_is_room_name` 辨識房間名稱；`_interrupted_voice_ids` 排除被中斷對局的語音房（交給繼續／結束流程）；`_is_orphan_room` 判斷沒人的殘留房並刪除；有人在等的房間重新接管——換新的房號、`_post_settings_panel` 重發設定面板，已滿 4 人則自動開局。`handle_voice_update` 另加步驟 3b 作為保險：未被追蹤的空房在最後一人離開時刪除。`run.on_ready` 每個行程只呼叫一次 `recover_rooms`。

## [0.8.5] - 2026-10-23

### 新增
- **天鳳牌山生成**：`engine.py` 新增 `WALL_RIICHI_CITY`／`WALL_TENHOU`／`WALL_METHODS`、`_MT19937`（mt19937ar.c 的
  `init_by_array`＋`genrand_int32`）、`_tenhou_tile`（天鳳牌號 0~135 → `Tile`：`//4`＝種類，萬筒索、東南西北、白發中；
  赤五＝16／52／88）、`tenhou_yama(seed, sanma)`（288 個 `genrand_int32` 以小端序串成 1152 bytes，每 128 bytes 一次
  SHA-512 共 9 次 → 144 個 uint32 `rnd`；`yama[i]=i`，`i=0..n-2` 做 `swap(yama[i], yama[i + rnd[i] % (n-i)])`；三麻去掉
  牌號 4~31＝2~8 萬）、`_tenhou_wall(sanma)`（`secrets.randbits(32)` ×624 當種子，回傳牌山、編碼、SHA-256、種子十六進位）。
  每局一組新種子＝天鳳範例的 nKyoku=0。`GameState` 加 `wall_method`、`wall_rng_seed`（`to_dict`／`from_dict`，舊資料預設
  一番街）；`new_game(..., wall_method=...)`。驗證：MT19937 與 Python `random`（同為 MT19937）輸出一致、mt19937ar 官方測資
  `init_by_array([0x123,0x234,0x345,0x456])` 前 5 個一致；天鳳 `validation.txt` 的 base64 種子解出 624 個 init、以
  `tenhou_yama` 算出的 136 張牌山與官方 `yama` **逐張相同**；四麻 34 種各 4 張、3 張赤五，三麻 27 種、2 張赤五。
  - 設定：`RoomSettingsView.wall_method`＋`toggle_wall`（「🎲 牌山：一番街／天鳳」，`settings.wall_*` 三語），標籤 tuple
    多一格；`/mahjong start`、`voice._config_from_settings`（含無面板預設）的設定加 `"wall"`。`launch_game`、`launch_dm_game`
    依 `config["wall"]` 開局，`deal_next_hand` 沿用 `prev.wall_method`；牌譜 `gamestart`／`handstart` 多記 `method`、`rng`。
  - `ui.send_fairness`：顯示牌山生成方式（`_WALL_NAMES`）；對局結束且為天鳳方式時以 `discord.File` 附
    `tenhou_seed_<gid>.txt`（624 個種子、SHA-256、牌山編碼），說明照 sample_code.txt（nKyoku=0）即可重算。
- **電腦語音**：`user_prefs` 加 `ai_voice`（舊資料庫 ALTER 補欄）；`db.get_user_ai_voice`／`set_user_ai_voice`。
  `sfx.AI` 標記＋`_ai_pack(gid, vc)`（房主優先，再來同桌真人，都要還在語音房；資料夾不存在＝無）、`_has_bots`；
  `_voice_pack(vc, uid, gid)` 遇到 `AI` 改用 `_ai_pack`；`_pick`／`say_together` 帶 gid；`_any_pack` 有電腦且有電腦語音也算
  （機器人才會進語音）。`flow._voice_uid`：電腦回傳 `sfx.AI`（掛機仍為 None）——吃碰槓、打牌、和牌儀式、九種九牌
  原本就走 `_voice_uid`，電腦自動有聲；電腦分支另補立直（兩立直優先、退回 riichi）與拔北的音效。
  `VoicePackSelect(..., ai=True)` 讀寫 `ai_voice`、不觸發提早進房；新指令 `/mahjong ai_voice`（沒有語音包時回
  `voice.no_packs`），大廳「🎚️ 語音」第二列放電腦語音選單。i18n `voice.ai_*`、`voice.no_packs`、`cmdtr.*`。
  以暫存資料庫實測：房主設了 → 電腦用該包、`_any_pack` 為真；房主離開語音房 → 無。
- **拔北事件音**：`sfx.EVENT_SOUNDS` 加 `kita`（`ALL_SOUND_NAMES` 73→74）；拔北後以 `_voice_uid(player)` 播放（電腦用電腦語音）。
- **指南權限提醒**：`commands.guide_text(lang, guild)`＝`guide.text`＋`_guide_perm_block`（依 `guild.me.guild_permissions`
  列出缺少的 `perms.CORE`＋`THREADS`（必要）與 `perms.FEATURES`（附 `perm.use.*` 用途，不含只給私有後台用的
  Create Invite）；管理員或都不缺＝不顯示）。新建指南、補發、切換顯示語言／伺服器語言都用它；`run.ensure_guild_setup`
  啟動時找到既有指南訊息，依 `_guide_lang` 保留原語言、內容或按鈕不同就更新。新增 i18n `guide.perm_*`、`perm.use.*`。

### 變更
- **log**：`run.py` 設 `logging.getLogger("discord.player").setLevel(WARNING)`，蓋掉每次播放結束的 INFO（`discord.utils.setup_logging` 只設根 logger，子 logger 的等級會保留，已實測）。
- **上弦月／下弦月對調**：`rating.TIERS` 第 4、5 階名稱互換（所需 pt、`PLACE_PT_4`／`PLACE_PT_3` 依階索引不動，註解跟著換）；`DEMOTABLE_TIER = 3` 不變（現在是下弦月）。資料庫存 `dan_idx`，不需遷移：原第 4 階玩家改顯示下弦月。`rank.info` 三語的升級範例與降級起點一併改。
- **段位賽固定天鳳牌山**：`launch_match_game` 設定加 `"wall": "tenhou" if ranked else "riichi_city"`（隨機匹配維持一番街）；
  `rank.info` 三語補上段位賽固定規則與牌山。
- **役名「拔北」→「北寶牌」**：`scoring` 的懸賞名、`commands._BONUS`、`sfx.YAKU_SOUNDS` 改為 `北寶牌`；ja／en 加
  `yaku.北寶牌`（保留 `yaku.拔北`）；`i18n.yaku` 先經 `_RENAMED = {"拔北": "北寶牌"}`，舊牌譜／回放的舊役名也顯示新名。
  `yakulist.notes` 改字。音效別名 `sfx._ALIASES = {"北寶牌": ("拔北", "北")}`，`_resolve` 每個資料夾依序找原名與別名
  （`pack_coverage` 一併適用）。
- **文件**：README 三語（功能、角色語音、指令表、房間設定、致謝加天鳳 <https://tenhou.net/stat/rand/>）；`Rules guide.MD`
  對局結構補途中流局／牌山生成／段位賽固定規則、新增途中流局表與流局滿貫，指令表換成現行指令。
- **語音房上限＋1**：`voice.fit_room_limit(vc, n)` 一律設 `n + 1`（拿掉 `reserve_bot` 與權限判斷）；新建語音房 5、
  三麻開局 4；`RoomSettingsView.toggle_sanma` 同步。原因：0.8.4 起機器人開局前就會進房，會佔一個名額。

### 修正
- **動作音效時機**：新增 `flow._act_sound(gid, name, p, fallback)`（`sfx.play` 只排進佇列、不等播完），電腦拔北／立直／打牌、真人拔北／暗槓／立直（含開立直、兩立直）／打牌、碰／吃／明槓都改在 `render_hand`／`render_board` **之前**呼叫；加槓改在宣告（`feed.kakan_declare`）當下喊；拿掉迴圈尾端共用的 `discard` 音效。原本動作音效排在 `render_board`（含 `refresh_feeds` 逐一編輯每位玩家的牌河與面板）之後，真人立直則在畫面更新前。`play_hand_t` 的 `refresh_rivers`／`refresh_feeds` 改為先依各人牌風算好文字（`_apply_skin` 是全域狀態，必須同步算）再 `asyncio.gather` 同時送出編輯。途中流局 `_draw_sound` 移到 `render_board` 前；荒牌流局的公開＋私人結果訊息改同時送出。和牌儀式（`win_ceremony` 的 `beat`）不動。無頭模擬：全電腦 6 局（四麻／三麻）音效 302 次、0 次落在牌桌更新之後；一位掛機真人＋三電腦 1 局，面板 67 次、牌河 53 次編輯正常送出。
- **裏寶牌 0 飜不唸**：`scoring` 立直時一律列出 `("裏寶牌", n)`（n 可為 0，方便確認），`win_ceremony` 逐役揭曉時`han == 0` 改傳 `sound=None`（照固定節奏揭曉，不呼叫 `voice.say`）。
- **中斷對局的舊頻道**：重啟後 `_threads` 已不在記憶體，`finalize_interrupted` 只公布結算、`resume_game` 另建新頻道，舊的頻道／討論串、語音房、開局公告都沒人收。新增 `flow.save_surfaces(gid)`：把`_deletable_surfaces` 的 ID、語音房 ID、開局公告（頻道 ID＋訊息 ID）寫進 `room_config["surfaces"]`，在 `launch_game`、`resume_game` 開局後與 `voice._start_voice_game` 綁定語音房後呼叫。`purge_saved_surfaces(guild, config, voice)` 依紀錄刪除（`get_channel_or_thread`→`fetch_channel`，找不到就略過）：`finalize_interrupted` 結算後全刪（含語音房，先 `sfx.leave`）；`resume_game` 建新頻道前刪舊的、`voice=False` 保留語音房並重新綁回 `th["voice"]`。以假伺服器物件實測：繼續＝刪 3 個頻道＋公告、結束＝再加語音房；沒有紀錄的舊對局不出錯。
- **對局中語音房不刪**：`handle_voice_update` 第 3 段，房裡沒有真人時若 `starting`（開局中／對局中）或
  `_room_in_game(room.id)`（`_threads[gid]["voice"]` 是這間且 `gid in _games`）就保留；對局結束仍由 `cleanup_room` 收掉。
- **語音連線排隊**：`sfx._ensure_client` 以每台伺服器一把 `asyncio.Lock`（`_connect_locks`）包住，實際邏輯移到
  `_ensure_client_locked`。原本兩人同時進房（`join_room`）等情況會同時呼叫 `vc.connect()`，Discord 語音閘道回
  4006（Session no longer valid）後由 discord.py 重試。以假 VoiceChannel 實測：同時三次呼叫只連線一次、三個都拿到連線。
- **拔北**：`play_hand_t` 拔北後設 `kita_again`，下一圈（同一人補摸）不發 `feed.your_turn`、不重發 `ping.your_turn`，
  沿用原本的提醒訊息（`kept_ping`，結果是拔北時不刪，回合真正結束才刪；牌山剛好摸完也會收掉）；拔北後不再先
  `render_hand`（下一步 `wait_turn_action` 會直接刷新面板）。拔北鈕加連點保護：`player._kita_at` 記時間，
  `KITA_GUARD`（2 秒）內再按拔北直接忽略。`wait_turn_action` 新增 `_lock_and_ack(inter)`：決定動作的按鈕（出牌、拔北、
  暗槓、加槓、九種、自摸、立直宣言牌）一按就把整個 view 停用並 `edit_message`，取代原本只 `defer()`（沒有可見反應，
  玩家以為沒按到會再按）。以無頭模擬實測（真實三麻牌局＋假頻道）：按一次拔北只拔一張、新面板馬上連點第二次被擋下、
  按鈕立即變灰；0.8.4 程式同樣模擬也只拔一張，確認多拔的那張來自第二次點擊。
- **指南刪除記憶**：`guild_settings` 加 `guide_dismissed`（舊資料庫以 ALTER 補欄）；`db.get_guide_dismissed`／
  `set_guide_dismissed`。`GuideView.delete_btn` 刪除成功後設為 1；`run.ensure_guild_setup` 有大廳或已刪過就不建指南；
  `on_guild_join` 先清成 0 再建（重新邀請＝重新開始）。以舊格式資料庫實測：沒刪過會建、刪過重啟不建、重新邀請會再建。

## [0.8.4] - 2026-10-08

### 變更
- **多語言大廳**：新表 `guild_hubs`（id／guild_id／lang／category_id／lobby_channel_id／hub_voice_id／play_channel_id），
  `guild_settings` 只剩 `play_channel_id`（/setup channel 指定）與 `guild_lang`。`PRAGMA user_version` 2：把舊
  `guild_settings` 的大廳欄位搬進 `guild_hubs`（lang 留空），`run.ensure_guild_setup` 啟動時依類別／大廳／配對語音
  的頻道名稱比對各語言的 `hub.*_name` 補上語言，認不出就用伺服器主要語言。`db.get_hubs`／`add_hub`／`set_hub_lang`／
  `delete_hub`；`get_guild_setup` 改回傳第一個大廳；`set_guild_setup` 移除；`delete_guild_settings` 一併刪大廳。
  新模組 `mahjong/hubs.py`：`all_hubs`／`for_lang`／`by_id`／`of_channel`（頻道是大廳／開房頻道／配對語音，或在其類別內，
  討論串看母頻道）／`lang_for`（多個大廳且頻道屬於其一＝該大廳語言，否則伺服器語言）／`lobby_ids`／`play_ids`。
  - `i18n.get_user_lang(user, guild, channel=None)`：沒有個人設定時，有 channel 就用 `hubs.lang_for`；commands／voice／ui／flow
    裡拿得到 interaction 的呼叫一律帶 `interaction.channel`（個人明確設定的語言仍優先）。
  - `/setup create`：同語言已有大廳（頻道還在）回 `hub.already`；同語言紀錄但頻道被刪＝刪紀錄重建；不同語言就 `_build_hub`
    再建一套並 `db.add_hub`。建立時不再把開房頻道設成整台伺服器的指定遊玩頻道；`/mahjong start`／`join` 允許的頻道＝
    指定遊玩頻道 ∪ 每個大廳的開房頻道（`_play_channels`）。移除 `_existing_lobby`／`_remove_hub`／`RebuildHubView` 與
    `hub.rebuild_*` 語言鍵。
  - 房間語言：在大廳的頻道 `/mahjong start` ＝ `hubs.lang_for`（其他頻道仍是房主語言），開房時彈出的
    `RoomSettingsView`（與其 ⚙️ 數值設定 `RoomSettingsModal`）也用這個 `room_lang`，不再用房主個人語言；語音配對以進入的配對語音找大廳
    （`_voice_rooms[vc]["hub_id"]`），語音房名稱、設定面板、三麻提示與開局後的房間語言都用 `hubs.lang_for`；
    `_lobby_channel(guild, hub)` 依序找該大廳的開房頻道 → 大廳 → 類別內文字頻道 → 指定遊玩頻道。
  - 對局頻道類別 `flow._guild_category(guild, channel)`、回放頻道類別都改用開房頻道所屬的大廳；大廳打字即刪的快取改為
    所有大廳頻道的集合；`teardown_hub` 逐一收掉每個大廳；`perms.guild_report` 逐一檢查每個大廳的三個頻道。
  - 以假 guild 實測：中文建立 → 英文另建（舊的不刪）→ 再建中文被擋；英文開房頻道／英文類別內對局頻道＝en、
    中文配對語音＝zh_tw、只剩一個大廳＝伺服器語言；舊資料庫轉換與語言辨識正確。
- **結束訊息＋刪除按鈕發到每個頻道**：`flow._finish_channels` 除了公開頻道，也對每位玩家的手牌頻道（DM 除外）
  各用該玩家語言發 `room.ended`＋`RoomDeleteButton`；任一顆按下都刪掉整場的頻道。
- **語音套件**：discord.py 2.7 的 `VoiceClient` 需要 `davey`（DAVE 語音端對端加密），缺少時 `connect()` 拋
  `RuntimeError('davey library needed…')`。`requirements.txt` 改為 `discord.py[voice]>=2.7.0`（帶 PyNaCl、davey），
  拿掉單獨的 PyNaCl 行；`sfx.load` 用 `importlib.util.find_spec` 檢查 `nacl`／`davey`，缺少就印提示；進語音失敗的提示同步改寫。
- **結束遊戲按鈕競態**：`EndGameButton` 在對局已不存在時的回覆包 `try/except discord.HTTPException`（語音房在對局結束時
  會被 `cleanup_room` 刪掉，同時按下就會 404 Unknown Channel）。
- **提早進語音房**：新增 `sfx.join_room(vc, uid)`（未就緒、這人沒選語音包、不在房裡就不進）。`voice.handle_voice_update`
  在玩家剛進入受管理的語音房時呼叫；`VoicePackSelect` 選了語音包、且選的人正在受管理的語音房裡也呼叫。
- **一台伺服器只能進一個語音頻道**：新增 `sfx._busy_elsewhere(vc)`——機器人目前所在的語音房若綁著進行中的對局（`_threads[gid]["voice"]`
  且 `gid in _games`），`_ensure_client` 就不 `move_to` 過來（回 None＝這場不出聲）；只是開局前待著的房間則照常搬。
  `sfx.leave(guild, vc)` 帶這場的語音房，機器人不在這間就不斷線（`flow._leave_voice_room` 傳入）。
- **和牌儀式與語音同步**：`flow.win_ceremony(targets, …, voice=CeremonyVoice)` 改為一次處理所有頻道（同語言共用文字），
  每一步 `beat()`：先改好所有訊息（等編輯 API 回來），再播音並等它唸完，整步至少原本的間隔（標題 1.0／手牌 1.0／每役
  0.9 秒）。不邊改邊播是因為同一則訊息頻繁編輯會被 Discord 限速延後，聲音會跑在文字前面（尤其前一個役沒錄音、步調變快時）。
  步驟：標題＋開場音（自摸／榮；雙榮第一位 `say_together("ron")`；流局滿貫 `say_table("ryuukyoku")`）→ 寶牌與手牌 →
  逐役唸役名 → 點數＋`tier_sound`（滿貫以上）。`sfx` 新增 `say`／`say_table`／`say_together`（排入佇列並等播完，
  上限 `SAY_TIMEOUT` 8 秒）、`_pick`（`play` 與 `say` 共用找檔）；佇列項目多帶 `done` 事件，播完或 `leave` 清佇列時 set。
  移除 `play_win`／`play_wins`／`play_together`／`WIN_INTRO_GAP`。雙榮改為依序對所有頻道各跑一次儀式（不再每個頻道各自跑一串）。
  以假頻道＋每句 1.2 秒的假語音實測：每個役名開始唸的同時兩個頻道都揭曉該役，下一役等唸完才出現。

### 修正
- **公開面用房間語言**：`_thread_langs(public, private, lang)` 公開面改用房間語言；和牌儀式、流局結果文字、
  `msg.player_back`、`msg.afk_takeover` 的公開通知同改用 `config["lang"]`（原本固定母本中文）。

## [0.8.3] - 2026-10-08

### 變更
- **重建大廳**：`_do_setup_create(interaction, lang=None)` 拆成檢查＋`_build_hub(interaction, lang)`（原建立本體，
  呼叫前已回應、結果走 followup）。已有大廳（`_existing_lobby`）時回 `hub.already`（帶目標語言）＋`RebuildHubView`：
  按「🔁」先查 `flow.list_rooms(guild)`，有房就回 `hub.rebuild_busy`；否則 `_remove_hub(guild)` 刪本伺服器還沒開局的
  語音配對房、大廳、配對語音、建在類別內的開房頻道（並清掉 play channel 設定），類別空了才刪，保留指南與
  `guild_settings` 其他欄位；接著 `_build_hub` 用新語言重建，刪不掉的舊頻道以 `hub.rebuild_left` 列出。
  指南的 `guide:create` 鈕以 `_guide_lang(message)`（比對指南訊息標題行）取得目前顯示語言傳入。
  i18n 佔位符用 `{language}`（`{lang}` 會和 `i18n.t` 的參數撞名）。以假 guild 實測：中文建立 → 英文重建、有房時擋下。

### 新增
- **兩立直／開立直事件音**：`sfx.EVENT_SOUNDS` 加 `double_riichi`、`open_riichi`（`ALL_SOUND_NAMES` 71→73）。
  `flow` 真人立直時依 `action == "riichi_open"`（優先）→ `double_rii[seat]` → 一般，選 `open_riichi`／`double_riichi`／`riichi`，
  以 `fallback="riichi"` 播放（包裡沒有專屬檔就播 riichi）。第一巡開立直歸 `open_riichi`。電腦立直仍不出聲。
- **音效變體隨機**：新增 `sfx._variants(dir, name)`——列出資料夾內檔名（去副檔名）符合 `name` 或 `name[ _-]?數字`
  （不分大小寫、副檔名在 `AUDIO_EXTS` 內）的檔；`_resolve` 先找語音包、再找根目錄，有就 `random.choice`。
  只接受「名稱＋數字」，所以 `四暗刻` 不會吃到 `四暗刻單騎`。`play_together`（雙榮同時喊）兩人各自隨機。
  以暫存資料夾實測：四個變體約各 1/4，相近役名不互相混入，根目錄後備照常。

## [0.8.2] - 2026-10-05

### 修正
- **語音房名稱改用房號代碼**：`voice.handle_voice_update` 建語音房時先 `rooms.next_room_no()` 配好房號，名稱改為
  `voice.room_name + 代碼`，存進 `_voice_rooms[vc.id]["room_no"]`；`_start_voice_game` 以 `rooms.register(..., room_no)`
  沿用（`register` 新增選填 `room_no`）。移除行程內的 `_room_seq` 流水號。沒開成局的語音房會跳過該號，不影響。

## [0.8.1] - 2026-10-04

### 變更
- **段位分級（參考天鳳、雀魂）**：`rating.py` 改以 `TIERS`（階名, [1／2／3 級所需 pt]）攤平成 `LEVELS`
  （[(階, 級)]，頂點級＝0），`dan_idx` 改存 `LEVELS` 索引（共 22 級）。所需 pt 每一級嚴格遞增：同一階 ×1／×1.25／×1.5，
  下一階 1 級＝上一階 1 級 ×2（新月 100 起到日全食 3200／4000／4800）；新人另訂 20／30／40；血月無上限。
  新增 `tier_of()`／`level_index()`／`need_pt()`／`start_pt()`（新人＝0、其他＝所需的一半、血月用日全食 3 的一半）／
  `pt_text()`（「250/400」）／`ladder_text()`（每階一行列出三級所需）／`place_table(is_sanma)`（由 `dan_place_pt`
  逐階產生依順位的增減，相同的相鄰階合併成「新人～殘月」）。
  `dan_place_pt` 改查表 `PLACE_PT_4`／`PLACE_PT_3`（與 `TIERS` 同序），取代原本固定 +90／+45 加「墊底按階加扣」公式：
  四麻一位 40／60／80／120／180／260／380／480、二位為一位的一半、三位從下弦月起 −15／−30／−50／−70、
  四位 0／−20／−50／−110／−190／−290／−420／−540；三麻一位同四麻、二位 0（新人 +10）、
  三位 0／−20／−40／−90／−150／−235／−350／−465。低階整桌加總為正（新手容易往上），高階偏小。
  估算打完一整階（1～3 級）：中上水準（30／25／25／20%）上弦月約 26 場、下弦月 43、月偏食 67、日全食 92
  （新人到血月約 260 場）；平均水準日全食約 240 場。
- **指南「🏗️ 建立大廳」**：`/setup create` 本體抽成 `_do_setup_create(interaction)`，`GuideView` 加 persistent 按鈕
  `guide:create`（`guide.create_btn`）呼叫它；開頭先查 `get_guild_setup().lobby_channel_id`，頻道還在就回 `hub.already`
  不重建。`guide.text` 第 ① 步改為「按下方 🏗️ 建立大廳（或 /setup create）」。`run.ensure_guild_setup` 對已有訊息的
  指南頻道掃最近 10 則，找到機器人的指南（有 `guide:delete`、沒 `guide:create`）就 `edit(view=GuideView(lang))`。`/mahjong rankinfo` 在 `rank.info` 後附 `rank.pt_table`。`apply_dan` 一場最多升／降一級、溢出不帶走，
  升降後都從 `start_pt`；`DEMOTABLE_TIER = 3`（上弦月起才降級）；`dan_place_pt` 的墊底加扣與三位微扣改以「階」計，
  數值不變。移除 `DAN_LADDER`／`DAN_NAMES`／`DAN_NEED`／`DEMOTABLE_FROM`；`BLACK_SKIN_IDX`＝日全食 1。
- **血月起始**：`rating.TOP_START_PT = 10_000`，`start_pt(TOP_IDX)` 回傳它（原為日全食 3 的一半）；
  舊資料轉換時原本的血月也會拿到 10,000。`ladder_text` 的血月列顯示「（10k 起）」。
- **數字縮寫**：新增 `mahjong/numfmt.py`——`compact(n)`（<1000 照原樣；以上 k／M／B，依大小留 2／1／0 位小數、
  無條件捨去避免出現「1000k」）與 `signed(n)`。套用到 `/mahjong daily`／大廳簽到的獎勵與活躍度、`/mahjong tasks`、
  個人資訊卡活躍度、`/mahjong stats` 的獲得／失點、`rating.pt_text`／`ladder_text`、（私有）網站活躍度榜。
  終局分數、和了打點不縮寫。
- **配對**：`matchmaking._dan_of` 改回傳階（`tier_of`），`DAN_BAND = 2` 以階計、不看 1～3 級。
- **顯示**：`/mahjong stats`、段位賽結算（`rank.result`）、（私有）網站個人頁／排行榜／後台都改顯示 `pt_text`；
  `/mahjong rankinfo` 的階梯附每級所需 pt，`rank.info` 三語說明改寫（分級、翻倍、一半起始、新人例外）。
- **舊資料轉換**：`db.init_db` 以 `PRAGMA user_version` 標記，<1 時把 `user_rating.dan_idx`（舊＝階）換成該階 1 級、
  `dan_pt` 設為起始值，再設 `user_version = 1`（只跑一次）。以舊格式資料庫實測轉換與重跑皆正確。

## [0.8.0] - 2026-10-04

### 新增
- **個人角色語音**（取代 0.7.2 的伺服器語音包）：
  - `db`：`user_prefs` 加 `voice_pack TEXT` 欄（ALTER 遷移）、`get_user_voice()`／`set_user_voice()`；
    移除 `get_voice_pack()`／`set_voice_pack()`，`guild_settings` 不再建立 `voice_pack` 欄。
  - `sfx`：移除 `VOICE_OFF`／`_guild_pack`／`_guild_muted`。新增 `_user_pack(uid)`（DB 值且資料夾存在才算）、
    `_in_room(vc, uid)`（`vc.members` 有此人；讀不到＝視為在）、`_voice_pack(vc, uid)`（uid=None／不在房＝None）、
    `_humans(gid)`（對局中取 `_games`，開局前取 `_waiting`）、`_table_pack(gid, vc)`（房主 → 其他真人，第一個有包且在房的）、
    `_any_pack(gid, vc)`。`play(gid, name, uid=None)` 以 `_voice_pack` 決定，None＝靜默；內部哨兵 `_TABLE` 改用
    `_table_pack`，僅供 `play_start(gid)`（開局音）。`play_win(gid, result, uid=None)` 以和牌者的包唸役與階級；
    `join(vc, gid)` 只有語音房裡有本局真人選了包才進。
  - **同時播放（混音）**：佇列項目改為 `(vc, paths)`，`_enqueue(vc, *paths)`；`_source(paths)` 多檔時以
    `before_options` 加入其餘 `-i`（`shlex.quote`，Windows 路徑反斜線／中文安全），`-filter_complex
    amix=inputs=n:duration=longest:dropout_transition=0,volume=n,alimiter=limit=0.9`（音量補回、防爆音）。
    `play_together(gid, name, uids)` 各取自己的包、同檔去重後混成一則；`play_wins(gid, [(result, uid), …])`
    依序唸多位和牌者（只等一次 `WIN_INTRO_GAP`），`play_win` 改為其單人包裝。實測兩檔混音長度＝較長者、峰值不爆。
  - `flow`：新增 `_voice_uid(p)`（真人＝user_id；電腦或 `afk`＝None），打牌／暗槓／加槓／立直／碰／吃／大明槓的音效
    都帶當事人；電腦回合不再呼叫 sfx。和牌用 `win_seats[0]`；雙榮 `play_together("ron")`＋`play_wins` 依
    `dbl_winners` 順序（同揭曉順序）唸兩位。
  - `voice.VoicePackSelect(lang, row=4, uid=None)`：改寫為個人設定，選項「🔇 不使用語音」(`__none__`→存 None)＋
    各語音包，有 uid 時以 `default=True` 標出目前選擇；回覆 `voice.pack_set`／`voice.pack_cleared`。
    語音房設定面板與大廳 `hub:voice` 鈕共用（後者帶點擊者 uid）。開局 `sfx.join(vc, gid)`＋`sfx.play_start(gid)`。
  - `commands`：移除 `/setup voice`（`cmd_setup_voice`）與 `_voice_pack_autocomplete`。
  - i18n：`voice.pack_select`／`pack_off`／`pack_set`／`pack_prompt` 改為個人用語，新增 `voice.pack_cleared`，
    移除 `voice.off_hint`；`guide.text` 第 ③ 步改為「每個人在大廳按 🎚️ 語音選自己的角色語音」。
  - （私有後台）移除伺服器列表的 🎚️ 標籤與詳細頁「語音包」欄、`_pack_label()`。
- **`/mahjong fairness`**：`commands.cmd_fairness` 依序從 `_user_game`、`_thread_game`、`_channel_games` 找出
  使用者所在對局，找不到回 `msg.fairness_no_game`；內容由 `ui.send_fairness(interaction, gid)` 產生
  （進行中只給 SHA-256；已結束從 `db.get_game` 取 `wall_seed` 完整編碼）。
- **途中流局開關**：`RoomSettingsView.abortive`（預設 True）＋ `toggle_abortive` 鈕（`settings.abort_on／off`），
  房間設定 dict 加 `"abortive"`（`commands` 文字開房、`voice._config_from_settings`、匹配局固定 True）。
  `play_hand_t` 讀 `allow_abort`：關閉時 `kyuushu_ok` 為 False，四風連打／四家立直／四槓散了的檢查略過；
  `collect_reactions_t(..., kan_ok, sancha_abort)`——`sancha_abort=False` 時三家榮和不回 `sanchahou`，
  而是以頭跳順序回傳三個 uid 走 `dblron`（`settlement.apply_ron_multi` 本就支援 N 家：本場與供託歸頭跳側）。
  `feed.dblron` 參數由 `n1`／`n2` 改為 `names`（「a」「b」「c」串接）。
  另修：場上已四槓（`kan_count >= 4`）時不再提供暗槓／加槓／大明槓（原本四槓散了前的那一打仍可第五槓）。
  流局滿貫（`_is_nagashi`／`apply_nagashi`）不受開關影響。
- **網站數據連結**：`commands._web_stats_button(lang, row)`／`_web_stats_view(lang)`——連結按鈕指向
  `WEB_BASE_URL/me`（`web.more_stats`），沒設定 `WEB_BASE_URL` 就不附。掛在 `/mahjong stats`、`/mahjong profile`、
  大廳 👤 個人資訊與 `HistoryView` 第二排。
- **流局語音**：`sfx.ABORT_SOUNDS`（九種九牌／四風連打／四槓散了／四家立直／三家和；`ALL_SOUND_NAMES` 66→71）；
  `sfx.play(..., fallback=)` 找不到主檔時改播 fallback；新增 `play_table(gid, name, fallback)`（房主的包，
  `play_start` 改為其包裝）。`flow._draw_sound(gid, name, p=None)`：有 p＝宣告者的包，否則 `play_table`，
  皆以 `ryuukyoku` 為後備。途中流局在宣告處播；荒牌流局在結果段播 `ryuukyoku`；流局滿貫先 `play_table("ryuukyoku")`
  再 `play_wins` 各自唸（原本誤播 `ron`）。

### 變更
- **新 AI（`mahjong/ai.py`）**：取代 `rules.ai_choose_discard`／`ai_should_pon`。
  - `shanten_counts()`：每種花色以記憶化 DFS 求（面子, 搭子, 雀頭）Pareto 組合再合併，含七對子、國士；
    與 `engine.is_complete` 交叉驗證 12,000 手（完成／拿一張／隨機）零差異。
  - `choose_discard(gs, me, banned)` → (牌, 是否立直)：向聽最小 → 有效牌（扣可見張）最多 → 價值最低；
    有人立直時以 `_danger`（現物＝立直者捨牌＋`riichi_snap` 之後他家通過的牌、筋、字牌已見張數、
    么九／中張）決定棄和（`_should_fold`：二向聽以上、或一向聽且寶牌少／兩家以上立直）或推（同向聽挑安全）。
    `_should_riichi`：門清、打完聽牌、有效牌 >0、點數 ≥1000、牌山 ≥4。
  - `call_choice(gs, me, tile, chi_options)`：立直中不鳴；有人立直且未聽不鳴；門清一向聽以內只碰役牌；
    鳴後最佳向聽須變小（碰役牌允許不變），且 `_has_yaku_path`（役牌／斷么／一色／對對）成立。
  - `flow`：AI 反應改用 `call_choice`（可吃）；AI 回合改用 `choose_discard`（含食替禁止），立直時比照真人記帳
    （供託、扣點、兩立直、一發、`riichi_snap`），立直後摸切並以 `feed.riichi_tsumogiri` 顯示；
    真人立直也記 `riichi_snap`。
  - 300 局 AI 對戰模擬：0 非法動作、0 例外；每局平均碰 1.0、吃 0.7、立直 1.1（四家合計）。
- **掛機＝自動摸切**：`AFK_TURNS_TO_AI` 改名 `AFK_TURNS`（2）。真人回合超時且非立直鎖定時 `afk_count += 1`，
  達門檻設 `player.afk = True`，公開串發 `msg.afk_takeover`、私人串附 `AfkBackButton`。`afk` 時：不發
  `feed.your_turn`、不 ping、不呼叫 `wait_turn_action`（結果 None＝摸切）；`collect_reactions_t`／
  `collect_chankan_t` 跳過 afk 真人。`AfkBackButton` 清 `afk`／`afk_count`。`deal_next_hand` 延續 `afk`／`afk_count`；
  結束判定改為「沒有任何非 afk 的真人」。`msg.afk_takeover`／`afk_end`／`back_not_afk` 改寫。
- **電腦名稱**：`config.AI_NAMES = ["AI(1)", "AI(2)", "AI(3)", "AI(4)"]`。
- **移除 🔐 按鈕**：`ui.FairnessButton` 刪除；`make_board_view` 只剩 🌐 翻譯；終局訊息不再附按鈕。
- **`/setup` 權限**：`setup_group` 加 `guild_only=True`、`default_permissions=discord.Permissions(manage_guild=True)`
  （`default_member_permissions=32`，需重新 sync 才生效）；各子指令原本的執行期權限檢查保留。
- **`/setup clear_room`**（取代 `/setup rooms delete:`）：`ClearRoomView`（`_RoomPick` 多選前 25 間＋
  「清除勾選的／只清等待房／全部清除」三鈕），按下時重新 `flow.list_rooms()` 以當下狀態篩選再 `flow.force_end()`，
  結果改寫原訊息並移除按鈕；無房間時只回文字。列表文字抽成 `_room_line()`／`_clear_room_text()`。
- **不再用布林／自由文字參數**：`/setup channel` 的 `clear: bool` 改為 `action: Choice`（set／clear）；
  `/setup lang` 的 `language` 由自由文字＋autocomplete 改為 `app_commands.choices`（`i18n.available()`＋auto），
  移除 `_guild_lang_autocomplete`。檢查後全部指令已無 BOOLEAN 或無選項的字串參數。新描述／選項補上 ja／en 的 `cmdtr.*`。
- **房號代碼**：`rooms.code(n)`／`rooms.parse(text)`——資料庫仍存遞增整數 `room_no`（不需遷移），顯示時以
  `(n × 7368787 + 9041533) mod 31⁵` 打散成 5 碼（字母表 `ABCDEFGHJKMNPQRSTUVWXYZ23456789`，去掉 0/O/1/I/L），
  `parse` 用模反元素還原（只收代碼，不再認舊式數字房號）。驗證 1～20,000 與上界往返一致、20 萬筆無重複。
  `rooms.label()` 改「房間 XXXXX」，新增 `rooms.tag()`（頻道／手牌討論串名稱）；`flow.setup_channels` 與手牌串名、
  `/setup clear_room` 列表、（私有）後台 `server.py`／`pages.py` 改用代碼。`replay.*` 的 `{room:04d}` 改 `{room}`。
- **牌譜檢視改版**：`_history_page(uid, mode, idx, lang)` 一頁一場（`get_recent_records(limit=1, offset=idx)`），
  內容＝房號代碼＋Discord 時間戳（`_discord_time`，DB 為 UTC）、自己的名次／點數／得失、和了（自摸・榮和）／放銃／立直，
  以及 `db.get_game()` 存檔 `game_data.players` 排出的全桌最終順位（含電腦、標出自己）。`HistoryView`：◀／頁碼（停用）／▶、
  「🎞 回放這場」（呼叫 `_start_replay`，無房號時停用）、🔁 切換四麻／三麻；一律附按鈕。`_send_history()` 供
  `/mahjong history` 與大廳 📜 共用，未指定模式時四麻無對局而三麻有就開三麻。移除 `_history_embed`／`_HIST_PAGE`。
- **回放指令**：`/mahjong replay room` 改字串＋`_replay_room_ac` 自動完成（`db.get_recent_games_any()`，不分模式最近 25 場，
  顯示「代碼 · 日期 · 名次 點數」）；大廳回放輸入框改收代碼（`rooms.parse`）。`replay.no_log` 移除「0.4.3 之前」字樣。
- **清掉不必要的相容程式（beta 資料不需保留）**：
  - `db`：移除沒在用的 `players`／`stats` 表與其 CRUD（`add_player`／`get_players`／`update_player_score`／
    `upsert_stats`／`get_stats`／被覆蓋的舊 `get_leaderboard`）、`get_active_game_in_channel`、`repair_game_records`
    與 `_counts_from_settles`。歷來以 ALTER 補上的欄位全併入 `CREATE TABLE`（`user_prefs` 的 skin／notify_queue／
    voice_pack，`guild_settings` 整張表），遷移只留 0.7.x → 0.8 的 `user_prefs.voice_pack` 一條。
    （既有資料庫裡的舊表／舊欄不動，只是不再建立或讀取。）
  - `commands`：移除 `/mahjong repair`；`stats.rates_note` 不再寫「有牌譜的」。
  - `rules`：移除舊 AI（`ai_choose_discard`／`ai_should_pon`／`_tile_connectivity`，已由 `ai.py` 取代）。
  - `views`：移除沒在用的 `DiscardInputModal`／`DiscardView`／`ReactionView`／`DiscardActionView`／
    `ChiSelectView`／`KanSelectView`／`BoardRefreshView`，只剩房間設定。
- **回放開頻道**：`_replay_channel()` 在雀月類別（無則目前頻道的類別）建文字頻道，`@everyone` 不可見、
  請求者唯讀、機器人可發言／管理，`topic=REPLAY_TOPIC`（`suzume-replay`），名稱 `replay.channel_name`；
  建不了才退回公開討論串，私訊則直接在原處播。`ReplayControl.own` 取代 `thread`，`_drop()` 於 ✖ 與
  `on_timeout`（3600 秒無操作）刪除；`run.on_ready` 呼叫 `cleanup_replay_channels()` 清掉重啟前遺留、
  帶標記的回放頻道。`replay.opened` 文案改為頻道；`guide.text` 的 `/setup rooms` 改 `/setup clear_room`。
- **不唸「翻」**：`sfx.TIER_SOUNDS` 移除 `han`（`ALL_SOUND_NAMES` 68→67）；`tier_sound()` 滿貫以下回 `""`，
  `play_wins` 因此只唸役名。`語音包音效清單.md` 同步。
- **移除 `ippatsu` 事件音**：`sfx.EVENT_SOUNDS` 刪掉 `ippatsu`（流程中從未呼叫；`ALL_SOUND_NAMES` 67→66），
  役名 `一發`（`YAKU_SOUNDS`）不受影響。

### 修正
- **榮和時誤標振聽／提示不消失**：`play_hand_t` 收完反應後，原本先把「所有聽這張的人」標同巡振聽再處理榮和，
  贏家面板因此在 `feed.ron` 重繪時顯示振聽。改為 `_mark_pass()` 並排除本次宣告榮和者（`ron`／`dblron`），
  三家和不標；榮和無效（無役）才補標該人。`collect_reactions_t`／`collect_chankan_t` 的按鈕回呼由
  `defer()` 改為 `edit_message(content="✅ 選項", view=None)` 立即回饋，倒數迴圈在選定後停止。
- **指南頻道空白**：`run.ensure_guild_setup` 建指南時 overwrites 只有 `@everyone: view_channel=False`，
  沒給 `guild.me` 允許 → 機器人無管理員權限時看不到自己的頻道、`send` 403、留下空頻道，且因名稱已存在
  永不重試。改為加入 `guild.me` 的 view／send／embed／read_history 允許；既有指南若機器人可用但
  `last_message_id is None` 就補發，若看不到則嘗試刪除並重建；新建後發送失敗則刪掉空頻道。
  指南改以 `GuideView(lang)` 發送。
- **語音房滿員／三麻上限**：新增 `voice.fit_room_limit(vc, max_players, reserve_bot=True)`——上限＝本局人數，
  機器人無 `administrator`／`move_members` 時再 +1（Discord：滿房只有「移動成員」能進）。`_start_voice_game`
  在開局訊息後呼叫；`views.RoomSettingsView.toggle_sanma` 在語音房（`gid` 以 `vc` 開頭）切換時以 3／4 呼叫
  （`reserve_bot=False`）。`sfx._ensure_client` 連線前若房已滿且無上述權限，`user_limit + 1` 作為後備。
  `perms.FEATURES` 的 Move Members 用途補上「語音房滿時機器人仍能進房」。

## [0.7.4] - 2026-10-03

### 新增
- **退出伺服器前清理**：`commands.teardown_hub(guild)` 刪除機器人建立的語音配對房、大廳／配對語音頻道、
  建在大廳類別內的開房頻道與指南頻道；類別只有在 `fetch_channels()` 確認裡面沒有其他頻道時才刪
  （管理員自己的頻道、用 `/setup channel` 指定在類別外的遊玩頻道一律不動）；最後
  `db.delete_guild_settings()` 清掉該伺服器設定。供（私有）後台「讓機器人退出」選用。
  有刪不掉的（多半權限不足）→ 以伺服器語言發 `leave.notice` 到系統頻道或第一個可發言的文字頻道，
  列出殘留頻道請管理員手動刪；全部刪乾淨則不留言。

### 變更
- **設定指南**：`guide.text`（zh_tw／ja／en）改寫為三步驟、語音開局為主（`/setup create` → 🔊 配對語音
  開局／三麻／🤖 與電腦開始 → 🎚️ 語音包），選用項目收成一行。
- **指南語言選單**：`commands._all_langs(key)` 把 `guide.disp_select`／`guide.srv_select` 在 `i18n.available()`
  全部語言的文字以「 / 」串成 placeholder（≤150 字），兩個鍵改為短句。
- **英文聽牌用語**：en.json 的 `term.tenpai`／`term.noten`／`feed.tenpai_note`／`msg.riichi_not_tenpai`／
  `result.tenpai_list`／`tenpai.advice_title` 改為 Tenpai (ready)／Noten (not ready)。
- **大廳 DM 按鈕**：`LobbyView.dm_btn` 標籤改 `lobby.dm_btn`（📩 手牌改私訊）；`_content` 尾端加
  `-# lobby.dm_hint` 說明，名單中 `dm_uids` 內的玩家標 📩；切換後即時 `lobby_message.edit` 更新名單。

### 修正
- **大廳離開鈕**：`LobbyView.leave_btn`（`lobby.leave_btn`，緊接在加入鈕後）——不在房內回 `msg.not_in_room`；
  一般玩家從 `_waiting` 移除（含 `dm_uids`）並重繪；房主離開：仍有真人→`_room_owners` 交給最早加入者並在頻道
  發 `lobby.host_transferred`（@提及），無真人→`_cleanup` 關房、訊息改 `lobby.closed_host_left`。
  `_content` 名單以 👑 標房主。
- **權限規格集中**：新增 `mahjong/perms.py`（`CORE`／`THREADS`／`VOICE`／`FEATURES`、`channel_missing()`、
  `guild_report()`），`run._global_perm_check` 改用 `perms.channel_missing`。修正原檢查漏了
  `create_private_threads`（手牌私人串）、誤含未使用的 `add_reactions`。

## [0.7.3] - 2026-09-30

### 新增
- **伺服器主要語言**：`db.guild_lang` 欄＋`get_guild_lang`／`set_guild_lang`；`i18n.guild_lang()`／
  `set_guild_lang()`／`detect_locale(preferred_locale)`；`i18n.get_user_lang(uid, guild_id=None)`——
  無個人設定時退伺服器語言、再退母本（只快取個人設定）。commands／ui／voice 的 `get_user_lang(...)` 補傳 guild_id。
  `run.ensure_guild_setup` 於 on_guild_join＋on_ready 依 `preferred_locale` 設定未設定的伺服器語言。
  `/setup lang [language|auto]`（manage_guild）＋autocomplete。指南頻道兩個下拉：`GuideDisplaySelect`
  （`guide:disp`，只重繪指南、不改設定）與 `GuideServerLangSelect`（`guide:srvlang`，`set_guild_lang`＋重繪＋確認）；
  `GuideView(lang)` 依語言重繪標籤／placeholder。
- **啟動檢測指南頻道**：`run.ensure_guild_setup(guild, create_guide=True)`——有 `category_id` 或已存在指南頻道
  （比對各語言的 `guide.channel_name`）就跳過，否則建立僅管理員可見的指南頻道；on_ready 對所有 guild 執行。
- **AFK →電腦接手＋重連**（取代 0.7.2 的「整局全超時就結束」）：`flow.play_hand_t` 每個真人回合累加
  `player.afk_count`（有得選卻超時＋非立直鎖手才算，做動作歸零），達 `AFK_TURNS_TO_AI`(=2) 且非段位賽
  →`player.is_bot=True`＋`player.afk_ai=True`，發 `msg.afk_takeover`。`deal_next_hand` 依 `afk_ai` 續接手並帶
  `afk_count`。`match_loop_t` 改判 `not any(not p.is_bot …)`（沒真人了）才 `over` 並發 `msg.afk_end`。
  `flow.AfkBackButton`（發在該玩家私人串的「接回座位」按鈕，驗證按者＝本人）把 `afk_ai` 座位
  `is_bot=False` 交回＋公開通知 `msg.player_back`。

### 變更
- **缺權限即時提示**：`run._global_perm_check` 指派給 `tree.interaction_check`——檢查
  `channel.permissions_for(guild.me)` 的 view_channel／send_messages／embed_links／add_reactions／manage_threads
  （TextChannel 另加 create_public_threads／send_messages_in_threads），缺則回 `perm.bot_missing`（英文權限名）
  並 return False 中止；administrator 直接放行、DM 略過。

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
