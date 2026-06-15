"""
sr_cal.py — SR マスク熱負荷の簡易計算（旧 xlsm の SR_Cal シートの移植）
========================================================================

偏向電磁石からの放射光（SR）がマスク面に与える熱負荷を見積もる。

    python sr_cal.py --ler                 # LER の既定値で計算
    python sr_cal.py --her                 # HER の既定値で計算
    python sr_cal.py --ler --L1 6200 --L2 6110   # 距離を差し替え
    python sr_cal.py --E 4 --I 4 --rho 31.854 --L1 6495.8 --L2 6405.8 --LM 90.27 --H 10

入力:
    E   ビームエネルギー [GeV]
    I   ビーム電流 [A]
    rho 偏向半径 [m]
    L1  光源 → マスク始め（面の根元）の距離 [mm]
    L2  光源 → マスク頂点（先端）の距離 [mm]
    LM  マスク面の長さ [mm]
    H   マスクの高さ [mm]

「光源」の取り方:
    SR は偏向磁石内の軌道全体から接線方向に放射される。点光源として扱う本計算
    では、光源 = 「マスクを照らす光線が軌道弧に接する点（接点）」に取るのが正しい。
    AutoCAD 上ではマスク頂点から上流の偏向軌道弧へ接線を引き、その接点から
    L1, L2 を直線距離で測る。磁石中心は接点が分からないときの粗い近似にすぎない。
    マスクが磁石全長を見込む場合は点光源近似が崩れるので、弧を分割して評価する。

出力（シートと同じ量）:
    Er  臨界エネルギー [keV]        Er = 2.22 E^3 / rho
    γ   ローレンツ因子              γ = E/0.000511
    P   全 SR パワー [MW]           P = 88.5e3 E^4 I / rho [W]
    t1  光源から見たマスク面の張角（余弦定理）
    t2, t3, t4  マスク三角形の角（t3 = acos(H/LM), t4 = π - t3 - t2）
    LS  影の長さ [mm]               LS = H tan(t4)（大きさが意味を持つ）
    PL  線パワー密度 [W/m]          PL = P · (t1/2π) / LM
    W   SR の縦広がり [mm]          W = 2 L1 / γ
    PA  面パワー密度 [W/mm^2]       PA = PL / W
"""

from __future__ import annotations
import argparse
import math

# 旧シートの既定値（上段 = LER, 下段 = HER）
PRESETS = {
    "LER": dict(E=4.0, I=4.0, rho=31.854,
                L1=6495.8046, L2=6405.8146, LM=90.2718, H=10.0),
    "HER": dict(E=7.0, I=3.0, rho=105.9833,
                L1=7326.5324, L2=7236.5405, LM=90.2718, H=10.0),
}


def sr_mask_load(E, I, rho, L1, L2, LM, H):
    """SR_Cal シートと同じ計算。結果を dict で返す。"""
    Er = 2.22 * E ** 3 / rho                      # 臨界エネルギー [keV]
    gamma = E * 1000.0 / 0.511                    # ローレンツ因子
    P = 88.5e3 * E ** 4 * I / rho                 # 全 SR パワー [W]

    # マスク三角形（光源・マスク始め・マスク頂点）
    t1 = math.acos((L1 ** 2 + L2 ** 2 - LM ** 2) / (2 * L1 * L2))
    t2 = math.acos((LM ** 2 + L2 ** 2 - L1 ** 2) / (2 * L2 * LM))
    t3 = math.acos(H / LM)
    t4 = math.pi - t3 - t2

    LS = H * math.tan(t4)                         # 影の長さ [mm]
    PL = P * t1 / (2 * math.pi) / LM * 1000.0     # 線パワー密度 [W/m]
    W = 2.0 / gamma * L1                          # SR 縦広がり [mm]
    PA = PL / 1000.0 / W                          # 面パワー密度 [W/mm^2]

    return dict(Er_keV=Er, gamma=gamma, P_MW=P / 1e6,
                t1_rad=t1, t1_deg=math.degrees(t1),
                t2_deg=math.degrees(t2), t3_deg=math.degrees(t3),
                t4_deg=math.degrees(t4),
                LS_mm=LS, PL_W_per_m=PL, W_mm=W, PA_W_per_mm2=PA)


