"""
cli.py  —  SKEKB レイアウトツール コマンドライン版 (tkinter 不要)
================================================================

GUI が使えない環境 (tkinter 未導入など) でも動く版。
コアロジックは gui.py と同一 (skekb_layout.py を使用)。

使い方
------
  # マグネット・ダクト両方を生成 (リングはファイル名から自動推定)
  python cli.py  sler_1802_60_1.dispog

  # 出力先フォルダを指定
  python cli.py  sler_1802_60_1.dispog  -o ./out

  # リングを明示
  python cli.py  some.dispog  --ring HER

  # マグネットだけ / ダクトだけ
  python cli.py  some.dispog  --mag-only
  python cli.py  some.dispog  --duct-only

  # 中身を確認するだけ (スクリプトは作らない)
  python cli.py  some.dispog  --preview
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import skekb_layout as sk


def guess_ring(path: str) -> str:
    name = Path(path).name.lower()
    if name.startswith(("sler", "ler")):
        return "LER"
    if name.startswith(("sher", "her")):
        return "HER"
    return "LER"  # 既定


def print_report(label: str, out: Path, res: sk.ConversionResult):
    print(f"\n[{label}]  -> {out}")
    print(f"  スクリプト行数  : {len(res.script_lines)}")
    print(f"  配置ブロック種類: {len(res.block_usage)}")
    print(f"  配置ブロック総数: {sum(res.block_usage.values())}")
    if res.unresolved:
        print(f"  ⚠ 未解決要素 {len(res.unresolved)} 件 "
              f"(config/ への追加を検討してください):")
        for u in res.unresolved[:30]:
            print(f"      {u}")
        if len(res.unresolved) > 30:
            print(f"      … 他 {len(res.unresolved) - 30} 件")
    else:
        print("  ✓ 全要素のブロックを解決しました")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="SKEKB dispog -> AutoCAD スクリプト生成 (CLI版)")
    p.add_argument("dispog", help="入力 .dispog ファイル")
    p.add_argument("-o", "--outdir", default=".",
                   help="出力フォルダ (既定: カレント)")
    p.add_argument("--ring", choices=["HER", "LER"],
                   help="リング (省略時はファイル名から推定)")
    p.add_argument("--mag-only", action="store_true", help="マグネットのみ生成")
    p.add_argument("--duct-only", action="store_true", help="ダクトのみ生成")
    p.add_argument("--clear", action="store_true",
                   help="実行前に対象リング層の既存図形を削除する処理を先頭に付ける(マグネット側)")
    p.add_argument("--preview", action="store_true",
                   help="読み込んで内容を表示するだけ (生成しない)")
    args = p.parse_args(argv)

    dispog = args.dispog
    if not Path(dispog).exists():
        print(f"エラー: ファイルが見つかりません: {dispog}", file=sys.stderr)
        return 1

    ring = args.ring or guess_ring(dispog)
    outdir = Path(args.outdir)
    stem = Path(dispog).stem

    print(f"入力 : {dispog}")
    print(f"リング: {ring}")

    elements = sk.parse_dispog(dispog)
    print(f"要素数: {len(elements)}")

    if args.preview:
        print(f"\n{'要素名':<16}{'OGx[m]':>12}{'OGy[m]':>14}{'長さ[m]':>10}")
        print("-" * 54)
        for el in elements[:30]:
            print(f"{el.name:<16}{el.ogx:>12.4f}{el.ogy:>14.4f}{el.length:>10.4f}")
        if len(elements) > 30:
            print(f"… 他 {len(elements) - 30} 要素")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    do_mag = not args.duct_only
    do_duct = not args.mag_only

    if do_mag:
        res = sk.generate_magnet_script(elements, ring, clear_layers=args.clear)
        out = outdir / f"{stem}_Mag.scr"
        out.write_text(res.text(), encoding="utf-8")
        print_report("マグネット", out, res)

    if do_duct:
        resolver = sk.DuctBlockResolver(ring)
        res = sk.generate_duct_script(elements, ring, resolver)
        out = outdir / f"{stem}_Duct.scr"
        out.write_text(res.text(), encoding="utf-8")
        print_report("ダクト", out, res)

    print("\n生成完了。AutoCAD のコマンドライン上で SCRIPT を実行し、"
          "生成された .scr を指定してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
