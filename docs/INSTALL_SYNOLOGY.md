# Synology DS1823xs+ への導入

対象は DSM 7.2 以降の Container Manager です。この手順は特定の IP アドレスや共有フォルダ名に依存しません。NAS 実機での起動確認は、このリポジトリを作成した環境では行っていません。

x86-64 用のイメージを使います。DS1823xs+ の CPU はこの構成で動作します。

## 1. 共有フォルダ

DSM の「コントロールパネル → 共有フォルダ」で、アプリケーション用と録画用の共有フォルダを作ります。名前は任意です。例として `docker` と `recordings` を使います。

録画用フォルダは、Container Manager を動かすユーザーから書き込める権限にします。

## 2. ディレクトリ

File Station または SSH で、次のディレクトリを作ります。パスは例です。

```text
/volume1/docker/live-recorder/database
/volume1/docker/live-recorder/config
/volume1/recordings
```

`database` は PostgreSQL、`config` は将来のローカル設定、`recordings` は録画ファイルです。

## 3. ファイルの配置

プロジェクト一式を `/volume1/docker/live-recorder/` に置きます。`compose.yaml`、`Dockerfile`、`src`、`alembic` が含まれている必要があります。

Git が使える場合:

```bash
cd /volume1/docker/live-recorder
git clone <このリポジトリのURL> .
```

## 4. .env

```bash
cd /volume1/docker/live-recorder
cp .env.example .env
```

少なくとも次を、例示の文字列ではない値に変えます。

- `POSTGRES_PASSWORD`
- `SESSION_SECRET`（32文字以上の無作為な文字列）
- `DATABASE_DIR`（例: `/volume1/docker/live-recorder/database`）
- `RECORDINGS_DIR`（例: `/volume1/recordings`）
- `CONFIG_DIR`（例: `/volume1/docker/live-recorder/config`）
- `PUID` と `PGID`（録画フォルダへ書き込める UID/GID）

UID/GID は DSM の「ユーザーとグループ」で確認します。共有フォルダの権限がその UID に書き込みを許可している必要があります。

ツイキャスを使う場合は開発者コンソールの Client ID / Secret を入れます。TikTok のコメントと第3録画手段を使う場合は Euler Stream の `SIGN_API_KEY` を入れます。空でも、配信検知と Streamlink による公開配信の録画は試せます。

`WEB_BIND` は LAN で待ち受けるアドレスです。`0.0.0.0` は NAS の全インターフェースです。DSM の管理ポートは公開しないでください。外出先からは Tailscale などの VPN でこの Web ポートへ到達させます。

## 5. Container Manager

パッケージセンターから Container Manager を入れ、NAS 起動時に自動起動する設定にします。

## 6. プロジェクト

Container Manager の「プロジェクト → 作成」で、プロジェクトのパスに `/volume1/docker/live-recorder` を指定します。

## 7. compose.yaml

ソースは同ディレクトリの `compose.yaml` を選択します。Container Manager が `.env` を読みます。

## 8. 起動

プロジェクトをビルドして起動します。初回はイメージのビルドに時間がかかります。`db` が healthy になってから `app` と `worker` がマイグレーションを実行します。

SSH で行う場合:

```bash
cd /volume1/docker/live-recorder
sudo docker compose up -d --build
sudo docker compose ps
```

## 9. 初回管理者

Web 画面に共通の初期パスワードはありません。

```bash
sudo docker compose exec -e ADMIN_USERNAME=admin -e ADMIN_INITIAL_PASSWORD='12文字以上のパスワード' app \
  python -m live_recorder.cli create-admin
```

パスワードをシェル履歴に残したくない場合は `ADMIN_INITIAL_PASSWORD` を付けず、対話入力にします。`exec` が TTY を必要とするときは `-it` を付けます。

## 10. 管理画面

ブラウザで `http://<NASのLANアドレス>:<WEB_PORT>` を開きます。既定ポートは 8080 です。HTTPS で公開する場合は NAS 側のリバースプロキシを使い、`.env` の `COOKIE_SECURE=true` にしてからプロジェクトを再起動します。

## 11. 配信者

「配信者」で TikTok はユーザーID、`@名前`、プロフィール URL、`/live` URL のいずれかを登録します。ツイキャスは screen_id または `https://twitcasting.tv/<id>` です。監視、録画、コメント収集は別々に切れます。

## 12. 録画テスト

公開中の配信者を登録し、概況で「配信中」「録画中」が増えることを確認します。共有フォルダに `.ts` または確定後の `.mp4` ができます。管理画面の履歴からダウンロードし、PC の動画プレイヤーで開きます。画面内では再生しません。

合言葉配信とメンバーシップ配信は録画しません。

## 13. コメントテスト

TikTok は `SIGN_API_KEY` がある状態で、配信詳細のコメント件数が増えることを見ます。キーが無い場合、コメントジョブは失敗することがあり、そのときも録画は継続します。

ツイキャスは API 認証がある状態で、コメント画面に投稿者 ID、表示名、本文、時刻が出ることを確認します。公式 API は直近のコメントをページ単位で返すため、収集開始前の全件は保証しません。

## 14. ログ

Container Manager のコンテナ詳細から `app` と `worker` のログを見ます。ログは JSON で、署名付き URL やトークンはマスクします。Docker の json-file ドライバで 10MB × 5 ファイルにローテーションします。

## 15. バックアップ

止めた状態で次をコピーします。

- PostgreSQL のデータディレクトリ（`DATABASE_DIR`）
- 録画ディレクトリ
- `.env`（秘密情報を含むので暗号化して保管）

稼働中にデータベースだけコピーする場合は `pg_dump` を使います。

```bash
sudo docker compose exec db pg_dump -U recorder live_recorder > live_recorder.sql
```

ユーザー名とデータベース名は `.env` に合わせます。

## 16. アップデート

```bash
cd /volume1/docker/live-recorder
git pull
sudo docker compose up -d --build
```

起動時に Alembic が未適用のマイグレーションを実行します。録画ファイルはボリュームに残ります。

## 17. 障害復旧

NAS を再起動すると、`restart: unless-stopped` によりコンテナが戻ります。ワーカーは起動時に、途中だった録画ファイルを検査し、配信が続いていれば新しいセグメントとして再開します。再起動中に終わった配信は、その区間を録画済みとは記録しません。

ワーカーがすぐ終了する場合は、ログの「別のワーカーが稼働中です」を確認します。古いコンテナが残っていなければ、PostgreSQL がロックを解放したあとに再起動で回復します。

ディスク不足のときは新規録画だけを止めます。自動削除は設定で明示的に有効にするまで動きません。
