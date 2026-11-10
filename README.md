# ライブ配信自動録画

TikTok LIVE とツイキャスの公開配信を監視し、録画と視聴者コメントを別々に保存する、自宅 NAS 向けの Web アプリケーションです。ブラウザで動画は再生しません。録画ファイルは共有フォルダへ保存し、管理画面からダウンロードします。

## できること

- 配信者の登録、監視、録画、コメント収集の個別オンオフ
- 配信開始と終了の検知
- Streamlink、yt-dlp、TikTokLive + FFmpeg による TikTok 録画
- ツイキャス公式 API の `hls_url` を優先した録画
- コメントの検索と、録画に合わせた SRT 字幕の書き出し
- NAS 再起動後の監視再開と、録画中断区間の記録

## 起動

```bash
cp .env.example .env
# SESSION_SECRET と POSTGRES_PASSWORD を置き換える
docker compose up -d --build
docker compose exec app python -m live_recorder.cli create-admin
```

ブラウザで `http://<ホスト>:<WEB_PORT>` を開きます。Synology への導入は [docs/INSTALL_SYNOLOGY.md](docs/INSTALL_SYNOLOGY.md) を見てください。

## テスト

```bash
uv sync --group dev
uv run pytest
```

実在の TikTok / ツイキャス配信への接続確認は、このリポジトリの自動テストには含まれていません。制限事項は [docs/LIMITATIONS.md](docs/LIMITATIONS.md) にまとめています。

## 構成

`app`（FastAPI）、`worker`（監視と録画）、`db`（PostgreSQL）の 3 コンテナです。詳細は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) を参照してください。
