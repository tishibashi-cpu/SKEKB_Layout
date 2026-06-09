# SKEKB Layout — AutoCAD 配置スクリプト生成ツール (Python版)

SuperKEKB メインリングのマグネット・ビームパイプ（ダクト）配置を AutoCAD 上に
展開し、建設用の Duct_Table（一覧表）を Excel で出力し、Synrad3D の wall file を
生成するための、一連の Python ツールです。設計の概要から具体的な操作手順、
`config/` の中身、新規要素が入ったときの変更方法までをこの 1 文書にまとめています。

対象読者：加速器グループのメンバー（AutoCAD は使うが、Python の中身は深く
知らなくても運用できることを目指しています）。

---

## 1. 新システムの方針

本ツールは **「データ（対応表）と処理（プログラム）を分離する」** ことを中心方針に
しています。

```
        [ 編集可能な対応表・台帳 (config/) ]   ←  人が直す（JSON / CSV）
                     +
        [ 小さく透明なエンジン (Python) ]      ←  基本は変えない
                     +
        [ コマンドライン (cli.py 他) ]         ←  実行
```

* **対応表を `config/` に外出し** … 「マグネット名 → AutoCAD ブロック名」などの対応は
  すべて `config/` の JSON / CSV にあります。対応を変えたいときは、これらを編集する
  だけで済み、Python 本体は触りません。
* **幾何計算は忠実に実装** … 座標変換・区間角度・マグネット名の注記位置・octant 距離
  ラベルなどは、移植元（Excel/VBA）と同じ結果になるよう実装しています。
* **未解決要素を必ず報告** … ブロックが見つからない要素は一覧表示されます。「どの表に
  何を足せばよいか」がすぐ分かるようにしてあります。
* **環境非依存の出力** … 生成する `.scr` は Mac/Windows・日本語版/英語版の AutoCAD で
  そのまま動くようにしています（→ 第7章）。

新しい磁石・ダクトへの対応は、基本的に `config/` の編集だけで完結します。

---

## 2. 全体の流れ

```
  格子計算 (SAD 等)
        │  dispog ファイル (sler_*.dispog / sher_*.dispog)
        ▼
  ┌─────────────────────────────────────────────┐
  │  cli.py         … dispog → AutoCAD スクリプト  │   ← 手順1（第5章）
  │                   <name>_Mag.scr / _Duct.scr  │
  └─────────────────────────────────────────────┘
        │  .scr
        ▼
  ┌─────────────────────────────────────────────┐
  │  AutoCAD で SCRIPT 実行 → 図にブロックを配置    │   ← 手順2（第6章）
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  duct_table.py  … dispog + 台帳 → Duct_Table  │   ← 手順3（第9章）
  │                   HER/LER_Duct_Table を .xlsx │
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  synrad3d_wall.py … dispog + 断面 → wall file │   ← 第10章（試作）
  └─────────────────────────────────────────────┘

  すべての対応表・台帳は config/ にあり、人が編集できる。
  xlsm を更新したら extract_config.py で config/ を作り直せる（第13章）。
```

---

## 3. ファイル構成

