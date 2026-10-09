"""
synrad3d_wall.py — dispog + Duct_Type の断面情報から Synrad3D の wall file を生成
======================================================================

Bmad/Synrad3D の wall file は Fortran namelist 形式で、縦位置 s ごとに断面を置く
`&place` と、断面の頂点形状を定義する `&shape_def` から成る:

    &place section = <s>, "<name>", "[<subchamber>:]<shape_id>[@START|@END]" /
    &shape_def
      name = "<shape_id>"
      r0 = <x0>, <y0>
      v(1) = <x> <y> [<radius_x> <radius_y> <tilt>]
      ...
    /

本ツールは既存パイプラインの 2 つの情報だけで wall file を半自動生成する:
  * 各要素の縦位置 s        … dispog（skekb_layout.parse_dispog）
  * 各要素の断面コード      … Duct_Type の Cross Section（config/*_ducttype.json）

断面コード → 形状 の対応:
  * レーストラック "WxH" … 自動。半幅 W/2・半高 H/2、両端は半径 H/2 の半円。
      例 104x50 → v(1)=(0.052,0), v(2)=(0.027,0.025,0.025), v(3)=(0,0.025)
      ※ 旧 wall file (sher_v2 / sler_v7) の該当形状 5 種すべてとこの規則が一致する。
        矩形ではないので注意（角が丸い）。
  * 円 "fNNN" / "fNN_MM" / "fNN.M" / "fNNN-n" … 自動。直径 NNN.MM[mm] の円。
      "_" と "." はどちらも小数点（旧ファイルは f15_98 と f9.6 が混在）。
      末尾の "-n" は同径パイプの枝番（f20-1 / f20-2）であってテーパーではない。
  * テーパー "A^B" / "A-B"（A,B が断面コードのとき）… A,B を要素の前後に分けて配置
  * それ以外（アンテチェンバ系 f90x220_Ar など）… config/wall_shapes.json でユーザ定義

Synrad3D は隣接断面間を r(θ) で線形補間するため、同一断面が続く区間は両端だけ置けば良い
（本ツールは連続同一を畳んで区間端のみ出力する）。

頂点の制約（Bmad wall3d_section_initializer より。書き出し前に検証する）:
  * 頂点は θ = atan2(y,x) が単調増加。隣接頂点の θ 差は 0 より大きく 180° 未満。
  * 総回転角は 2π 未満。
  * radius_x = 0 で radius_y ≠ 0 は不可。radius_x と radius_y は同符号。
  * 頂点 1 個かつ radius_x = 0 のとき x または y が 0 だと断面積 0 でエラー。
  * つまり断面は r0 から見て「星形」（r(θ) が一価）でなければならない。
    コリメータのように内側へ張り出す形状は表現できず、近似が必要（サブチェンバでは
    直せない。サブチェンバは和集合なので開口を広げる方向にしか効かない）。

注意:
  * 生成した wall file の s は Bmad ラティスの s と一致している必要があります
    （dispog の s が機械 s と一致している前提）。
  * patch 要素と s が重なる位置に断面は置けません（Synrad3D 側の制約）。
  * アンテチェンバの向き（ウィングが +x / -x のどちら側か）は wall_shapes.json の
    頂点そのもので決まります。HER / LER で向きが逆になる場合はリング別のライブラリを
    用意してください（旧 wall file では HER / LER とも同一形状が使われています）。
"""

