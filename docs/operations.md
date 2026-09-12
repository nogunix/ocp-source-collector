# 運用: 新しい OpenShift リリースが出たときのメンテ


[pipeline.md](pipeline.md) は *1つのバージョンをどう取得するか* の話。ここは
*どのバージョンが対象で、いつ取り直し、いつ本番に反映するか* という運用の軸
——`config/` の対象一覧と `state/registry.json` のライフサイクル記録（staged →
live → retired）を軸に、4本の driver スクリプトでつなぐ。個別フェーズの
`discover.sh` 等は無改造（唯一の例外: `phase-b-operand-combine.sh` の product
一覧は `config/phase-b-operand-products.tsv` から読むようにした）。

`a-rpm` と `b-operand` は、A/B と同じ取得元をもう1段
深く見るサブフェーズであって独立フェーズではない、という前提でフェーズ識別子
自体もこの命名にしている（[README](../README.md) の「現状」節参照）。

```
config/minors.txt                A/B/B-operand の対象 OCP minor（8: 4.14〜4.21）
config/phase-a-rpm-minors.txt    A-rpm の対象 minor（7: 4.21 は収集PAUSED）
config/phase-b-operand-products.tsv  B-operand の対象製品（package<TAB>infix、7行）
state/registry.json              アーティファクトのライフサイクル（build/registry.py管理、手編集しない）
```

鮮度シグナル（フェーズごとに「新しくなったか」を判定する値）:
a=stable channelの最新patch文字列 / b・b-operand=`redhat-operator-index:v<minor>`
のmanifest digest（b-operandはbと同じindexを見るため共用） / a-rpm=対象minorぶんの
patch文字列を束ねたsha256（rhel-coreosイメージが動いた"だろう"という代理指標）。
詳細は `scripts/lib-fingerprint.sh` のコメント参照。

```bash
# 1. 鮮度チェック（読み取り専用、cron/systemd timerで自動実行してよい唯一のコマンド）
./scripts/casket-check.sh --phase a        # or a-rpm/b/b-operand/all（省略時 all）
#   a          4.20     STALE      current=4.20.28   registry=4.20.27   ← 要ビルド
#   終了コード: 1件でもSTALE/MISSING/UNREACHABLEがあれば非ゼロ

# 2. ビルド（既存のフェーズ別スクリプトをそのまま順に呼ぶだけ。まずdry-runで計画を確認）
./scripts/casket-build.sh --phase a --unit 4.20              # dry-run
./scripts/casket-build.sh --phase a --unit 4.20 --apply      # 実行 + registryにstaged登録

# 3. 本番反映（fstab は使わない — 2026-07-11 からマウントは registry 駆動）
./scripts/casket-swap.sh --phase a --unit 4.20                # dry-run（差し替え内容を表示）
sudo ./scripts/casket-swap.sh --phase a --unit 4.20 --apply    # その場で remount + registryをlive化

# 4. 古いアーティファクトの掃除（retired かつ 14日超のものだけ、既定dry-run）
./scripts/casket-cleanup.sh
./scripts/casket-cleanup.sh --apply
```

**自動化の境界**: `casket-check.sh` は読み取り専用（ネットワーク照会のみ）なので
自動実行してよい。**build も非破壊**（新ファイル作成 + registry への staged 登録のみ、
sudo 不要）なので自動化してよく、**2026-07-16 からタイマーで自動発火**
（金曜 23:30 起床・**3週に1回**実行。2026-09-12 に週次から変更。下記
casket-auto-update 節参照。2026-07-12〜16 の間は手動運用で、
その間に全フェーズが STALE 化した反省から有効化）。swap/cleanup は常に人間が
`--apply` を打つ運用に留める——a-rpmはサブスク済みVMへのログインが元々必要で
無人化できず、本番へのdestructive操作は本リポジトリの既存文化
（`swap-source-index.sh` 等のdry-run既定＋明示apply）に合わせて手動ゲートを残す。

## 更新チェック＋staged ビルド (casket-auto-update)

新リリース検知〜staged ビルドまでをまとめて回す仕組み。**staging は3週に1回自動**
（2026-07-16 に timer 有効化、2026-09-12 に週次→3週次）、**swap は自動化しない**
（人手ゲート）。

```
systemd/casket-auto-update.service      → scripts/casket-auto-update.sh --apply --min-interval-days 21
systemd/casket-auto-update.timer        毎週金曜 23:30 に「起床」だけする（casket-host で有効化済み、2026-07-16）
config/auto-update-phases.txt           自動ビルド対象フェーズ（既定: a のみ。b/b-operand はコメントアウト）
state/auto-update.status                最新実行の unit 別サマリ（.gitignore、registry.json は従来どおり追跡）
state/auto-update.last-run              cadence スタンプ。mtime = 最後に**完走**した apply 実行（.gitignore）
```

> 3週にした理由: 1回の full run が端から端まで **2.5〜3日**かかる
> （08-28 23:38→08-31 15:53、09-04 23:40→09-07 14:50）。週次だとホストが
> 稼働時間の半分近くを、中身のほとんど動いていない casket の再ビルドに
> 使うことになる。毎日でない理由は従来どおり: operator カタログの digest は
> ほぼ毎日回転するので、どの頻度で回しても swap 直後には「STALE」に戻る。