```
skekb_layout/
├── cli.py                 ★ .scr 生成コマンド
├── duct_table.py            Duct_Table(建設用一覧表)を xlsx 書き出し
├── synrad3d_wall.py         Synrad3D wall file を生成（試作）
├── skekb_layout.py          コア（パース・幾何計算・スクリプト生成）
├── extract_config.py        xlsm から config/ を再生成するユーティリティ
├── README.md                このファイル
└── config/                ★ 編集可能な設定・対応表・台帳（{her,ler} はリング別）
    ├── settings.json              幾何パラメータ・レイヤ名・色・挿入書式・クリア設定
    │
    │   ── .scr 生成（cli.py）で使う対応表 ──
    ├── {her,ler}_mag_by_element.json   要素名 → マグネットブロック名（Lattice 由来の正解マップ・最優先）
    ├── {her,ler}_mag_blocks.json       BN → マグネットブロック名（導出フォールバック）
    ├── {her,ler}_mag_overrides.csv     要素名固有の上書き規則（element,expect_bn,result_bn）
    ├── {her,ler}_duct_by_element.json  要素名 → ダクトブロック名リスト（Lattice 由来の正解マップ・最優先）
    ├── {her,ler}_duct_blocks.json      キー → ダクトブロック名リスト（フォールバック）
    ├── {her,ler}_duct_ordered.json     末尾に "_Or" を付ける（オーダー済み）ブロック名一覧
    │
    │   ── Duct_Table 書き出し（duct_table.py）で使う台帳 ──
    ├── {her,ler}_component.json         Component シート（行の背骨。Mag Type/IP,CCG/RP/VSW/GV/設置日 等）
    ├── {her,ler}_ducttype.json          Duct_Type シート（断面/注記/BPM/NEG 等。ダクト名で引く）
    ├── {her,ler}_mag_others.json        Mag Others 列（手維持データを行位置で適用）
    │
    │   ── Synrad3D wall 生成（synrad3d_wall.py）で使う ──
    └── wall_shapes.json                 断面コード → 断面形状（頂点）の定義（自動生成できない断面のみ）
```

★印のファイルだけ理解すれば運用できます。`config/` は xlsm から
`python extract_config.py <xlsm>` ですべて再生成できます。

---

## 4. 動作環境とインストール

* **Python 3.9 以上**。
  * `.scr` 生成（`cli.py`）は **標準ライブラリのみ**で動きます（追加インストール不要）。
  * Duct_Table 生成（`duct_table.py`）・wall 生成（`synrad3d_wall.py`）・config 再生成
    （`extract_config.py`）は `openpyxl` が必要。`pip install -r requirements.txt` で
    入ります（xlsm の VBA 解析まで行う場合は `oletools` も）。
* **AutoCAD**（生成した `.scr` を読み込む）。AutoCAD 2027 for Mac と
  AutoCAD 2026 (Windows) で動作検証済み（前者の方が処理が速かったです）。
  Mac 版・Windows 版、日本語版・英語版のいずれでも動くよう作っています（→ 第7章）。

---

## 5. 手順1 — dispog から `.scr` を生成する（`cli.py`）

dispog ファイルを渡すと、マグネット用とダクト用の 2 つの `.scr` を作ります。
出力名は `<dispog名>_Mag.scr` / `<dispog名>_Duct.scr`。リング（HER/LER）は
ファイル名から自動判定します（`sler_*`→LER、`sher_*`→HER）。

```bash
# 両方生成（出力先 ./out）
python cli.py  sler_1802_60_1.dispog  -o ./out
# → ./out/sler_1802_60_1_Mag.scr  と  _Duct.scr が出来る
```

主なオプション：

| オプション | 意味 |
|------------|------|
| `-o <dir>` | 出力先フォルダ |
| `--ring HER` / `--ring LER` | リングを明示（ファイル名で判定できないとき） |
| `--mag-only` | マグネットスクリプトだけ生成 |
| `--duct-only` | ダクトスクリプトだけ生成 |
| `--preview` | ファイルを作らず、中身の要約だけ表示 |
| `--clear` | スクリプト先頭に「対象リング図形の削除」処理を付ける（→ 第6章の注意） |

実行すると、配置数のサマリと **未解決要素**（対応表にブロックが無い要素）が
表示されます。未解決が出たら第12章の手順で `config/` に追記します。
`✓ 全要素のブロックを解決しました` と出れば対応表は完全です。

**座標変換**：dispog の `OGx, OGy`［m］を `X = OGx × (-1000)`、`Y = OGy × (-1000)`
で［mm］に変換して配置します（係数は `config/settings.json` の
`coordinate.scale_mm_per_m`）。

### 他プログラムからの利用（API）

```python
import skekb_layout as sk

# 1) パースだけ
elements = sk.parse_dispog("sler_1802_60_1.dispog")

# 2) マグネットスクリプト生成
result = sk.convert_dispog_to_magnet_scr(
    "sler_1802_60_1.dispog", "out_Mag.scr", ring="LER")
print("未解決:", result.unresolved)

# 3) ダクトスクリプト生成
sk.convert_dispog_to_duct_scr(
    "sler_1802_60_1.dispog", "out_Duct.scr", ring="LER")
```

