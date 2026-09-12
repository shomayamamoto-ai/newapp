# snsauto — SNS運用自動化ツール

市場分析 → 台本 → 絵コンテ → 画像生成 → 映像生成 → 投稿 → 分析 → 改善 → データ蓄積 を
ひとつのパイプラインで一貫実行するツール。TikTok / YouTube / Instagram / X 対応。

```
research ─▶ structure ─▶ script ─▶ storyboard ─▶ narration ─▶ visuals ─▶ video ─▶ publish ─▶ metrics ─▶ A/B ─▶ PDCA ─▶ report
   │           │            │           │            │           │          │         │          │        │       │        │
競合上位50件  構成/テロップ   台本      絵コンテ    TTS＋尺の   3モードで  ワンタッチ 予約投稿   自動収集  勝ち筋  仮説検証 HTML/PDF
   分析         分析                                再調整      映像生成    編集      自動実行            の確定
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

## 映像生成の3モード

各カットの画を、3通りのどれでも作れます。`SNSAUTO_VISUAL_MODE` か `--visual` で選択します。

| モード | 中身 | コスト | 必要なもの |
|---|---|---|---|
| `still` | 生成画像＋Ken Burnsズーム | 最小 | なし（プレースホルダで動作） |
| `animate` | 画像→動画生成AIで実際に動かす | 高 | `VIDEOGEN_ENDPOINT` |
| `footage` | 実写・ストック素材をカットに自動マッチ | ゼロ | `SNSAUTO_FOOTAGE_DIR` |
| `auto` | 設定済みの中で最良のものを自動選択 | — | — |

```bash
snsauto footage index ./clips        # 素材を実尺・解像度つきで索引化
snsauto create video 1 --visual footage --narrate
```

素材のマッチはキーワード重なり・縦横比・尺の収まりで採点します。1本の動画の中で
同じクリップを使い回さず、素材が足りない場合でも**直前のカットとは必ず別のクリップ**を選びます
（カットをまたぐ同一素材は事故に見えるためです）。ファイル名から自動でキーワードを拾い、
`clip.json` を置けば日本語のキーワードも付けられます。

`animate` は Runway / Kling / Veo / Luma いずれも同じ非同期形（POST→ジョブID→ポーリング→DL）
なので、ベンダー固有ではなくその形に対して実装してあります。1カットの生成に失敗しても
そのカットだけ静止画に落ちて、動画全体は完成します（どのカットがどう落ちたかは記録されます）。

---

## ナレーション（TTS）と尺の再調整

`--narrate` で台本の各行を音声合成し、**実際の発話長を測ってカットの尺を組み直します。**

台本の秒数は誰も読み上げる前に決めた見積もりなので、実際の音声とは必ずずれます。
そのまま書き出すとナレーションが途中で切れるか、無音が残ります。

音声トラックは各カットの尺ぴったりに無音を詰めて連結します。発話を詰めて並べると、
尺の長いカットの後でナレーションが前倒しになり、映像と音がずれていきます。

TTSが未設定でも再調整は動きます（1秒7文字の読み上げ速度で見積もり）。

---

## 予約投稿とワーカー

```bash
snsauto worker run                 # 常駐。予約投稿の実行と実績収集
snsauto worker run --once          # 1回だけ実行
```

- **予約投稿**: 時刻が来た投稿を実行します。投稿行はAPI呼び出しの前に排他ロックを取るので、
  ワーカーを2つ動かしても**同じ動画が二重投稿されることはありません**（公開投稿は静かに取り消せないためです）
- **実績収集**: 投稿後 1h → 3h → 6h → 12h → 24h → 48h → 72h → 1週間 と間隔を空けて収集します。
  ショート動画の勝敗は初日でほぼ決まるので、全投稿を毎時ポーリングし続けるのはAPI枠の無駄です

---

## A/Bテスト

```bash
snsauto ab create mybrand 12 --dimension hook --arms 3
snsauto ab attach 1 B 45 46 47      # 案Bに投稿を紐づけ
snsauto ab review 1
```

**1回のテストで変える条件は1つだけ**です（`hook` / `telop_density` / `duration` / `cta` / `hashtags`）。
フックも尺もタグも同時に変えたテストは「この動画が伸びた」しか教えてくれず、
次に何を再現すればいいのか分かりません。案Aは常に対照群（元の台本そのまま）です。

勝者判定は保守的です。同じ内容の動画でも実績は3割くらい平気でばらつくので、
**各案が最低3投稿あり、かつ差が案の内部のばらつきより大きいとき**にだけ勝ちを宣言します。
それ以外は理由を添えて `inconclusive` を返します。

---

## アカウント連携と自動投稿

各SNSの**公式API**でアカウントを接続します。接続後はそのアカウントとして自動投稿できます。
スクレイピングや自動操作は一切行っていないため、規約の範囲内で運用できます。

```bash
snsauto serve      # → /accounts から各SNSを接続
```

| プラットフォーム | トークンの寿命 | 自動更新 | 投稿上限 |
|---|---|:--:|---|
| YouTube | アクセス約1時間／リフレッシュは無期限 | ✅ | APIクォータ単位（本数ではない） |
| TikTok | アクセス24時間／リフレッシュ365日 | ✅ | 1日25本・アップロード開始は毎分6回 |
| Instagram | 長期トークン60日（更新して延長） | ✅ | API側に問い合わせて残量を確認 |
| X | OAuth 1.0a（期限なし） | 不要 | プラン依存 |

接続に必要なアプリ資格情報:

```ini
YOUTUBE_CLIENT_ID= / YOUTUBE_CLIENT_SECRET=      # Google Cloud
TIKTOK_CLIENT_KEY= / TIKTOK_CLIENT_SECRET=       # TikTok for Developers
FACEBOOK_APP_ID=  / FACEBOOK_APP_SECRET=         # Instagram は Facebook アプリ経由
X_API_KEY=        / X_API_SECRET=                # X Developer Portal
SNSAUTO_PUBLIC_BASE_URL=https://...              # 承認後の戻り先（各社に登録する）
```

**トークンは自動更新されます。** ワーカーが期限の6時間前に更新するので、常時稼働でも
止まりません。TikTokは24時間で失効するため、これが無いと翌日には投稿できなくなります。
更新に失敗した場合はアラートとメールで通知されます。

**投稿上限はアプリ側でも数えています。** 上限に達した投稿は失敗ではなく「延期」として
予約状態のまま残り、枠が空いた次のtickで自動的に再試行されます。上限に当たってから
リトライを繰り返すと、その日の枠をエラーで使い切ってしまうためです。
Instagramについては、ドキュメントの数字（25／50／100）が食い違うため、
`content_publishing_limit` でアカウント自身の残量を問い合わせます。

### 複数アカウントの運用

1つのプラットフォームに複数アカウントを接続できます。プロジェクト単位でも、
全プロジェクト共通でも紐づけられます。

```bash
snsauto account list                    # 接続中のアカウントと期限
snsauto account limits                  # 投稿枠の消費状況
snsauto create all ブランド "キーワード" --publish tiktok        # tiktokの全アカウントに投稿
snsauto create all ブランド "キーワード" --account 1 --account 4  # 指定アカウントだけに投稿
```

Web UIでは投稿先をアカウント単位のチェックボックスで選びます。
上限に達したアカウントとトークンが失効したアカウントは選択できない状態で表示されます。

**投稿枠はアカウントごとに数えます。** 1つのアカウントが上限に達しても、
他のアカウントへの投稿は止まりません。実績の収集も、投稿したアカウントのトークンで行います
（別のアカウントのトークンでは同じ投稿IDが「見つからない」と返ってくるためです）。

---

## 公開ストレージ（Instagram投稿に必須）

Instagram は動画のバイト列を受け取らず、**こちらが渡したURLを Meta のサーバーが取りに来ます**。
つまり公開HTTPS URL が無いと投稿できません。2方式を用意しています。

```ini
# S3互換（AWS S3 / Cloudflare R2 / MinIO / Wasabi）
STORAGE_BACKEND=s3
S3_BUCKET=my-bucket
S3_ENDPOINT_URL=https://xxx.r2.cloudflarestorage.com   # AWSなら空
S3_ACCESS_KEY=... 
S3_SECRET_KEY=...
S3_PUBLIC_BASE_URL=https://cdn.example.com   # 公開バケット/CDNなら恒久リンク。空なら署名付き期限リンク

