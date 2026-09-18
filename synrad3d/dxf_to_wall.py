"""
dxf_to_wall.py — Inventor 等が出力した断面 DXF を Synrad3D の断面定義に変換する
============================================================================

3D CAD で断面を切って DXF に出し、それを `&shape_def`（namelist）または
`config/wall_shapes.json` のエントリに変換します。後者で出せば、そのまま
synrad3d_wall.py の形状ライブラリに取り込めます。

```bash
python dxf_to_wall.py f90x220A.dxf --name f90x220_Ar            # namelist を表示
python dxf_to_wall.py f90x220A.dxf --name f90x220_Ar --json     # JSON 断片を表示
python dxf_to_wall.py f90x220A.dxf --name f90x220_Ar \\
       --merge config/wall_shapes.json                          # ライブラリへ追記

# dxf/ フォルダを一括取り込み（ファイル名がそのまま断面コードになる）
#   dxf/f10_04.dxf → wall_shapes.json の "f10_04"
python dxf_to_wall.py --dir dxf --merge config/wall_shapes.json
```

対応エンティティ: LINE / ARC / CIRCLE / LWPOLYLINE / POLYLINE（bulge 付き）。
INSERT の中身も展開します。

Bmad の頂点規則に関する要点:

* `radius_x` は「**1 つ前の頂点からその頂点まで**」の区間に付く円弧の指定です
  （頂点そのものの丸めではありません）。本スクリプトは区間の終点側の頂点に
  半径を書きます。
* 符号は、反時計回り（θ 増加）に辿ったときに**外側へ膨らむ弧が正、内側へ
  えぐれる弧が負**です。DXF の bulge（= tan(θ/4)、正で左回り）と一致するので、
  CCW に揃えたあとは bulge の符号をそのまま使えます。
  半径は |R| = (弦長 / 2) / sin(2·atan(|bulge|))。
* 断面は r0 から見て星形（θ = atan2(y, x) が単調増加）でなければなりません。
  変換後に検査し、違反があれば警告します。
* 全頂点が x ≥ 0 かつ y ≥ 0 なら両軸対称として 1/4 のみ、y ≥ 0 のみなら
  x 軸対称として上半分のみを出力します（残りは Bmad が展開します）。
  このとき、先頭頂点（+x 軸上）に書いた半径は Bmad 側の対称展開で上書きされて
  消えるため、本スクリプトは出力しません。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import ezdxf

# $INSUNITS（DXF ヘッダの単位コード）→ m への換算係数
INSUNITS = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0, 8: 1e-6, 13: 1e-9}
INSUNITS_NAME = {1: "inch", 2: "ft", 4: "mm", 5: "cm", 6: "m", 8: "µm", 13: "nm"}

TOL = 1e-6          # 頂点一致の許容差（入力単位、既定 mm）
MIN_SEG = 1e-4      # これより短い区間は縮退とみなして捨てる（同上）


# --------------------------------------------------------------------------
# DXF → 区間リスト
# --------------------------------------------------------------------------


def _bulge_radius(p1, p2, bulge):
    """DXF の bulge → (符号付き半径, 中心角)。bulge 正で左回り（CCW）。"""
    if abs(bulge) < 1e-12:
        return 0.0, 0.0
    chord = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    theta = 4.0 * math.atan(abs(bulge))
    r = (chord / 2.0) / math.sin(theta / 2.0)
    return math.copysign(r, bulge), math.copysign(theta, bulge)


def _explode(msp):
    """LINE / ARC / CIRCLE / (LW)POLYLINE を {'start','end','bulge'} の列にする。"""
    segs, circles = [], []

    def add_poly(points, closed):
        n = len(points)
        last = n if closed else n - 1
        for i in range(last):
            x1, y1, b = points[i]
            x2, y2, _ = points[(i + 1) % n]
            segs.append({"start": (x1, y1), "end": (x2, y2), "bulge": b})

    def handle(e):
        k = e.dxftype()
        if k == "LINE":
            segs.append({"start": (e.dxf.start.x, e.dxf.start.y),
                         "end": (e.dxf.end.x, e.dxf.end.y), "bulge": 0.0})
        elif k == "ARC":
            p1 = (e.start_point.x, e.start_point.y)
            p2 = (e.end_point.x, e.end_point.y)
            sweep = math.radians((e.dxf.end_angle - e.dxf.start_angle) % 360.0)
            segs.append({"start": p1, "end": p2, "bulge": math.tan(sweep / 4.0)})
        elif k == "CIRCLE":
            circles.append((e.dxf.center.x, e.dxf.center.y, e.dxf.radius))
        elif k == "LWPOLYLINE":
            add_poly([(x, y, b) for x, y, _sw, _ew, b in e.get_points()], e.closed)
        elif k == "POLYLINE":
            pts = [(v.dxf.location.x, v.dxf.location.y,
                    v.dxf.bulge if v.dxf.hasattr("bulge") else 0.0)
                   for v in e.vertices]
            add_poly(pts, e.is_closed)

    for e in msp:
        if e.dxftype() == "INSERT":
            for v in e.virtual_entities():
                handle(v)
        else:
            handle(e)

    # 長さ 0 の区間を落とす。CST の DXF は面ごとに輪郭を書き出すため、
    # 面の継ぎ目で同じ点が 2 回出てくる（頂点が重複する）。
    n0 = len(segs)
    segs = [s for s in segs if math.dist(s["start"], s["end"]) >= MIN_SEG]
    if len(segs) < n0:
        print(f"※ 重複頂点による長さ 0 の区間を {n0 - len(segs)} 個除去しました")
    return segs, circles


def _chain(segs):
    """区間列を一筆書きの閉ループに束ねる。複数ループなら複数返す。"""
    loops = []
    pool = list(segs)
    while pool:
        loop = [pool.pop(0)]
        while True:
            last = loop[-1]["end"]
            if math.dist(last, loop[0]["start"]) < TOL and len(loop) > 1:
                break
            for i, s in enumerate(pool):
                if math.dist(last, s["start"]) < TOL:
                    loop.append(pool.pop(i)); break
                if math.dist(last, s["end"]) < TOL:
                    s["start"], s["end"] = s["end"], s["start"]
                    s["bulge"] = -s["bulge"]
                    loop.append(pool.pop(i)); break
            else:
                break
        loops.append(loop)
    return loops


def _signed_area(loop):
    a = 0.0
    for s in loop:
        a += s["start"][0] * s["end"][1] - s["end"][0] * s["start"][1]
    return a / 2.0


def _to_ccw(loop):
    if _signed_area(loop) >= 0:
        return loop
    loop = list(reversed(loop))
    for s in loop:
        s["start"], s["end"] = s["end"], s["start"]
        s["bulge"] = -s["bulge"]
    return loop


# --------------------------------------------------------------------------
# 区間リスト → Bmad の頂点
# --------------------------------------------------------------------------


def _vertices(loop, scale):
    """CCW の閉ループ → [(x, y, radius), ...]。半径は区間の終点側に付ける。"""
    out = []
    for s in loop:
        r, _ = _bulge_radius(s["start"], s["end"], s["bulge"])
        out.append([s["end"][0] * scale, s["end"][1] * scale, r * scale])
    return out


def _rotate_to_start(v):
    """θ = atan2(y, x) が最小の頂点が先頭に来るよう回す。"""
    ang = [math.atan2(p[1], p[0]) % (2 * math.pi) for p in v]
    k = min(range(len(v)), key=lambda i: ang[i])
    return v[k:] + v[:k]


def validate(v):
    """Bmad の頂点規則に照らして問題点を列挙する（synrad3d_wall.py と同じ検査）。"""
    errs, prev = [], None
    for i, p in enumerate(v, 1):
        a = math.atan2(p[1], p[0])
        if prev is not None:
            if a <= prev:                   # Bmad と同じく 2π を 1 回だけ足して巻き戻す
                a += 2 * math.pi
            if a <= prev:
                errs.append(f"v({i}) = ({p[0]:.5g}, {p[1]:.5g}): θ が増加していない "
                            f"({math.degrees(prev):.2f}° → {math.degrees(a):.2f}°)")
            elif a >= prev + math.pi:
                errs.append(f"v({i}): 直前の頂点との θ 差が 180° 以上")
        prev = a
    return errs


def _sym(v, axis):
    """axis='x'（上下対称）/ 'y'（左右対称）か判定する。"""
    def key(p, flip):
        x, y = (p[0], -p[1]) if flip == "x" else (-p[0], p[1])
        return (round(x / TOL), round(y / TOL))
    have = {(round(p[0] / TOL), round(p[1] / TOL)) for p in v}
    return all(key(p, axis) in have for p in v)


def reduce_symmetry(v):
    """対称なら 1/4 または上半分に切り詰める。戻り値 (頂点, 説明)。"""
    if all(p[0] >= -TOL for p in v) and all(p[1] >= -TOL for p in v):
        return v, "入力が既に第1象限のみ（両軸対称として展開されます）"
    if not _sym(v, "x"):
        return v, "非対称。全周を出力します"
    half = [p for p in v if p[1] >= -TOL]
    half = _rotate_to_start(half)
    if abs(half[0][1]) < TOL:
        # 先頭頂点が +x 軸上にある場合、その半径は Bmad の対称展開で上書きされる
        half[0] = [half[0][0], half[0][1], 0.0]
    if _sym(half, "y"):
        quarter = [p for p in half if p[0] >= -TOL]
        if quarter and abs(quarter[-1][0]) < TOL:
            return quarter, "上下・左右対称。第1象限のみを出力します"
    return half, "上下対称。上半分のみを出力します"


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------


def _flatten(loop, max_deg=2.0):
    """円弧を短い直線に割って、単純な多角形の点列にする。"""
    pts = []
    for s in loop:
        r, theta = _bulge_radius(s["start"], s["end"], s["bulge"])
        pts.append(s["start"])
        if abs(theta) < 1e-12:
            continue
        # 弧の中心
        x1, y1 = s["start"]; x2, y2 = s["end"]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = x2 - x1, y2 - y1
        chord = math.hypot(dx, dy)
        h = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
        sgn = 1.0 if theta > 0 else -1.0
        cx, cy = mx - sgn * h * dy / chord, my + sgn * h * dx / chord
        a1 = math.atan2(y1 - cy, x1 - cx)
        n = max(int(abs(math.degrees(theta)) / max_deg), 1)
        for i in range(1, n):
            a = a1 + theta * i / n
            pts.append((cx + abs(r) * math.cos(a), cy + abs(r) * math.sin(a)))
    return pts


def _ray_min_radius(pts, ang):
    """原点から角度 ang に出した半直線が、多角形の境界と最初に交わる距離。"""
    dx, dy = math.cos(ang), math.sin(ang)
    best = None
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]; x2, y2 = pts[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        den = dx * ey - dy * ex
        if abs(den) < 1e-15:
            continue
        t = (x1 * ey - y1 * ex) / den            # 半直線側の距離
        u = (x1 * dy - y1 * dx) / den            # 辺上の位置 0..1
        if t > 1e-12 and -1e-9 <= u <= 1 + 1e-9:
            best = t if best is None else min(best, t)
    return best


def star_envelope(loop, scale, n_ang=360):
    """
    星形でない断面を、原点から見て最初に当たる壁（r(θ) の最小値）で近似する。
    ブレードの裏側のような、ビーム軸から見えないポケットは埋められる。
    """
    pts = _flatten(loop)

    def r_at(a):
        r = _ray_min_radius(pts, a)
        if r is None:
            raise ValueError(f"θ = {math.degrees(a):.1f}° で壁が見つかりません（閉じていない？）")
        return r

    samp = [(2 * math.pi * i / n_ang, None) for i in range(n_ang)]
    samp = [(a, r_at(a)) for a, _ in samp]

    # r が急変するところ（コリメータのブレード先端など）は二分して刻みを細かくする。
    # 不連続そのものは消せないが、影響する角度範囲を狭められる。
    for _ in range(8):
        new = []
        for i, (a1, r1) in enumerate(samp):
            new.append((a1, r1))
            a2, r2 = samp[(i + 1) % len(samp)]
            if i == len(samp) - 1:
                a2 += 2 * math.pi
            if abs(r2 - r1) > 0.2 * max(r1, r2):
                am = (a1 + a2) / 2
                new.append((am % (2 * math.pi), r_at(am)))
        if len(new) == len(samp):
            break
        samp = sorted(new, key=lambda t: t[0])

    return [[r * math.cos(a) * scale, r * math.sin(a) * scale, 0.0] for a, r in samp]


def _fmt(p):
    vals = [p[0], p[1]] + ([p[2]] if abs(p[2]) > 1e-12 else [])
    return ", ".join(f"{x:.6g}" for x in vals)


def as_namelist(name, v):
    out = ["&shape_def", f'  name = "{name}"']
    out += [f"  v({i}) = {_fmt(p)}" for i, p in enumerate(v, 1)]
    out.append("/")
    return "\n".join(out)


def as_json_entry(v):
    return {"r0": [0.0, 0.0],
            "v": [[round(p[0], 6), round(p[1], 6)] +
                  ([round(p[2], 6)] if abs(p[2]) > 1e-12 else []) for p in v]}


# --------------------------------------------------------------------------


def _resolve_scale(doc, scale):
    """--scale が指定されていなければ $INSUNITS から決める。戻り値 (係数, 説明)。"""
    if scale is not None:
        return scale, f"--scale {scale} を使用"
    code = doc.header.get("$INSUNITS")
    if code in INSUNITS:
        return INSUNITS[code], f"$INSUNITS = {code}（{INSUNITS_NAME[code]}）から判定"
    return 0.001, ("$INSUNITS がヘッダに無いため mm と仮定します"
                   "（違う場合は --scale で指定してください）")


def convert(path, scale=None, envelope=False, n_ang=360):
    """DXF → (頂点, 説明, 警告)。scale は入力単位 → m。None なら $INSUNITS から判定。"""
    doc = ezdxf.readfile(path)
    scale, unit_note = _resolve_scale(doc, scale)
    segs, circles = _explode(doc.modelspace())
    warn = [f"単位: {unit_note}"]

    loops = _chain(segs) if segs else []
    for cx, cy, r in circles:
        pts = [(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)]
        q = math.tan(math.pi / 8)          # 1/4 円の bulge = tan(90°/4)
        loops.append([{"start": pts[i], "end": pts[(i + 1) % 4], "bulge": q}
                      for i in range(4)])
    if not loops:
        raise ValueError("断面の輪郭線が見つかりませんでした")

    if len(loops) > 1:
        areas = [abs(_signed_area(l)) for l in loops]
        warn.append(f"閉ループが {len(loops)} 本あります（面積 "
                    + ", ".join(f"{a:.1f}" for a in areas)
                    + "）。最大のものだけを使います。"
                    "残りが別チェンバなら、サブチェンバとして別エントリにしてください。")
        loops = [max(loops, key=lambda l: abs(_signed_area(l)))]

    loop = _to_ccw(loops[0])
    v = _rotate_to_start(_vertices(loop, scale))
    errs = validate(v)
    if errs and envelope:
        v = star_envelope(loop, scale, n_ang)
        warn.append(f"星形でないため、r(θ) の最小値による近似に置き換えました"
                    f"（{n_ang} 分割）。ビーム軸から見えないポケットは埋まります。")
        errs = []
    elif errs:
        warn.append("断面が r0 から見て星形ではありません: " + "; ".join(errs[:3])
                    + " … --envelope で近似できます")
    v, note = reduce_symmetry(v)
    errs = validate(v)
    if errs:
        warn.append("出力した頂点が規則に違反しています: " + "; ".join(errs[:3]))
    return v, note, warn


SOURCE_TAG = " から変換"


def _entry(v, dxf_path):
    e = as_json_entry(v)
    e["_source"] = Path(dxf_path).name + SOURCE_TAG
    return e


def import_dir(folder, lib_path, scale=None, force=False, envelope=False, n_ang=360):
    """
    フォルダ内の *.dxf をまとめて形状ライブラリに取り込む。
    ファイル名（拡張子を除いた部分）がそのまま断面コードになる。

    既存エントリの扱い:
      * DXF 由来（_source が "....dxf から変換"）なら更新する
      * 手で書いたエントリは上書きしない（--force で強制）
      * 頂点規則に違反した断面は書き込まない
    """
    folder, lib_path = Path(folder), Path(lib_path)
    files = sorted(folder.glob("*.dxf"))
    if not files:
        print(f"{folder} に *.dxf がありません")
        return 1

    lib = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else {}
    added, updated, skipped, failed = [], [], [], []

    for f in files:
        name = f.stem
        try:
            v, note, warn = convert(f, scale, envelope, n_ang)
        except Exception as e:                       # noqa: BLE001
            failed.append((name, str(e)))
            continue
        warn = [w for w in warn if not w.startswith("単位: $INSUNITS = ")]
        bad = [w for w in warn if "規則に違反" in w or "星形ではありません" in w]
        if bad:
            failed.append((name, bad[0]))
            continue
        old = lib.get(name)
        if old is not None and not force and not str(old.get("_source", "")).endswith(SOURCE_TAG):
            skipped.append(name)
            continue
        lib[name] = _entry(v, f)
        (updated if old is not None else added).append(name)
        for w in warn:
            print(f"  ※ {name}: {w}")

    lib_path.parent.mkdir(parents=True, exist_ok=True)
    lib_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{lib_path}: 追加 {len(added)} / 更新 {len(updated)} / "
          f"据え置き {len(skipped)} / 失敗 {len(failed)}")
    if added:   print("  追加  :", ", ".join(added))
    if updated: print("  更新  :", ", ".join(updated))
    if skipped: print("  据え置き（手書きエントリ。--force で上書き）:", ", ".join(skipped))
    for n, why in failed:
        print(f"  失敗  : {n}: {why}")
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="断面 DXF → Synrad3D の断面定義")
    ap.add_argument("dxf", nargs="?", help="DXF ファイル（--dir と排他）")
    ap.add_argument("--dir", metavar="FOLDER",
                    help="フォルダ内の *.dxf を一括取り込み（ファイル名 = 断面コード）")
    ap.add_argument("--name", help="断面コード（形状名）。単一ファイルのとき必須")
    ap.add_argument("--scale", type=float, default=None,
                    help="入力単位 → m。省略時は DXF の $INSUNITS から判定し、"
                         "無ければ mm と仮定")
    ap.add_argument("--json", action="store_true", help="JSON 断片で出力")
    ap.add_argument("--merge", metavar="wall_shapes.json",
                    help="既存の形状ライブラリに追記／更新する")
    ap.add_argument("--force", action="store_true",
                    help="手書きエントリも上書きする（--dir のとき）")
    ap.add_argument("--envelope", action="store_true",
                    help="星形でない断面を r(θ) の最小値で近似する（コリメータ等）")
    ap.add_argument("--n-ang", type=int, default=360,
                    help="--envelope の角度分割数（既定 360）")
    a = ap.parse_args(argv)

    if a.dir:
        if not a.merge:
            ap.error("--dir には --merge <wall_shapes.json> が必要です")
        return import_dir(a.dir, a.merge, a.scale, a.force, a.envelope, a.n_ang)

    if not a.dxf:
        ap.error("DXF ファイルか --dir を指定してください")
    name = a.name or Path(a.dxf).stem

    v, note, warn = convert(a.dxf, a.scale, a.envelope, a.n_ang)
    for w in warn:
        print("※ " + w)
    print(f"※ {note}（頂点 {len(v)} 個）\n")

    if a.merge:
        p = Path(a.merge)
        lib = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        exists = name in lib
        lib[name] = _entry(v, a.dxf)
        p.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{p} を{'更新' if exists else '追加'}しました: {name}")
    elif a.json:
        print(json.dumps({name: as_json_entry(v)}, ensure_ascii=False, indent=2))
    else:
        print(as_namelist(name, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
