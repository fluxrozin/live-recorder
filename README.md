# ライブ配信自動録画

TikTok LIVE とツイキャスの公開配信を監視し、録画と視聴者コメントを別々に保存する Web アプリケーションです。ブラウザで動画は再生しません。録画ファイルはディスクへ保存し、管理画面からダウンロードします。

## できること

- 配信者の登録、監視、録画、コメント収集の個別オンオフ
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

`windows\tray\build\LiveRecorderTray.exe` ができます。同じフォルダに `tray.json` を置きます。項目は [windows/tray/tray.example.json](windows/tray/tray.example.json) のとおりで、`distro` は WSL のディストリビューション名、`project` はディストリビューション内のプロジェクトパス、`web_port` は `.env` の `WEB_PORT` です。

「起動」は `docker compose up -d`、「停止」は `docker compose stop` です。停止すると、録画中のセグメントはワーカーが最大 30 秒で区切ります。「トレイを終了」はアイコンだけを消し、コンテナは動いたままです。

アイコンは管理画面の favicon と同じ図形です。待機中は青、録画中は赤、停止中は灰色です。録画中かどうかは `/healthz` の `recording` を見ます。赤へ替えるには、そのフィールドを含む app を一度 `docker compose up -d --build` で入れます。

## テスト

```bash
uv sync --group dev
uv run pytest
```

実在の TikTok / ツイキャス配信への接続確認は、このリポジトリの自動テストには含まれていません。制限事項は [docs/LIMITATIONS.md](docs/LIMITATIONS.md) にまとめています。

## 構成

`app`（FastAPI）、`worker`（監視と録画）、`transcriber`（音声字幕）、`db`（PostgreSQL）の 4 コンテナです。`transcriber` は NVIDIA GPU を使います。詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。
