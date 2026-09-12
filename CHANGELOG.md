# Changelog

このプロジェクトはバージョンタグを切っていないため、日付単位でまとめています。新しい順。

## 2026-07-12

- **A-rpm fresh 完了 = 全フェーズ fresh 達成**: rhel9-srpm VM で 9 パッチ分の rpmdb を再抽出し
  SRPM を再収集（EUS/E4S は dist tag 別 `--releasever` + enablerepo リトライ）。
  `casket-20260712-ocp-srpms` を live 化（by-ocp に 4.21.22 / 4.22.3 を初収録、
  bin→src 解決 4983 中 unresolved 28 = 99.44%。残りは EUS 終了後の
  grub2/openssh/python3 z-stream 再ビルド）。
- `phase-a-rpm-extract-one.sh`: rpm2cpio の決定的 SIGPIPE (rc 141) を許容
  （抽出完了後の trailing padding 書き込み失敗を pipefail が失敗扱いにしていた）。
- **OpenGrok を索引対象マイナー whitelist 制に変更 → 索引を SSD へ戻した**:
  全マイナー索引（named volume ~905G）をやめ、`config/opengrok-minors.txt`
  （既定 4.20/4.21/4.22 の 3 マイナー）に載ったものだけを stage・索引する方式に。
  未収録マイナーも casket-mcp の ripgrep で検索可能。footprint が ~150-250G に
  縮み SSD に十分収まるため `DATA_VOLUME` を `/srv/opengrok-data`（SSD）へ戻した
  （同日午前に一旦 `/mnt/hdd/opengrok-data` へ退避したが whitelist 化で不要に）。
  `run-opengrok.sh` は staging tree が実際に参照する `/srv/sources-*` マウントだけを
  bind する（48 casket 全 bind をやめた）。退役済み旧パッチ 5 project の索引遺骸も削除。
- **OpenGrok 起動レースの 0 件検索バグ対策を configuration.xml 永続化に一本化**
  (root-cause 2026-07-12): コンテナ内起動 sync が Tomcat の REST デプロイにレースで
  負けると全 project が `indexed=false` のままになり、xref は正常なのに全文/シンボル
  検索だけが**何を検索しても 0 件**を返す沈黙バグ（ログに
  `IndexNotFoundException: no segments*`）。`run-opengrok.sh` が `/opengrok/etc`
  （configuration.xml）を `ETC_VOLUME`（既定 `/srv/opengrok-etc`）に永続化することで、
  初回 sync 完走後は indexed 済み config が作り直し・再起動をまたいで残り、レースが
  再顕在化しない。0 件バグに陥ったら `run-opengrok.sh` を叩き直す（作り直し + sync
  再実行）のが正しい復旧経路。`PUT .../indexed` での手動復旧は suggester 全再構築を
  誘発し OOM を招くため厳禁（README に明記）。
  ※ 一旦 sync を毎起動再トリガーする `wait-for-ready.sh`（+ `opengrok.service` の
  `ExecStartPost`）を入れたが、config 永続化で不要になったため削除。suggester 夜間
  再構築の無効化は `run-opengrok.sh` 内に戻した。
- **ansible**: unit ファイルが変わったときだけ `casket-mcp` / `opengrok` を
  `restarted`（従来は `started` のみ）にし、更新後の `ExecStart` が同一 playbook
  実行内で効くように。
- **ドキュメント前提の明確化**: casket はファイル配布しない（利用は稼働ホストの
  サービス経由 or 自ホスト構築の2択、docs/setup.md 全面改稿）。
  Red Hat サブスクリプション必須を README / setup.md に明記。
- CHANGELOG 追記、scratch/ の使い捨てスクリプト掃除。

## 2026-07-11 (続き)

- **フェーズ全量 fresh リビルド計画を完遂**（方針: 既存 casket は registry に
  バックフィルせず新規ビルドで置換）:
  - Phase A ×9（4.14.58〜4.22.3、**4.22 新規追跡**）→ live
  - B ×9 / B-operand ×9（解決オーバーホール + 当日の resolver 改善反映）→ live
  - certified / community カタログ ×18 を新規展開（`CATALOG` 環境変数で
    phase-b 5 スクリプトを共用、auto-update 対象外の手動リビルド運用）
  - 旧世代 casket（20260523〜0608）を削除、~57G+ 解放
- **fstab レス化**: casket 行 50 超を全廃し、`state/registry.json` +
  `config/static-mounts.tsv` を単一の真実源とする `casket-mounts.sh`
  リコンサイラ + boot 時 `casket-mounts.service` に移行。
  `casket-swap.sh` は「その場 remount + registry 遷移」に単純化。
- **auto-update 機構**: systemd user timer（毎日 06:00）が
  check→build(staged) を無人実行。swap は手動ゲート維持。
  b/b-operand も registry 整備後に解禁。