def rho_from_dispog(dispog_path: str, magnet: str):
    """dispog から偏向磁石の曲率半径 ρ = 長さ/|偏向角| [m] を求める。

    magnet は要素名（例 "BS2NP.1"）。先頭の "-"（反転印）は無視して照合する。
    完全一致が無ければ "名前." で始まる要素群（例 "B2P" → B2P.1, B2P.2, ...）を
    探し、全てが同じ ρ ならそれを使う。
    戻り値: (rho, 使用した要素名, 長さ, 偏向角)
    """
    import skekb_layout as sk
    els = sk.parse_dispog(dispog_path)

    def norm(n):
        return n.lstrip("-")

    matches = [e for e in els if norm(e.name) == magnet or e.name == magnet]
    if not matches:
        fam = [e for e in els if norm(e.name).startswith(magnet + ".")]
        rhos = {round(e.length / abs(e.value), 6)
                for e in fam if e.length > 0 and e.value and abs(e.value) > 1e-12}
        if fam and len(rhos) == 1:
            matches = fam[:1]
            print(f"  ({magnet} 系 {len(fam)} 要素はすべて同じ ρ。"
                  f"{fam[0].name} を代表に使用)")
        elif fam:
            names = sorted({norm(e.name) for e in fam})[:10]
            raise SystemExit(f"エラー: {magnet} 系の ρ が一意でありません。"
                             f"要素名を特定してください: {names}")
        else:
            cand = sorted({norm(e.name) for e in els
                           if magnet.upper() in norm(e.name).upper()})[:10]
            hint = f" 似た名前: {cand}" if cand else ""
            raise SystemExit(f"エラー: 要素 {magnet} が見つかりません。{hint}")

    el = matches[0]
    if not norm(el.name).startswith("B"):
        print(f"  警告: {el.name} は偏向磁石（B*）ではないようです。")
    if el.length <= 0 or not el.value or abs(el.value) < 1e-12:
        raise SystemExit(f"エラー: {el.name} は長さまたは偏向角が 0 のため "
                         f"ρ を計算できません。")
    rho = el.length / abs(el.value)
    return rho, el.name, el.length, el.value


def _main(argv=None):
    p = argparse.ArgumentParser(
        description="SR マスク熱負荷の簡易計算（旧 SR_Cal シート相当）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ler", action="store_true", help="LER の既定値を使う")
    g.add_argument("--her", action="store_true", help="HER の既定値を使う")
    p.add_argument("--E", type=float, help="ビームエネルギー [GeV]")
    p.add_argument("--I", type=float, help="ビーム電流 [A]")
    p.add_argument("--rho", type=float, help="偏向半径 [m]")
    p.add_argument("--L1", type=float, help="光源→マスク始め [mm]")
    p.add_argument("--L2", type=float, help="光源→マスク頂点 [mm]")
    p.add_argument("--LM", type=float, help="マスク面の長さ [mm]")
    p.add_argument("--H", type=float, help="マスクの高さ [mm]")
    p.add_argument("--dispog", help="dispog ファイル（--source-magnet と併用）")
    p.add_argument("--source-magnet",
                   help="光源の偏向磁石名（dispog から ρ=長さ/|偏向角| を自動計算。"
                        "--rho より優先）")
    a = p.parse_args(argv)

    ring = "HER" if a.her else "LER"
    prm = dict(PRESETS[ring])
    for k in ("E", "I", "rho", "L1", "L2", "LM", "H"):
        v = getattr(a, k)
        if v is not None:
            prm[k] = v

    src_note = ""
    if a.source_magnet:
        if not a.dispog:
            p.error("--source-magnet には --dispog <ファイル> が必要です。")
        rho, name, L, theta = rho_from_dispog(a.dispog, a.source_magnet)
        prm["rho"] = rho
        src_note = (f"  光源磁石: {name}  L={L:.4f} m, θ={theta:+.6f} rad "
                    f"→ ρ = {rho:.3f} m\n")

    r = sr_mask_load(**prm)
    print(f"=== SR マスク熱負荷 ({ring} 既定 + 上書き) ===")
    if src_note:
        print(src_note, end="")
    print(f"  入力: E={prm['E']} GeV, I={prm['I']} A, rho={prm['rho']:.3f} m")
    print(f"        L1={prm['L1']} mm, L2={prm['L2']} mm, "
          f"LM={prm['LM']} mm, H={prm['H']} mm")
    print(f"  臨界エネルギー  Er = {r['Er_keV']:.3f} keV")
    print(f"  ガンマ          γ  = {r['gamma']:.1f}")
    print(f"  全 SR パワー    P  = {r['P_MW']:.3f} MW")
    print(f"  マスク面の張角  t1 = {r['t1_rad']:.6e} rad = {r['t1_deg']:.5f} deg")
    print(f"  (t2 = {r['t2_deg']:.2f} deg, t3 = {r['t3_deg']:.2f} deg, "
          f"t4 = {r['t4_deg']:.2f} deg)")
    print(f"  影の長さ        LS = {r['LS_mm']:.1f} mm")
    print(f"  線パワー密度    PL = {r['PL_W_per_m']:.1f} W/m")
    print(f"  SR 縦広がり     W  = {r['W_mm']:.3f} mm")
    print(f"  面パワー密度    PA = {r['PA_W_per_mm2']:.3f} W/mm^2")


if __name__ == "__main__":
    _main()
