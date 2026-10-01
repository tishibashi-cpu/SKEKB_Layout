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


def _flatten(loop, max_deg=0.25):
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

    対称性は元図形から判定し、**必要な範囲だけサンプリング**する。全周を刻んでから
    切り出すと、浮動小数の差で左右の点が一致せず対称と判定されなくなるため。
    戻り値 (頂点, 説明, 閉じているか)。
    """
    vfull = _vertices(loop, scale)
    sym_x, sym_y = _sym(vfull, "x"), _sym(vfull, "y")
    if sym_x and sym_y:
        a0, a1, closed = 0.0, math.pi / 2, False
        note = "上下・左右対称。第1象限のみを出力します"
    elif sym_x:
        a0, a1, closed = 0.0, math.pi, False
        note = "上下対称。上半分のみを出力します"
    else:
        a0, a1, closed = 0.0, 2 * math.pi, True
        note = "非対称。全周を出力します"

    pts = _flatten(loop)

    def r_at(a):
        r = _ray_min_radius(pts, a)
        if r is None:
            raise ValueError(f"θ = {math.degrees(a):.1f}° で壁が見つかりません（閉じていない？）")
        return r

    n = max(int(round(n_ang * (a1 - a0) / (2 * math.pi))), 4)
    step = (a1 - a0) / (n if closed else n - 1)
    samp = [(a0 + step * i, None) for i in range(n)]
    samp = [(a, r_at(a)) for a, _ in samp]

    # r が急変するところ（コリメータのブレード先端など）は二分して刻みを細かくする。
    # 不連続そのものは消せないが、影響する角度範囲を狭められる。
    for _ in range(8):
        new = []
        for i, (aa, r1) in enumerate(samp):
            new.append((aa, r1))
            if i == len(samp) - 1 and not closed:
                break
            ab, r2 = samp[(i + 1) % len(samp)]
            if i == len(samp) - 1:
                ab += 2 * math.pi
            if abs(r2 - r1) > 0.2 * max(r1, r2):
                am = (aa + ab) / 2
                new.append((am, r_at(am)))
        if len(new) == len(samp):
            break
        samp = sorted(new, key=lambda t: t[0])

    # 断面積（扇形の積分）。対称で切り出した場合は全周ぶんに換算する。
    area = 0.0
    for i in range(len(samp) - 1):
        (aa, r1), (ab, r2) = samp[i], samp[i + 1]
        area += 0.5 * r1 * r2 * math.sin(ab - aa)
    if closed:
        (aa, r1), (ab, r2) = samp[-1], samp[0]
        area += 0.5 * r1 * r2 * math.sin(2 * math.pi - aa + ab)
    else:
        area *= (2 * math.pi) / (a1 - a0)

    v = [[r * math.cos(a) * scale, r * math.sin(a) * scale, 0.0] for a, r in samp]
    return v, note, closed, area


# Synrad3D の shape_def namelist は v(100) 固定。終端検出に 1 枠使うので実質 99 個。
MAX_VERTEX = 99


def _collapse_arcs(v, tol):
    """原点から等距離の点が続く区間を、1 個の円弧頂点にまとめる。"""
    n = len(v)
    r = [math.hypot(p[0], p[1]) for p in v]
    out, i = [], 0
    while i < n:
        j = i
        lo = hi = r[i]
        # 区間の最大・最小の差で判定する。円弧を折れ線に割った影響で r が
        # 細かく上下するため、隣接差だけ見ると区間が切れてしまう。
        while j + 1 < n and max(hi, r[j + 1]) - min(lo, r[j + 1]) < tol:
            j += 1
            lo, hi = min(lo, r[j]), max(hi, r[j])
        if j - i >= 2:                      # 3 点以上が同一円周上 → 円弧にまとめる
            sweep = abs(math.atan2(v[j][1], v[j][0]) - math.atan2(v[i][1], v[i][0]))
            if sweep < math.pi - 1e-9:
                out.append(list(v[i]))
                out.append([v[j][0], v[j][1], (lo + hi) / 2])
                i = j + 1
                continue
        out.append(list(v[i]))
        i += 1
    return out


def _drop_collinear(v, tol, closed=True):
    """直線上に並ぶ中間点を落とす（円弧頂点と、開いた列の両端は残す）。"""
    out = []
    n = len(v)
    for i, p in enumerate(v):
        if not closed and (i == 0 or i == n - 1):
            out.append(p)
            continue
        a, b = v[i - 1], v[(i + 1) % n]
        if abs(p[2]) > 1e-12 or abs(b[2]) > 1e-12:
            out.append(p)                   # 円弧の端点は落とせない
            continue
        ex, ey = b[0] - a[0], b[1] - a[1]
        L = math.hypot(ex, ey)
        if L < 1e-15:
            continue
        d = abs(ex * (a[1] - p[1]) - ey * (a[0] - p[0])) / L   # a-b 線からの距離
        if d > tol:
            out.append(p)
    return out


def simplify(v, tol=1e-6, max_vertex=MAX_VERTEX, closed=True):
    """
    サンプリングで作った多角形を、意味を変えずに間引く。

    1. 原点から等距離の点が続くところは円弧 1 個にまとめる（φ27 の円弧など）
    2. 直線上に並ぶ中間点を落とす（ブレード面など）

    それでも max_vertex を超える場合は、許容差を広げながら繰り返す。
    """
    for _ in range(20):
        w = _drop_collinear(_collapse_arcs(v, tol), tol, closed)
        if len(w) <= max_vertex:
            return w, tol
        tol *= 2
    return w, tol


def _poly_area(pts):
    a = 0.0
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


# --------------------------------------------------------------------------
# ブレードで分断された断面を、中央ギャップ + 左右ローブに分ける
# --------------------------------------------------------------------------


def find_blade_x(loop, tol=1e-3):
    """ブレード側壁の |x| を推定する。左右に対で立つ縦線のうち最も内側のもの。"""
    xs = []
    for s in loop:
        if abs(s["start"][0] - s["end"][0]) < tol and abs(s["bulge"]) < 1e-12:
            xs.append(abs(s["start"][0]))
    cand = sorted({round(x, 6) for x in xs if x > tol})
    pair = [x for x in cand if sum(1 for y in xs if abs(y - x) < tol) >= 2]
    return pair[0] if pair else None


def _rot_pt(p, k):
    """点を 90° × k だけ反時計回りに回す（向きと bulge の符号は保たれる）。"""
    x, y = p
    for _ in range(k % 4):
        x, y = -y, x
    return (x, y)


def _rot_loop(loop, k):
    return [{"start": _rot_pt(s["start"], k), "end": _rot_pt(s["end"], k),
             "bulge": s["bulge"]} for s in loop]


def _lobe_path(loop, x_blade, side):
    """|x| >= x_blade の側を切り出して閉じた区間列にする。"""
    sgn = 1 if side == "R" else -1
    keep = [s for s in loop
            if min(sgn * s["start"][0], sgn * s["end"][0]) >= x_blade - 1e-6
            and max(sgn * s["start"][0], sgn * s["end"][0]) > x_blade + 1e-6]
    if not keep:
        raise ValueError(f"{side} 側のローブが取り出せませんでした")
    path = _chain(keep)[0]
    path.append({"start": path[-1]["end"], "end": path[0]["start"], "bulge": 0.0})
    return path


def _path_vertices(path, r0, scale):
    """区間列 → r0 基準の Bmad 頂点列（対称なら切り詰め）。規則違反なら None。"""
    v = [[(s["end"][0] - r0[0]) * scale, (s["end"][1] - r0[1]) * scale,
          _bulge_radius(s["start"], s["end"], s["bulge"])[0] * scale] for s in path]
    v = _rotate_to_start(v)
    if validate(v):
        return None
    vr, _note = reduce_symmetry(v)
    return None if validate(vr) else vr


def _find_r0(path, x_blade, side, scale, k_back):
    """ローブが星形になる r0 を作業座標の x 軸上で探す。元座標の r0 と頂点を返す。"""
    sgn = 1 if side == "R" else -1
    x_max = max(abs(s["end"][0]) for s in path)
    orig = _rot_loop(path, k_back)
    best = None
    for frac in [i / 40 for i in range(1, 40)]:
        r0 = _rot_pt((sgn * (x_blade + frac * (x_max - x_blade)), 0.0), k_back)
        v = _path_vertices(orig, r0, scale)
        if v is not None and (best is None or len(v) < len(best[1])):
            best = (r0, v)
    if best is None:
        raise ValueError(f"{side} 側のローブが星形になる r0 を見つけられませんでした")
    return best


def _split_on_axis(loop, name, scale, axis, x_blade, overlap_mm):
    """
    axis = "x": ブレード側壁が縦線（垂直コリメータ）。|x| >= 側壁で左右に切る。
    axis = "y": ブレード側壁が横線（水平コリメータ）。|y| >= 側壁で上下に切る。
    y のときは断面を 90° 回して x の場合に帰着させ、結果を回し戻す。
    """
    k = 1 if axis == "y" else 0
    k_back = (4 - k) % 4
    work = _rot_loop(loop, k)

    xb = x_blade if x_blade is not None else find_blade_x(work)
    if xb is None:
        raise ValueError("ブレード側壁が見つかりません")

    pts = _flatten(work)                      # 作業座標で x = 0 と輪郭の交点
    cross = []
    for i, (x1, y1) in enumerate(pts):
        x2, y2 = pts[(i + 1) % len(pts)]
        if ((x1 <= 0 <= x2) or (x2 <= 0 <= x1)) and abs(x2 - x1) > 1e-12:
            cross.append(y1 + (y2 - y1) * (0 - x1) / (x2 - x1))
    up = [y for y in cross if y > 1e-9]
    dn = [y for y in cross if y < -1e-9]
    if not up or not dn:
        raise ValueError("中央ギャップの上下面が見つかりません")
    g_hi, g_lo = min(up), max(dn)

    w = xb + overlap_mm
    corners = [_rot_pt(c, k_back) for c in ((w, g_lo), (w, g_hi), (-w, g_hi), (-w, g_lo))]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    hx, hy = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2

    label = {"x": {"R": "R", "L": "L"}, "y": {"R": "B", "L": "T"}}[axis]
    entries, subs = {}, []
    for side in ("R", "L"):
        path = _lobe_path(work, xb, side)
        r0, v = _find_r0(path, xb, side, scale, k_back)
        tag = label[side]
        sid = f"{name}_lobe{tag}"
        subs.append(sid)
        entries[sid] = {
            "subchamber": f"{name}_col{tag}",
            "r0": [round(r0[0] * scale, 9), round(r0[1] * scale, 9)],
            "v": [[round(x, 9) for x in (p[:2] + ([p[2]] if abs(p[2]) > 1e-12 else []))]
                  for p in v],
        }
    entries[name] = {
        "r0": [round(cx * scale, 9), round(cy * scale, 9)],
        "v": [[round(hx * scale, 9), round(hy * scale, 9)]],
        "overlays": subs,
    }
    return entries, xb, (min(xs), max(xs), min(ys), max(ys))


def _entry_polygon(entry, scale, step_deg=1.0):
    """ライブラリのエントリ 1 個を Bmad と同じ規則で展開した外周多角形（入力単位）。"""
    r0 = entry.get("r0", [0.0, 0.0])
    v = [list(p) + [0.0] * (3 - len(p)) for p in entry["v"]]
    T = 1e-12
    if len(v) == 1 and v[0][2] == 0:                 # 1 頂点 = 長方形
        a, b = v[0][0], v[0][1]
        v = [[a, b, 0.0], [-a, b, 0.0], [-a, -b, 0.0], [a, -b, 0.0]]
    else:
        for quarter in (True, False):
            n = len(v)
            ok = (all(p[0] >= -T for p in v) and all(p[1] >= -T for p in v)) if quarter \
                else all(p[1] >= -T for p in v)
            if not ok:
                continue
            j = 0 if quarter else 1
            if abs(v[n - 1][j]) < T:
                mir = [list(p) for p in v[n - 2::-1]]
            else:
                mir = [list(p) for p in v[::-1]]
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
    pts, n = [], len(v)
    for i in range(n):
        x1, y1, _ = v[i - 1]
        x2, y2, R = v[i]
        pts.append((x1, y1))
        if abs(R) < 1e-12:
            continue
        xm, ym, dx, dy = (x1 + x2) / 2, (y1 + y2) / 2, (x2 - x1) / 2, (y2 - y1) / 2
        a2 = (R * R - dx * dx - dy * dy) / (dx * dx + dy * dy)
        if a2 < 0:
            continue
        a = math.sqrt(a2)
        if xm * dy > ym * dx:
            a = -a
        if R < 0:
            a = -a
        cx, cy = xm + a * dy, ym - a * dx
        t1, t2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
        d = (t2 - t1 + math.pi) % (2 * math.pi) - math.pi
        m = max(int(abs(math.degrees(d)) / step_deg), 1)
        for j in range(1, m):
            pts.append((cx + abs(R) * math.cos(t1 + d * j / m),
                        cy + abs(R) * math.sin(t1 + d * j / m)))
    return [((x + r0[0]) / scale, (y + r0[1]) / scale) for x, y in pts]


def _inside(pts, x, y):
    c = False
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def union_agreement(loop, entries, main, scale, step=0.25):
    """サブチェンバの和集合が元の輪郭とどれだけ一致するか（0〜1、格子で判定）。"""
    real = _flatten(loop)
    polys = [_entry_polygon(entries[main], scale)] + \
            [_entry_polygon(entries[s], scale) for s in entries[main].get("overlays", [])]
    xs = [p[0] for p in real]
    ys = [p[1] for p in real]
    # 格子点が丸い座標の境界線（y = 7 の段など）にちょうど乗ると内外判定が割れるので、
    # 半端な量だけずらして境界上に乗らないようにする
    off = step * 0.5 + 1.234567e-3
    both = only = 0
    x = min(xs) - 1 + off
    while x < max(xs) + 1:
        y = min(ys) - 1 + off
        while y < max(ys) + 1:
            a = _inside(real, x, y)
            b = any(_inside(p, x, y) for p in polys)
            if a and b:
                both += 1
            elif a or b:
                only += 1
            y += step
        x += step
    return both / (both + only) if both + only else 0.0


def split_blade(path, name, scale=None, x_blade=None, overlap_mm=0.2, axis="auto"):
    """
    ブレードで開口が絞られた断面を、中央ギャップ ＋ 2 つのローブの 3 サブチェンバに分ける。

    axis = "x"   : 垂直コリメータ（ブレード側壁が縦線）。左右のローブ（_lobeR / _lobeL）
    axis = "y"   : 水平コリメータ（ブレード側壁が横線）。上下のローブ（_lobeT / _lobeB）
    axis = "auto": 両方試し、和集合が元の輪郭と一致するほうを選ぶ（既定）

    どちらを選んでも、和集合が元の輪郭と 99.5% 以上一致しなければエラーにする。
    戻り値: (エントリ辞書, 側壁位置, 中央ギャップの範囲, 単位の説明, 軸, 一致率)
    """
    doc = ezdxf.readfile(path)
    scale, unit_note = _resolve_scale(doc, scale)
    segs, _circ = _explode(doc.modelspace())
    loop = _to_ccw(_chain(segs)[0])

    tried = []
    for ax in (("x", "y") if axis == "auto" else (axis,)):
        try:
            entries, xb, gap = _split_on_axis(loop, name, scale, ax, x_blade, overlap_mm)
        except ValueError as e:
            tried.append((ax, None, str(e)))
            continue
        tried.append((ax, union_agreement(loop, entries, name, scale), (entries, xb, gap)))

    ok = [t for t in tried if t[1] is not None]
    if not ok:
        raise ValueError("; ".join(f"{ax} 軸: {msg}" for ax, _a, msg in tried))
    ax, agree, (entries, xb, gap) = max(ok, key=lambda t: t[1])
    if agree < 0.995:
        detail = ", ".join(f"{a} 軸 {g * 100:.1f}%" for a, g, _ in ok)
        raise ValueError(f"どちらの軸で分けても元の輪郭と一致しません（{detail}）")

    src = Path(path).name
    for k, e in entries.items():
        if k == name:
            e["_source"] = (f"{src} の中央ギャップ（{ax} 軸で分割、x = {gap[0]:g} .. {gap[1]:g}, "
                            f"y = {gap[2]:g} .. {gap[3]:g} mm、重なり {overlap_mm:g} mm）")
        else:
            e["_source"] = f"{src} のローブ（|{ax}| >= {xb:g} mm を切り出し）"
    return entries, xb, gap, unit_note, ax, agree


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
        v, note, closed, a_env = star_envelope(loop, scale, n_ang)
        v, used_tol = simplify(v, closed=closed)
        warn.append(f"星形でないため、r(θ) の最小値による近似に置き換えました"
                    f"（間引き後 {len(v)} 頂点、許容差 {used_tol * 1e6:.2g} µm）。"
                    f"ビーム軸から見えないポケットは埋まります。")
        a_true = _poly_area(_flatten(loop))
        if find_blade_x(loop) is not None and a_env < 0.95 * a_true:
            warn.append(f"近似で断面積が {a_env / a_true * 100:.0f}% に減っています。"
                        "ブレードの影で外側の真空領域が失われています。"
                        "--split-blade でサブチェンバに分けてください（分割方向は自動判定）")
        errs = validate(v)
        if errs:
            warn.append("出力した頂点が規則に違反しています: " + "; ".join(errs[:3]))
        if len(v) > MAX_VERTEX:
            warn.append(f"頂点が {len(v)} 個あります。Synrad3D の shape_def は v(100) 固定なので、"
                        f"{MAX_VERTEX} 個以下にしないと読み込みでエラーになります")
        return v, note, warn
    if errs:
        warn.append("断面が r0 から見て星形ではありません: " + "; ".join(errs[:3])
                    + " … --envelope で近似できます")
    v, note = reduce_symmetry(v)
    errs = validate(v)
    if errs:
        warn.append("出力した頂点が規則に違反しています: " + "; ".join(errs[:3]))
    if len(v) > MAX_VERTEX:
        warn.append(f"頂点が {len(v)} 個あります。Synrad3D の shape_def は v(100) 固定なので、"
                    f"{MAX_VERTEX} 個以下にしないと読み込みでエラーになります")
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


def _merge(lib_path, entries, force=False):
    """
    形状ライブラリに書き込む。DXF 由来のエントリは更新し、**手で書いたエントリや
    このツール以外が作ったエントリは上書きしない**（--force で強制）。
    --dir と単一ファイルで挙動を揃えてある。
    """
    lib = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else {}
    added, updated, skipped, removed = [], [], [], []
    for k, e in entries.items():
        old = lib.get(k)
        if old is not None and not force and not str(old.get("_source", "")).endswith(SOURCE_TAG):
            skipped.append(k)
            continue
        # 以前このエントリが参照していたサブチェンバのうち、今回参照しなくなったもの
        # （例: 左右ローブ → 上下ローブに切り直したときの旧 _lobeR/_lobeL）を消す
        stale = set((old or {}).get("overlays", [])) - set(e.get("overlays", []))
        lib[k] = e
        (updated if old is not None else added).append(k)
        for s in sorted(stale):
            still_used = any(s in (v.get("overlays") or []) for kk, v in lib.items()
                             if isinstance(v, dict) and kk != s)
            if s in lib and not still_used and s.startswith(f"{k}_lobe"):
                del lib[s]
                removed.append(s)
    lib_path.parent.mkdir(parents=True, exist_ok=True)
    lib_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
    if added:   print(f"{lib_path}: 追加 " + ", ".join(added))
    if updated: print(f"{lib_path}: 更新 " + ", ".join(updated))
    if removed:
        print(f"{lib_path}: 削除 " + ", ".join(removed) + "（参照されなくなった旧サブチェンバ）")
    if skipped:
        print(f"{lib_path}: 据え置き " + ", ".join(skipped)
              + "（手書き／別経路のエントリ。上書きするには --force）")
    return 0


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
    ap.add_argument("--split-blade", nargs="?", const="auto", default=None,
                    metavar="MM",
                    help="ブレードで絞られた断面を中央ギャップ＋2 ローブの 3 サブチェンバに"
                         "分ける。値を省くとブレード側壁の位置を自動判定")
    ap.add_argument("--axis", choices=["auto", "x", "y"], default="auto",
                    help="--split-blade の分割方向。x = 垂直コリメータ（左右ローブ）、"
                         "y = 水平コリメータ（上下ローブ）、auto = 和集合が元の輪郭と"
                         "一致するほうを自動選択（既定）")
    ap.add_argument("--overlap", type=float, default=0.2, metavar="MM",
                    help="--split-blade: サブチェンバ同士の重なり量 [mm]（既定 0.2）")
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

    # --- ブレード分割 ---
    if a.split_blade is not None:
        xb = None if a.split_blade == "auto" else float(a.split_blade)
        try:
            entries, xb, gap, unit_note, ax, agree = split_blade(
                a.dxf, name, a.scale, xb, a.overlap, a.axis)
        except ValueError as e:
            print(f"エラー: {e}")
            return 1
        kind = {"x": "垂直コリメータ型（左右ローブ）", "y": "水平コリメータ型（上下ローブ）"}[ax]
        print(f"※ 単位: {unit_note}")
        print(f"※ 分割方向: {ax} 軸 = {kind}")
        print(f"※ ブレード側壁 |{ax}| = {xb:g} mm、中央ギャップ x = {gap[0]:g} .. {gap[1]:g}, "
              f"y = {gap[2]:g} .. {gap[3]:g} mm")
        print(f"※ 和集合と元の輪郭の一致: {agree * 100:.2f}%")
        for k, e in entries.items():
            print(f"※ {k}: 頂点 {len(e['v'])} 個"
                  + (f"（サブチェンバ {e['subchamber']}）" if "subchamber" in e else "（主チェンバ）"))
        print()
        if a.merge:
            return _merge(Path(a.merge), entries, a.force)
        if a.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            for k, e in entries.items():
                print(as_namelist(k, e["v"]) if not e.get("r0") or e["r0"] == [0.0, 0.0]
                      else as_namelist(k, e["v"]).replace(
                          f'name = "{k}"',
                          f'name = "{k}"\n  r0 = {e["r0"][0]:.6g}, {e["r0"][1]:.6g}'))
                print()
        return 0

    v, note, warn = convert(a.dxf, a.scale, a.envelope, a.n_ang)
    for w in warn:
        print("※ " + w)
    print(f"※ {note}（頂点 {len(v)} 個）\n")

    if a.merge:
        bad = validate(v)
        if bad or len(v) > MAX_VERTEX:
            print(f"エラー: {name} は頂点規則に違反しているため、ライブラリに書き込みません"
                  + (f"（{bad[0]}）" if bad else f"（頂点 {len(v)} 個）"))
            print("  星形でない断面は --envelope または --split-blade を使ってください")
            return 1
        return _merge(Path(a.merge), {name: _entry(v, a.dxf)}, a.force)
    if a.json:
        print(json.dumps({name: as_json_entry(v)}, ensure_ascii=False, indent=2))
    else:
        print(as_namelist(name, v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
