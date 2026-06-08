"""
synrad3d_wall.py — dispog + Duct_Type の断面情報から Synrad3D の wall file を生成（試作）
=========================================================================================

Bmad/Synrad3D の wall file は Fortran namelist 形式で、縦位置 s ごとに断面を置く
`&place` と、断面の頂点形状を定義する `&shape_def` から成る:

    &place section = <s>, "<name>", "<shape_id>" /
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
  * 矩形 "WxH"   … 自動（半幅 W/2, 半高 H/2［m］、頂点1個）
  * 円   "fNNN"  … 自動（半径 NNN/2［m］、第1象限の円弧。両軸対称で 1/4 のみ記述）
  * テーパー "A-B"(や "A^B", "A-B-C") … A,B,… を要素の前後に分けて配置（区間で補間）
  * それ以外（アンテチェンバ系 f90x220_Ar など）… config/wall_shapes.json でユーザ定義
    （未定義なら寸法から仮の外形を置き、警告する＝“半自動”の手入力箇所）

Synrad3D は隣接断面間を r(θ) で線形補間するため、同一断面が続く区間は両端だけ置けば良い
（本ツールは連続同一を畳んで区間端のみ出力する）。

注意:
  * 生成した wall file の s は Bmad ラティスの s と一致している必要があります
    （dispog の s が機械 s と一致している前提）。
  * アンテチェンバ形状の向き（ウィングが内側/外側か）や中心オフセット r0 は
    リング側・偏向方向で変わり得るため、最終確認は wall_shapes.json で行ってください。
"""

from __future__ import annotations
import json
import re
from pathlib import Path

import skekb_layout as sk

_CONFIG = Path(__file__).resolve().parent / "config"


def _load(name, default=None):
    p = _CONFIG / name
    if not p.exists():
        return default
    return json.load(open(p, encoding="utf-8"))


def _cross_section(duct: str, ducttype: dict) -> str:
    d = ducttype.get(duct, {})
    return str(d.get("Cross Sect", "") or d.get("Cross Section", "")).strip()


def _auto_shape(code: str):
    """矩形 WxH / 円 fNNN をコードから自動生成。出来なければ None。寸法は mm→m。"""
    m = re.fullmatch(r"(\d+)x(\d+)", code)
    if m:                                   # 矩形（半幅, 半高）
        w = int(m.group(1)) / 2000.0
        h = int(m.group(2)) / 2000.0
        return {"r0": [0.0, 0.0], "v": [[w, h]], "_auto": "rectangle"}
    m = re.fullmatch(r"f(\d+)", code)
    if m:                                   # 円（半径 r、両軸対称で第1象限のみ）
        r = int(m.group(1)) / 2000.0
        return {"r0": [0.0, 0.0],
                "v": [[r, 0.0, 0.0, 0.0, 0.0], [0.0, r, r, r, 0.0]],
                "_auto": "circle"}
    return None


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


def _split_taper(code: str):
    """テーパーコードを基本コードのリストに分解（区切りは '-' と '^'）。"""
    return [p for p in re.split(r"[-^]", code) if p]


def _resolve_shape(code, library, auto_cache, placeholders):
    """断面コード → shape_id を返し、必要な shape_def を auto_cache に登録。"""
    if code in library:                     # ユーザ定義（最優先）
        auto_cache.setdefault(code, library[code])
        if library[code].get("_placeholder"):
            placeholders.add(code)
        return code
    if code in auto_cache:
        return code
    sh = _auto_shape(code)                  # 矩形・円は自動
    if sh is not None:
        auto_cache[code] = sh
        return code
    auto_cache[code] = _placeholder_shape(code)   # 仮形状（要定義）
    placeholders.add(code)
    return code


