# SKEKB Layout ツール 使い方マニュアル

SuperKEKB のマグネット・ビームダクト配置図を AutoCAD 上に描き、建設用の
Duct_Table（一覧表）を Excel で出力するための一連の Python ツールの操作手順と
内部解説です。

対象読者：加速器グループのメンバー（AutoCAD は使うが、Python の中身は深く
知らなくても運用できることを目指しています）。

---

## 目次

1. 全体の流れ
2. 動作環境と準備
3. 手順1 — dispog から `.scr` を生成する（`cli.py`）
4. 手順2 — AutoCAD で `.scr` を読み込んでブロックを配置する
5. 手順3 — Duct_Table（Excel）を生成する（`duct_table.py`）
6. 生成スクリプトが使っている AutoCAD コマンドの解説
7. `config/` の中身（各ファイルの意味）
8. 新しいビームダクト・マグネットが入ったときの変更方法
9. xlsm から `config/` を作り直す（`extract_config.py`）
10. うまくいかないとき（Mac の注意点を含む）

---

## 1. 全体の流れ

```
  格子計算 (SAD 等)
        │  dispog ファイル (sler_*.dispog / sher_*.dispog)
        ▼
  ┌─────────────────────────────────────────────┐
  │  cli.py        … dispog → AutoCAD スクリプト   │   ← 手順1
  │                  <name>_Mag.scr / _Duct.scr   │
  └─────────────────────────────────────────────┘
        │  .scr
        ▼
  ┌─────────────────────────────────────────────┐
  │  AutoCAD で SCRIPT 実行 → 図にブロックを配置    │   ← 手順2
  └─────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────┐
  │  duct_table.py … dispog + 台帳 → Duct_Table   │   ← 手順3
  │                  HER/LER_Duct_Table を .xlsx  │
  └─────────────────────────────────────────────┘

  すべての対応表・台帳は config/ にあり、人が編集できる。
  xlsm を更新したら extract_config.py で config/ を作り直せる。
```

ポイントは **「データ（`config/`）と処理（Python）を分離」** していることです。
新しい磁石・ダクトへの対応は基本的に `config/` の編集だけで済み、Python 本体は
触りません。

---

## 2. 動作環境と準備

- **Python 3.9 以上**。
  - `.scr` 生成（`cli.py`）は **標準ライブラリのみ**で動きます（追加不要）。
  - Duct_Table 生成（`duct_table.py`）と config 再生成（`extract_config.py`）は
    `openpyxl` が必要。`pip install -r requirements.txt` で入ります
    （xlsm の VBA 解析まで行う場合は `oletools` も）。
- **AutoCAD**（生成した `.scr` を読み込む）。Mac 版・Windows 版、日本語版・英語版の
  いずれでも動くよう作っています（→ 第6章）。

フォルダ構成（★は普段触るもの）：

```
skekb_layout/
├── cli.py              ★ .scr 生成コマンド
├── duct_table.py         Duct_Table(.xlsx) 生成
├── synrad3d_wall.py      Synrad3D wall file 生成（試作）
├── skekb_layout.py       コア（パース・幾何計算・スクリプト生成）
├── extract_config.py     xlsm から config/ を再生成
├── config/             ★ 対応表・台帳（編集対象。wall_shapes.json も含む）
├── MANUAL.md             このファイル
└── README.md             設計の概要
```

---

## 3. 手順1 — dispog から `.scr` を生成する（`cli.py`）

dispog ファイルを渡すと、マグネット用とダクト用の 2 つの `.scr` を作ります。
リング（HER/LER）はファイル名から自動判定します（`sler_*`→LER、`sher_*`→HER）。

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
| `--clear` | スクリプト先頭に「対象リング図形の削除」処理を付ける（→ 手順2の注意） |

実行すると、配置数のサマリと **未解決要素**（対応表にブロックが無い要素）が
表示されます。未解決が出たら第8章の手順で `config/` に追記します。
`✓ 全要素のブロックを解決しました` と出れば対応表は完全です。

dispog の読み方（座標変換）：dispog の `OGx, OGy`［m］を
`X = OGx × (-1000)`、`Y = OGy × (-1000)` で［mm］に変換して配置します
（係数は `config/settings.json` の `coordinate.scale_mm_per_m`）。

---

## 4. 手順2 — AutoCAD で `.scr` を読み込んでブロックを配置する

### 4.1 事前準備（重要）