---

## 6. 手順2 — AutoCAD で `.scr` を読み込んでブロックを配置する

### 6.1 事前準備（重要）

`.scr` は**ブロックを「挿入」するだけ**です。マグネットやダクトの図形そのもの
（ブロック定義）は、あらかじめ図面（または参照する DWG）に登録されている必要が
あります。`.scr` が参照するブロック名は、`config/` の対応表が示すブロック
（例 `Q344E`、`D-QKALP`、`D-QKALP_Or`）です。

### 6.2 実行

1. ブロック定義の入った図面を開く。
2. コマンドラインに `SCRIPT` と入力（Mac 版はリボンが無いので**コマンド入力で実行**）。
3. 生成した `_Mag.scr` を選ぶ → マグネットが配置される。
4. 続けて `SCRIPT` → `_Duct.scr` を選ぶ → ダクトが配置される。

スクリプトは、レイヤ作成 → 点モード設定 → ブロック挿入 → マグネット名の文字 →
区間線（マグネット用）の順に流れます。配置先レイヤは `config/settings.json` の
リング別 `layers`（例 HER は `hermagnet`/`herduct`/`hermagnetname` …）。

### 6.3 実行前クリア（`--clear`）の使い方と注意

`--clear` を付けると、スクリプト先頭に「対象リングの既存図形を消してから描き直す」
処理が入ります（API では `generate_magnet_script(elements, ring, clear_layers=True)`）。
中身は次のとおり（すべて言語非依存の `_` 付きコマンド）：

1. 全レイヤをロック（`_-LAYER _LOCK *`）
2. 対象リング（`her*`/`ler*`）と共通レイヤ（`0,1,Base,Chamber,Defpoint,Dim`）をアンロック
3. `_ERASE _ALL` で削除（ロック層の図形は自動除外＝Ctrl+A+Del と同じ挙動）

アンロックする共通レイヤは `config/settings.json` の `clear.unlock_common` で編集できます。

> **注意：クリアは“最初に流すスクリプト（通常はマグネット）だけ”に付ける。**
> 手順は「マグネット（`--clear`付き）を実行 → ダクト（クリアなし）を実行」。
> ダクト側にもクリアを付けると、先に描いたマグネットまで消えます。

---

## 7. 生成される `.scr` の互換性（Mac / Windows / 日英）

出力スクリプトは、次の工夫により **AutoCAD for Mac / Windows のどちらでも、
また日本語版・英語版のどちらでも**同じファイルがそのまま動くようにしています。

* **改行コードは LF のみ**（`\r` を一切含まない）。点の区切りに `\r` が混じると
  Mac で挿入や線描画が途中で止まる原因になります。
* **挿入は尺度・回転を「点の前」に先付け**：`_-INSERT <ブロック> _S 1 _R <回転角> <座標>`
  の順。座標を最後に置くことで、点の確定後にプロンプトが出ず余分なトークンが
  はみ出しません（配置前に尺度・回転を設定する仕様に準拠）。
* **コマンドは `_` 付き**（`_-INSERT` `_POINT` `_TEXT` `_LINE` `_ZOOM` 等）で英語
  コマンドを強制し、**キーワードを使わず順番で指定**するため、日本語版 AutoCAD でも
  「r（回転）」等の翻訳に依存しません。
* **オブジェクトスナップは `OSMODE 0`** で解除（コマンドではなくシステム変数を使うので
  言語非依存）。

`config/settings.json` の `insert_command`（既定 `_-INSERT`）と `insert_scale`
（既定 `1`）で挿入の書式は調整できます。

---

## 8. 生成スクリプトが使っている AutoCAD コマンドの解説

`.scr` の中身は AutoCAD コマンドの列です。第7章の方針で書かれており、主なコマンドは
次のとおりです。

