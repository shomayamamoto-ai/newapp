# snsauto — SNS運用自動化ツール

市場分析 → 台本 → 絵コンテ → 画像生成 → 映像生成 → 投稿 → 分析 → 改善 → データ蓄積 を
ひとつのパイプラインで一貫実行するツール。TikTok / YouTube / Instagram / X 対応。

```
research ─▶ structure ─▶ script ─▶ storyboard ─▶ images ─▶ video ─▶ publish ─▶ metrics ─▶ PDCA ─▶ report
   │           │            │           │           │         │         │          │         │        │
競合上位50件  構成/テロップ   台本      絵コンテ    画像生成  ワンタッチ  自動投稿   実績収集  仮説検証  HTML/PDF
   分析         分析                                          編集
```

各ステージは個別に実行でき、出力はすべてDBに永続化されるため、途中から再開できます。

---

## クイックスタート

```bash
pip install -e ".[llm,pdf]"
cp .env.example .env          # 認証情報を記入（無くても動きます）

snsauto init                  # DBとワークスペースを作成
snsauto doctor                # 今このインストールで何ができるかを表示
snsauto project create mybrand --description "副業系ショート動画"

# 全工程を一括実行（--live を付けるまで投稿はドライラン）
snsauto create all mybrand "副業の始め方" --platform youtube --duration 24
```

`create all` は競合分析→台本→絵コンテ→画像→動画→レポートまでを実行し、
成果物のパスをJSONで返します。

---

## 認証情報が無くても動く範囲

`ANTHROPIC_API_KEY` もSNSの認証情報も無い状態で、以下は**そのまま動きます**：

| 機能 | 状態 |
|---|---|
| 競合データのCSV取り込み・スコアリング・上位ランキング | ✅ |
| フック分類 / CTA検出 / テロップ密度 / ビート推定 | ✅ ヒューリスティック |
| カット検出（ローカル動画ファイルから編集リズムを計測） | ✅ ffmpeg |
| 台本・絵コンテの**構造**生成（尺配分・ビート数・タグ） | ✅ 文面は要人手 |
| 画像生成（プレースホルダ） | ✅ 尺とテロップ可読性の検証用 |
| 動画レンダリング（Ken Burns・テロップ焼き込み・BGM合成） | ✅ |
| HTML/CSSテンプレート・PDFレポート | ✅ |
| PDCA管理・判定・データ蓄積 | ✅ |

`ANTHROPIC_API_KEY` を入れると、台本・絵コンテ・構成分析・改善提案がClaudeによる生成に切り替わります
（構造化出力を使用、モデル既定は `claude-opus-5`）。キーが無い場合は自動でヒューリスティックに退避し、
**パイプラインは止まりません**。

---

## プラットフォーム対応の実際

各プラットフォームは「検索」「投稿」「実績取得」を**別々に**ゲートします。実APIがそうなっているためです。

| | 検索 | 投稿 | 実績取得 | 必要なもの |
|---|---|---|---|---|
| **YouTube** | ✅ | ✅ | ✅ | 検索はAPIキーのみ。投稿はOAuth2（`youtube.upload`スコープ） |
| **X** | ✅ | ✅ | ✅ | 検索はBearer。投稿はOAuth 1.0a（v1.1チャンクアップロード + v2 tweets） |
| **Instagram** | △ | ✅ | ✅ | Business/Creator + Facebookアプリ審査 |
| **TikTok** | ❌ | ✅ | ✅ | Content Posting API。アプリ審査前は投稿が `SELF_ONLY` に強制されます |

### 正直に伝えるべき制約

- **TikTokにキーワード検索の公開APIは存在しません。** Research APIは承認された学術機関限定です。
  スクレイピングは利用規約違反なので、このツールは実装していません。代わりに
  `snsauto research import <csv>` で、手動収集またはデータベンダーから取得したCSVを取り込みます。