from __future__ import annotations
import json
import math
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# skekb_layout.py が 1 つ上にある構成（SKEKB_Layout/synrad3d/ に置いた場合）にも対応
for _p in (_HERE, _HERE.parent):
    if (_p / "skekb_layout.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import skekb_layout as sk

# config は「このスクリプトの隣」→「1 つ上」の順に探す。
#   wall_shapes.json / *_wall_inserts.json / wall_profiles/ … synrad3d 側の config
#   *_ducts.json / *_ducttype.json                        … Duct_Table 側の config
_CONFIG_DIRS = [_HERE / "config", _HERE.parent / "config"]
_CONFIG = _CONFIG_DIRS[0]          # 相対パス（wall_profiles など）の基準


def _find(name):
    """設定ファイルを config 探索パスから見つける。無ければ None。"""
    for d in _CONFIG_DIRS:
        p = d / name
        if p.exists():
            return p
    return None

# 断面コードとして認識するパターン（テーパー分解の判定にも使う）
_RE_RACETRACK = re.compile(r"(\d+)x(\d+)")
_RE_CIRCLE = re.compile(r"f(\d+)(?:[._](\d+))?(?:-(\d+))?")


def _load(name, default=None):
    p = _find(name)
    if p is None:
        return default
    return json.load(open(p, encoding="utf-8"))


def _load_library():
    """wall_shapes.json を読む。'_' 始まりのキー（注記）は落とす。"""
    raw = _load("wall_shapes.json", {}) or {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _cross_section(duct: str, ducttype: dict) -> str:
    d = ducttype.get(duct, {})
    return str(d.get("Cross Sect", "") or d.get("Cross Section", "")).strip()


# --------------------------------------------------------------------------
# 断面コード → 形状
# --------------------------------------------------------------------------


def _auto_shape(code: str):
    """レーストラック WxH / 円 fNNN をコードから自動生成。出来なければ None。単位 mm→m。"""
    m = _RE_RACETRACK.fullmatch(code)
    if m:
        w = int(m.group(1)) / 2000.0          # 半幅
        h = int(m.group(2)) / 2000.0          # 半高 = 端部半円の半径
        if w < h:                             # 縦長はレーストラックとして解釈できない
            return {"r0": [0.0, 0.0], "v": [[w, 0.0], [0.0, h, h]], "_auto": "ellipse"}
        return {"r0": [0.0, 0.0],
                "v": [[w, 0.0], [round(w - h, 9), h, h], [0.0, h]],
                "_auto": "racetrack"}
    m = _RE_CIRCLE.fullmatch(code)
    if m:
        d_mm = float(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
        r = d_mm / 2000.0
        return {"r0": [0.0, 0.0], "v": [[r, 0.0], [0.0, r, r]], "_auto": "circle"}
    return None


def _is_code(part: str) -> bool:
    """テーパー分解の判定用。断面コードらしいか（ライブラリ照合は呼び出し側）。"""
    return bool(_RE_RACETRACK.fullmatch(part) or _RE_CIRCLE.fullmatch(part)
                or re.fullmatch(r"f?\d+x\d+[A-Za-z_0-9]*", part))


def _placeholder_shape(code: str):
    """未定義コードの仮形状（寸法らしき数字から外接矩形）。要ユーザ確認。"""
    nums = [int(x) for x in re.findall(r"\d+", code)]
    if len(nums) >= 2:
        w = max(nums[:2]) / 2000.0
        h = min(nums[:2]) / 2000.0
    elif nums:
        w = h = nums[0] / 2000.0
    else:
        w = h = 0.05
    return {"r0": [0.0, 0.0], "v": [[w, h]], "_placeholder": True}


def _split_taper(code: str, library=None):
    """
    テーパーコードを基本コードのリストに分解する。

    '^' は常に区切り。'-' は「両側が断面コードとして解釈できるとき」だけ区切る。
    これにより f20-1 / f20-2（同径パイプの枝番）を誤ってテーパーに分解しない。
    """
    library = library or {}

    def known(p):
        return p in library or _is_code(p)

    parts = [p for p in code.split("^") if p]
    out = []
    for p in parts:
        if "-" not in p or known(p):
            out.append(p)
            continue
        cand = [q for q in p.split("-") if q]
        if len(cand) >= 2 and all(known(q) for q in cand):
            out.extend(cand)
        else:
            out.append(p)
    return out


# Synrad3D の shape_def namelist は v(100) 固定。終端検出に 1 枠使うので実質 99 個。
MAX_VERTEX = 99


def validate_shape(shape) -> list[str]:
    """Bmad の頂点規則に照らして問題点を列挙する（空リストなら OK）。"""
    v = shape.get("v") or []
    errs: list[str] = []
    if not v:
        return ["頂点がありません"]
    if len(v) > MAX_VERTEX:
        errs.append(f"頂点が {len(v)} 個あります。Synrad3D の shape_def は v(100) 固定なので"
                    f"{MAX_VERTEX} 個以下にしてください（超えると "
                    f"'Index 1 out of range for namelist variable v' で落ちます）")
    if len(v) == 1:
        rx = v[0][2] if len(v[0]) > 2 else 0.0
        if rx == 0 and (v[0][0] == 0 or v[0][1] == 0):
            errs.append("頂点1個・radius_x=0 で x または y が 0（断面積 0）")
    # Bmad (wall3d_section_initializer) と同じ判定にする。θ は atan2 で求め、
    # 直前の頂点以下なら 2π を 1 回だけ足して巻き戻す（全周記述で 180° をまたぐため）。
    prev = first = None
    for i, p in enumerate(v, start=1):
        a = math.atan2(p[1], p[0])
        if prev is None:
            first = a
        else:
            if a <= prev:
                a += 2 * math.pi
            if a <= prev:
                errs.append(f"v({i}) = ({p[0]:.5g}, {p[1]:.5g}): θ が増加していない "
                            f"({math.degrees(prev):.2f}° → {math.degrees(a):.2f}°)")
            elif a >= prev + math.pi:
                errs.append(f"v({i}): 直前の頂点との θ 差が 180° 以上")
        prev = a
        rx = p[2] if len(p) > 2 else 0.0
        ry = p[3] if len(p) > 3 else 0.0
        if rx == 0 and ry != 0:
            errs.append(f"v({i}): radius_x = 0 なのに radius_y ≠ 0")
        if rx * ry < 0:
            errs.append(f"v({i}): radius_x と radius_y の符号が違う")
    if len(v) > 1 and prev - first >= 2 * math.pi:
        errs.append("総回転角が 2π 以上")
    return errs


def _resolve_shape(code, library, auto_cache, placeholders):
    """断面コード → shape_id を返し、必要な shape_def を auto_cache に登録。"""
    if code in library:                     # ユーザ定義（最優先）
        auto_cache.setdefault(code, library[code])
        if library[code].get("_placeholder"):
            placeholders.add(code)
        return code
    if code in auto_cache:
        return code
    sh = _auto_shape(code)                  # レーストラック・円は自動
    if sh is not None:
        auto_cache[code] = sh
        return code
    auto_cache[code] = _placeholder_shape(code)   # 仮形状（要定義）
    placeholders.add(code)
    return code


# --------------------------------------------------------------------------
# s 指定の差し込み（IR・コリメータなど）
# --------------------------------------------------------------------------
#
# 断面コード（= 型番）で決まらない断面、つまり「この s にこの形状を置く」という
# 情報は config/{ring}_wall_inserts.json に置く。wall_shapes.json が Duct_Type に
# 相当する「型番の諸元（重複しない）」であるのに対し、こちらは Component に相当する
# 「設置の一覧（同じ形状が何か所にも現れる）」で、多対一の関係になっている。


def _num_code(mm: float, prefix: str = "f") -> str:
    """20.0 → 'f20' / 44.8 → 'f44_8'（既存の円コードの命名に合わせる）。"""
    s = f"{mm:.4f}".rstrip("0").rstrip(".")
    return prefix + s.replace(".", "_")


def _aperture_shape(w_mm: float, h_mm: float):
    """全幅 w × 全高 h［mm］→ (shape_id, shape)。等しければ円、違えば楕円。"""
    if abs(w_mm - h_mm) < 1e-9:
        return _num_code(w_mm), None        # 円は shape_id から自動生成できる
    sid = "e" + _num_code(w_mm, "") + "x" + _num_code(h_mm, "")
    return sid, {"r0": [0.0, 0.0], "v": [[0.0, 0.0, w_mm / 2000.0, h_mm / 2000.0]],
                 "_auto": "ellipse"}


def _profile_sections(prof: dict, base_dir: Path):
    """
    CSV（s, 水平全幅, 垂直全幅）→ [(s[m], shape_id, shape|None), ...]。

    s = s_origin + s_direction * (生値 * s_scale + s_offset)
    で機械 s［m］に換算する。QCSR のように IP から逆向きに測った表は
    s_origin = 3016.315, s_direction = -1 と書けば扱える。
    """
    path = Path(prof["file"])
    if not path.is_absolute():
        for d in _CONFIG_DIRS:
            if (d / path).exists():
                path = d / path
                break
        else:
            path = base_dir / path
    ic = prof.get("s_column", 0)
    iw = prof.get("width_column", 1)
    ih = prof.get("height_column", 2)
    scale = float(prof.get("s_scale", 1.0))
    off = float(prof.get("s_offset", 0.0))
    origin = float(prof.get("s_origin", 0.0))
    direc = float(prof.get("s_direction", 1.0))
    size = float(prof.get("size_scale", 1.0))     # 生値 → mm

    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            c = [x.strip() for x in line.split(",")]
            if not c[ic].replace(".", "", 1).replace("-", "", 1).isdigit():
                continue                              # ヘッダ行を読み飛ばす
            s = origin + direc * (float(c[ic]) * scale + off)
            rows.append((s, float(c[iw]) * size, float(c[ih]) * size))
    # s_direction < 0 の表は行順も反転する。同じ s に 2 行ある段差で、どちらが
    # 上流側の値かが入れ替わるため（安定ソートなので反転しないと段差が裏返る）。
    if direc < 0:
        rows.reverse()
    rows.sort(key=lambda r: r[0])

    out = []
    for s, w, h in rows:
        sid, sh = _aperture_shape(w, h)
        out.append((s, sid, sh))
    return out


def _ridge_sections(rg: dict):
    """
    リッジ構造（テーパー管の内面に並ぶ山）を展開して [(s, 直径[mm]), ...] を返す。

      pitch_mm       山のピッチ（長手方向の繰り返し間隔）
      crest_width_mm 山の頂上の長手方向の幅（shape="trapezoid" のとき）
      flank_mm       山の斜面の長手方向の長さ（同上。0 なら最小値 0.01 mm）
      height_mm      山の高さ（管内面から頂点まで。半径方向）。負で溝になる
      base_d_start_mm / base_d_end_mm  ベース管の内径。s_start → s_end で線形テーパー
      shape          "sawtooth"（1 周期 2 断面の三角波）/ "trapezoid"（1 周期 4 断面）
      start_with     "base"（既定）/ "crest"。s_start を谷から始めるか山から始めるか
      round_mm       径の丸め（既定 0.01 mm。同径がまとまって shape_def が減る）
    """
    s0, s1 = float(rg["s_start"]), float(rg["s_end"])
    pitch = float(rg["pitch_mm"]) / 1000.0
    kind = rg.get("shape", "sawtooth")
    crest = float(rg.get("crest_width_mm", 0.0)) / 1000.0
    flank = float(rg.get("flank_mm", 0.0)) / 1000.0
    hgt = float(rg["height_mm"])
    d0 = float(rg["base_d_start_mm"])
    d1 = float(rg.get("base_d_end_mm", d0))
    rnd = float(rg.get("round_mm", 0.01))
    if pitch <= 0 or s1 <= s0:
        raise ValueError("pitch_mm > 0 かつ s_end > s_start が必要")

    def base_d(s):
        return d0 + (d1 - d0) * (s - s0) / (s1 - s0)

    def q(d):
        return round(d / rnd) * rnd if rnd > 0 else d

    out = []
    crest_first = rg.get("start_with", "base") == "crest"
    if kind == "sawtooth":
        step = pitch / 2.0
        i = 0
        while True:
            s = s0 + i * step
            if s > s1 + 1e-12:
                break
            on = (i % 2 == 0) if crest_first else (i % 2 == 1)
            out.append((min(s, s1), q(base_d(s) - (2 * hgt if on else 0.0))))
            i += 1
    else:
        f = flank if flank > 0 else 1e-5
        if 2 * f + crest >= pitch:
            raise ValueError("2*flank_mm + crest_width_mm が pitch_mm 以上です")
        k = 0
        while True:
            a = s0 + k * pitch
            if a > s1 + 1e-12:
                break
            for ds, on in ((0.0, False), (f, True), (f + crest, True),
                           (2 * f + crest, False)):
                s = a + ds
                if s > s1 + 1e-12:
                    break
                out.append((s, q(base_d(s) - (2 * hgt if on else 0.0))))
            k += 1
    if out and out[-1][0] < s1 - 1e-12:
        out.append((s1, q(base_d(s1))))
    return out


def _insert_sections(ins: dict, base_dir: Path):
    """1 件の insert レコード → [(s, shape_id, shape|None, label), ...] と s 範囲。"""
    name = ins.get("name", ins.get("shape", "insert"))
    if "profile" in ins:
        secs = [(s, sid, sh, name) for s, sid, sh in
                _profile_sections(ins["profile"], base_dir)]
        if not secs:
            return [], None
        return secs, (secs[0][0], secs[-1][0])
    if "ridge" in ins:
        secs = [(s, _num_code(d), None, name) for s, d in _ridge_sections(ins["ridge"])]
        if not secs:
            return [], None
        return secs, (secs[0][0], secs[-1][0])

    sid = ins["shape"]
    if "s_center" in ins:                    # 中心 s と先端長 L で指定
        c = float(ins["s_center"])
        half = float(ins.get("length", 0.0)) / 2.0
        s_list = [c - half, c + half]
    else:
        s = ins["s"]
        s_list = ([float(s)] if isinstance(s, (int, float))
                  else [float(x) for x in s])
    if len(s_list) == 1:
        s_list = [s_list[0], s_list[0]]
    lab = f"{name}_tip" if ("s_center" in ins or s_list[0] != s_list[-1]) else name
    return ([(s_list[0], sid, None, lab), (s_list[-1], sid, None, lab)],
            (s_list[0], s_list[-1]))


# dispog のコリメータマーカー。PMD02V1 → D02V1。
# 先頭の "-" は反転ブロックの印。"F" は Fake の意味で、マーカーだけがあり実機は
# 設置されていないので既定では拾わない。LPMD06V1A のように前に別の文字が付くものは、
# コリメータ本体ではなく周辺ダクトなので拾わない。
_RE_COLLIMATOR = re.compile(r"-?(F?)PM(D\d{2}[HV]\d+[A-Za-z0-9]*)")


def collimator_positions(dispog_path):
    """dispog からコリメータマーカーの s を拾う。({実機: s}, {Fake: s})"""
    real: dict[str, float] = {}
    fake: dict[str, float] = {}

    def add(name, s):
        m = _RE_COLLIMATOR.fullmatch(str(name).strip())
        if m:
            (fake if m.group(1) else real).setdefault(m.group(2), float(s))

    try:
        for e in sk.parse_dispog(dispog_path):
            add(e.name, e.s)
    except Exception:                                    # noqa: BLE001
        pass
    if real or fake:
        return real, fake
    # skekb_layout がマーカー要素を落とす場合に備えた直接読み（列: 名前, …, s, 長さ）
    with open(dispog_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            c = line.split()
            if len(c) < 6:
                continue
            try:
                add(c[0], float(c[4]))
            except ValueError:
                pass
    return real, fake


def update_collimator_inserts(dispog_path, ring, length=None, guard=None,
                              enable=True, path=None, include_fake=False):
    """
    dispog のマーカー位置を読んで {ring}_wall_inserts.json の s_center を埋める。

    形状ライブラリに断面のあるコリメータだけを書き込み、無いものは一覧で報告する。
    既存行の length / guard は、引数で明示しない限りそのまま残す。
    """
    ring = ring.lower()
    p = Path(path) if path else (_find(f"{ring}_wall_inserts.json")
                                 or _CONFIG / f"{ring}_wall_inserts.json")
    doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    inserts = doc.setdefault("inserts", [])
    library = _load_library()

    # コリメータごとの length / guard。優先順位は
    #   collimator_params[コード] > コマンドラインの --length/--guard
    #   > 台帳に既にある値 > collimator_params._default > 0.010 / 0.30
    params = doc.get("collimator_params", {})
    dflt = params.get("_default", {})

    def pick(code, key, cli, builtin, row):
        if key in params.get(code, {}):
            return params[code][key]
        if cli is not None:
            return cli
        if key in row:
            return row[key]
        return dflt.get(key, builtin)

    real, fake = collimator_positions(dispog_path)
    pos = dict(real)
    if include_fake:
        pos.update(fake)
    by_name = {r.get("name"): r for r in inserts}
    added, updated, missing = [], [], []

    for code in sorted(pos):
        if code not in library:
            missing.append(code)
            continue
        row = by_name.get(code)
        if row is None:
            row = {"name": code, "shape": code}
            inserts.append(row)
            added.append(code)
        else:
            updated.append(code)
        row.pop("s", None)
        row["s_center"] = round(pos[code], 6)
        row["length"] = pick(code, "length", length, 0.010, row)
        row["guard"] = pick(code, "guard", guard, 0.30, row)
        if enable:
            row.pop("disabled", None)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(p), "added": added, "updated": updated, "missing": missing,
            "real": sorted(real), "fake": sorted(fake)}


def _ambient_at(sections, s):
    """自動生成された断面列のうち、位置 s の直前（無ければ直後）の shape_id。"""
    prev = None
    for ss, sid, _lab in sections:
        if ss <= s:
            prev = sid
        else:
            return prev if prev is not None else sid
    return prev


def apply_inserts(sections, inserts, library, auto_cache, placeholders, base_dir):
    """自動生成の断面列に insert を適用して返す。"""
    notes = []
    for ins in inserts:
        if ins.get("disabled"):
            continue
        name = ins.get("name", ins.get("shape", "?"))
        try:
            secs, span = _insert_sections(ins, base_dir)
        except (OSError, KeyError, ValueError) as e:
            notes.append(f"{name}: 読み込み失敗 ({e})")
            continue
        if not secs:
            notes.append(f"{name}: 断面が 0 個")
            continue
        s0, s1 = span
        guard = float(ins.get("guard", 0.0))

        keep, bracket = [], []
        if ins.get("replace", True):
            lo, hi = s0 - guard, s1 + guard
            if guard > 0:
                a0, a1 = _ambient_at(sections, lo), _ambient_at(sections, hi)
                if a0: bracket.append((lo, a0, f"{name}_in"))
                if a1: bracket.append((hi, a1, f"{name}_out"))
            keep = [r for r in sections if not (lo <= r[0] <= hi)]
        else:
            keep = list(sections)

        for s, sid, sh, _lab in secs:
            if sh is not None:
                auto_cache.setdefault(sid, sh)
            else:
                _resolve_shape(sid, library, auto_cache, placeholders)
        for _s, sid, _lab in bracket:
            _resolve_shape(sid, library, auto_cache, placeholders)

        sections = sorted(keep + bracket + [(s, sid, lab) for s, sid, _sh, lab in secs],
                          key=lambda r: r[0])
        notes.append(f"{name}: s={s0:.4f}..{s1:.4f} に {len(secs)} 断面")
    return sections, notes


# --------------------------------------------------------------------------

# 断面列の構築
# --------------------------------------------------------------------------


def build_sections(dispog_path: str, ring: str):
    """(s, shape_id) の列と、使用した shape_def 辞書、未定義コード集合を返す。"""
    elements = sk.parse_dispog(dispog_path)
    _d = _load(f"{ring.lower()}_ducts.json", None)
    if _d is not None:
        by_element = {k: [b[:-3] if b.endswith("_Or") else b for b in v]
                      for k, v in _d.items()}
    else:
        by_element = _load(f"{ring.lower()}_duct_by_element.json", {})
    ducttype = _load(f"{ring.lower()}_ducttype.json", {})
    library = _load_library()

    auto_cache: dict = {}
    placeholders: set = set()
    raw: list[tuple[float, str, str]] = []  # (s, shape_id, &place のラベル)

    for el in sorted(elements, key=lambda e: e.s):
        ducts = by_element.get(el.name) or by_element.get(el.name.lstrip("-"))
        if not ducts:
            continue
        # 主ビームパイプ（断面が定義されている最初のダクト）の断面コード
        code = ""
        for d in ducts:
            cs = _cross_section(d, ducttype)
            if cs and cs != "-":
                code = cs
                break
        if not code:
            continue

        parts = _split_taper(code, library)
        if len(parts) == 1:
            sid = _resolve_shape(parts[0], library, auto_cache, placeholders)
            raw.append((el.s, sid, el.name))
        else:                               # テーパー: 要素長で前後に振り分け
            n = len(parts)
            length = el.length or 0.0
            for i, p in enumerate(parts):
                sid = _resolve_shape(p, library, auto_cache, placeholders)
                s = el.s + (length * i / (n - 1) if length else i * 1e-4)
                raw.append((s, sid, el.name))

    raw.sort(key=lambda t: t[0])

    # 連続同一断面は区間端のみ残す（補間で内部は一定になる）
    sections: list[tuple[float, str, str]] = []
    for i, (s, sid, lab) in enumerate(raw):
        prev = raw[i - 1][1] if i > 0 else None
        nxt = raw[i + 1][1] if i < len(raw) - 1 else None
        if sid == prev and sid == nxt:
            continue
        sections.append((s, sid, lab))

    # s 指定の差し込み（IR・コリメータなど）
    inserts = (_load(f"{ring.lower()}_wall_inserts.json", {}) or {}).get("inserts", [])
    sections, insert_notes = apply_inserts(
        sections, inserts, library, auto_cache, placeholders, _CONFIG)

    # s を厳密に増加させる（同値は微小量ずらす）
    eps = 1e-6
    for i in range(1, len(sections)):
        if sections[i][0] <= sections[i - 1][0]:
            sections[i] = (sections[i - 1][0] + eps,) + sections[i][1:]

    used = {sid: auto_cache[sid] for _, sid, _lab in sections if sid in auto_cache}
    return sections, used, placeholders, insert_notes


# --------------------------------------------------------------------------
# 書き出し
# --------------------------------------------------------------------------


def _fmt_vertex(v):
    parts = [f"{v[0]:.6g}", f"{v[1]:.6g}"]
    tail = list(v[2:])
    while tail and tail[-1] == 0:
        tail.pop()
    parts += [f"{x:.6g}" for x in tail]
    return ", ".join(parts)


def _section_id(sid: str, shape: dict, edge: str = "") -> str:
    sub = shape.get("subchamber")
    out = f"{sub}:{sid}" if sub else sid
    return f"{out}@{edge}" if edge else out


def _overlay_runs(sections, library):
    """
    主断面に付随するサブチェンバ（アンテチェンバ奥壁など）の配置を組み立てる。

    wall_shapes.json で主断面に "overlays": ["BackWall_shape"] と書いておくと、
    その主断面が置かれている区間にだけサブチェンバを重ねて置く。
    区間の両端には @START / @END を付ける。これが無いと Synrad3D は
    「open-ended サブチェンバ」と解釈して機械全長にわたって存在させてしまう。

    差し込みに guard がある場合、サブチェンバは先端部だけでなく guard の外端
    （ラベル <名前>_in / <名前>_out の断面）まで広げる。コリメータのローブを先端の
    10 mm だけに置くと、前後のテーパー区間では主チェンバ（中央ギャップ）だけになり、
    ブレードの無い方向にまで架空の絞りができてしまうため。

    戻り値: {s: [(overlay_id, edge), ...]} と、使った overlay の名前集合。
    """
    # 先端部の断面が持つ overlays を、同じ差し込みの _in / _out 断面にも引き継ぐ
    tip_overlays: dict[str, set] = {}
    for _s, sid, lab in sections:
        if lab.endswith("_tip"):
            tip_overlays[lab[:-4]] = set(library.get(sid, {}).get("overlays") or [])

    at: dict[float, list[tuple[str, str]]] = {}
    names: set[str] = set()
    active: dict[str, float] = {}           # overlay_id -> START の s
    last_s: dict[str, float] = {}           # overlay_id -> 直近に有効だった s
    short: list[str] = []

    def close(oid):
        s_end = last_s[oid]
        if s_end == active[oid]:
            short.append(f"{oid}@{s_end:.4f}")
        at.setdefault(s_end, []).append((oid, "END"))
        del active[oid]

    for s, sid, lab in sections:
        entry = library.get(sid, {})
        if entry.get("overlays_pass") and active:
            # コリメータのように区間の途中に挟まる断面。サブチェンバは継続させる。
            continue
        want = set(entry.get("overlays") or [])
        if lab.endswith("_in"):
            want |= tip_overlays.get(lab[:-3], set())
        elif lab.endswith("_out"):
            want |= tip_overlays.get(lab[:-4], set())
        for oid in sorted(set(active) - want):
            close(oid)
        for oid in sorted(want):
            if oid not in active:
                active[oid] = s
                at.setdefault(s, []).append((oid, "START"))
            names.add(oid)
            last_s[oid] = s
    for oid in sorted(active):
        close(oid)
    return at, names, short


def _shape_polygon(shape, step_deg=2.0):
    """
    shape_def 1 個を、Bmad と同じ対称展開・円弧中心の規則で外周多角形に戻す（絶対座標 [m]）。
    包含判定などの検査用。
    """
    r0 = shape.get("r0", [0.0, 0.0])
    v = [list(p) + [0.0] * (3 - len(p)) for p in shape["v"]]
    T = 1e-12
    if len(v) == 1 and v[0][2] == 0:
        a, b = v[0][0], v[0][1]
        v = [[a, b, 0.0], [-a, b, 0.0], [-a, -b, 0.0], [a, -b, 0.0]]
    elif len(v) == 1:                                   # 頂点 1 個＋半径 = 円／楕円
        rx = v[0][2]; ry = v[0][3] if len(v[0]) > 3 and v[0][3] else rx
        return [(r0[0] + v[0][0] + rx * math.cos(2 * math.pi * k / 90),
                 r0[1] + v[0][1] + ry * math.sin(2 * math.pi * k / 90)) for k in range(90)]
    else:
        for quarter in (True, False):
            n = len(v)
            ok = (all(p[0] >= -T for p in v) and all(p[1] >= -T for p in v)) if quarter \
                else all(p[1] >= -T for p in v)
            if not ok:
                continue
            j = 0 if quarter else 1
            mir = [list(p) for p in (v[n - 2::-1] if abs(v[n - 1][j]) < T else v[::-1])]
            if abs(v[n - 1][j]) >= T:
                mir[0][2] = 0.0
            for p in mir:
                p[j] = -p[j]
            src = [p[2] for p in v[n - 1:0:-1]]
            for i in range(len(src)):
                mir[len(mir) - len(src) + i][2] = src[i]
            v = v + mir
            if not quarter and abs(v[0][1]) < T:
                v[0] = v[-1]
                v = v[:-1]
    pts = []
    for i in range(len(v)):
        x1, y1 = v[i - 1][0], v[i - 1][1]
        x2, y2, R = v[i][0], v[i][1], v[i][2]
        pts.append((x1, y1))
        if abs(R) < 1e-12:
            continue
        xm, ym, dx, dy = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 2, (y2 - y1) / 2
        a2 = (R * R - dx * dx - dy * dy) / (dx * dx + dy * dy)
        if a2 < 0:
            continue
        a = math.sqrt(a2) * (-1 if xm * dy > ym * dx else 1) * (-1 if R < 0 else 1)
        cx, cy = xm + a * dy, ym - a * dx
        t1, t2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
        d = (t2 - t1 + math.pi) % (2 * math.pi) - math.pi
        m = max(int(abs(math.degrees(d)) / step_deg), 1)
        pts += [(cx + abs(R) * math.cos(t1 + d * k / m), cy + abs(R) * math.sin(t1 + d * k / m))
                for k in range(1, m)]
    return [(x + r0[0], y + r0[1]) for x, y in pts]


def _inside_poly(pts, x, y):
    c = False
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def check_lobe_containment(sections, library, tol=1e-5):
    """
    guard の外端で、コリメータのローブが周囲の標準チェンバに収まっているかを調べる。
    はみ出していると、テーパー区間で和集合が標準チェンバより太くなってしまう。
    """
    tips = {lab[:-4]: sid for _s, sid, lab in sections if lab.endswith("_tip")}
    warns, seen = [], set()
    for s, sid, lab in sections:
        for suf in ("_in", "_out"):
            if not lab.endswith(suf):
                continue
            name = lab[: -len(suf)]
            amb = library.get(sid) or _auto_shape(sid)
            if name not in tips or amb is None:
                continue
            amb_poly = _shape_polygon(amb)
            for oid in library.get(tips[name], {}).get("overlays") or []:
                key = (name, oid, sid)
                if key in seen or oid not in library:
                    continue
                seen.add(key)
                lobe = library[oid]
                c = lobe.get("r0", [0.0, 0.0])
                # ローブの外周を r0 側へわずかに縮めて、境界の共有を「内側」と判定させる
                out = [p for p in _shape_polygon(lobe)
                       if not _inside_poly(amb_poly, p[0] - tol * (p[0] - c[0]) / max(math.hypot(p[0] - c[0], p[1] - c[1]), 1e-12),
                                           p[1] - tol * (p[1] - c[1]) / max(math.hypot(p[0] - c[0], p[1] - c[1]), 1e-12))]
                if out:
                    worst = max(out, key=lambda q: math.hypot(*q))
                    warns.append(f"{name}: ローブ {oid} が {sid} からはみ出しています"
                                 f"（例 x = {worst[0] * 1000:.1f}, y = {worst[1] * 1000:.1f} mm）")
    return warns


def write_wall_file(dispog_path: str, ring: str, out_path: str,
                    allow_invalid: bool = False) -> dict:
    """Synrad3D wall file を書き出す。戻り値に断面数・未定義コード・検証結果を含む。"""
    sections, used, placeholders, insert_notes = build_sections(dispog_path, ring)
    library = _load_library()

    overlay_at, overlay_names, short_runs = _overlay_runs(sections, library)
    containment = check_lobe_containment(sections, library)
    for oid in overlay_names:
        if oid in library:
            used.setdefault(oid, library[oid])
        else:
            placeholders.add(oid)

    bad = {sid: errs for sid, sh in used.items() if (errs := validate_shape(sh))}

    lines = []
    lines.append(f"! Synrad3D wall file (auto-generated) ring={ring}")
    lines.append(f"! sections={len(sections)}  shapes={len(used)}")
    if placeholders:
        lines.append("! NOTE: 次の断面コードは仮形状です。wall_shapes.json で定義してください:")
        lines.append("!   " + ", ".join(sorted(placeholders)))
    if bad:
        lines.append("! WARNING: 頂点規則に違反している形状があります（Synrad3D が読めません）:")
        for sid in sorted(bad):
            lines.append(f"!   {sid}: {bad[sid][0]}")
    if insert_notes:
        lines.append("! 差し込み（config/<ring>_wall_inserts.json）:")
        for n in insert_notes:
            lines.append(f"!   {n}")
    if containment:
        lines.append("! WARNING: guard の外端でローブが標準チェンバからはみ出しています:")
        for w in containment:
            lines.append(f"!   {w}")
    if short_runs:
        lines.append("! WARNING: START と END が同じ s になったサブチェンバ区間:")
        lines.append("!   " + ", ".join(short_runs))
    lines.append("")

    def place(s, sid, label="", edge=""):
        sh = used.get(sid, {})
        sec_id = _section_id(sid, sh, edge)
        surf = sh.get("surface")
        if surf:
            lines.append(f'&place section = {s:.5f}, "{label}", "{sec_id}"')
            lines.append(f'       surface = "{surf}" /')
        else:
            lines.append(f'&place section = {s:.5f}, "{label}", "{sec_id}" /')

    # &place（縦位置に断面を配置）。同じ s では主チェンバ → サブチェンバの順。
    for s, sid, label in sections:
        place(s, sid, label)
        for oid, edge in overlay_at.get(s, []):
            place(s, oid, label, edge)
    lines.append("")

    # &shape_def（断面形状の定義）
    for sid, sh in sorted(used.items()):
        tag = ""
        if sh.get("_placeholder"):
            tag = "   ! ← PLACEHOLDER: 実形状に置き換えてください"
        elif sh.get("_review"):
            tag = "   ! ← 近似形状: 要確認"
        elif sh.get("_auto"):
            tag = f"   ! auto ({sh['_auto']})"
        lines.append(f"&shape_def{tag}")
        lines.append(f'  name = "{sid}"')
        r0 = sh.get("r0", [0.0, 0.0])
        if r0[0] or r0[1]:
            lines.append(f"  r0 = {r0[0]:.6g}, {r0[1]:.6g}")
        if sh.get("absolute_vertices"):
            lines.append("  absolute_vertices = T")
        for i, v in enumerate(sh["v"], start=1):
            lines.append(f"  v({i}) = {_fmt_vertex(v)}")
        lines.append("/")
        lines.append("")

    result = {"sections": len(sections), "shapes": len(used),
              "placeholders": sorted(placeholders), "invalid": bad,
              "inserts": insert_notes, "written": False, "containment": containment}
    # 頂点規則に違反した形状があると Synrad3D は読み込みで落ちるので、既定では書かない
    if bad and not allow_invalid:
        return result
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["written"] = True
    return result


def check_library() -> dict:
    """wall_shapes.json 全体を頂点規則で検証する（dispog なしで実行できる）。"""
    library = _load_library()
    return {name: validate_shape(sh) for name, sh in library.items()}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="dispog + Duct_Type から Synrad3D wall file を生成")
    p.add_argument("dispog", nargs="?", help="dispog ファイル")
    p.add_argument("--ring", choices=["HER", "LER"], default=None)
    p.add_argument("-o", "--out", default=None,
                   help="出力 wall ファイル（既定 <dispog名>.wall3d）")
    p.add_argument("--check-shapes", action="store_true",
                   help="wall_shapes.json を頂点規則で検証して終了")
    p.add_argument("--update-collimators", action="store_true",
                   help="dispog のコリメータマーカーから差し込み台帳の s_center を埋める")
    p.add_argument("--length", type=float, default=None,
                   help="--update-collimators: 先端部の長さ [m]（既定 0.010、既存行は保持）")
    p.add_argument("--guard", type=float, default=None,
                   help="--update-collimators: 補間を止める距離 [m]（既定 0.30、既存行は保持）")
    p.add_argument("--allow-invalid", action="store_true",
                   help="頂点規則に違反した形状があっても wall file を書き出す（確認用）")
    p.add_argument("--include-fake", action="store_true",
                   help="--update-collimators: FPM*（実機なしのマーカー）も取り込む")
    a = p.parse_args(argv)

    if a.check_shapes:
        res = check_library()
        ng = 0
        for name in sorted(res):
            if res[name]:
                ng += 1
                print(f"NG  {name}")
                for e in res[name]:
                    print(f"      {e}")
            else:
                print(f"OK  {name}")
        print(f"\n{len(res)} 形状中 {ng} 個が要修正")
        return 1 if ng else 0

    if not a.dispog:
        p.error("dispog ファイルを指定してください（または --check-shapes）。")
    ring = a.ring
    if ring is None:
        base = Path(a.dispog).name.lower()
        ring = "LER" if base.startswith("sler") else "HER" if base.startswith("sher") else None
        if ring is None:
            p.error("リングを判定できません。--ring HER/LER を指定してください。")

    if a.update_collimators:
        r = update_collimator_inserts(a.dispog, ring, a.length, a.guard,
                                      include_fake=a.include_fake)
        print(f"  実機 {len(r['real'])} 台 / Fake マーカー {len(r['fake'])} 個"
              + ("（Fake も取り込みました）" if a.include_fake else "（Fake は除外）"))
        if r["fake"]:    print("  Fake:", ", ".join(r["fake"]))
        if r["added"]:   print("  追加:", ", ".join(r["added"]))
        if r["updated"]: print("  更新:", ", ".join(r["updated"]))
        if r["missing"]:
            print("  形状未定義（wall_shapes.json に断面がないため書き込みませんでした）:")
            print("   ", ", ".join(r["missing"]))
        print(f"出力: {r['path']}")
        return 0

    out = a.out or (Path(a.dispog).stem + ".wall3d")
    info = write_wall_file(a.dispog, ring, out, a.allow_invalid)
    print(f"  断面配置 {info['sections']} 個 / 形状 {info['shapes']} 種")
    for n in info["inserts"]:
        print(f"  差し込み: {n}")
    if info["placeholders"]:
        print("  ※ 要定義（仮形状）:", ", ".join(info["placeholders"]))
    for w in info.get("containment", []):
        print(f"  ※ {w}")
    if info["invalid"]:
        print()
        print("  " + "!" * 60)
        print("  エラー: 頂点規則に違反した形状があります。Synrad3D は読み込みで停止します。")
        for sid in sorted(info["invalid"]):
            print(f"    {sid}: {info['invalid'][sid][0]}")
        if not info["written"]:
            print("  wall file は書き出していません（--allow-invalid で強制的に書き出せます）。")
        print("  " + "!" * 60)
        return 1
    print(f"出力: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