`.scr` は**ブロックを「挿入」するだけ**です。マグネットやダクトの図形そのもの
（ブロック定義）は、あらかじめ図面（または参照する DWG）に登録されている必要が
あります。`.scr` が参照するブロック名は、`config/` の対応表が示すブロック
（例 `Q344E`、`D-QKALP`、`D-QKALP_Or`）です。

### 4.2 実行

1. ブロック定義の入った図面を開く。
2. コマンドラインに `SCRIPT` と入力（Mac 版はリボンが無いので**コマンド入力で実行**）。
3. 生成した `_Mag.scr` を選ぶ → マグネットが配置される。
4. 続けて `SCRIPT` → `_Duct.scr` を選ぶ → ダクトが配置される。

スクリプトは、レイヤ作成 → 点モード設定 → ブロック挿入 → マグネット名の文字 →
区間線（マグネット用）の順に流れます。配置先レイヤは `config/settings.json` の
リング別 `layers`（例 HER は `hermagnet`/`herduct`/`hermagnetname` …）。

### 4.3 実行前クリア（`--clear`）の使い方と注意

`--clear` を付けると、スクリプト先頭に「対象リングの既存図形を消してから描き直す」
処理（レイヤをロック→対象リング層と共通層だけアンロック→全選択削除＝Ctrl+A+Del
相当）が入ります。アンロックする共通レイヤは `settings.json` の
`clear.unlock_common` で調整できます。

> **注意：クリアは“最初に流すスクリプト（通常はマグネット）だけ”に付ける。**
> 手順は「マグネット（`--clear`付き）を実行 → ダクト（クリアなし）を実行」。
> ダクト側にもクリアを付けると、先に描いたマグネットまで消えます。

---

## 5. 手順3 — Duct_Table（Excel）を生成する（`duct_table.py`）

旧 VBA の `Make_HER/LER_Duct_Table` が作っていた一覧表
（`HER_Duct_Table` / `LER_Duct_Table`）を `.xlsx` で出力します。

```bash
# HER と LER の両シートを 1 つの xlsx に
python duct_table.py --her sher_5781_60_1.dispog --ler sler_1802_60_1.dispog -o Duct_Table.xlsx

# 片方だけ
python duct_table.py --ler sler_1802_60_1.dispog -o Duct_Table.xlsx
```

各列の供給元（しくみ）：

- **位置列（Loc / S）** … dispog から磁石名で引いて再計算（IR cryostat 部は固定値）。
- **磁石・機器の台帳列**（Mag Type / BM Note / Q Support / IP,CCG / RP / VSW / GV /
  Bellows / 各設置日 / 真空 / NEG-L,R,Act / Length） … `config/*_component.json`。
- **ダクト物理列**（Cross Section / Duct Note / BPM Height / NEG 群） …
  `config/*_ducttype.json` をダクト名で。
- **Room** … 磁石名で D01..D12、さらに GV 列のセクター標識でサブ表記（D01_IRL /
  D01_STP …）を付与。
- **Mag Others** … 手維持データを `config/*_mag_others.json` から行位置で適用。
- **GV セクター境界行**（Duct Note が "GV"）には、その行の全列の上下に太め罫線を引く。

`config/*_component.json` は表の「行の背骨」で、Duct_Table と 1 行ずつ位置で対応
します。台帳（component / ducttype / mag_others）は dispog に無い手維持データです。

---

## 5.5 Synrad3D の wall file を生成する（`synrad3d_wall.py`・試作）

Bmad/Synrad3D の `wall_file`（真空チェンバ断面の定義）を、**既存の情報だけで半自動
生成**します。使う情報は 2 つだけです。

- 各要素の縦位置 **s** … dispog
- 各要素の **断面コード**（`f90x220_Ar`、`104x50`、`f80` など） … Duct_Type の Cross Section

```bash
python synrad3d_wall.py sler_1802_60_1.dispog -o sler_1802_LER.wall
```

出力は Synrad3D の namelist 形式（`&place` で s に断面を配置、`&shape_def` で断面形状を定義）。
Synrad3D は隣接断面間を補間するので、同一断面が続く区間は両端だけ置きます。

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
`! ← PLACEHOLDER` と印を付けます。実行例の警告：

```
※ 要定義（仮形状）: f90x220H24, f90x220_St
```

この数種をライブラリに定義すれば完成です。

### 注意・確認事項

- 生成した wall の **s は Bmad ラティスの s と一致**している必要があります
  （dispog の s が機械 s と一致している前提）。`patch` 要素と重なる位置に断面を置けない
  などの制約は Synrad3D 側の仕様（マニュアル参照）。
