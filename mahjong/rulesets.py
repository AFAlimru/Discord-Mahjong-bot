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
"""規則集：把「這桌用什麼規則」集中成一份 RuleSet，計分、牌山、流程、結算都照它走。

預設（雀月原本的規則）、天鳳、雀魂、M聯盟、WRC 五種預設，數值依各自官方規則（2026-11 查證）：
  天鳳   https://tenhou.net/man/
  雀魂   https://mahjongsoul.com/news/46（段位戰規則）
  M聯盟  https://m-league.jp/about/（公式戰規則；「場ゾロ」2 翻只是記法不同，點數與一般規則相同，不另外實作）
  WRC    WRC Rules 2025（https://www.worldriichi.org/wrc-rules）
官方沒寫清楚的少數項目採常見慣例（見各欄註解）。

房間設定（_room_configs[gid]）存：
  "rules": 預設名稱（default／tenhou／majsoul／mleague／wrc）
  "rule_over": {欄位: 值}——玩家在「細項」裡改過的欄位（只允許 OVERRIDABLE 裡的欄位）
舊設定相容："ruleset": "mixed"→default、"tenhou"→tenhou；另外 "tobi"、"abortive" 這兩個舊開關仍然有效（見 resolve）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace, asdict

ABORT_ALL = frozenset({"九種九牌", "四風連打", "四槓散了", "四家立直", "三家和"})


@dataclass(frozen=True)
class RuleSet:
    key: str
    # ── 點數／精算 ──
    start: int                  # 起始點數（四麻）
    return_pts: int             # 返點（四麻）；> 起始點數時差額×人數＝頭名賞（歸一位）
    uma: tuple                  # 順位馬（千點），四麻
    start_3: int                # 三麻
    return_3: int
    uma_3: tuple
    leftover_sticks: str        # 終局時桌上剩的立直棒："top"＝給第一名、"none"＝不給任何人
    tobi: bool                  # 擊飛：點數 < 0 就終局（剛好 0 不飛）
    # ── 牌 ──
    red_fives: int              # 赤寶牌張數（四麻；1 種 1 張：0＝無、3＝5m5p5s 各一）
    red_fives_3: int            # 三麻（2＝5p5s 各一、0＝無）
    # ── 役／計分 ──
    ippatsu: bool               # 一發
    ura: bool                   # 裏寶牌（含槓裏）
    kuitan: bool                # 喰斷（副露的斷么九）
    kiriage: bool               # 切上滿貫（4 翻 30 符、3 翻 60 符算滿貫）
    kazoe: str                  # 13 翻以上："yakuman"＝累計役滿；"sanbaiman"＝封頂三倍滿
    double_yakuman: bool        # 四暗刻單騎／國士十三面／純正九蓮／大四喜 算雙倍役滿
    renhou: str                 # 人和："none"＝無、"mangan"＝滿貫（不與其他役複合）
    pao: tuple                  # 包牌（責任支付）適用的役滿
    riichi_need_1000: bool      # 立直需持有 1000 點以上
    # ── 流程 ──
    multi_ron: str              # 一張牌多家榮和："double"＝雙響可、三家和依途中流局；"triple"＝雙響三響都可；"atamahane"＝頭跳（只有上家和）
    abortive: frozenset         # 啟用的途中流局種類（三麻自動去掉四風連打／四家立直／三家和）
    nagashi: bool               # 流局滿貫
    kan_dora: str               # 槓寶牌翻開時機："immediate"＝每種槓都立刻翻；"tenhou"＝暗槓立刻、明槓／加槓在打牌後
    west: bool                  # 南入／西入：終局時第一名未達門檻就延長、達門檻即結束（サドンデス）
    west_target: int            # 延長門檻（四麻）
    west_target_3: int          # 延長門檻（三麻）
    agari_yame: bool            # 和了止め／聽牌止め：最後一局莊家是第一名且達門檻（west_target）就結束
    sanma_ok: bool = True       # 這套規則能不能用在三麻（M聯盟、WRC 沒有正式三麻規則）
    label: dict = field(default_factory=dict, compare=False)   # 顯示名稱（依語言）

    def for_mode(self, sanma: bool) -> dict:
        """依人數取出點數相關數值：start／return_pts／uma／red／west_target。"""
        if sanma:
            return {"start": self.start_3, "return_pts": self.return_3, "uma": self.uma_3,
                    "red": self.red_fives_3, "west_target": self.west_target_3}
        return {"start": self.start, "return_pts": self.return_pts, "uma": self.uma,
                "red": self.red_fives, "west_target": self.west_target}

    def abortive_for(self, sanma: bool) -> frozenset:
        return self.abortive - {"四風連打", "四家立直", "三家和"} if sanma else self.abortive


PRESETS: dict[str, RuleSet] = {
    # 雀月原本的規則（0.8.x 的「預設」）：行為完全不變
    "default": RuleSet(
        key="default",
        start=25000, return_pts=30000, uma=(20, 10, -10, -20),
        start_3=35000, return_3=40000, uma_3=(20, 0, -20),
        leftover_sticks="none", tobi=True,
        red_fives=3, red_fives_3=2,
        ippatsu=True, ura=True, kuitan=True, kiriage=False, kazoe="yakuman",
        double_yakuman=True, renhou="none", pao=(), riichi_need_1000=True,
        multi_ron="double", abortive=ABORT_ALL, nagashi=True, kan_dora="immediate",
        west=False, west_target=30000, west_target_3=40000, agari_yame=False,
        label={"zh_tw": "預設", "ja": "標準", "en": "Default"}),
    # 天鳳 段位戰
    "tenhou": RuleSet(
        key="tenhou",
        start=25000, return_pts=30000, uma=(20, 10, -10, -20),
        start_3=35000, return_3=40000, uma_3=(20, 0, -20),
        leftover_sticks="top", tobi=True,
        red_fives=3, red_fives_3=2,            # 三麻赤牌枚數官方未寫，沿用本機器人原本的 2 張
        ippatsu=True, ura=True, kuitan=True, kiriage=False, kazoe="yakuman",
        double_yakuman=False, renhou="none", pao=("大三元", "大四喜"), riichi_need_1000=True,
        multi_ron="double", abortive=ABORT_ALL, nagashi=True, kan_dora="tenhou",
        west=True, west_target=30000, west_target_3=40000, agari_yame=True,
        label={"zh_tw": "天鳳", "ja": "天鳳", "en": "Tenhou"}),
    # 雀魂 段位戰
    "majsoul": RuleSet(
        key="majsoul",
        start=25000, return_pts=25000, uma=(15, 5, -5, -15),
        start_3=35000, return_3=35000, uma_3=(15, 0, -15),
        leftover_sticks="top", tobi=True,
        red_fives=3, red_fives_3=2,
        ippatsu=True, ura=True, kuitan=True, kiriage=False, kazoe="yakuman",
        double_yakuman=True, renhou="none", pao=("大三元", "大四喜"), riichi_need_1000=True,
        multi_ron="triple", abortive=frozenset({"九種九牌", "四風連打", "四槓散了", "四家立直"}),
        nagashi=True, kan_dora="tenhou",
        west=True, west_target=30000, west_target_3=40000, agari_yame=True,
        label={"zh_tw": "雀魂", "ja": "雀魂", "en": "Mahjong Soul"}),
    # M聯盟 公式戰（四麻限定）
    "mleague": RuleSet(
        key="mleague",
        start=25000, return_pts=30000, uma=(30, 10, -10, -30),
        start_3=35000, return_3=40000, uma_3=(20, 0, -20),
        leftover_sticks="top", tobi=False,
        red_fives=3, red_fives_3=2,
        ippatsu=True, ura=True, kuitan=True, kiriage=True, kazoe="sanbaiman",
        double_yakuman=False, renhou="none", pao=("大三元", "大四喜", "四槓子"), riichi_need_1000=True,
        multi_ron="atamahane", abortive=frozenset(), nagashi=False, kan_dora="immediate",
        west=False, west_target=30000, west_target_3=40000, agari_yame=False,
        sanma_ok=False,
        label={"zh_tw": "M聯盟", "ja": "Mリーグ", "en": "M.League"}),
    # WRC 2025（四麻限定）
    "wrc": RuleSet(
        key="wrc",
        start=30000, return_pts=30000, uma=(15, 5, -5, -15),
        start_3=35000, return_3=40000, uma_3=(20, 0, -20),
        leftover_sticks="none", tobi=False,
        red_fives=0, red_fives_3=0,
        ippatsu=True, ura=True, kuitan=True, kiriage=True, kazoe="yakuman",   # 13 翻＝四倍滿＝單倍役滿點數
        double_yakuman=False, renhou="mangan", pao=("大三元", "大四喜", "四槓子"), riichi_need_1000=False,
        multi_ron="atamahane", abortive=frozenset(), nagashi=False, kan_dora="immediate",
        west=False, west_target=30000, west_target_3=40000, agari_yame=False,
        sanma_ok=False,
        label={"zh_tw": "WRC", "ja": "WRC", "en": "WRC"}),
}

# 玩家可以在「細項」裡個別改的欄位
OVERRIDABLE = ("ippatsu", "ura", "red_fives", "kuitan", "kiriage", "multi_ron", "leftover_sticks")
_LEGACY = {"mixed": "default", "tenhou": "tenhou"}


def preset_key(config: dict) -> str:
    """房間設定用的是哪一套預設（含舊設定 ruleset 的相容）。"""
    k = (config or {}).get("rules")
    if k in PRESETS:
        return k
    return _LEGACY.get((config or {}).get("ruleset"), "default")


def resolve(config: dict) -> RuleSet:
    """房間設定 → 這桌實際的規則。面板的 tobi／abortive 開關有給就蓋過預設
    （abortive True 而預設沒有途中流局時用全部五種）；三麻用不了的預設退回 default。"""
    config = config or {}
    rs = PRESETS[preset_key(config)]
    if config.get("is_sanma") and not rs.sanma_ok:   # M聯盟／WRC 沒有正式三麻規則 → 三麻退回預設
        rs = PRESETS["default"]
    over = {}
    for k, v in (config.get("rule_over") or {}).items():
        if k in OVERRIDABLE:
            over[k] = v
    # 面板上的「擊飛」「途中流局」開關一律以玩家最後的選擇為準（選預設時面板會先同步成預設值）
    if isinstance(config.get("tobi"), bool):
        over["tobi"] = config["tobi"]
    if config.get("abortive") is False:
        over["abortive"] = frozenset()
    elif config.get("abortive") is True and not rs.abortive:   # 預設沒有途中流局（M聯盟／WRC）但玩家打開了
        over["abortive"] = ABORT_ALL
    if "red_fives" in over:                      # 細項的赤寶牌只有「有／無」：三麻跟著
        over["red_fives_3"] = 2 if over["red_fives"] else 0
    return replace(rs, **over) if over else rs


def label(key: str, lang: str) -> str:
    rs = PRESETS.get(key) or PRESETS["default"]
    return rs.label.get(lang) or rs.label.get("zh_tw") or key


def to_dict(rs: RuleSet) -> dict:
    """存檔／牌譜用（frozenset／tuple 轉成 list）。"""
    d = asdict(rs)
    d["abortive"] = sorted(d["abortive"])
    d.pop("label", None)
    return d


def summary(rs: RuleSet, lang: str, sanma: bool = False, selected: str | None = None,
            start_points: int | None = None, hanchan: bool | None = None) -> str:
    """這桌實際規則的簡短說明（4～6 行：預設名稱＋點數／寶牌與役／流局與結束，顯示在設定面板上）。
    rs＝resolve() 的結果；sanma＝是否三麻（決定用哪組點數／赤牌）；
    selected＝玩家選的預設名稱——若選了沒有三麻規則的預設（M聯盟／WRC）又開三麻，多加一行說明退回預設。
    start_points＝房間自訂的起始點數（「⚙️ 數值設定」）：起始、返點、延長門檻都以同樣的差額平移（沒自訂＝規則原值）；
    hanchan＝False 東風戰（延長叫南入）／True 半莊（西入）／None 不知道（兩個都寫）。
    開了南入／西入或和了止め才會多一行。"""
    from . import i18n                      # 延後匯入：rulesets 本身不依賴語言檔
    def t(key: str, **kw) -> str:
        return i18n.t(key, lang, **kw)
    def onoff(b) -> str:
        return t("rules.v.on" if b else "rules.v.off")
    def pt(n: int) -> str:
        return f"{n:,}"
    m = rs.for_mode(sanma)
    shift = (start_points - m["start"]) if start_points else 0     # 自訂起始點數造成的平移
    uma = "/".join(f"{u:+d}" if u else "0" for u in m["uma"])
    red = t("rules.sum.red_n", n=m["red"]) if m["red"] else t("rules.v.off")
    lines = [                                   # 分組成帶標籤的幾行（點數／寶牌與役／流局與結束），方便一眼掃過
        t("rules.sum.name", name=label(rs.key, lang)),
        t("rules.sum.l1", start=pt(m["start"] + shift), ret=pt(m["return_pts"] + shift), uma=uma),
        t("rules.sum.l2", red=red, ippatsu=onoff(rs.ippatsu), ura=onoff(rs.ura),
          kuitan=onoff(rs.kuitan), kiriage=onoff(rs.kiriage)),
        t("rules.sum.l3", multi=t(f"rules.multi.{rs.multi_ron}"), abort=onoff(rs.abortive_for(sanma)),
          tobi=onoff(rs.tobi), west=onoff(rs.west), left=t(f"rules.left.{rs.leftover_sticks}")),
    ]
    extra = []                                  # 南入／西入、和了止め
    if rs.west:
        ext = (t("rules.sum.ext_t") if hanchan is False else t("rules.sum.ext_h") if hanchan
               else f"{t('rules.sum.ext_t')}／{t('rules.sum.ext_h')}")
        extra.append(t("rules.sum.ext", ext=ext, target=pt(m["west_target"] + shift)))
    if rs.agari_yame:
        extra.append(t("rules.sum.yame"))
    if extra:
        lines.append(t("rules.sum.l4", parts="・".join(extra)))
    sel = PRESETS.get(selected) if selected else None
    if sanma and sel is not None and not sel.sanma_ok:
        lines.append(t("rules.sum.fallback", name=label(selected, lang)))
    return "\n".join(lines)


def compact(rs: RuleSet, lang: str, sanma: bool = False, selected: str | None = None,
            start_points: int | None = None, hanchan: bool | None = None,
            kuikae: bool = False, open_riichi: bool = False,
            spectate: bool = True) -> list[str]:
    """設定面板用的精簡規則文字（3 行）：
    規則名稱（後面接三麻退回預設的說明、跟「預設」不同的項目、南入／西入、和了止め）／起始與返點／馬點。kuikae／open_riichi 是面板自己的開關（不在 RuleSet 裡），跟預設不同（允許食替／開立直）也列在這行。
    參數意義同 summary()。"""
    from . import i18n
    def t(key: str, **kw) -> str:
        return i18n.t(key, lang, **kw)
    pt = lambda n: f"{n:,}"
    m = rs.for_mode(sanma)
    shift = (start_points - m["start"]) if start_points else 0
    uma = "/".join(f"{u:+d}" if u else "0" for u in m["uma"])
    pts = t("rules.sum.pts", start=pt(m["start"] + shift), ret=pt(m["return_pts"] + shift))
    uma_line = t("rules.sum.uma", uma=uma)
    d = PRESETS["default"]
    dm = d.for_mode(sanma)
    items = []
    if m["red"] != dm["red"]:
        items.append(t("rules.diff.red_n", n=m["red"]) if m["red"] else t("rules.diff.red0"))
    if not rs.ippatsu:
        items.append(t("rules.diff.ippatsu0"))
    if not rs.ura:
        items.append(t("rules.diff.ura0"))
    if not rs.kuitan:
        items.append(t("rules.diff.kuitan0"))
    if rs.kiriage:
        items.append(t("rules.diff.kiriage"))
    if not rs.tobi:
        items.append(t("rules.diff.tobi0"))
    ab, ab0 = rs.abortive_for(sanma), d.abortive_for(sanma)
    if ab != ab0:
        items.append(t("rules.diff.abort_n", n=len(ab)) if ab else t("rules.diff.abort0"))
    if rs.multi_ron != d.multi_ron:
        items.append(t("rules.diff.multi", v=t(f"rules.multi.{rs.multi_ron}")))
    if rs.leftover_sticks != d.leftover_sticks:
        items.append(t("rules.diff.left", v=t(f"rules.left.{rs.leftover_sticks}")))
    if kuikae:
        items.append(t("rules.diff.kuikae"))
    if open_riichi:
        items.append(t("rules.diff.openriichi"))
    if not spectate:
        items.append(t("rules.diff.nospectate"))
    if rs.west:
        ext = (t("rules.sum.ext_t") if hanchan is False else t("rules.sum.ext_h") if hanchan
               else f"{t('rules.sum.ext_t')}／{t('rules.sum.ext_h')}")
        items.append(t("rules.sum.ext", ext=ext, target=pt(m["west_target"] + shift)))
    if rs.agari_yame:
        items.append(t("rules.sum.yame"))
    extra = ""
    sel = PRESETS.get(selected) if selected else None
    if sanma and sel is not None and not sel.sanma_ok:
        extra += t("rules.sum.fallback", name=label(selected, lang))
    if items:
        extra += t("rules.diff.head", items=t("rules.diff.sep").join(items))
    return [t("rules.sum.c1", name=label(rs.key, lang), extra=extra), pts, uma_line]
