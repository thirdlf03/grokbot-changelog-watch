# grokbot-changelog-watch

Grok Bot の [変更履歴](https://x.ai/changelog/bot)（RSS）を定期的に確認し、**新しいリリースが公開されたときだけ** Webhook で Grok Bot を起こす GitHub Actions ワークフローです。更新がない日は Webhook を送りません。

## 必要なリポジトリ Secrets

| Secret | 用途 |
|--------|------|
| `GROKBOT_WEBHOOK_URL` | POST 先 URL |
| `GROKBOT_WEBHOOK_KEY` | `Authorization: Bearer …` および `X-Automation-Key` ヘッダー用 |

Secrets の値はログやコミットに出力しません。

## 動作概要

- RSS: 変更履歴ページの `link rel=alternate` から取得（通常 `https://x.ai/changelog/bot/rss.xml`）
- 直近に通知済みのバージョンは `state/last_seen.json` に保存
- 未通知のエントリが複数ある場合は**古い順**に 1 件ずつ POST し、HTTP 200 のたびに state を更新
- 200 以外の場合はジョブを失敗させ、state は進めません
- state が変わったときだけ `github-actions[bot]` がコミット（スケジュール実行の維持にも利用）

## スケジュール

UTC の毎日 0:17 / 6:17 / 12:17 / 18:17（6 時間ごと）。

## 手動テスト（dry run）

Actions タブ → **Watch Grok Bot changelog** → **Run workflow**。

`dry_run` は既定で **true** です。送信予定の JSON がログに出ますが、Webhook は呼ばれず state も更新されません。実際に通知する場合は `dry_run` を **false** にしてください。

ローカルでも同様に試せます:

```bash
python3 scripts/check_changelog.py --dry-run
```
