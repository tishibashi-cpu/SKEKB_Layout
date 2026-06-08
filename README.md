# SKEKB Layout — AutoCAD スクリプト生成ツール (Python版)

SuperKEKBメインリングのマグネット・ビームパイプ（ダクト）配置を
AutoCAD 上に展開するための、旧 Excel/VBA システムの Python 移植版です。

---

## 1. なぜ作り直したか — 旧システムの問題点

旧システム（`SKEKB_Layout_*.xlsm`）は、約 **76,000 行の VBA** が 1 つの
Excel ブックに埋め込まれた構造でした。解析の結果、保守を難しくしている
原因は次の点にありました。

| 問題 | 具体例 |
|------|--------|
| **対応表がコードに直書き** | 「マグネット名 → AutoCAD ブロック名」の規則が、`If Cells(i,1) = "QTBOTE.1" Then …` のような分岐として **数百個** VBA に直接書かれている（HER 112件 / LER 70件）。表を直すにはコードを直す必要があった。 |
| **データと処理の混在** | 座標・対応表・処理ロジックがすべて 1 ブックに同居し、どこを触ると何が変わるか把握困難。 |
| **実行手順が属人的** | Excel を開き、シートを選び、ボタンを正しい順序で押す必要があり、開発者本人以外には使いにくい。 |
| **エラーが見えない** | 対応表にない要素は無言でスキップ／`～error` という名前が列に残るだけで、気づきにくい。 |

### 新システムの方針

```
        旧:  [ Excel + VBA(全部入り) ]
                     │
        新:  [ 編集可能な対応表 (config/) ]  ←  人が直す
                     +
             [ 小さく透明なエンジン (Python) ]  ←  基本変えない
                     +
             [ コマンドライン (cli.py) ]
```

* **対応表を config/ に外出し** … JSON / CSV を編集するだけで対応を変更できる。コードは触らない。
* **幾何計算は VBA を忠実に移植** … 座標変換・角度・注記位置・octant 距離ラベルまで同じ結果。
* **未解決要素を必ず報告** … ブロックが見つからない要素を一覧表示。「どの表に何を足せばいいか」が即わかる。

---

## 2. ファイル構成

```
skekb_layout/
├── skekb_layout.py        ★ コアライブラリ（パース・幾何計算・スクリプト生成）
├── cli.py                 ★ コマンドライン実行スクリプト（.scr 生成）
├── duct_table.py          Duct_Table(建設用一覧表)を xlsx 書き出し
├── extract_config.py      xlsm から config/ を再生成するユーティリティ
├── README.md              このファイル
└── config/                ★ 編集可能な設定・対応表（{her,ler} はリング別）
    ├── settings.json             幾何パラメータ・レイヤ名・色・挿入書式・クリア設定
    │
    │   ── .scr 生成（cli.py）で使う対応表 ──
    ├── {her,ler}_mag_by_element.json   要素名 → マグネットブロック名（Lattice 由来の正解マップ・最優先）
    ├── {her,ler}_mag_blocks.json       BN → マグネットブロック名（導出フォールバック）
    ├── {her,ler}_mag_overrides.csv     要素名固有の上書き規則（element,expect_bn,result_bn）
    ├── {her,ler}_duct_by_element.json  要素名 → ダクトブロック名リスト（Lattice 由来の正解マップ・最優先）
    ├── {her,ler}_duct_blocks.json      キー → ダクトブロック名リスト（フォールバック）
    ├── {her,ler}_duct_ordered.json     末尾に "_Or" を付ける（オーダー済み＝緑セル）ブロック名一覧
    │
    │   ── Duct_Table 書き出し（duct_table.py）で使う台帳 ──
    ├── {her,ler}_component.json         Component シート（行の背骨。Mag Type/IP,CCG/RP/VSW/GV/設置日 等）
    ├── {her,ler}_ducttype.json          Duct_Type シート（断面/注記/BPM/NEG 等。ダクト名で引く）
    └── {her,ler}_mag_others.json        Mag Others 列（手維持データを行位置で適用）
```

★印のファイルだけ理解すれば運用できます。`config/` は xlsm から
`python extract_config.py <xlsm>` ですべて再生成できます。

---

## 3. 動作環境とインストール

* **Python 3.9 以上**（標準ライブラリのみで動作。追加インストール不要）
* xlsm から対応表を再生成する場合のみ `pip install openpyxl oletools`

AutoCAD は別途必要です（生成した `.scr` を AutoCAD で読み込みます）。
AutoCAD 2027 for Mac と AutoCAD 2026 (Windows) で動作検証済み（前者の方が処理が早かったです）。

---

## 4. 使い方

`cli.py` を実行します。出力ファイル名は `<dispog名>_Mag.scr` / `<dispog名>_Duct.scr`。

