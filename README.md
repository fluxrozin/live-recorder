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

## テスト

```bash
uv sync --group dev
uv run pytest
```

実在の TikTok / ツイキャス配信への接続確認は、このリポジトリの自動テストには含まれていません。制限事項は [docs/LIMITATIONS.md](docs/LIMITATIONS.md) にまとめています。

## 構成

`app`（FastAPI）、`worker`（監視と録画）、`transcriber`（音声字幕）、`db`（PostgreSQL）の 4 コンテナです。`transcriber` は NVIDIA GPU を使います。詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。
