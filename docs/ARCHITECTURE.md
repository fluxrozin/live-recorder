# 構成

ブラウザは `app` だけに接続します。`worker` と `app` は PostgreSQL を共有し、録画ファイルは同じボリュームを見ます。Redis や Celery は使いません。

```text
Browser
  -> app (FastAPI / Jinja2 / HTMX)
       -> PostgreSQL
  worker
       -> PostgreSQL
       -> Recordings volume
       -> TikTokLive is_live / CommentEvent
       -> Streamlink / yt-dlp / FFmpeg
       -> TwitCasting API v2
```

ワーカーは PostgreSQL のアドバイザリロックを1つ保持し、コンテナを二重起動しても録画が重ならないようにします。プロセスが落ちるとロックは解放されます。

## ディレクトリ

- `src/live_recorder/platforms` — ユーザーIDの正規化、配信状態、コメント変換
- `src/live_recorder/recording` — 外部コマンド、ファイル名、確定、容量、照合
- `src/live_recorder/comments` — 重複排除、時刻オフセット、書き出し
- `src/live_recorder/monitor` — 監視判断、録画ジョブ、コメントジョブ、復旧
- `src/live_recorder/web` — 認証付き管理画面
- `alembic` — スキーマ初期化

## 状態

録画は `PENDING → STARTING → RECORDING` です。切断後は `RECONNECTING` から新しいセグメントを開き、終了時は `FINALIZING` を経て `COMPLETED`、`PARTIAL`、`FAILED`、`CANCELLED` のいずれかになります。

コメントは `PENDING → CONNECTING → COLLECTING` です。切断は `RECONNECTING`、欠損があれば `PARTIAL`、設定オフは `DISABLED` です。

配信状態が取得できないときは `OFFLINE` にせず `ERROR` または `UNKNOWN` のままにします。その間、開いている配信は終了扱いしません。

## 録画とコメントの分離

配信者ごとに録画タスクとコメントタスクを別々に起動します。片方が例外で終わっても、もう一方のタスクはキャンセルしません。合言葉配信とメンバーシップ配信は、公式レスポンスの `is_protected` / `is_membership` を見て両方とも開始しません。

## 保存

内部時刻は UTC です。画面、書き出し、ファイル名の日付は表示タイムゾーン（既定 `Asia/Tokyo`）です。プラットフォームが開始時刻を返さない場合、`platform_started_at` は NULL のままです。

録画はまず MPEG-TS に書き、ffprobe で確認してから `-c copy` で MP4 に詰め直します。詰め直しに失敗したファイルは、再生できる元の形式のまま残します。セグメントは結合しません。

`recording_offset_ms` は録画開始時刻が分かっているときだけ計算します。開始より前のコメントは負の値です。録画が始まっていなければ NULL です。

## 外部サービス

TikTok の配信有無 (`is_live`) は署名サービスを使いません。コメント受信と、第3手段の TikTokLive 録画は、ライブラリ既定の Euler Stream (`https://api.eulerstream.com`) へ署名要求を送ります。送るのはルーム解決に必要な識別子と User-Agent、設定している場合の API キーです。`sessionid` は送りません。CAPTCHA 解決 API は呼びません。

ツイキャスは公式 API です。Client ID と Client Secret の Basic 認証、または Bearer トークンを使います。パスワード配信の合言葉は渡しません。