| コマンド（.scr 内） | 役割 |
|---------------------|------|
| `OSMODE 0` | オブジェクトスナップを解除（システム変数なので言語非依存）。意図しない点吸着を防ぐ。 |
| `_-LAYER _N <名> _C <色> <名>` | レイヤを作成し色を設定（`-LAYER` はダイアログを出さないコマンド版）。 |
| `PDMODE <値>` / 点サイズ | 点（`_POINT`）の表示形を設定（`settings.json` の `autocad.point_mode` 等）。 |
| `_ZOOM _A` / `_ZOOM _E` | 全体表示 / 図形範囲にズーム。 |
| `_-INSERT <ブロック> _S 1 _R <角度> <X,Y>` | **ブロック挿入**。尺度 `_S` と回転 `_R` を**座標の前**に先付けする確実な形式（座標が最後なので、点確定後に余計なプロンプトが出ない）。 |
| `_POINT <X,Y>` | 点を打つ（マグネット中心位置の目印など）。 |
| `_TEXT <X,Y> <高さ> <回転> <文字>` | マグネット名などの文字を書く。 |
| `_LINE … （空行）` | 区間線を引く（空行＝Enter でコマンド終了）。 |

クリア処理（`--clear`）で追加されるもの：

| コマンド | 役割 |
|----------|------|
| `_-LAYER _LOCK *` | 全レイヤをロック。 |
| `_UNLOCK <レイヤ>`（複数） | 対象リング（`her*`/`ler*`）と共通レイヤだけアンロック。 |
| `_ERASE _ALL` （空行で確定） | 全選択削除（ロック層の図形は除外＝Ctrl+A+Del 相当）。 |

> Mac 版 AutoCAD の小技：ブロック一覧は `-INSERT ? D*` で確認できます。コマンド履歴を
> 見たいときは `COPYHIST` でクリップボードにコピーできます（Mac は Text Window が
> 開けないため）。

---

## 9. 手順3 — Duct_Table（Excel）を生成する（`duct_table.py`）

リング全体のマグネット配置・ダクト種類などをまとめた一覧表
（`HER_Duct_Table` / `LER_Duct_Table`）を `.xlsx` で出力します。

```bash
# HER と LER の両シートを 1 つの xlsx に
python duct_table.py --her sher_5781_60_1.dispog --ler sler_1802_60_1.dispog -o Duct_Table.xlsx

# 片方だけ
python duct_table.py --ler sler_1802_60_1.dispog -o Duct_Table.xlsx
```

### しくみ（Component 駆動）

`*_component.json`（Component シート）は Duct_Table と **1 行ずつ位置で対応する
「行の背骨」** です（HER 1105/1105・LER 1176/1176 行で (Mag名, Duct名) が完全一致する
ことを確認済み）。本ツールは Component を背骨として駆動し、各列を次の源から埋めます。

- **磁石・機器の台帳列**（Mag Type / BM Note / Q Support / Duct Name / IP,CCG / RP /
  VSW / GV / Bellows / Temp / Flow / 各設置日 / 真空 / NEG-L,R,Act / Length） …
  `config/*_component.json`
- **ダクト物理列**（Cross Section / Duct Note / BPM Height / NEG 群） …
  `config/*_ducttype.json` を Duct 名で引く
- **位置列（Loc / S）** … dispog を Mag 名で引いて再計算（IR cryostat 部は固定値）
- **Room** … Mag 名で D01..D12、さらに GV 列のセクター標識でサブ表記
  （D01_IRL / D01_STP / …）を付与
- **Mag Others** … `config/*_mag_others.json` から行位置で適用（手維持データ）
- **GV セクター境界行**（Duct Note が "GV"）には、その行の全列の上下に太め（thin）罫線を
  引く（通常行の細いグリッド線とは区別）

台帳（Component / Duct_Type / Mag Others）は dispog に無い手維持データです。最新の
xlsm が出たら `python extract_config.py <xlsm>` で再生成できます（第13章）。

### xlsm 版との一致（実測）

行数は両リングとも完全一致。主要列は **IP,CCG / RP / VSW / GV / Mag Type / Mag Others が
100%**、Cross Section 99〜100%、Room 99%（差は GV 挿入由来のセクション境界 1 行ずれで、
いずれも Mag 名が空の境界行）。Mag Others は手維持データ（旧マクロが手作業の例外で特定行へ
振り分け）のため、xlsm から抽出して `config/*_mag_others.json` に保存し行位置で適用する
ことで全リング 100% 一致しています。