- **CNV downstream ギャップ調査**（JANUS case）:
  未解決 5 コンポーネント中 4 つを公開経路で救済（ipam-extensions の
  commit 一致、virt-artifacts-server の kubevirt monorepo 判定、
  hostpath の sibling-version fixup）。virt-core の downstream 差分は
  ftp.redhat.com の kubevirt SRPM が唯一の公開経路だが GA 追随止まりのため
  取り込みは見送り（design-notes に記録）。
- **汎用化**: `CASKET_OUT` 環境変数、mount サービスのインストーラ、
  新規ホスト診断 `casket-doctor.sh`、docs/setup.md 新設。
- casket-mcp の phase 識別子を新体系 (a/a-rpm/b/b-operand/b-certified/
  b-community) に更新、カタログマウントの分類漏れ修正。
- バグ修正: `lib-fingerprint.sh` の arch filter (`linux/x86_64`→`linux/amd64`、
  b/b-operand ビルドが全滅する潜在バグ)、sudo 実行時の `CASKET_WORK` 解決
  （$HOME 由来→checkout 由来 + export）、registry.json の root 所有化防止、
  community カタログの `oc image info` ハング（timeout 60s）、
  `casket-*.sh` の実行ビット欠落。
- OpenGrok: certified/community を専用 project 名で staging、README を
  ~54 project 構成に更新、rootless 必須（sudo 起動で Docker Hub レート制限）
  を明記。

## 2026-07-11

- **ブランチ統合**: `casket-source-index` を `main` にマージ (PR #5, #6, #7)。
- **フェーズ名称の変更**（2段階）: 旧 Phase B/D → `a-rpm`/`c-operand` → 最終的に `b`/`b-operand`。
  2つの起点（リリースペイロード／operatorカタログ）× 2段階の深さという構造を、
  `A`/`A-rpm`・`B`/`B-operand` の名前で連番として読めるように整理。
  `phase-b/` 作業ディレクトリも `phase-a-rpm/` へ移動。
- README / CLAUDE.md / USAGE.md をリネームに合わせて全面改訂、ホスト固有パスを一般化。
- **公開準備**: MIT LICENSE 追加、casket-host の IP/ホスト名を一般化、
  顧客ケース識別子を除去、`decks/`（ライセンス制約のある社内テンプレート成果物）を追跡除外、
  `.mcp.json` のマシン固有サーバ設定をクリア。
- **リリース運用の自動化**: `casket-check/build/swap/cleanup.sh` を追加し、
  `config/minors.txt` / `config/phase-a-rpm-minors.txt` / `config/phase-b-operand-products.tsv`
  を対象minor/製品の単一情報源に統合。`scripts/registry.py` でcasketアーティファクトの
  ライフサイクルを追跡。
- `lib-fingerprint.sh`: `mount_path_for` と鮮度フィンガープリント取得を共通化。
- opengrok: `WORKERS` で reindex 時の並列 JVM 数を上限制御できるように。
- `swap-source-index.sh` の fstab 書き換え no-op バグ（`#` エスケープ漏れ）を修正。

## 2026-06-10

- opengrok: webapp のヒープ (`CATALINA_OPTS`) を設定可能化。ヒープ設定・reindexハング
  のトラブルシュートをドキュメント化。

## 2026-06-09

- **Phase C（現 B）解決オーバーホール**: 未収集operatorを救済する3つの解決バグを修正、
  bundle取得の堅牢化、解決ロジックを `lib-resolve.sh` に切り出して `resolve-v2.sh` から利用。
- **Phase D（現 B-operand）**: レイヤード製品 (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay) の
  operand ソース取得スクリプトを追加。
- opengrok airgap: インデックスを zstd 圧縮し、スパースゼロによる肥大化を解消。
- CI: lint とネットワーク不要の回帰テストを追加。
- リポジトリ整理: `decks/` と `analysis/` をルートから分離、作業用ディレクトリを gitignore。
- `swap-operators-remount.sh`: 4.21 対応、busy-loop フォールバック追加。
- README/CLAUDE.md: Phase C 解決オーバーホールの記録、Phase D の追記、
  source-index (`INDEX.tsv` + `by-component`/`by-repo`) のドキュメント化。

## 2026-06-08

- **casket-mcp** 実装: stage 1（FSナビゲーション + ripgrep MCPサーバ）、
  stage 2（OpenGrok REST バックエンド：symbol/xref/全文検索）、
  stage 3（`diff_file` + Phase D の OpenGrok インデックス化）。
  LAN/リモートからの HTTP アクセスを許可しドキュメント化。
- casket: package時に source-index (`by-component`/`by-repo`/`INDEX.tsv`) を生成する処理を追加、
  overlay backfill + swap ツール、24 casket 全てを本番反映。
- phase-b（現 B）: applied-tree mode 追加 (`PHASE_B_APPLY=1`, `rpmbuild -bp`)、`.git` 除去、
  `KEEP_STAGE` での再利用に対応した casket を出荷。
- opengrok: 既に appuser 所有の場合、215G データボリュームへの不要な `chown -R` をスキップ。

## 2026-06-02

- opengrok: airgap デプロイスクリプトとドキュメントを最終化。

## 2026-06-01

- Initial commit。