```bash
# マグネット・ダクト両方を生成（リングはファイル名から自動推定: sler_*→LER, sher_*→HER）
python cli.py  sler_1802_60_1.dispog  -o ./out

# リング明示 / どちらか一方だけ / 中身確認のみ
python cli.py  some.dispog  --ring HER
python cli.py  some.dispog  --mag-only
python cli.py  some.dispog  --duct-only
python cli.py  some.dispog  --preview

# 実行前に対象リングの既存図形を削除してから描き直す
python cli.py  sler_1802_60_1.dispog  --clear
```

実行すると、結果サマリと **未解決要素**（ブロックが見つからない要素）が表示されます。
あとは AutoCAD のコマンドラインで `SCRIPT` を実行し、生成された `.scr` を指定します。

### コマンドライン / 他プログラムからの利用

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


## 生成される .scr の互換性（Mac / Windows / 日英）

出力スクリプトは、次の工夫により **AutoCAD for Mac / Windows のどちらでも、
また日本語版・英語版のどちらでも**同じファイルがそのまま動くようにしています。

* **改行コードは LF のみ**（`\r` を一切含まない）。旧版は点の区切りに `\r`
  を使っており Mac で挿入や線描画が途中で止まる原因になっていました。
* **挿入は尺度・回転を「点の前」に先付け**：`_-INSERT <ブロック> _S 1 _R <回転角> <座標>` の順。座標を最後に置くことで、点の確定後にプロンプトが出ず余分なトークンがはみ出しません（ヘルプ記載の、配置前に尺度・回転を設定する仕様に準拠）。
* **コマンドは `_` 付き**（`_-INSERT` `_POINT` `_TEXT` `_LINE` `_ZOOM` 等）で
  英語コマンドを強制し、**キーワードを使わず順番で指定**するため、
  日本語版AutoCADでも「r（回転）」等の翻訳に依存しません。
* **オブジェクトスナップは `OSMODE 0`** で解除（コマンドではなくシステム変数を
  使うので言語非依存）。

`config/settings.json` の `insert_command`（既定 `_-INSERT`）と
`insert_scale`（既定 `1 1`）で挿入の書式は調整できます。

## 5. 対応表のメンテナンス（重要）

レポートに **未解決要素** が出たら、その要素のブロックがまだ対応表に
無いということです。次のいずれかで対応します。

### A. マグネットブロックを足す

レポート例: `QKALP  (BN=Q344)` が未解決
→ `config/her_mag_blocks.json`（または ler）に 1 行追加:

```json
{
  "Q344": "Q344E",
  ...
}
```

`"BN" : "AutoCADブロック名"` の形式です。

### B. 要素名ごとの特別扱いを足す

ある要素だけ特殊なブロックを使う場合は
`config/her_mag_overrides.csv` に追記:

```csv
element,expect_bn,result_bn
QTBOTE.1,Q826,Q826new
```

意味: 要素 `QTBOTE.1` の計算上の BN が `Q826` なら `Q826new` に置き換える。
（`expect_bn` を `*` にすると無条件で置き換え）

### C. 幾何パラメータ・レイヤを変える

`config/settings.json` を編集。例えばレイヤ名・色、テキスト高さ、
座標スケール（現状 `OGx × -1000` で mm 化）などを変更できます。

### D. Excel 側で表を更新した場合

最新の xlsm を渡して config を作り直せます:

```bash
python extract_config.py  path/to/SKEKB_Layout_latest.xlsm
```

---





### 実行前のクリア処理（レイヤーのアンロック＋既存図形の削除）の自動化

旧手順では、スクリプト実行前に手作業で「対象リング層のアンロック → Ctrl+A → Del」を
行っていました。これを自動化するオプションを用意しました。

- CLI: `--clear` を付ける（例: `python cli.py sler_1802_60_1.dispog --clear`）
- API: `generate_magnet_script(elements, ring, clear_layers=True)`

有効にすると、スクリプト先頭に次の処理が入ります（すべて言語非依存の `_` 付きコマンド）:
1. 全レイヤをロック（`_-LAYER _LOCK *`）
2. 対象リング（`her*`/`ler*`）と共通レイヤ（`0,1,Base,Chamber,Defpoint,Dim`）をアンロック
3. `_ERASE _ALL` で削除（ロック層の図形は自動除外＝Ctrl+A+Del と同じ挙動）

アンロックする共通レイヤは `config/settings.json` の `clear.unlock_common` で編集できます。

**注意**: これは図面の対象リング図形を削除して描き直す処理です。複数スクリプトを
続けて流す場合、**クリアは最初に実行するスクリプト（通常マグネット）だけに付けて**
ください。ダクト側に付けると、先に描いたマグネットも消えてしまいます。
手順は「マグネット(クリア付き)を実行 → ダクト(クリアなし)を実行」となります。