> **3週間は timer 側では表現していない**。systemd の OnCalendar に
> 「3週おき」は無い（週番号はフィールドに無く、日付範囲では月単位の近似に
> しかならない）。そこで timer は従来どおり毎週金曜に起床し、
> `casket-auto-update.sh --min-interval-days 21` が
> `state/auto-update.last-run` を見て「今週は自分の番か」を判定する。
> - 3回に2回の起床は journal に `skipping: last completed run was Nd ago` を
>   1行残して exit 0（失敗ではない）
> - スタンプは**完走時にだけ**打つので、途中で kill された run は3週後では
>   なく**翌週の金曜に再試行**される
> - スタンプが無い/壊れている場合は fail-open（実行する）。手動で今すぐ
>   回したいときは `--force`
> - 間隔を変えるのは .timer ではなく **.service の `--min-interval-days`**。
>   変えたら `scripts/lib-freshness.sh` の `CASKET_STALE_AFTER_DAYS`（既定
>   28日）も一緒に見直すこと——窓が cadence を下回ると casket-check.sh が
>   また万年赤に戻る

動作: フェーズ×unit ごとに鮮度フィンガープリントを取得し、
- `current == live` → fresh（何もしない）
- `current == staged` → **awaiting-swap**（ビルド済みで swap 待ち。再ビルドしない——これが無いと swap するまで走らせるたびビルドし続けてしまう）
- それ以外 → `casket-build.sh --apply` を実行して staged 登録
- `a-rpm` は check のみ（`manual-needed` を報告するだけで絶対にビルドしない）

引数なしで手動実行すると dry-run（`would-build` を表示するだけ。dry-run は
cadence ガードの対象外で、いつでも「今なら何をビルドするか」を聞ける）。実行結果は
journald（`journalctl --user -u casket-auto-update.service`）と
`state/auto-update.status` で確認。flock でラン全体を排他し、多重起動しない。

手動実行（どちらでもよい）:

```bash
# サービス経由（PATH 等の環境込みで起動、journald にログが残る）
systemctl --user start casket-auto-update.service
journalctl --user -u casket-auto-update.service -f      # 進捗

# または直接（dry-run は引数なし、実行は --apply）
./scripts/casket-auto-update.sh              # dry-run（would-build 表示のみ）
./scripts/casket-auto-update.sh --apply      # staged ビルド実行
```

インストール（初回のみ。casket-host では 2026-07-16 に実施済み）:

```bash
ln -s $CASKET_WORK/systemd/casket-auto-update.service ~/.config/systemd/user/
ln -s $CASKET_WORK/systemd/casket-auto-update.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger $USER     # ログアウト中も timer/ジョブを走らせ続ける
systemctl --user enable --now casket-auto-update.timer
systemctl --user list-timers casket-auto-update.timer   # 次回発火の確認
```

`Persistent=true` なので、発火時刻にホストが落ちていても次回起動時に
キャッチアップ実行される。timer を止めたいときは
`systemctl --user disable --now casket-auto-update.timer`（手動 start は引き続き可能）。

swap 待ちの成果物は `cat state/auto-update.status` か
`python3 scripts/registry.py list --status staged` で一覧できる。

**Phase A の swap は「追加」、A-rpm/B/B-operand の swap は「置換」**: Phase A はパッチ
版ごとに別マウント (`/srv/sources-ocp<patch>`) なので、patchが上がると新しい
マウントが追加されるだけで旧patchのマウントは残る（旧バージョンのソース参照
はそのまま使えるのが利点）。`casket-swap.sh` はこれを検知して、Phase A では
registryの旧 `live` エントリを retired にしない（`cleanup.sh` が消さない）。
A-rpm/B/B-operand はマウント先が安定（minor単位 or 単一パス）なので、旧エントリは
retired に遷移し、`cleanup.sh` の対象になる。

## マウント管理 (2026-07-11〜、fstab レス)

casket のマウントは fstab に書かない（50行超に膨張したため廃止。casket 行は
`/etc/fstab.bak-20260711-pre-reconciler` 時点で全撤去）。単一の真実源は:

```
state/registry.json の live エントリ     ← check/build/swap が管理
config/static-mounts.tsv                ← registry 外の casket（RHEL 2本、
                                           certified/community カタログ、暫定の srpms）
```

これを `scripts/casket-mounts.sh` が実マウントと突き合わせて mount / 差し替え /
umount する（dry-run 既定、`--apply` は root）。boot 時は
`systemd/casket-mounts.service`（`/etc/systemd/system/` にインストール・有効化済み）
が `--apply` で全マウントを再構築する。

- `casket-swap.sh --apply` はその場で remount + registry live 化するだけ。
  再起動後の永続化は service が担う。
- 新しい静的 casket（カタログ再ビルド等）を足すときは `static-mounts.tsv` に
  1行追加して `sudo ./scripts/casket-mounts.sh --apply`。
- a-rpm の fresh ビルドが registry に載ったら、static-mounts.tsv の
  ocp-srpms 行を削除すること（二重管理防止）。
