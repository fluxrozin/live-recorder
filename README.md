# ライブ配信自動録画

TikTok LIVE とツイキャスの公開配信を監視し、録画と視聴者コメントを別々に保存する Web アプリケーションです。ブラウザで動画は再生しません。録画ファイルはディスクへ保存し、管理画面からダウンロードします。

## できること

- 配信者の登録、監視、録画、コメント収集の個別オンオフ
- 配信者一覧の並び。デフォルトはプラットフォームと表示名。カスタムはドラッグした順
- 配信開始と終了の検知
- Streamlink、yt-dlp、TikTokLive + FFmpeg による TikTok 録画
- ツイキャス公式 API の `hls_url` を優先した録画
- コメントの検索と、録画に合わせた SRT 字幕の書き出し
- 確定した録画の音声字幕と、音声とコメントを一つにした ASS
- ホスト再起動後の監視再開と、録画中断区間の記録

## 起動

```bash
cp .env.example .env
# SESSION_SECRET と POSTGRES_PASSWORD を置き換える
docker compose up -d --build
docker compose exec app python -m live_recorder.cli create-admin
```

ブラウザで `http://<ホスト>:<WEB_PORT>` を開きます。

## Windows のタスクトレイ

管理画面はブラウザのままです。トレイアプリは WSL 上の Docker Compose を起動・停止します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File windows\tray\build.ps1
```

`windows\tray\build` に `LiveRecorderTray.exe` ができます。このフォルダを Windows 側の任意の場所へ置きます。プロジェクトのファイルと `.env` は WSL 内の `project` に残します。

exe と同じフォルダへ [windows/tray/tray.example.json](windows/tray/tray.example.json) を `tray.json` としてコピーし、`distro` と `project` を合わせます。`distro` は WSL のディストリビューション名、`project` はディストリビューション内のプロジェクトパスです。管理画面のポートは、そのディレクトリの `.env` にある `WEB_PORT` です。無いときや空のときは 8080 です。

`LiveRecorderTray.exe` には発行元の署名がありません。起動時に SmartScreen が「Windows によって PC が保護されました」と出すことがあります。プロパティ下部に「許可する」があればオンにします。ダイアログでは「詳細情報」を開き、「実行」を選びます。同じ exe では、その後は表示されません。

トレイを開くと `docker compose up -d` を実行します。Docker がまだ受け付けないときは、5 秒ごとの確認で起動をやり直します。この自動のやり直しでは、「起動に失敗しました」は最初の1回だけ出ます。「起動」は同じコマンドで、失敗のたびにバルーンを出します。「停止」は `docker compose stop` で、トレイは残ります。やり直しは「停止」でやめられます。「再構築」は `docker compose up -d --build` です。失敗しても自動ではやり直しません。待ちは 30 分です。録画中のセグメントは、停止や再構築でコンテナが止まると、ワーカーが最大 20 秒で区切り、コンテナの停止待ちは 30 秒です。再構築と「停止して終了」は、録画中なら確認してから実行します。「停止して終了」は `docker compose down` が終わってからアイコンを消します。データベースと録画ファイルは残ります。プロセスを強制終了したときは、コンテナは動き続けます。

Windows を再起動すると、トレイは消え、WSL ごとコンテナも止まります。終了処理は走らないので、録画中のファイルは区切りを待たずに切れます。データベースと録画ファイルは残ります。右クリックの「Windows起動時に自動実行」がオフのとき、再ログイン後はトレイもコンテナも起動しません。exe を開くと `docker compose up -d` が走ります。チェックを入れると、このユーザーのログオン時に同じ exe が起動し、そこから同じ起動を行います。外すと、その登録は消えます。登録されるのはこの exe のパスです。別の場所へ移したあとは、移した exe で入れ直します。ログオン直後に Docker が遅れるときは、上と同じく最初の失敗だけバルーンが出て、5 秒ごとにやり直します。ワーカーが戻ると、未完了の録画は `RECONNECTING` になり、中断区間を `worker_restart` として残します。開いたままのセグメントはファイルがあれば確定し、配信が続いていれば新しいセグメントで再開します。

アイコンは管理画面の favicon と同じ図形です。待機中は青、録画中は赤、停止中は灰色、起動中と停止処理中は琥珀です。録画中かどうかは `/healthz` の `recording` を見ます。赤へ替えるには、「再構築」でそのフィールドを含む app を入れます。

## テスト

```bash
uv sync --group dev
uv run pytest
```

実在の TikTok / ツイキャス配信への接続確認は、このリポジトリの自動テストには含まれていません。制限事項は [docs/LIMITATIONS.md](docs/LIMITATIONS.md) にまとめています。

## 構成

`app`（FastAPI）、`worker`（監視と録画）、`transcriber`（音声字幕）、`db`（PostgreSQL）の 4 コンテナです。`transcriber` は NVIDIA GPU を使います。詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。
