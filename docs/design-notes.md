# 設計判断・既知の注意点

各フェーズの設計で選んだ方式と、その理由。

## Phase A

- **`*-source` コンテナを追いかける案を不採用にした理由**: OCP の release payload にひもづくソースコンテナは命名規則が安定せず、`quay.io/openshift-release-dev/ocp-v4.0-art-dev:<...>-source` 等を実機で probe したがすべて 404。代わりに `oc adm release info --commits` が各コンポーネントの GitHub repo URL + 正確な commit SHA を綺麗に返すため、これを source of truth とした。
- **GitHub archive を直接取得**: tarball は `https://github.com/<owner>/<repo>/archive/<sha>.tar.gz` で認証不要・1 リクエストで完結。164 件で約 95 秒。
- **(repo, commit) で dedupe**: 191 イメージのうち約 27 件は同じ repo+commit を共有する（例: `csi-operator` を使う複数の CSI ドライバ operator）。tarball は dedupe して保存し、MANIFEST.json でイメージ→tarball の many-to-one を維持。
- **`.sqfs.xz` の正体**: 既存 casket (`casket-20251118-rhel10.sqfs.xz` 等) を `file` / `xz -t` で確認した結果、**外側 xz ラップ無し**の「内部 xz 圧縮 squashfs」だった。本プロトタイプも同じ形式に揃えた。

## A-rpm

- **`--rpmdb-image=rhel-coreos` が必須**: 4.14 / 4.15 には `machine-os-content` (旧 el8 ベース ostree) と `rhel-coreos` (新 el9 ベース OCI) が共存。`oc adm release info --rpmdb` の既定動作だと前者を引き、本プロジェクトのサブスク (RHEL 9 のみ) では SRPM が取れない。明示で `rhel-coreos` を指定して全 7 版を **el9 統一**にして処理する。
- **rpmdb 抽出は RHEL 9 VM 内で実施**: Fedora ホストの新しい rpm では 4.14-4.18 の rpmdb 変換が `rpm -qa` 段階で失敗 (host rpm との format mismatch)。RHEL 9 ネイティブの rpm を持つ VM 内なら全版通る。
- **`dnf download --source` で SRPM を引く**: binary RPM の NEVR を渡せば dnf が repodata で sourcerpm を解決して `.src.rpm` を保存。出力 dir 内で同名 SRPM は自然に dedup される。
- **EUS / E4S は `--releasever=<minor>` 指定が必要**: el9_2 / el9_4 / el9_6 errata の SRPM は EUS 専用チャネル経由でしか入手できず、VM 自身が 9.8 だと `eus/rhel9/9.8/...` を引いて 404。dist tag のマイナーごとに `--releasever=9.2` 等で再 fetch することで初回 32% → 最終 99.86% カバレッジに到達。

## Phase B

- **対象は redhat-operator-index の `vN.M` タグ**。Phase A が patch 単位なのに対し Phase B は **minor 単位**で動く (カタログがそうなっているため)。
- **File-Based Catalog (FBC) は `/configs` ディレクトリ**: opm サーバーを立てる必要は無く、`oc image extract --path /configs/:dest/` で NDJSON を取り出せる。
- **head bundle 算出は `replaces` + `skips` を考慮**: **他 entry の `replaces` / `skips` に含まれない entry** を集合演算で求め、複数残る場合は配列末尾を採用する。
- **container image labels は不揃い**: `io.openshift.build.source-location` + `vcs-ref` が揃うはずだが実際は不揃い。Phase B v2 (CSV 起点 + branch fallback) で 6→34 件 (4.20 は 86 件) まで増加。残る取りこぼしは CSV の `repository` 自体が github を指していない operator で、brew/cachito ビルド由来のため公開 tarball として再現不可。

## トラブルシューティング

- **GitHub レート制限**: 認証なしで 164 並列 6 を実測した範囲では問題なし（アーカイブ DL は API レート制限とは別枠）。429 が出たら `--jobs` を下げるか `GITHUB_TOKEN` を設定。
- **Phase A/RPM VM**: `rhel9-srpm` (libvirt) は登録 + 設定済み状態で永続化。`sudo virsh start rhel9-srpm` → `sudo virsh net-dhcp-leases default` で IP 確認。
- **SRPM 取り残し 2 件**: `redhat-release-9.2-0.15.el9` / `redhat-release-eula-9.2-0.15.el9` は EUS チャネルからも消失 (9.2 EUS 終了直前の中間バージョン)。実害なし。