- アンテチェンバの**向き**（ウィングが内側／外側か）や中心オフセット `r0` は、リング側や
  偏向方向で変わり得ます。最終的な形状・向きは `wall_shapes.json` で確認してください。
- 本機能は試作です。まず数か所を Synrad3D の `-plot` で断面確認し、`wall_shapes.json` を
  詰めていく使い方を想定しています。

---

## 6. 生成スクリプトが使っている AutoCAD コマンドの解説

`.scr` の中身は AutoCAD コマンドの列です。**Mac/Win・日英のどの環境でも同じファイルが
動く**よう、次の方針で書いています。

- **すべて `_` 付きの英語コマンド**（`_-INSERT` 等）で、日本語版でも英語コマンドを強制。
- **キーワードを使わず“順番”で指定**（日本語版の「r（回転）」等の翻訳に依存しない）。
- **改行は LF のみ**（`\r` を含まない。Mac で挿入や線描画が途中で止まるのを防ぐ）。

使っている主なコマンド：

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

挿入の書式は `settings.json` の `autocad.insert_command`（既定 `_-INSERT`）と
`insert_scale`（既定 `1`）で変更できます。

> Mac 版 AutoCAD の小技：ブロック一覧は `-INSERT ? D*` で確認できます。コマンド履歴を
> 見たいときは `COPYHIST` でクリップボードにコピーできます（Mac は Text Window が
> 開けないため）。

---

## 7. `config/` の中身（各ファイルの意味）

`{her,ler}` はリング別（HER 用 / LER 用）に同じ構造で 2 つあります。

### 7.1 `settings.json` — 幾何・レイヤ・色・挿入書式・クリア設定

旧 VBA に直書きされていた定数を集約したもの。主な項目：

- `coordinate.scale_mm_per_m`：座標変換係数（既定 `-1000`、m→mm）。
- `geometry`：円周長、octant 数、名前ラベルのオフセット率など。
- `text.magnet_name_height`：マグネット名の文字高さ。
- `autocad`：点モード、`insert_command`、`insert_scale`。
- `HER` / `LER`：リング別の `layers`（レイヤ名・色）、`element_suffix`（E/P）、
  名前ラベルのオフセット方向、除外規則、QC のサンプリングなど。
- `clear.unlock_common`：クリア時にアンロックする共通レイヤ。

### 7.2 `.scr` 生成で使う対応表

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
例：

```json
{ "QKALE": ["D-QKALE", "DQCSLaE"], "BLC1LE": ["D-BLC1LE"] }
```

**`{her,ler}_duct_blocks.json`** … キー → ダクトブロック名リスト（フォールバック）。

**`{her,ler}_duct_ordered.json`** … 末尾に `_Or` を付けるブロック名の一覧
（オーダー済み＝発注/製作済みの印。旧版で Lattice セルが緑のもの）。ここに載る
ブロックは `D-QKALE` ではなく `D-QKALE_Or` として挿入されます。例：

```json
["D-BLC1LE", "D-BLC2RE", "D-QKALE"]
```

マグネット解決の優先順位：①`mag_by_element` → ②先頭 `-` を外して再試行 →
③BN を導出して `mag_blocks`。さらに `mag_overrides.csv` の置換を適用。
ダクト解決の優先順位：①`duct_by_element` → ②`duct_blocks` を要素名で →
③ドット除去名で（`BLX4LP.1`→`BLX4LP1`）→ ④磁石 BN で。最後に `duct_ordered`
に載るブロックへ `_Or` を付与。

### 7.3 Duct_Table 生成で使う台帳

**`{her,ler}_component.json`** … Component シートそのもの（**表の行の背骨**）。
Mag Name / Mag Type / Duct Name / IP,CCG / RP / VSW / GV / Bellows / 各設置日 など、
手で維持する機器情報が 1 行ずつ入っています。

**`{her,ler}_ducttype.json`** … Duct_Type シート（ダクト名 → 断面・注記・BPM 高・
NEG など）。ダクト名で引きます。例：

```json
{ "DBAk3340aE": { "Cross Section": "104x50", "Note": "", "BPM Height": "", ... } }
```

**`{her,ler}_mag_others.json`** … Duct_Table の Mag Others 列（同居するステアリング系の
リスト）を**行位置で**保持。旧 VBA が手作業の例外で特定行へ振り分けていた手維持
データのため、計算で作らず台帳として持っています。

> `*_component.json` / `*_ducttype.json` / `*_mag_others.json` は dispog には無い
> 手維持データです。最新の xlsm から `extract_config.py` で作り直せます（第9章）。