### マグネット名表示・ダクト角度の リング別調整（重要）

マグネット名の注記とダクトの角度計算は HER と LER で定数・方向が異なります（VBA より）。
本ツールは `config/settings.json` のリング別設定で再現しています。
- 名前ラベルのオフセット方向: HER は X<0→内/X≥0→外、LER は逆（X<0→外/X≥0→内）。
  これを誤るとマグネット名がビームラインの反対側（上下逆）に出ます。
- Q磁石の距離ラベル（例 `(TR148)`）の octant 計算定数もリングで異なります。
- 除外規則: LER は先頭 "D" も除外し、長さ0でも "SK**" は残します。
- 反転要素（先頭 "-"、例 `-QLA7RP`/`-BLA6RP.1`）は、名前表示では "-" を除き、
  Q/B 判定にも除去後の名前を使います。これによりダクトの角度（偏向磁石の
  2個目以降のダクトは「もう一つ前」の角度）が正しく計算され、段差が出ません。
- ダクト角度には X<0,Y≥0 での「角度<-80°なら+180°」補正（日光シケイン）も含みます。

### ダクトブロックの割り当て方法（要素名→ダクト 直接マップ）

旧VBAの `*InsertDuctBlockName` は、要素名から複雑な規則で中間キー(DuctBN)を作り、
ドットの除去や多数の特殊分岐を経て `*_DuctBlock` 表を引いていました（例: 要素
`BLX4LP.1` → キー `BLX4LP1`）。この導出を推測で再現すると取りこぼしが出るため、
本ツールは **Lattice シートが実際に各要素へ割り当てた結果を「要素名→ダクトブロック」の
直接マップとして抽出** し（`config/*_duct_by_element.json`）、要素名でそのまま引きます。

解決の優先順位（`DuctBlockResolver`）:
1. `*_duct_by_element.json`（Lattice 由来の正解マップ。要素名そのもの）
2. `*_duct_blocks.json` を要素名で
3. `*_duct_blocks.json` をドット除去名で（`BLX4LP.1`→`BLX4LP1`）
4. マグネットBNキーで

`config/*_duct_by_element.json` と `*_duct_ordered.json`（_Or 対象）は、最新の
xlsm から `python extract_config.py <xlsm>` で再生成できます。
新しいラティスで未割当の要素が出た場合は、`*_duct_by_element.json` に
`"要素名": ["ダクトブロック名", ...]` を直接追記すれば対応できます。


本ツールはマグネットブロックも同様に、Latticeシート由来の直接マップ `config/*_mag_by_element.json`（要素名→ブロック名）を最優先で引きます。これにより QK系・ZV系ステアリング・BWウィグラー極など、長さベースの導出では取りこぼす特殊磁石も正しく配置されます。未解決が出た要素は `*_mag_by_element.json` に直接追記できます。

### ダクトの「_Or」（オーダー済み）について

旧VBAは、Lattice シートのダクトブロック・セルが**緑色**（オーダー済み＝発注/製作済みの印）の場合、
ブロック名の末尾に `_Or` を付けて挿入していました（例: `D-QKALP` → `D-QKALP_Or`）。
図面側には `_Or` 付きのブロックが定義されているため、本ツールも同じ規則で `_Or` を付けます。

どのブロックがオーダー済みかは `config/her_duct_ordered.json` / `config/ler_duct_ordered.json`
（ブロック名の一覧）で管理しています。オーダー状況が変わったら、この一覧を編集するか、
最新の xlsm から `python extract_config.py <xlsm>` で再生成してください。
特定のダクトだけ `_Or` の有無が図面と食い違う場合は、この一覧でそのブロック名を足し引きすれば直ります。


## 5.5 Duct_Table の書き出し（建設用 一覧表）

旧VBA `Make_HER_Duct_Table` / `Make_LER_Duct_Table` が作っていた一覧表
（`HER_Duct_Table` / `LER_Duct_Table`）を xlsx に書き出します。

```bash
# 両シートを 1 つの xlsx に
python duct_table.py --her sher_5781_60_1.dispog --ler sler_1802_60_1.dispog -o Duct_Table.xlsx

# 片方だけ
python duct_table.py --ler sler_1802_60_1.dispog -o Duct_Table.xlsx
```

### しくみ（Component 駆動）
`*_Component` シートは Duct_Table と **1 行ずつ位置で対応する背骨** です
（HER 1105/1105・LER 1176/1176 行で (Mag名, Duct名) が完全一致することを確認済み）。
本ツールは Component を行の背骨として駆動し、各列を次の源から埋めます。

- Mag Type / BM Note / Q Support / Duct Name / IP,CCG / RP / VSW / GV / Bellows /
  Temp / Flow / 各設置日 / 真空 / NEG-L,R,Act / Length … **Component**（`config/*_component.json`）