# 自前サーバーから配信
STORAGE_BACKEND=local
SNSAUTO_PUBLIC_BASE_URL=https://snsauto.example.com
```

`local` は追加契約が不要な代わりに、**localhost や自己署名証明書では動きません**
（Meta 側から到達できる必要があるため）。だから `SNSAUTO_PUBLIC_BASE_URL` は推測せず必須にしています。

ファイルは内容のハッシュをキーにするので、同じ動画を再アップロードしても重複しません。

---

## 失敗通知

無人運転は静かに壊れます。トークンが失効したワーカーは毎分リトライを続け、
その週の投稿が丸ごと無いことに後から気づく、というのが典型です。

```ini
ALERT_EMAIL_TO=ops@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
```

投稿失敗・トークン失効・生成エラーは、**ダッシュボードの「要対応」** と **メール** の両方に出ます。

同じ失敗の繰り返しは1行にまとめ、回数だけ増やします（同じメールが60通来るのは0通と同じです）。
メールは新規に開いたときだけ送り、「対応済み」にした後で再発したら改めて送ります。
再発は新しい情報だからです。

---

## Web UI

```bash
pip install -e ".[web]"
snsauto serve                 # http://127.0.0.1:8000
```

ブラウザから閲覧・実行できます。

| 画面 | 内容 |
|---|---|
| ダッシュボード | 全プロジェクト横断の集計、最近の調査履歴 |
| 接続状況 | どのプラットフォームで何ができるか、ローカル環境（ffmpeg/Chromium/LLM）の状態 |
| プロジェクト | 調査・台本・動画・投稿・PDCAの一覧と実績サマリ |
| 調査詳細 | 上位50件のランキング、フック類型の分布、頻出タグ・語 |
| 台本詳細 | ビート表、絵コンテのカット一覧、**動画プレイヤー**、ダウンロード |
| PDCA詳細 | 仮説・目標・実測・判定・次アクション |
| A/Bテスト | 各案の変更内容・実績・勝者判定 |
| 実行履歴 | Webから実行した処理の進行状況（自動更新） |
| ブランド設定 | 語り手・トーン・NGワード・必須注記。全生成に反映されます |
| 要対応（ダッシュボード） | 投稿失敗・トークン失効などの未対応アラート |

ライトとダークの両方に対応します（OSの設定に追従）。
`/api/projects` `/api/runs/{id}` `/api/capabilities` でJSONも返します（`/api/docs` にOpenAPI）。

### 認証（サーバー公開時は必須）

DBにはSNSのアクセストークンが入り、UIからは課金の発生する生成と公開投稿ができます。
localhost 以外に出すなら認証を必ず有効にしてください。

```bash
snsauto user secret                          # SNSAUTO_SECRET_KEY 用の鍵を生成
export SNSAUTO_AUTH_ENABLED=true SNSAUTO_SECRET_KEY=...
snsauto user create you@example.com --role admin
```

| 権限 | 閲覧 | 調査・生成 | 投稿 |
|---|:--:|:--:|:--:|
| `viewer` | ✅ | — | — |
| `editor` | ✅ | ✅ | — |
| `admin` | ✅ | ✅ | ✅ |

パスワードは scrypt（標準ライブラリ）で保存、セッションはHMAC署名クッキー（サーバー再起動で
ログアウトされず、セッションテーブルも不要）、POSTは全てCSRFトークンを検証します。
ログイン失敗時は「メールアドレスまたはパスワードが違います」の一文だけを返し、
存在しないアカウントと同じ応答時間になるようにしてあります（アカウント列挙を防ぐため）。

### 実行と安全弁

調査・台本生成・動画生成・A/Bテスト作成・実績収集はブラウザから実行できます。
時間のかかる処理はジョブ行に積んでバックグラウンドで走らせ、UIは進行状況をポーリングします
（リクエスト内で走らせると、ブラウザがタイムアウトし、再読み込みで二重実行されるためです）。

**公開投稿だけは確認欄に `PUBLISH` と入力しないと実行できません。** 管理者権限も必要です。
取り消せない操作をワンクリックに置くのは妥当ではないためです。

動画と画像はDBに記録されたIDから解決して配信します。ワークスペースを静的ディレクトリとして
公開していないのは、細工したパスでディレクトリ外に出られるのを防ぐためです。

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

## 配備

常時稼働サーバー向けに Docker Compose と systemd の両方を用意しています。
手順は [`deploy/README.md`](deploy/README.md) にあります。

```bash
docker compose up -d --build
docker compose exec web snsauto user create you@example.com --role admin
```

Web と worker は別プロセスです。**worker を止めると予約投稿と実績収集が止まりますが、
Web は動き続けるので気づきにくい**点に注意してください。

Nginx 設定（`deploy/nginx.conf`）は TLS 終端、`X-Forwarded-Proto` の受け渡し、
レンダリング待ちに耐える 600 秒のタイムアウト、大きな動画のための `client_max_body_size 512M`
を含みます。

### スキーマ移行

起動時に自動でマイグレーションが走ります（Alembic）。

```bash
snsauto db current      # 適用済みリビジョン
snsauto db upgrade      # 最新まで適用
snsauto db revision -m "add column"   # モデル変更から自動生成
```

`create_all` ではなくマイグレーションにしているのは、**既存テーブルへの列追加ができない**ためです。
蓄積したデータを持つ常時稼働の環境では、スキーマ変更のたびに壊れます。
移行前に作られたDB（バージョン印の無いもの）は、テーブルを作り直さず印を打つだけで取り込みます。

---

## 開発

```bash
python -m pytest                    # 380 tests
python -m pytest -m "not slow"      # ffmpeg/ブラウザを使わない分だけ
```

ffmpegはPATH → `FFMPEG_BINARY` → `imageio-ffmpeg` 同梱バイナリの順で解決するため、
システムにffmpegが無い環境でも動作します。

## ライセンス

未設定。
