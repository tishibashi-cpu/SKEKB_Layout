"""
skekb_layout.py  —  SuperKEKB レイアウト用 AutoCAD スクリプト生成ライブラリ
==========================================================================

旧来の Excel/VBA システム (SKEKB_Layout_*.xlsm) を Python に移植したもの。

設計方針
--------
1. **データとロジックの分離**
   旧VBAではマグネット名→ブロック名の対応規則が数百個の `If` 文として
   コードに直接書かれていた。本実装ではそれらを config/ 以下の
   編集可能なファイル (JSON / CSV) に外出しし、本体は小さく保つ。

2. **幾何計算の忠実な再現**
   座標変換・角度計算・注記配置は VBA の Make_*_ScriptFile を
   そのまま移植している (定数も settings.json に保持)。

3. **未解決要素の可視化**
   ブロック名が解決できなかった要素は ConversionResult.unresolved に
   記録され、レポートできる (旧VBAの「Unknown」報告に相当)。

データの流れ
-----------
    .dispog ファイル
        │  parse_dispog()
        ▼
    list[Element]                  ← 1行 = 加速器構成要素1点
        │  derive_block_name()     ← マグネット名から AutoCAD ブロック名を決定
        ▼
    ブロック名つき Element
        │  generate_magnet_script() / generate_duct_script()
        ▼
    .scr ファイル (AutoCAD スクリプト)
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# 設定の読み込み
# --------------------------------------------------------------------------

CONFIG_DIR = Path(__file__).parent / "config"


def _load_json(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _load_overrides_csv(name: str) -> list[dict]:
    """element / expect_bn / result_bn の3列CSVを読み込む。"""
    path = CONFIG_DIR / name
    if not path.exists():
        return []
    rules = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rules.append(row)
    return rules


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------


@dataclass
class Element:
    """dispog ファイルの 1 行 = 加速器ラティスの 1 点。"""

    name: str          # 要素名 (例: "QC1LE.1", "BLC1LE", "ESLP0")
    ogx: float         # OGx [m]
    ogy: float         # OGy [m]
    ogz: float         # OGz [m]
    s: float           # IP からの経路長 [m]
    length: float      # 要素長 [m]
    value: float       # Value (偏向量など。符号でブロック向き反転判定に使う)
    chi1: float = 0.0  # OChi1 [deg] 水平面内の軌道方位角 (入口での値)
    chi2: float = 0.0  # OChi2 [deg] 鉛直面内
    chi3: float = 0.0  # OChi3 [deg] ロール
    index: int = 0     # 通し番号

    # 導出される値
    mag_block: str = ""          # マグネット AutoCAD ブロック名
    duct_blocks: list[str] = field(default_factory=list)  # ダクトブロック名 (複数可)

    @property
    def x_mm(self) -> float:
        """AutoCAD 用 X 座標 [mm] (VBA: OGx * -1000)。"""
        return self.ogx * SETTINGS["coordinate"]["scale_mm_per_m"]

    @property
    def y_mm(self) -> float:
        return self.ogy * SETTINGS["coordinate"]["scale_mm_per_m"]


@dataclass
class ConversionResult:
    """変換結果とレポート用情報。"""

    script_lines: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)   # ブロック未解決の要素名
    block_usage: dict[str, int] = field(default_factory=dict)  # ブロック使用回数

    def text(self) -> str:
        return "\n".join(self.script_lines) + "\n"


# --------------------------------------------------------------------------
# 設定をモジュールロード時に読み込む
# --------------------------------------------------------------------------

SETTINGS: dict = _load_json("settings.json")


# --------------------------------------------------------------------------
# dispog ファイルのパース
# --------------------------------------------------------------------------


def parse_dispog(path: str | Path) -> list[Element]:
    """
    .dispog ファイル (固定幅・空白区切りテキスト) を読み込む。

    列: Element  OGx  OGy  OGz  s  Length  Value  OChi1  OChi2  OChi3  #
    先頭のヘッダ行と末尾の "$$$" 行はスキップする。
    """
    elements: list[Element] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "Element":      # ヘッダ行
                continue
            if parts[0] == "$$$":          # 終端
                break
            if len(parts) < 11:
                continue
            try:
                elements.append(
                    Element(
                        name=parts[0],
                        ogx=float(parts[1]),
                        ogy=float(parts[2]),
                        ogz=float(parts[3]),
                        s=float(parts[4]),
                        length=float(parts[5]),
                        value=float(parts[6]),
                        chi1=float(parts[7]),
                        chi2=float(parts[8]),
                        chi3=float(parts[9]),
                        index=int(parts[10]),
                    )
                )
            except (ValueError, IndexError):
                # 数値変換に失敗した行は警告だけして飛ばす
                continue
    return elements


# --------------------------------------------------------------------------
# マグネットブロック名の導出 (旧 *InsertMagnetBlockName の移植)
# --------------------------------------------------------------------------


class MagnetBlockResolver:
    """
    マグネット要素名から AutoCAD ブロック名を決定する。

    旧VBAのアルゴリズムを忠実に移植:
      1. BNN = round(length_m * 1000)          … 長さ[mm]の整数
      2. BN  = 先頭1文字 + BNN                  … 既定キー (例 "B5907", "Q560")
      3. ステアリング(Z) / Half-Q / 特殊Q / Wiggler の規則を適用
      4. 要素名固有の上書き規則 (CSV) を適用
      5. BN を *_MagBlock テーブル(JSON)で引いて AutoCAD ブロック名を得る
    """

    def __init__(self, ring: str):
        self.ring = ring
        # 統合 config: {ring}_magnets.json = {"by_element": {...}, "by_bn": {...}}
        #   by_element … 要素名 -> ブロック名 (Lattice 由来の正解マップ。最優先)
        #   by_bn      … BN(種別キー) -> ブロック名 (直マップに無い要素のフォールバック)
        try:
            merged = _load_json(f"{ring.lower()}_magnets.json")
            self.by_element: dict[str, str] = merged.get("by_element", {})
            self.block_map: dict[str, str] = merged.get("by_bn", {})
            self._override_index: dict[str, list[tuple[str, str]]] = {}
        except FileNotFoundError:
            # 旧形式 (分割ファイル) との後方互換
            self.block_map = _load_json(f"{ring.lower()}_mag_blocks.json")
            try:
                self.by_element = _load_json(f"{ring.lower()}_mag_by_element.json")
            except FileNotFoundError:
                self.by_element = {}
            self._override_index = {}
            try:
                for r in _load_overrides_csv(f"{ring.lower()}_mag_overrides.csv"):
                    self._override_index.setdefault(r["element"], []).append(
                        (r["expect_bn"], r["result_bn"]))
            except FileNotFoundError:
                pass
        self._zv_rules = self._load_zv_rules()

    @staticmethod
    def _load_zv_rules() -> list[tuple[float, float, str]]:
        """ステアリング ZV344 の s 位置レンジ規則 (HER 固有)。"""
        return [
            (28.5, 106.5, "ZV344(287)"),
            (106.5, 152.9, "ZV344(279)"),
            (161.7, 651.2, "ZV344(160)"),
            (666.0, 837.7, "ZV344(210)"),
            (854.0, 2158.9, "ZV344(160)"),
            (2174.3, 2232.8, "ZV344(287)"),
            (2249.4, 2328.5, "ZV344(210)"),
            (2345.4, 2361.4, "ZV344(279)"),
            (2381.4, 2852.6, "ZV344(160)"),
            (2862.7, 2895.8, "ZV344(279)"),
            (2911.0, 2987.9, "ZV344(287)"),
        ]

    def derive_bn(self, el: Element) -> str:
        """要素名から中間キー BN を導出する (ブロック名引き当て前)。"""
        name = el.name.strip()
        if name.startswith("-"):
            name = name[1:]

        bnn = str(int(el.length * 1000.0 + 0.1))   # 長さ[mm]
        first = name[:1]
        bn = first + bnn                            # 既定キー

        # --- ステアリング (Z) ---
        if first == "Z":
            bn = name[:2] + bnn
            ss = el.s
            if bn[:2] == "ZV":
                for lo, hi, val in self._zv_rules:
                    if lo < ss < hi:
                        bn = val
                        break
            # iBump
            if name[:3] == "ZVF":
                bn = "ZV250(279)"
            if name[:3] == "ZHF":
                bn = "ZH250"

        # --- IR 例外 ---
        if name in ("ZHQLC2LE", "ZHQLC2RE"):
            bn = name

        # --- Half-Q ---
        if first == "Q":
            tail3 = name[-3:]
            if tail3 in ("L.1", "R.1", "L.3", "R.3"):
                bn = bn + "H1"
            elif tail3 in ("L.2", "R.2", "L.4", "R.4"):
                bn = bn + "H2"

        # --- 特殊 Q ---
        if name[:3] == "QY5":
            bn = first + bnn
        if name[:3] == "QC2":
            bn = first + "C" + bnn

        # --- Wiggler (BW) ---
        if name[:2] == "BW" and len(name) >= 3 and name[2] in ("1", "2"):
            ud = int(name[2]) + int(name[-1])
            if len(name) >= 2 and name[-2] == ".":
                wn = int(name[-1])
            else:
                wn = int(name[-2:]) if name[-2:].isdigit() else 0
            in_special = (7 <= wn <= 12) or (19 <= wn <= 24) or (31 <= wn <= 36)
            if ud % 2 == 0:                         # 和が偶数
                bn = first + bnn + ("u" if in_special else "d")
            else:                                   # 和が奇数
                bn = first + bnn + ("d" if in_special else "u")

        # --- 要素名固有の上書き (CSV) ---
        if name in self._override_index:
            applied = False
            for expect, result in self._override_index[name]:
                if expect == "*" or expect == bn:
                    bn = result
                    applied = True
                    break
            if not applied:
                bn = name + "error"

        return bn

    def resolve(self, el: Element) -> Optional[str]:
        """要素 -> AutoCAD ブロック名。解決できなければ None。"""
        # 1) Lattice 由来の直接マップ (要素名そのもの / 先頭"-"を除いた名前)
        raw = el.name
        stripped = raw.lstrip("-")
        for name in (raw, stripped) if raw != stripped else (raw,):
            if name in self.by_element:
                return self.by_element[name]
        # 2) BN 導出 → 対応表
        bn = self.derive_bn(el)
        return self.block_map.get(bn)


# --------------------------------------------------------------------------
# 角度・座標ヘルパ (Make_*_ScriptFile の移植)
# --------------------------------------------------------------------------

_PI = SETTINGS["geometry"]["deg_conversion_pi"]
_OFFSET = SETTINGS["geometry"]["label_offset_ratio"]
_INS = SETTINGS["autocad"].get("insert_command", "Insert")
_INS_SCALE = SETTINGS["autocad"].get("insert_scale", "1 1")


def _segment_angles(x0, y0, x, y, prev_name, prev_value,
                    prev_tn, prev_tb):
    """
    1 区間 (前点→現点) の注記角度 TNangle とブロック角度 TBangle を計算する。
    VBA の角度計算ロジックを忠実に移植。
    """
    if (x0 - x) != 0:
        base = math.atan((y0 - y) / (x0 - x)) / _PI * 180.0
        tn = tb = base
        if x < 0 and y >= 0:
            tn -= 90.0
        elif x < 0 and y < 0:
            tn += 90.0
            tb += 180.0
        elif x >= 0 and y < 0:
            tn -= 90.0
            tb += 180.0
        else:  # x >= 0 and y >= 0
            tn += 90.0
    else:
        # 鉛直区間
        if x < 0:
            tb, tn = 90.0, 0.0
        else:
            tb, tn = 270.0, 0.0

    if prev_name[:2] == "BW":
        tn = 0.0

    if (x0 - x) == 0 and (y0 - y) == 0:
        tn, tb = prev_tn, prev_tb

    if prev_name[:1] == "B" and prev_value < 0.0:
        tb += 180.0

    return tn, tb


def _label_point(x0, y0, x, y, ring):
    """
    注記テキストの挿入位置 (区間中点を半径方向に微小オフセット)。
    オフセット方向・量は HER と LER で異なる (VBA より):
      HER: X<0 → ×0.996(内), X>=0 → ×1.004(外)
      LER: X<0 → ×1.008(外), X>=0 → ×0.991(内)
    この符号が逆だと名前がビームラインの反対側(上下逆)に出る。
    """
    mx = (x0 + x) / 2.0
    my = (y0 + y) / 2.0
    off = SETTINGS[ring]["label_offset"]
    f = off["neg_x"] if x < 0 else off["pos_x"]
    return mx * f, my * f


def _q_distance_label(s_value: float, ring: str) -> str:
    """
    Q マグネットに付ける「実験室からの距離」ラベル (TL/NR/NL/.../TR + 数値)。
    octant 分割は同じだが、各 octant の計算定数が HER と LER で異なる (VBA より)。
    """
    circ = SETTINGS["geometry"]["ring_circumference_m"]
    octant = circ / SETTINGS["geometry"]["octant_count"]   # 3016.315 / 8
    isl = int(s_value / octant)
    s = s_value
    if ring == "LER":
        table = {
            0: ("TL", lambda: int(s + 0.4)),
            1: ("NR", lambda: int(754.6 - s - 0.2 + 0.5)),
            2: ("NL", lambda: int(s + 0.4 - 754.6 - 0.5)),
            3: ("FR", lambda: int(1509.5 - s - 0.3 + 0.5)),
            4: ("FL", lambda: int(s + 0.3 - 1509.5 - 0.5)),
            5: ("OR", lambda: int(2262.6 - s - 0.2 + 0.5)),
            6: ("OL", lambda: int(s + 0.4 - 2262.6 - 0.5)),
            7: ("TR", lambda: int(circ - s - 0.2 + 0.4)),
        }
    else:  # HER
        table = {
            0: ("TL", lambda: int(s + 0.7 - 1)),
            1: ("NR", lambda: int(753.6 - s - 0.2)),
            2: ("NL", lambda: int(s + 0.7 - 753.6)),
            3: ("FR", lambda: int(1506.7 - s - 0.3)),
            4: ("FL", lambda: int(s + 0.3 - 1506.7)),
            5: ("OR", lambda: int(2261.7 - s - 0.2)),
            6: ("OL", lambda: int(s + 0.7 - 2261.7)),
            7: ("TR", lambda: int(circ - s - 0.2 + 1)),
        }
    if isl in table:
        prefix, fn = table[isl]
        return f"{prefix}{fn()}"
    return ""


# --------------------------------------------------------------------------
# マグネットスクリプト生成 (Make_HER_Mag_ScriptFile の移植)
# --------------------------------------------------------------------------


def generate_clear_preamble(ring: str) -> list[str]:
    """
    スクリプト実行前の「クリア処理」コマンド列を返す。
    Webサイト記載の手順を自動化:
      1. 全レイヤをロック
      2. 対象リング(her*/ler*)と共通レイヤ(0,1,Base,Chamber,Defpoint,Dim)をアンロック
      3. ERASE ALL でアンロック層の図形を削除 (ロック層は自動的に除外 = Ctrl+A+Del 相当)
    すべて _ 付きコマンド/標準キーワードで言語版に依存しない。
    """
    prefix = SETTINGS[ring]["layers"]["base"][:3]   # "her" / "ler"
    unlock = [f"{prefix}*"] + SETTINGS["clear"]["unlock_common"]
    lines = ["OSMODE 0", "_-LAYER", "_LOCK *"]
    for lay in unlock:
        lines.append(f"_UNLOCK {lay}")
    lines.append("")                 # 空行 = Enter で -LAYER を終了
    # ERASE ALL (ロックされていない層の図形だけ削除)
    lines += ["_ERASE", "_ALL", ""]  # _ALL 選択 → 空行(Enter)で確定・削除
    return lines


def generate_magnet_script(elements: list[Element], ring: str,
                           clear_layers: bool = False,
                           draw_bend_arcs: bool = False) -> ConversionResult:
    """
    マグネット配置用 AutoCAD スクリプト (.scr 内容) を生成する。

    clear_layers=True のとき、先頭に「クリア処理」(対象リング層のアンロックと
    既存図形の削除)を付ける。複数スクリプトを続けて流す場合は、最初に実行する
    スクリプト(通常マグネット)だけ True にすること。ダクトを True にすると
    先に描いたマグネットも消えてしまうため注意。

    生成内容:
      - (任意) クリア処理
      - レイヤ作成 (base / magnetname / magnet)
      - 各要素位置に点
      - マグネット名テキスト (角度つき)
      - マグネットブロックの Insert
      - ビーム中心線 (Line)
    """
    res = ConversionResult()
    L = SETTINGS[ring]["layers"]
    resolver = MagnetBlockResolver(ring)

    # まず全要素のブロック名を解決
    for el in elements:
        block = resolver.resolve(el)
        if block:
            el.mag_block = block
            res.block_usage[block] = res.block_usage.get(block, 0) + 1
        else:
            el.mag_block = ""
            bn = resolver.derive_bn(el)
            # 旧VBA同様、B/Q/S/Z/M 始まりで長さ非ゼロのものだけ「未解決」報告
            if bn[:1] in ("B", "Q", "S", "Z", "M") and not bn.endswith("0"):
                res.unresolved.append(f"{el.name}  (BN={bn})")

    out = res.script_lines
    if clear_layers:
        out.extend(generate_clear_preamble(ring))
    # 全コマンド・キーワードに _ を付け、言語(日本語/英語)版に依存しないようにする。
    # OSMODE=0 でオブジェクトスナップを解除 (キーワード不要のシステム変数)。
    out.append("OSMODE 0")
    out.append(
        f"_-LAYER _N {L['base']} _C {L['base_color']} {L['base']} "
        f"_N {L['magnet_name']} _C {L['magnet_name_color']} {L['magnet_name']} "
    )
    out.append(f"_-LAYER _N {L['magnet']} _C {L['magnet_color']} {L['magnet']} ")
    if draw_bend_arcs and L.get("bend_arc"):
        out.append(f"_-LAYER _N {L['bend_arc']} _C {L['bend_arc_color']} {L['bend_arc']} ")
    out.append(f"PDMODE {SETTINGS['autocad']['point_mode']}")
    out.append(f"PDSIZE {SETTINGS['autocad']['point_size']}")
    out.append("_ZOOM _A")

    # ループ用の「前要素」状態
    x0 = y0 = None
    nelem0 = "L"
    nmagblock0 = ""
    tn0 = tb0 = 0.0
    tvalue0 = 0.0
    length0 = 0.0
    polyline: list[str] = []
    text_h = SETTINGS["text"]["magnet_name_height"]
    # QC1 / QC2 のサンプリングカウンタ (代表だけ名前を出す)
    iqc1 = iqc2 = 0

    for i, el in enumerate(elements):
        name = el.name
        x, y = el.x_mm, el.y_mm

        # --- 偏向磁石の軌道円弧 (任意, OChi1 ベースで厳密に描く) ---
        # dispog の各行は「要素入口」での軌道位置(x,y)と方位 OChi1[deg] を持つ。
        # 連続する次要素の入口 = この磁石の出口。水平面内の方位変化
        #   Δchi1 = chi1(次) - chi1(この要素)
        # を含み角として、入口→出口を円弧で結ぶ (_ARC 始点 _E 終点 _A 含み角)。
        # これで半径・接線・端点が厳密に再現される。垂直偏向は Δchi1≈0 のため
        # 直線同然となり描かない (平面図では曲がらない)。分割された磁石も
        # 各区間が正しい小弧になる。OChi1 を使うので向きの推定・反転が起きない。
        dname = name.lstrip("-")
        if draw_bend_arcs and dname[:1] == "B" and el.length > 0 \
                and i + 1 < len(elements) and L.get("bend_arc"):
            # 出口の行 = s ≒ 入口s + 磁石長 にある行 (磁石内部に軌道サンプル点が
            # 挿入されている場合 (例 BS2FRP) でも全長を弧でカバーするため、
            # 直後の行ではなく「出口」まで結ぶ)。通常は直後の行が出口。
            s_exit = el.s + el.length
            j = i + 1
            while j + 1 < len(elements) and elements[j].s < s_exit - 1e-6:
                j += 1
            dchi = elements[j].chi1 - el.chi1         # 水平方位変化 [deg]
            # OChi1 は ±180° で折り返すため、差を (-180,180] に正規化して
            # 最短回転(=本来の偏向角)に直す。これをしないと ±180° をまたぐ磁石
            # (BSWFRP, BX1E/BX2E, BSWFRE 等)で約±360°となり円が描かれてしまう。
            dchi = (dchi + 180.0) % 360.0 - 180.0
            if abs(dchi) > 1e-3:                      # 水平に曲がる場合のみ
                x1, y1 = elements[j].x_mm, elements[j].y_mm
                out.append(f"CLAYER {L['bend_arc']}")
                out.append(f"_ARC {_fmt(x)},{_fmt(y)} _E {_fmt(x1)},{_fmt(y1)} "
                           f"_A {_fmt(dchi)}")

        # 除外判定 (リング別):
        #   共通: 先頭 "P"、長さ0
        #   LER: 先頭 "D" も除外、ただし長さ0でも先頭2文字 "SK" は除外しない
        cfg = SETTINGS[ring]
        excluded = name.startswith("P")
        if cfg.get("exclude_d_prefix") and name.startswith("D"):
            excluded = True
        if el.length == 0:
            if cfg.get("sk_zero_length_exception") and name[:2] == "SK":
                pass  # SK** は長さ0でも除外しない
            else:
                excluded = True
        if excluded:
            continue

        # --- 点を打つ ---
        out.append(f"CLAYER {L['base']}")
        out.append(f"_POINT {_fmt(x)},{_fmt(y)}")

        nex = nelem0[:1]
        if nex != "L" and x0 is not None:
            tn, tb = _segment_angles(x0, y0, x, y, nelem0, tvalue0, tn0, tb0)

            # --- マグネット名テキスト ---
            # 表示名は先頭の "-"(反転印) を除く ("-QLA7RP" -> "QLA7RP")。
            # これにより名前が正しく出て、Q磁石の距離ラベル判定も効く。
            disp = nelem0.lstrip("-")
            xn, yn = _label_point(x0, y0, x, y, ring)
            out.append(f"CLAYER {L['magnet_name']}")

            qc = cfg.get("qc_sample", {})
            if disp[:1] == "Q":
                slength = _q_distance_label(el.s, ring)
                label = f"{disp} ({slength})" if slength else disp
                # QCS は代表のみ表示
                if disp[:3] == "QC1":
                    if iqc1 in tuple(qc.get("QC1", (20, 270))):
                        out.append(f"_TEXT {_fmt(xn)},{_fmt(yn)} {text_h} {_fmt(tn)} {label}")
                    iqc1 += 1
                elif disp[:3] == "QC2":
                    if iqc2 in tuple(qc.get("QC2", (20, 200))):
                        out.append(f"_TEXT {_fmt(xn)},{_fmt(yn)} {text_h} {_fmt(tn)} {label}")
                    iqc2 += 1
                else:
                    out.append(f"_TEXT {_fmt(xn)},{_fmt(yn)} {text_h} {_fmt(tn)} {label}")
            elif disp[:3] != "ECS":
                out.append(f"_TEXT {_fmt(xn)},{_fmt(yn)} {text_h} {_fmt(tn)} {disp}")

            # --- マグネットブロック挿入 ---
            xb = (x0 + x) / 2.0
            yb = (y0 + y) / 2.0
            if nmagblock0:
                out.append(f"CLAYER {L['magnet']}")
                # 挿入: ブロック名 → _S 尺度 → _R 回転角 → 挿入点(最後)。
                # 尺度・回転を点の前に先付けするので、点の確定後にプロンプトが出ず、
                # 余分なトークンがコマンド行へはみ出さない (全環境で確実)。
                out.append(
                    f"{_INS} {nmagblock0} _S {_INS_SCALE} "
                    f"_R {_fmt(tb)} {_fmt(xb)},{_fmt(yb)}"
                )

            # 前状態を更新 (角度も)
            tn0, tb0 = tn, tb

        # 前状態の更新 (毎要素)
        x0, y0 = x, y
        nelem0 = name
        nmagblock0 = el.mag_block
        tvalue0 = el.value
        length0 = el.length
        polyline.append(f"{_fmt(x)},{_fmt(y)}")

    # 最後にビーム中心線を引く
    if polyline:
        out.append(f"CLAYER {L['base']}")
        # LINE: 各点を改行区切りで与え、最後に空行(Enter)でコマンドを終了する。
        out.append("_LINE")
        out.extend(polyline)
        out.append("")          # 空行 = Enter, LINE コマンドを終了
        out.append("_ZOOM _E")
        out.append("_REGEN")

    out.append("FILEDIA 1")
    return res


# --------------------------------------------------------------------------
# ダクトスクリプト生成 (Make_HER_Duct_ScriptFile の移植・簡約版)
# --------------------------------------------------------------------------


def generate_duct_script(elements: list[Element], ring: str,
                         duct_resolver: "DuctBlockResolver",
                         lock_after: bool = False) -> ConversionResult:
    """
    ダクト(ビームパイプ)配置用 AutoCAD スクリプトを生成する。

    各要素について、対応するダクトブロック (複数可) を 1 個前の点の位置に
    区間の角度で挿入する。ダクトブロック名の決定規則は非常に多岐に渡るため、
    本実装では duct_resolver (要素名->ブロック名リスト) に委譲し、
    その対応表は config/{ring}_duct_blocks.json として編集可能にしている。
    """
    res = ConversionResult()
    L = SETTINGS[ring]["layers"]

    out = res.script_lines
    out.append("OSMODE 0")
    out.append(f"_-LAYER _N {L['duct']} _C {L['duct_color']} {L['duct']} ")
    out.append("_ZOOM _A")

    x0 = y0 = None
    x1 = y1 = None
    nelem0 = "L"
    blocks0: list[str] = []
    tb0 = tb1 = 0.0

    for el in elements:
        name = el.name
        x, y = el.x_mm, el.y_mm

        if name.startswith("P"):
            continue

        if blocks0 and x0 is not None:
            # 現区間 (前点→現点) の角度
            tb = _duct_angle(x0, y0, x, y, tb0)
            # 前要素が B(偏向磁石) なら「もう一つ前」の角度を再計算 (2個目以降のダクト用)。
            # 反転印 "-" を除いて B 判定する (-BLA6RP.1 等も B として扱う)。
            prev_is_B = nelem0.lstrip("-")[:1] == "B"
            if prev_is_B and x1 is not None:
                tb0 = _duct_angle(x1, y1, x0, y0, tb1)

            out.append(f"CLAYER {L['duct']}")
            for k, blk in enumerate(blocks0):
                # n>1 (2個目以降) かつ前要素が B なら、もう一つ前の角度 tb0 を使う
                angle = tb0 if (prev_is_B and k > 0) else tb
                out.append(
                    f"{_INS} {blk} _S {_INS_SCALE} "
                    f"_R {_fmt(angle)} {_fmt(x0)},{_fmt(y0)}"
                )
                res.block_usage[blk] = res.block_usage.get(blk, 0) + 1
        else:
            tb = tb0

        # 前状態の保存 (VBA の順序に忠実に)
        x1, y1 = x0, y0
        x0, y0 = x, y
        nelem0 = name
        blocks0 = duct_resolver.resolve(el)
        tb1 = tb0
        tb0 = tb

    if lock_after:
        # 配置完了後、全画層をロックする (誤操作防止)。
        out.append("_-LAYER")
        out.append("_LOCK")
        out.append("*")
        out.append("")          # 空行 = Enter, LAYER コマンドを終了
    out.append("FILEDIA 1")
    return res


def _duct_angle(x0, y0, x, y, prev_tb):
    """ダクトブロックの挿入角度 (Make_*_Duct_ScriptFile の角度計算を忠実移植)。"""
    if (x0 - x) != 0:
        tb = math.atan((y0 - y) / (x0 - x)) / _PI * 180.0
        if x < 0 and y >= 0:
            if tb < -80.0:           # 日光シケイン補正 (この分岐が抜けていた)
                tb += 180.0
        elif x < 0 and y < 0:
            tb += 180.0
        elif x >= 0 and y < 0:
            tb += 180.0
        # x>=0, y>=0 は変更なし
    else:
        tb = 90.0 if x < 0 else 270.0
    if (x0 - x) == 0 and (y0 - y) == 0:
        tb = prev_tb
    return tb


class DuctBlockResolver:
    """
    要素名 -> ダクトAutoCADブロック名リスト の解決器。

    統合 config {ring}_ducts.json は「要素名 -> 挿入するブロック名リスト」の
    単一マップで、ブロック名は "_Or"(オーダー済み) も含む**最終名**。
    書いてある名前がそのまま図面に挿入される (WYSIWYG)。ダクトの差し替えは
    このファイルの該当行を書き換えるだけでよい。

    (補足) 旧VBAの *InsertDuctBlockName は要素名から中間キー DuctBN を作り
    *_DuctBlock テーブルで引いていたが、現行ラティスでは Lattice 由来の
    直接マップが全要素をカバーしており、フォールバック表・"_Or"付与規則は
    不要になったため廃止した (旧ファイルは config/legacy/ に保存)。
    """

    def __init__(self, ring: str):
        self.ring = ring
        try:
            # 統合形式 (最終ブロック名の直接マップ)
            self.by_element: dict[str, list[str]] = _load_json(
                f"{ring.lower()}_ducts.json")
            self._legacy = False
            self.block_map: dict[str, list[str]] = {}
            self.ordered: set = set()
        except FileNotFoundError:
            # 旧形式 (分割ファイル) との後方互換
            self._legacy = True
            self.block_map = _load_json(f"{ring.lower()}_duct_blocks.json")
            try:
                self.by_element = _load_json(f"{ring.lower()}_duct_by_element.json")
            except FileNotFoundError:
                self.by_element = {}
            try:
                self.ordered = set(_load_json(f"{ring.lower()}_duct_ordered.json"))
            except FileNotFoundError:
                self.ordered = set()
        # 旧形式の BN フォールバック用
        self._mag = MagnetBlockResolver(ring) if self._legacy else None

    def _apply_ordered(self, blocks: list[str]) -> list[str]:
        """(旧形式のみ) オーダー済みブロックには "_Or" を付ける。"""
        return [b + "_Or" if b in self.ordered else b for b in blocks]

    def resolve(self, el: Element) -> list[str]:
        # 要素名そのものと、先頭の "-"(反転印) を除いた名前の両方で試す。
        raw = el.name
        stripped = raw.lstrip("-")
        candidates = (raw, stripped) if raw != stripped else (raw,)
        if not self._legacy:
            for name in candidates:
                if name in self.by_element:
                    return list(self.by_element[name])
            return []
        # ---- 以下、旧形式のフォールバック解決 ----
        for name in candidates:
            if name in self.by_element:
                return self._apply_ordered(self.by_element[name])
            if name in self.block_map:
                return self._apply_ordered(self.block_map[name])
            nodot = name.replace(".", "")
            if nodot in self.block_map:
                return self._apply_ordered(self.block_map[nodot])
        bn = self._mag.derive_bn(el)
        if bn in self.block_map:
            return self._apply_ordered(self.block_map[bn])
        return []


# --------------------------------------------------------------------------
# 数値整形 (VBA の Str/Trim 相当: 不要な桁を出さない)
# --------------------------------------------------------------------------


def _fmt(v: float) -> str:
    """AutoCAD に渡す数値文字列。整数はそのまま、小数は冗長な0を落とす。"""
    if v == int(v):
        return str(int(v))
    return repr(round(v, 6))


# --------------------------------------------------------------------------
# 高レベル API
# --------------------------------------------------------------------------


def convert_dispog_to_magnet_scr(dispog_path, scr_path, ring,
                                 clear_layers=False,
                                 draw_bend_arcs=False) -> ConversionResult:
    elements = parse_dispog(dispog_path)
    result = generate_magnet_script(elements, ring, clear_layers=clear_layers,
                                    draw_bend_arcs=draw_bend_arcs)
    Path(scr_path).write_text(result.text(), encoding="utf-8")
    return result


def convert_dispog_to_duct_scr(dispog_path, scr_path, ring,
                               lock_after=False) -> ConversionResult:
    elements = parse_dispog(dispog_path)
    resolver = DuctBlockResolver(ring)
    result = generate_duct_script(elements, ring, resolver, lock_after=lock_after)
    Path(scr_path).write_text(result.text(), encoding="utf-8")
    return result