def build_sections(dispog_path: str, ring: str):
    """(s, shape_id) の列と、使用した shape_def 辞書、未定義コード集合を返す。"""
    elements = sk.parse_dispog(dispog_path)
    by_element = _load(f"{ring.lower()}_duct_by_element.json", {})
    ducttype = _load(f"{ring.lower()}_ducttype.json", {})
    library = _load("wall_shapes.json", {})

    auto_cache: dict = {}
    placeholders: set = set()
    raw: list[tuple[float, str]] = []       # (s, shape_id)

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

        parts = _split_taper(code)
        if len(parts) == 1:
            sid = _resolve_shape(parts[0], library, auto_cache, placeholders)
            raw.append((el.s, sid))
        else:                               # テーパー: 要素長で前後に振り分け
            n = len(parts)
            length = el.length or 0.0
            for i, p in enumerate(parts):
                sid = _resolve_shape(p, library, auto_cache, placeholders)
                s = el.s + (length * i / (n - 1) if length else i * 1e-4)
                raw.append((s, sid))

    raw.sort(key=lambda t: t[0])

    # 連続同一断面は区間端のみ残す（補間で内部は一定になる）
    sections: list[tuple[float, str]] = []
    for i, (s, sid) in enumerate(raw):
        prev = raw[i - 1][1] if i > 0 else None
        nxt = raw[i + 1][1] if i < len(raw) - 1 else None
        if sid == prev and sid == nxt:
            continue
        sections.append((s, sid))

    # s を厳密に増加させる（同値は微小量ずらす）
    eps = 1e-6
    for i in range(1, len(sections)):
        if sections[i][0] <= sections[i - 1][0]:
            sections[i] = (sections[i - 1][0] + eps, sections[i][1])

    used = {sid: auto_cache[sid] for _, sid in sections if sid in auto_cache}
    return sections, used, placeholders


def _fmt_vertex(v):
    parts = [f"{v[0]:.6g}", f"{v[1]:.6g}"]
    # radius_x, radius_y, tilt は非ゼロのものまで出力
    tail = list(v[2:])
    while tail and tail[-1] in (0, 0.0):
        tail.pop()
    parts += [f"{x:.6g}" for x in tail]
    return ", ".join(parts)


def write_wall_file(dispog_path: str, ring: str, out_path: str) -> dict:
    """Synrad3D wall file を書き出す。戻り値に断面数・未定義コードを含む。"""
    sections, used, placeholders = build_sections(dispog_path, ring)
    lines = []
    lines.append(f"! Synrad3D wall file (auto-generated) ring={ring}")
    lines.append(f"! sections={len(sections)}  shapes={len(used)}")
    if placeholders:
        lines.append("! NOTE: 次の断面コードは仮形状です。wall_shapes.json で定義してください:")
        lines.append("!   " + ", ".join(sorted(placeholders)))
    lines.append("")

    # &place（縦位置に断面を配置）
    for s, sid in sections:
        lines.append(f'&place section = {s:.4f}, "", "{sid}" /')
    lines.append("")

    # &shape_def（断面形状の定義）
    for sid, sh in sorted(used.items()):
        tag = ""
        if sh.get("_placeholder"):
            tag = "   ! ← PLACEHOLDER: 実形状に置き換えてください"
        elif sh.get("_auto"):
            tag = f"   ! auto ({sh['_auto']})"
        lines.append(f"&shape_def{tag}")
        lines.append(f'  name = "{sid}"')
        r0 = sh.get("r0", [0.0, 0.0])
        lines.append(f"  r0 = {r0[0]:.6g}, {r0[1]:.6g}")
        if sh.get("absolute_vertices"):
            lines.append("  absolute_vertices = T")
        for i, v in enumerate(sh["v"], start=1):
            lines.append(f"  v({i}) = {_fmt_vertex(v)}")
        lines.append("/")
        lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return {"sections": len(sections), "shapes": len(used),
            "placeholders": sorted(placeholders)}


def _main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        description="dispog + Duct_Type から Synrad3D wall file を生成（試作）")
    p.add_argument("dispog", help="dispog ファイル")
    p.add_argument("--ring", choices=["HER", "LER"], default=None)
    p.add_argument("-o", "--out", default=None, help="出力 wall ファイル")
    a = p.parse_args(argv)
    ring = a.ring
    if ring is None:
        base = Path(a.dispog).name.lower()
        ring = "LER" if base.startswith("sler") else "HER" if base.startswith("sher") else None
        if ring is None:
            p.error("リングを判定できません。--ring HER/LER を指定してください。")
    out = a.out or (Path(a.dispog).stem + f"_{ring}.wall")
    info = write_wall_file(a.dispog, ring, out)
    print(f"  断面配置 {info['sections']} 個 / 形状 {info['shapes']} 種")
    if info["placeholders"]:
        print("  ※ 要定義（仮形状）:", ", ".join(info["placeholders"]))
    print(f"出力: {out}")


if __name__ == "__main__":
    _main()