- **Instagramの検索はハッシュタグ検索のみ**（`ig_hashtag_search` → `top_media`）で、返却件数に上限があります。
- **Instagramの投稿は公開HTTPS URLが必須です。** Graph APIが動画を取りに来る仕様で、
  バイト列を直接POSTできません。自前のストレージにアップロード後、
  `extra={"video_url": ...}` でURLを渡してください。
- **Xの検索は有料ティアが必要**です（コードは完全実装済み）。

`snsauto doctor` が、いま実際に使える能力を表で出します。

---

## 主要コマンド

```bash
# 競合調査（上位50件を収集・スコアリング・構成分析）
snsauto research run mybrand "副業 始め方" --platform youtube --limit 50
snsauto research import mybrand "副業" competitors.csv --platform tiktok

# 台本・動画
snsauto create script mybrand "副業の始め方" --duration 30 --run 1
snsauto create video 1 --bgm bgm.mp3 --style "cinematic, warm light"

# 実績収集
snsauto metrics collect --project mybrand

# PDCA
snsauto pdca plan mybrand "フック改善" -h "疑問形フックで維持率が上がる" --target 0.06 -a "冒頭3秒を疑問形に"
snsauto pdca attach 1 12 13 14
snsauto pdca review 1

# レポート
snsauto report research 1
snsauto report performance mybrand
snsauto report pdca 1

# テンプレート
snsauto template list
snsauto template eject research.html.j2   # 編集用にワークスペースへコピー
```

---

## 設計上の判断

**ランキングは再生数順ではありません。** フォロワー1000万のアカウントの200万再生は
「その構成が効く」証拠になりません。エンゲージメント率(55%)・伸び速度(27%)・到達(18%)の
合成スコアで並べ替えます。エンゲージメント重みは他2つの合計より**厳密に大きく**設定してあり
（コード内で `assert` 済み）、大アカウントの弱い投稿が小アカウントの強い投稿と同点になる事態を防いでいます。
レポートの重み表記はコードから渡されるので、数値がずれることはありません。

**テロップはASS字幕で焼き込みます。** SRTではなく縁取り・影・整列・セーフエリアを個別制御できるためです。
セーフエリアはプラットフォーム毎に変えています（TikTokは右側のアクションレールと下部キャプションで
画面が大きく隠れるため、marginを厚く取る）。日本語には空白が無いので、
禁則処理付きの文字幅ベース折り返しを実装しています。フォントはfontconfigから
実在するCJKフォントを解決します（未インストールのファミリを指定すると豆腐になるため）。

**PDCAはn=1で成功を宣言しません。** 3投稿未満は `inconclusive` を返し、
必要なサンプル数を明示します。ソーシャルの分散を考えれば、1本のヒットは仮説の証明になりません。

**スコアの正規化はlog圧縮してからmin-max**します。ソーシャル指標は裾が極端に重く、
素のmin-maxだと1本のバズが他全部を0付近に潰してしまいます。

---

## テンプレートのカスタマイズ

`<workspace>/templates/` に同名ファイルを置くと、組み込みテンプレートを上書きします。
パッケージには一切触れずにブランド全体のスタイルを変えられます。

```bash
snsauto template eject base.html.j2
# workspace/templates/base.html.j2 の --brand や --font を編集
```

CSS変数（`--brand`, `--ink`, `--font`）とJinjaコンテキスト（`brand_color`, `font_stack`）で
色とフォントを差し替えられます。PDFはChromiumで印刷CSSを適用して出力するため、
画面表示と印刷結果が一致します。

---

## 開発

```bash
python -m pytest                    # 96 tests
python -m pytest -m "not slow"      # ffmpeg/ブラウザを使わない分だけ
```

ffmpegはPATH → `FFMPEG_BINARY` → `imageio-ffmpeg` 同梱バイナリの順で解決するため、
システムにffmpegが無い環境でも動作します。

## ライセンス

未設定。
