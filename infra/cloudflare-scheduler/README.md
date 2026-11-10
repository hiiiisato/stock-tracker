# Cloudflare server-side scheduler

GitHub Actionsの定期発火遅延を避けるため、Cloudflare Cron Triggerを一次スケジューラとして使い、
実処理は既存のGitHub Actionsへ `workflow_dispatch` する。5分ごとのTriggerを1本だけ登録し、
日本時間の実行表は `src/schedule.ts` に集約している。

日次メイン便は市場データ確定後の16:05 JSTに発火し、未完了時だけ18:00便が再実行する。Cloudflareが渡す
`scheduled_for` を日次処理の論理日として使うため、GitHubランナーの割当が翌日に
遅れても実行時の時計で対象日がずれない。欠損日の手動救済は `daily.yml` の
`catch_up` と `target_date`（YYYY-MM-DD）を使う。

## 初回設定

1. GitHubで対象リポジトリだけに限定したfine-grained Personal Access Tokenを作る。
   Repository permissionsは `Actions: Read and write`、有効期限は最短の運用可能期間にする。
2. このディレクトリで `npx wrangler secret put GITHUB_TOKEN` を実行し、対話入力する。
3. `npm run check` と `npm run deploy:dry` の後、`npm run deploy` で公開する。

秘密値は `wrangler.jsonc`、`.dev.vars`、ソースコードへ保存しない。

## 運用

- CronはUTCで動くため、`controller.scheduledTime` をJSTへ変換して判定する。
- 実行履歴はCloudflare Workers LogsとGitHub Actionsの双方で確認できる。
- 時刻の正本はこのWorkerだけとし、GitHub Actions自身のcronやMac/Codexローカル便は使わない。
- GitHub Actionsは`workflow_dispatch`の実行基盤であり、失敗・キャンセル時だけ1回自動再実行する。
- みんかぶ・IFISコンセンサスは両サイトがサーバー取得を拒否しているため定期発火を停止中。
  既存データは保持し、対応APIまたは許可された取得経路を確保するまで手動診断だけに限定する。