- Cross Section / Duct Note / BPM Height / NEG 群 … **Duct_Type**（`config/*_ducttype.json`）を Duct名で
- Loc / S … **dispog** を Mag名で引いて再計算（IR cryostat 部は固定値）
- Room … Mag名で D01..D12、さらに GV 列のセクター標識でサブ表記
  （D01_IRL / D01_STP / …）を付与
- Mag Others … dispog のステアリング系（Z/FZ/FQ/SD/SF/SL）を直前磁石に集約

台帳（Component / Duct_Type）は dispog に無い手維持データです。最新の xlsm が出たら
`python extract_config.py <xlsm>` で全 config を再生成できます。

- Duct Note の "GV": GVセクター境界行(GV列に標識があり、Mag名・ダクト名が空の行)には
  Duct Note に "GV" を立て、その行の全列の上下に太め(thin)罫線を引く(旧マクロの
  GV行挿入＋罫線の再現)。通常行の細いグリッド線とは区別される。

### xlsm 版との一致（実測）
行数は両リングとも完全一致。主要列は IP,CCG / RP / VSW / GV / Mag Type / Mag Others が **100%**、
Cross Section 99〜100%、Room 99%（差は GV 挿入由来のセクション境界 1 行ずれ、いずれも
Mag 名が空の境界行）。Mag Others は手維持データ（旧マクロが手作業の例外で特定行へ振り分け）のため、
Component/Duct_Type と同様に xlsm から抽出して `config/*_mag_others.json` に保存し、
行位置で適用する。全リングで **100% 一致**。
GVセクター境界行(Duct Note="GV")には全列の上下に太め罫線を引いています。

## 6. 旧VBAとの対応（移植元の対照表）

| 新システム | 旧VBA |
|------------|-------|
| `parse_dispog()` | `HER_Lattice` シートへの dispog 貼り付け |
| `MagnetBlockResolver` | `HERInsertMagnetBlockName` / `LERInsertMagnetBlockName` |
| `generate_magnet_script()` | `Make_HER_Mag_ScriptFile` / `Make_LER_Mag_ScriptFile` |
| `DuctBlockResolver` | `HERInsertDuctBlockName` / `LERInsertDuctBlockName` |
| `generate_duct_script()` | `Make_HER_Duct_ScriptFile` / `Make_LER_Duct_ScriptFile` |
| `config/*_blocks.json` | `*_MagBlock` / `*_DuctBlock` シート |
| `config/*_overrides.csv` | VBA 内の `If Cells(i,1)=…` 分岐群 |
| `config/settings.json` | VBA 内の定数（`-1000`, レイヤ名, `pdmode` 等） |

### 移植時に忠実に再現した処理

* 座標変換 `X = OGx × -1000`, `Y = OGy × -1000`（mm 化）
* 区間の角度計算（`atan` ＋ 象限別の +90/-90/+180 補正、鉛直区間の 90/270）
* 偏向磁石（先頭 `B`）で `Value < 0` のときブロックを 180° 反転
* マグネット名注記の位置（区間中点を内外へ ±0.4% オフセット）
* Q 磁石の距離ラベル（`TL/NR/NL/FR/FL/OR/OL/TR` ＋ octant 内距離）
* QC1/QC2 は代表 1 本だけ名前を表示するサンプリング
* 除外規則（先頭 `P` または長さ 0 の要素はスキップ）
* ステアリング `ZV344` の s 位置レンジ別ブロック、Half-Q の `.1～.4`、
  特殊 Q（`QY5`, `QC2`）、ウィグラー `BW` の up/down 判定

---

## 7. 既知の制限・今後の発展

* **ダクト／マグネットブロック名の規則**：旧VBAの `*InsertDuctBlockName` は
  要素名から中間キーを作る規則が極めて多岐・ラティス依存が強いものでした。本実装は
  推測での再現をやめ、**Lattice シートが実際に各要素へ割り当てた結果を「要素名→ブロック」の
  直接マップ**（`config/*_{mag,duct}_by_element.json`）として引く方式にしています。
  これにより現行ラティスの全要素が解決します（未解決 0）。新ラティスで未割当の要素が
  出た場合は、当該 JSON に 1 行追記すれば対応できます。
* **検証は要対象比較で**：本ツールは旧VBAのロジックを移植したものですが、
  実機の図面・既知の正解 `.scr` と照合して確認することを推奨します。
  未解決要素レポートが差分発見の助けになります。
* **発展の方向**
  * `ezdxf` を使って `.scr` を介さず `.dxf` を直接生成（AutoCAD コマンド非依存）
  * 対応表の Web 編集 UI／変更履歴管理（git）
  * 複数 dispog の一括処理・差分レイアウト

---
