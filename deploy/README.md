# 配備手順

Web と worker の2プロセス構成です。worker を止めると予約投稿と実績収集が止まります
（Web は動き続けるので、止まっていることに気づきにくい点に注意してください）。

どちらの方式でも、最初に必ず次の2つを済ませてください。

```bash
cp .env.example .env
python -c "import secrets;print(secrets.token_urlsafe(48))"   # SNSAUTO_SECRET_KEY に設定
```

`.env` で最低限必要なもの:

```ini
SNSAUTO_AUTH_ENABLED=true
SNSAUTO_SECRET_KEY=<上で生成した値>
SNSAUTO_PUBLIC_BASE_URL=https://snsauto.example.com
ANTHROPIC_API_KEY=<Claudeの鍵>
```

---

## A. Docker Compose

```bash
mkdir -p clips                      # 実写素材を置く場所（無くても動きます）
docker compose up -d --build
docker compose exec web snsauto user create you@example.com --role admin
docker compose logs -f worker
```

- データは名前付きボリューム `snsauto-data` に入ります（DB・レンダリング結果・レポート）
- `./clips` は読み取り専用でマウントします。索引化は `docker compose exec web snsauto footage index /clips`
- 8000番は **127.0.0.1 にだけ** 公開されます。外に出すには下の Nginx を前段に置いてください

バックアップは、このボリュームを丸ごと取れば足ります。

```bash
docker run --rm -v snsauto-data:/data -v "$PWD:/backup" alpine \
  tar czf /backup/snsauto-$(date +%F).tar.gz -C /data .
```

---

## B. systemd + Nginx

```bash
sudo useradd --system --home /opt/snsauto --shell /usr/sbin/nologin snsauto
sudo mkdir -p /opt/snsauto/data && sudo chown -R snsauto:snsauto /opt/snsauto

sudo apt install -y python3-venv ffmpeg fonts-noto-cjk nginx \
    tesseract-ocr tesseract-ocr-jpn tesseract-ocr-jpn-vert
sudo -u snsauto python3 -m venv /opt/snsauto/.venv
sudo -u snsauto /opt/snsauto/.venv/bin/pip install "snsauto[llm,web,pdf,storage] @ ."
sudo -u snsauto /opt/snsauto/.venv/bin/playwright install chromium
```

データ（DBと作業フォルダ）は `/opt/snsauto/data` に置かれます。場所は systemd
ユニットの `Environment=` で指定しているので、**`.env` に `SNSAUTO_DB_URL` と
`SNSAUTO_WORKSPACE` を書かないでください**。systemd では `.env` の値がユニットの
指定を上書きし、`ProtectSystem=strict` のため書き込めない場所を指して起動できなく
なります（`.env.example` では両方ともコメントにしてあります）。

`tesseract-ocr-jpn` は競合動画のテロップを読むために必要です。
入れない場合、テロップ解析は「未測定」と表示され、他の機能はそのまま動きます。

```bash

sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now snsauto-web snsauto-worker

sudo cp deploy/nginx.conf /etc/nginx/sites-available/snsauto
sudo ln -s /etc/nginx/sites-available/snsauto /etc/nginx/sites-enabled/
sudo certbot --nginx -d snsauto.example.com    # 証明書はここで入ります
sudo nginx -t && sudo systemctl reload nginx
```

`.env` は `/opt/snsauto/.env` に置き、`chmod 600` にしてください
（SNSのアクセストークンとAPIキーが入ります）。

状態確認:

```bash
systemctl status snsauto-web snsauto-worker
journalctl -u snsauto-worker -f
```

---

## スキーマの更新

アプリ起動時に自動でマイグレーションが走ります。手動なら:

```bash
snsauto db current      # いま適用されているリビジョン
snsauto db upgrade      # 最新まで適用
```

移行前に作られたデータベース（バージョン印の無いもの）は、テーブルを作り直さず
印を打つだけで取り込みます。既存データは失われません。

---

## 動作確認

```bash
snsauto doctor          # 各プラットフォーム・ffmpeg・Chromium・LLM・ストレージの状態
curl -fsS https://snsauto.example.com/healthz

# web と worker が同じDBを見ていること（systemd の場合）
journalctl -u snsauto-web -u snsauto-worker | grep -E "DB |database:"
```

最後の確認は必ず行ってください。2行のパスが違えば、Web で予約した投稿は worker の
見ているDBに無いため、**エラーも出ずに永遠に投稿されません**。管理者のシェルで
`snsauto doctor` を打っても、サービスとは環境変数が違うため、この確認にはなりません。

Web の「接続状況」画面に同じ内容が出ます。

---

## 気をつける点

- **web と worker は同じDBを見ている必要があります。** DBの既定の場所は起動した
  フォルダからの相対パスなので、別々の場所から起動すると別のDBになります。
  どちらも起動時にDBの場所を表示し、worker は見ているDBにプロジェクトが無ければ
  警告します
- **worker の生存監視をしてください。** 落ちると予約投稿が静かに止まります。
  `systemctl` と Docker の `restart: unless-stopped` で自動再起動はしますが、
  連続で落ちる状態には気づけません
- **`ALERT_EMAIL_TO` と `SMTP_HOST` を設定してください。** 投稿失敗・トークン失効は
  メールとダッシュボードの「要対応」に出ます。設定しないと画面を見るまで分かりません
- **SNSのアクセストークンには期限があります。** Instagram の長期トークンは約60日、
  TikTok は更新が必要です。失効すると投稿失敗としてアラートが上がります
- **`SNSAUTO_COOKIE_SECURE=true` のままにしてください。** HTTPS 前提の設定です。
  HTTP で試すときだけ false にし、本番では必ず戻してください