---

## 8. 新しいビームダクト・マグネットが入ったときの変更方法

基本は **`config/` の該当ファイルに追記するだけ**。Python 本体は変更しません。
追記後はそのリングの dispog で再生成し、未解決が消えたか確認します。

### 8.1 新しいマグネットが未解決になった場合

`cli.py` 実行時に例えば `未解決: QNEWLE` と出たら：

1. その要素に対応する AutoCAD ブロック名を決める（既存図面のブロックを確認）。
2. `config/{ring}_mag_by_element.json` に 1 行追加：

   ```json
   "QNEWLE": "QNEW_blockE"
   ```

3. 同じ種別（BN）の磁石が今後も増えるなら、`{ring}_mag_blocks.json` に
   `"BNキー": "ブロック名"` を足しておくと、要素名を個別登録しなくても解決できます。
4. 特定要素だけ別ブロックにしたい（計算 BN と違うブロックにしたい）場合は
   `{ring}_mag_overrides.csv` に `要素名,期待BN,置換BN` を追記。
5. 再生成して確認：

   ```bash
   python cli.py  sler_1802_60_1.dispog  --ring LER  --mag-only --preview
   ```

### 8.2 新しいビームダクトが未解決／未配置の場合

1. その要素に対応するダクトブロック名を決める。
2. `config/{ring}_duct_by_element.json` に追加（複数ダクトはリストで）：

   ```json
   "QNEWLE": ["D-QNEWLE", "DQCSnewE"]
   ```

3. そのダクトが**オーダー済み**で `_Or` 付きにしたいなら、
   `config/{ring}_duct_ordered.json` にブロック名を追加：

   ```json
   ["D-BLC1LE", "D-QNEWLE"]
   ```

   逆に `_Or` を外したいときはこの一覧から削除します。
4. 再生成して確認（`--duct-only`）。

### 8.3 Duct_Table にも新しい行・列値を反映したい場合

Duct_Table は Component（行の背骨）・Duct_Type・Mag Others の台帳から作ります。
新しい磁石・ダクトを表に出すには：

- **おすすめ（確実）**：元の xlsm に新規行・機器情報を追記し、
  `python extract_config.py <その xlsm>` で台帳 config を作り直す（第9章）。
  Component の行構成・GV 境界行・Mag Others の振り分けは手維持データなので、
  xlsm を「正」として更新するのが安全です。
- **手早く直す**：`{ring}_ducttype.json` にダクトの断面・注記等を 1 件追加すれば、
  そのダクトの物理列は引けるようになります。ただし**行（Component の背骨）自体の
  追加は xlsm 経由が確実**です。

### 8.4 幾何・レイヤ・色を変えたい場合

`config/settings.json` を編集します（レイヤ名・色、文字高さ、座標スケール、
クリア時のアンロック対象など）。

---

## 9. xlsm から `config/` を作り直す（`extract_config.py`）

Excel 側で対応表・台帳を更新したら、最新の xlsm を渡して `config/` 一式を
再生成できます。

```bash
python extract_config.py  path/to/SKEKB_Layout_latest.xlsm
```

これで次がまとめて作り直されます：マグネット/ダクトのブロック表、要素→ブロックの
直接マップ、オーダー済み一覧（セル色から判定）、Component / Duct_Type 台帳、
Mag Others 列。再生成後はバックアップ（または Git）で差分を確認すると安心です。

---

## 10. うまくいかないとき（Mac の注意点を含む）

- **ブロックが入らない／途中で止まる**：参照しているブロック名が図面に未定義の
  可能性。`-INSERT ? D*` で定義済みブロックを確認。`.scr` の改行が LF か（CR が
  混じると Mac で停止しやすい）。
- **マグネット名が線の反対側（上下逆）に出る**：リング別のラベルオフセット方向の
  設定（`settings.json` の `label_offset`）。HER と LER で向きが逆です。
- **未解決要素が残る**：第8章のとおり `*_by_element.json` 等に追記。
- **Mac でコマンド履歴を見たい**：`COPYHIST` でクリップボードにコピー（Text Window は
  Mac では開けない）。コマンドラインのパレットを広げて確認することも可。
- **Duct_Table の値が合わない**：台帳（Component/Duct_Type/Mag Others）が古い可能性。
  最新 xlsm から `extract_config.py` で作り直す。

---

困ったときは、まず `cli.py ... --preview` で中身を確認し、未解決要素の一覧を
手がかりに `config/` を直す、という流れが基本です。