---

## 10. Synrad3D の wall file を生成する（`synrad3d_wall.py`・試作）

Bmad/Synrad3D の `wall_file`（真空チェンバ断面の定義）を、**既存の情報だけで半自動
生成**します。使う情報は 2 つだけです。

- 各要素の縦位置 **s** … dispog
- 各要素の **断面コード**（`f90x220_Ar`、`104x50`、`f80` など） … Duct_Type の Cross Section

```bash
python synrad3d_wall.py sler_1802_60_1.dispog -o sler_1802_LER.wall
```

出力は Synrad3D の namelist 形式（`&place` で s に断面を配置、`&shape_def` で断面形状を
定義）。Synrad3D は隣接断面間を補間するので、同一断面が続く区間は両端だけ置きます。

### 断面コード → 形状 の対応

| 種別 | 例 | 扱い |
|------|----|------|
| 矩形 `WxH` | `104x50`, `60x40` | **自動**（半幅 W/2・半高 H/2［m］） |
| 円 `fNNN` | `f80`, `f150` | **自動**（半径 NNN/2［m］の円弧） |
| テーパー `A-B`（`A^B`, `A-B-C`） | `f90-f90x220_Ar` | A,B,… を要素の前後に分けて配置（区間で補間＝テーパー） |
| アンテチェンバ等 | `f90x220_Ar`, `f90x220_St`, `f80x220_Ar`, `f90x220H24`, `f50x190_Ar` | **要定義**（`config/wall_shapes.json`） |

矩形・円は寸法から自動生成します。**自動で作れない断面（アンテチェンバ系など、実質
数種類）だけ `config/wall_shapes.json` に頂点を一度定義すれば、あとは全自動**です。
これが「半自動」の意味で、手入力はこの形状ライブラリの整備に限られます。

### `config/wall_shapes.json`（形状ライブラリ）

断面コードごとに `shape_def` を定義します。

```json
"f90x220_Ar": {
  "r0": [0.0, 0.0],
  "v": [
    [0.045, 0.0, 0.0, 0.0, 0.0],
    [0.045, 0.025, 0.045, 0.025, 0.0],
    [-0.045, 0.025],
    [-0.110, 0.025],
    [-0.110, 0.004],
    [-0.045, 0.004]
  ]
}
```

- `r0`：断面中心［m］。
- `v`：頂点 `[x, y, (radius_x), (radius_y), (tilt)]`［m］。反時計回り（θ 増加順）。
  - 全頂点が `x,y ≥ 0` なら両軸対称として 1/4 だけ、`y ≥ 0` のみなら x 軸対称として
    上半分だけの記述で済みます。
  - 頂点間は直線。`radius_x` を与えると円弧（`+`凸/`−`凹）、`radius_y` も与えると楕円弧。

未定義のコードは寸法から**仮の外接矩形**を置き、実行時に「要定義」と警告し、ファイル内にも
`! ← PLACEHOLDER` と印を付けます（例：`※ 要定義（仮形状）: f90x220H24, f90x220_St`）。
この数種を定義すれば完成です。

### 注意・確認事項

- 生成した wall の **s は Bmad ラティスの s と一致**している必要があります（dispog の s が
  機械 s と一致している前提）。`patch` 要素と重なる位置に断面を置けないなどの制約は
  Synrad3D 側の仕様（マニュアル参照）。
- アンテチェンバの**向き**（ウィングが内側／外側か）や中心オフセット `r0` は、リング側や
  偏向方向で変わり得ます。最終的な形状・向きは `wall_shapes.json` で確認してください。
- 本機能は試作です。まず数か所を Synrad3D の `-plot` で断面確認し、`wall_shapes.json` を
  詰めていく使い方を想定しています。

---

## 11. `config/` の中身（各ファイルの意味）

`{her,ler}` はリング別（HER 用 / LER 用）に同じ構造で 2 つあります。

### 11.1 `settings.json` — 幾何・レイヤ・色・挿入書式・クリア設定

主な項目：

