# 設定

秘密情報は環境変数だけに置きます。データベースの `system_settings` には入れません。

## 環境変数

| 変数 | 説明 |
| --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | データベース認証 |
| `POSTGRES_HOST` | コンテナ内では `db`。compose が設定します |
| `DATABASE_URL` | 設定すると上記より優先。テスト用 |
| `DATABASE_DIR` | ホスト側の PostgreSQL データディレクトリ |
| `RECORDINGS_DIR` | ホスト側の録画ディレクトリ。コンテナ内では `/data/recordings` |
| `CONFIG_DIR` | ホスト側の設定ディレクトリ。コンテナ内では `/data/config`。音声字幕のモデルを置きます |
| `SESSION_SECRET` | 32文字以上。例示の値では起動しません |
| `WEB_BIND` / `WEB_PORT` | 公開する待ち受け。コンテナ内部ポートは 8080 |
| `PUID` / `PGID` | app、worker、transcriber の UID/GID。データベースディレクトリには使わない |
| `DISPLAY_TIMEZONE` | 画面とファイル名の初期タイムゾーン。あとから設定画面でも変更できます |
| `LOG_LEVEL` | 起動時のログレベル |
| `COOKIE_SECURE` | HTTPS のとき `true` |
| `TWITCASTING_CLIENT_ID` / `TWITCASTING_CLIENT_SECRET` | 公式 API の Basic 認証 |
| `TWITCASTING_ACCESS_TOKEN` | ある場合は Bearer を優先 |
| `SIGN_API_KEY` | Euler Stream の API キー。TikTok コメントと第3録画手段で使用 |
| `ADMIN_USERNAME` / `ADMIN_INITIAL_PASSWORD` | `create-admin` だけが読む |

パスワードに `@` や空白が入っていても、アプリは部品から URL を組み立てます。`DATABASE_URL` を自分で書く場合はパスワードを URL エンコードしてください。

## 画面から変える設定

次は保存したあと、ワーカーの次の周期で反映されます。再起動は不要です。

- 監視間隔（30秒未満は拒否。ツイキャスの登録数に対して 60 リクエスト/分を超えそうなときは自動で間隔を広げます）
- 同時録画数（既定 3）
- 録画品質 `best` / `high` / `medium`
- 最低空き容量
- 録画とコメントの保存期限
- 自動削除（既定オフ）
- TikTok 録画エンジンの順序
- コメント収集の全体スイッチ
- ログレベル
- 表示タイムゾーン
- 再接続回数
- ツイキャスコメントの取得間隔
- 完了時の SRT 自動書き出し（既定オフ）。同じタイミングで、そのセグメントの統合 ASS も更新します
- 音声字幕の有効、モデル（`large-v3` / `large-v3-turbo`）、言語
- 配信者ごとの字幕の単語（200文字まで。空ならヒントなし。次の生成から使います）

保存先、ポート、秘密情報、Cookie の Secure は環境変数です。変更後はプロジェクトを再作成または再起動します。

## 管理者

```bash
python -m live_recorder.cli create-admin
```

既存のパスワードを置き換えるときだけ `ADMIN_RESET_PASSWORD=1` を付けます。パスワードは 12 文字以上です。