- `coordinate.scale_mm_per_m`：座標変換係数（既定 `-1000`、m→mm）。
- `geometry`：円周長、octant 数、名前ラベルのオフセット率など。
- `text.magnet_name_height`：マグネット名の文字高さ。
- `autocad`：点モード、`insert_command`、`insert_scale`。
- `HER` / `LER`：リング別の `layers`（レイヤ名・色）、`element_suffix`（E/P）、
  名前ラベルのオフセット方向、除外規則、QC のサンプリングなど。
- `clear.unlock_common`：クリア時にアンロックする共通レイヤ。

### 11.2 `.scr` 生成で使う対応表

**`{her,ler}_mag_by_element.json`** … 要素名 → マグネットブロック名（**最優先**の正解
マップ。Lattice シート由来）。例：

```json
{ "QC1LPE435": "Q10E", "QKALE": "QKnewE" }
```

**`{her,ler}_mag_blocks.json`** … BN（磁石種別キー）→ ブロック名（上の直接マップで
引けないときのフォールバック）。例：

```json
{ "B1239": "B1239E", "B2900": "B2900E" }
```

**`{her,ler}_mag_overrides.csv`** … 要素名固有の上書き規則。`element,expect_bn,result_bn`
の 3 列。「その要素の計算上の BN が `expect_bn` なら `result_bn` に置換」。
`expect_bn` を `*` にすると無条件置換。例：

```csv
element,expect_bn,result_bn
QTBOTE.1,Q826,Q826new
QTBOTE.1,Q817,Q817new
```

**`{her,ler}_duct_by_element.json`** … 要素名 → ダクトブロック名リスト（**最優先**）。
要素名から中間キーを作る規則は多岐・ラティス依存が強いため、推測での再現はやめ、
Lattice シートが実際に各要素へ割り当てた結果を直接マップとして引きます。例：

```json
{ "QKALE": ["D-QKALE", "DQCSLaE"], "BLC1LE": ["D-BLC1LE"] }
```

**`{her,ler}_duct_blocks.json`** … キー → ダクトブロック名リスト（フォールバック）。

**`{her,ler}_duct_ordered.json`** … 末尾に `_Or` を付けるブロック名の一覧
（オーダー済み＝発注/製作済みの印。旧版で Lattice セルが緑のもの）。ここに載る
ブロックは `D-QKALE` ではなく `D-QKALE_Or` として挿入されます。図面側に `_Or` 付き
ブロックが定義されているための規則です。例：

```json
["D-BLC1LE", "D-BLC2RE", "D-QKALE"]
```

**解決の優先順位**

- マグネット：①`mag_by_element` → ②先頭 `-` を外して再試行 → ③BN を導出して
  `mag_blocks`。さらに `mag_overrides.csv` の置換を適用。
- ダクト：①`duct_by_element` → ②`duct_blocks` を要素名で → ③ドット除去名で
  （`BLX4LP.1`→`BLX4LP1`）→ ④磁石 BN で。最後に `duct_ordered` に載るブロックへ
  `_Or` を付与。

### 11.3 Duct_Table 生成で使う台帳

**`{her,ler}_component.json`** … Component シートそのもの（**表の行の背骨**）。
Mag Name / Mag Type / Duct Name / IP,CCG / RP / VSW / GV / Bellows / 各設置日 など、
手で維持する機器情報が 1 行ずつ入っています。

**`{her,ler}_ducttype.json`** … Duct_Type シート（ダクト名 → 断面・注記・BPM 高・
NEG など）。ダクト名で引きます。例：

```json
{ "DBAk3340aE": { "Cross Section": "104x50", "Note": "", "BPM Height": "", ... } }
```

**`{her,ler}_mag_others.json`** … Duct_Table の Mag Others 列（同居するステアリング系の
リスト）を**行位置で**保持。手作業の例外で特定行へ振り分けられた手維持データのため、
計算で作らず台帳として持っています。

### 11.4 wall 生成で使うライブラリ

**`wall_shapes.json`** … 断面コード → 断面形状（頂点）の定義。矩形・円は自動生成
されるので、自動生成できない断面（アンテチェンバ系など）だけを書きます（第10章）。

> `*_component.json` / `*_ducttype.json` / `*_mag_others.json` などの台帳は dispog には
> 無い手維持データです。最新の xlsm から `extract_config.py` で作り直せます（第13章）。

---

## 12. 対応表のメンテナンス（新規マグネット・ダクトの追加）

基本は **`config/` の該当ファイルに追記するだけ**。Python 本体は変更しません。
追記後はそのリングの dispog で再生成し、未解決が消えたか確認します。

### 12.1 新しいマグネットが未解決になった場合

`cli.py` 実行時に例えば `未解決: QNEWLE` と出たら：

1. その要素に対応する AutoCAD ブロック名を決める（既存図面のブロックを確認）。
2. `config/{ring}_mag_by_element.json` に 1 行追加：`"QNEWLE": "QNEW_blockE"`
3. 同じ種別（BN）の磁石が今後も増えるなら、`{ring}_mag_blocks.json` に
   `"BNキー": "ブロック名"` を足すと、要素名を個別登録しなくても解決できます。
4. 特定要素だけ計算 BN と違うブロックにしたい場合は、`{ring}_mag_overrides.csv` に
   `要素名,期待BN,置換BN` を追記。
5. 再生成して確認：`python cli.py sler_1802_60_1.dispog --ring LER --mag-only --preview`

### 12.2 新しいビームダクトが未解決／未配置の場合

1. その要素に対応するダクトブロック名を決める。
2. `config/{ring}_duct_by_element.json` に追加（複数ダクトはリストで）：
   `"QNEWLE": ["D-QNEWLE", "DQCSnewE"]`
3. そのダクトが**オーダー済み**で `_Or` 付きにしたいなら、
   `config/{ring}_duct_ordered.json` にブロック名を追加（外したいときは削除）。
4. 再生成して確認（`--duct-only`）。

### 12.3 Duct_Table にも新しい行・列値を反映したい場合

Duct_Table は Component（行の背骨）・Duct_Type・Mag Others の台帳から作ります。

- **おすすめ（確実）**：元の xlsm に新規行・機器情報を追記し、
  `python extract_config.py <その xlsm>` で台帳 config を作り直す（第13章）。
  Component の行構成・GV 境界行・Mag Others の振り分けは手維持データなので、xlsm を
  「正」として更新するのが安全です。
- **手早く直す**：`{ring}_ducttype.json` にダクトの断面・注記等を 1 件追加すれば、
  そのダクトの物理列は引けます。ただし**行（Component の背骨）自体の追加は xlsm 経由が
  確実**です。

### 12.4 幾何・レイヤ・色を変えたい場合

`config/settings.json` を編集します（レイヤ名・色、文字高さ、座標スケール、クリア時の
アンロック対象など）。

### 12.5 マグネット名表示・ダクト角度のリング別調整

マグネット名の注記とダクトの角度計算は HER と LER で定数・方向が異なり、
`config/settings.json` のリング別設定で再現しています。

- 名前ラベルのオフセット方向：HER は X<0→内/X≥0→外、LER は逆（X<0→外/X≥0→内）。
  これを誤るとマグネット名がビームラインの反対側（上下逆）に出ます。
- Q 磁石の距離ラベル（例 `(TR148)`）の octant 計算定数もリングで異なります。
- 除外規則：LER は先頭 "D" も除外し、長さ 0 でも "SK**" は残します。
- 反転要素（先頭 "-"、例 `-QLA7RP`/`-BLA6RP.1`）は、名前表示では "-" を除き、Q/B 判定にも
  除去後の名前を使います。これによりダクトの角度（偏向磁石の 2 個目以降のダクトは
  「もう一つ前」の角度）が正しく計算され、段差が出ません。
- ダクト角度には X<0,Y≥0 での「角度<-80°なら +180°」補正（日光シケイン）も含みます。

---

## 13. xlsm から `config/` を作り直す（`extract_config.py`）

Excel 側で対応表・台帳を更新したら、最新の xlsm を渡して `config/` 一式を
再生成できます。

```bash
python extract_config.py  path/to/SKEKB_Layout_latest.xlsm
```

これで次がまとめて作り直されます：マグネット/ダクトのブロック表、要素→ブロックの
直接マップ、オーダー済み一覧（セル色から判定）、Component / Duct_Type 台帳、
Mag Others 列。再生成後はバックアップ（または Git）で差分を確認すると安心です。

---

## 14. 旧VBA との対応（移植元の対照表）

| 本ツール | 旧VBA |
|----------|-------|
| `parse_dispog()` | `HER_Lattice` シートへの dispog 貼り付け |
| `MagnetBlockResolver` | `HERInsertMagnetBlockName` / `LERInsertMagnetBlockName` |
| `generate_magnet_script()` | `Make_HER_Mag_ScriptFile` / `Make_LER_Mag_ScriptFile` |
| `DuctBlockResolver` | `HERInsertDuctBlockName` / `LERInsertDuctBlockName` |
| `generate_duct_script()` | `Make_HER_Duct_ScriptFile` / `Make_LER_Duct_ScriptFile` |
| `duct_table.py` | `Make_HER_Duct_Table` / `Make_LER_Duct_Table` |
| `config/*_blocks.json` | `*_MagBlock` / `*_DuctBlock` シート |
| `config/*_component.json` 他台帳 | `*_Component` / `*_Duct_Type` シート |
| `config/*_overrides.csv` | VBA 内の `If Cells(i,1)=…` 分岐群 |
| `config/settings.json` | VBA 内の定数（`-1000`, レイヤ名, `pdmode` 等） |

### 忠実に実装した処理

* 座標変換 `X = OGx × -1000`, `Y = OGy × -1000`（mm 化）
* 区間の角度計算（`atan` ＋ 象限別の +90/-90/+180 補正、鉛直区間の 90/270）
* 偏向磁石（先頭 `B`）で `Value < 0` のときブロックを 180° 反転
* マグネット名注記の位置（区間中点を内外へ ±0.4% オフセット）
* Q 磁石の距離ラベル（`TL/NR/NL/FR/FL/OR/OL/TR` ＋ octant 内距離）
* QC1/QC2 は代表 1 本だけ名前を表示するサンプリング
* 除外規則（先頭 `P` または長さ 0 の要素はスキップ 等）
* ステアリングや Half-Q、特殊 Q、ウィグラー `BW` の up/down 判定

---

## 15. うまくいかないとき（Mac の注意点を含む）

- **ブロックが入らない／途中で止まる**：参照しているブロック名が図面に未定義の可能性。
  `-INSERT ? D*` で定義済みブロックを確認。`.scr` の改行が LF か（CR が混じると Mac で
  停止しやすい）。
- **マグネット名が線の反対側（上下逆）に出る**：リング別のラベルオフセット方向の設定
  （`settings.json` の `label_offset`）。HER と LER で向きが逆です（→ 12.5）。
- **未解決要素が残る**：第12章のとおり `*_by_element.json` 等に追記。
- **Mac でコマンド履歴を見たい**：`COPYHIST` でクリップボードにコピー（Text Window は
  Mac では開けない）。コマンドラインのパレットを広げて確認することも可。
- **Duct_Table の値が合わない**：台帳（Component/Duct_Type/Mag Others）が古い可能性。
  最新 xlsm から `extract_config.py` で作り直す。

困ったときは、まず `cli.py ... --preview` で中身を確認し、未解決要素の一覧を
手がかりに `config/` を直す、という流れが基本です。

---

## 16. 既知の制限・今後の発展

* **検証は対象比較で**：本ツールの出力は、実機の図面・既知の正解 `.scr` や既存の
  Duct_Table と照合して確認することを推奨します。未解決要素レポートが差分発見の
  助けになります。
* **Synrad3D wall は試作**：アンテチェンバ断面の形状ライブラリ整備と、`-plot` での
  断面確認を前提とした半自動生成です。
* **発展の方向**
  * `ezdxf` を使って `.scr` を介さず `.dxf` を直接生成（AutoCAD コマンド非依存）
  * 対応表の編集 UI／変更履歴管理
  * 複数 dispog の一括処理・差分レイアウト
