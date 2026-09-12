# パイプライン手順

各フェーズのスクリプト構成と実行手順。成果物の中身は [artifacts.md](artifacts.md)、リリース追随の運用は [operations.md](operations.md) を参照。

## スクリプト構成

`$CASKET_WORK/scripts/`

| ファイル | 役割 |
|---------|------|
| `lib.sh`                  | 共通ヘルパー（引数パース、ロガー、パス解決） |
| `discover.sh`             | Phase A: `oc adm release info` から images.tsv / commits.tsv 生成 |
| `fetch-git.sh`            | Phase A: GitHub archive API から tarball を並列 DL、(repo, commit) で dedupe |
| `manifest.sh`             | Phase A: MANIFEST.json 生成（SHA256, サイズ含む） |
| `package.sh`              | Phase A: mksquashfs で `.sqfs.xz` 出力 |
| `phase-a-rpm-package.sh`  | A-rpm: `phase-a-rpm/srpms/` + `phase-a-rpm/rpmdb/` を 1 本の `.sqfs.xz` に集約 |
| `phase-b-discover.sh`     | Phase B: redhat-operator-index から FBC `/configs` 展開 + operators.tsv/bundles.tsv 生成 |
| `phase-b-fetch-bundles.sh`| Phase B: 各 operator の default-channel head bundle を pull、manifests/metadata 抽出、containers.tsv 生成（`containerImage` 注釈が無くても行を残す＝csv_repo 救済の前提） |
| `phase-b-fetch-source.sh` | Phase B: 各 containerImage の `vcs-ref` + `io.openshift.build.source-location` を読み、github tarball 取得 (v1) |
| `phase-b-resolve-v2.sh`   | Phase B v2: CSV `annotations.repository` / `spec.links[]` + repository 別 ref 戦略 / tag/branch fallback で v1 取りこぼしを救済し `20-git/` へ追記 |
| `lib-resolve.sh`          | Phase B: source 解決の純粋関数 (`normalize_github` / `candidate_source_urls`)。副作用なし・sourceable で `tests/` が回帰テスト |
| `phase-b-package.sh`      | Phase B: catalog + bundles + git + meta を 1 本の `.sqfs.xz` に（meta/MANIFEST/INDEX は `git-v2.tsv` を優先） |
| `phase-b-operand-discover.sh`     | B-operand: レイヤード製品の head bundle を pull、CSV `spec.relatedImages` を `images.tsv` に列挙 |
| `phase-b-operand-resolve-labels.py`| B-operand: operand image を上流 github source へ解決 (`upstream-vcs-url`/`component-map`/`oci-source`/`source-location`/`url`) |
| `phase-b-operand-fetch-source.sh` | B-operand: operand の github archive を取得、(repo, ref|tag) で dedupe |
| `phase-b-operand-cargo-vendor.sh` | B-operand: Rust 製品の `Cargo.lock` を `cargo vendor` 相当で解決し `30-vendor/<comp>/<crate>-<ver>/` へ展開（crates.io の `.crate` + git dep は github archive）。対象は `config/cargo-vendor.txt`（現在 trustee-operator のみ）。DL は `$CASKET_WORK/cargo-cache/` にキャッシュしマイナー間で共有 |
| `phase-b-operand-package.sh`      | B-operand: 1 製品の operand ソースを stage / `.sqfs.xz` 化 (`--stage-only` で統合待ち)。`30-vendor/` があれば `vendor/` として同梱 |
| `phase-b-operand-combine.sh`      | B-operand: 1 マイナーの 7 製品 stage を `cp -al` で集約し統合 casket を mksquashfs (製品ごとに subdir) |
| `build-source-index.py`   | 全 Phase: staged `meta/MANIFEST.json` から `git/INDEX.tsv` + `by-component/` + `by-repo/` 逆引き索引を生成（package スクリプトが staging 直後に自動実行）。dedup ディレクトリ名で実体 repo が隠れる問題への対策 |
| `swap-operators-v2.sh`             | (運用) 新 operator casket への fstab 書換 + .mount restart (sudo, 一度きり) |
| `swap-operators-v2-remount-only.sh`| (運用) fstab 既に更新済時の remount のみ実行 |
| `fix-perms-rebuild.sh`             | (運用) 古い operator casket の perms 修正用 unpacker (chmod a+rX して再 pack) |
| `repackage-add-index.sh`          | (運用) 既存 casket に索引層を後付け: overlayfs で `INDEX.tsv`/`by-component/`/`by-repo/` を読取専用マウントに重ねて再 mksquashfs（単一 A/B・製品別 layered B-operand 両対応） |
| `swap-source-index.sh`            | (運用) 索引付き新 casket への fstab 差し替え + mount 再起動（既定 dry-run、`--apply` で実行）。全 24 casket を 2026-06-08 に `casket-20260608-*` として本番反映済 |

A-rpm 関連の補助物は `$CASKET_WORK/phase-a-rpm/vm/` 配下:

| ファイル | 役割 |
|---------|------|
| `bootstrap.sh`                    | Fedora ホスト側で RHEL 9 cloud VM を起動 (cloud-init seed + virt-install) |
| `user-data` / `meta-data`         | cloud-init seed (cloud-user 作成、SSH 鍵注入) |
| `scripts/01-extract-rpmdb.sh`     | VM 内: 7 版分 `oc adm release info --rpmdb --rpmdb-image=rhel-coreos` |
| `scripts/02-fetch-srpms.sh`       | VM 内: 全版を union → `dnf download --source` で SRPM 一括取得 |
| `scripts/machineos-pullspecs.tsv` | 7 OCP 版 → rhel-coreos image pullspec マップ (参考用) |

### パイプライン（任意の OCP バージョンで再現）

```bash
cd "$CASKET_WORK"
V=4.20.22       # 任意

./scripts/discover.sh   -v "$V" -a x86_64
./scripts/fetch-git.sh  -v "$V" --jobs 6
./scripts/manifest.sh   -v "$V"
./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp
```

オプション:
- `discover.sh -a {x86_64|aarch64|ppc64le|s390x|multi}` — アーキ指定
- `fetch-git.sh --limit N` — スモークテスト用に N 件だけ取得
- `fetch-git.sh --jobs N` — 並列度（既定 6）
- `package.sh -o <dir>` — 出力先（既定 `/mnt/hdd/casket-ocp`）

### 複数バージョンを一括取得

stable-N チャネルから各最新パッチ版を順次処理する例:

```bash
cd "$CASKET_WORK"

# 各 stable-N の最新パッチ版を取得
for v in 4.20 4.19 4.18 4.17 4.16 4.15 4.14; do
  curl -sSL "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-${v}/release.txt" \
    | awk '/^ *Version:/ {print $2; exit}'
done

# 上記で得たバージョンを逐次パイプライン実行
for V in 4.19.31 4.18.41 4.17.53 4.16.55 4.15.59 4.14.58; do
  ./scripts/discover.sh   -v "$V" -a x86_64 \
  && ./scripts/fetch-git.sh  -v "$V" --jobs 6 \
  && ./scripts/manifest.sh   -v "$V" \
  && ./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp \
  || { echo "FAILED $V"; break; }
done
```

実測: 7 版で約 12 分（1 版あたり ~2 分、2026-05-27 時点）。展開レイアウトにしたことで tarball 同梱版の ~18.7 GB から大きく縮んだ。

### A-rpm パイプライン (rhel-coreos SRPM 収集)

Phase A と同じリリースペイロードの `rhel-coreos` イメージだけを RPM 単位で
深掘りするサブフェーズ。Red Hat サブスクリプションを持つ RHEL 9 マシン
(本プロジェクトでは libvirt VM `rhel9-srpm` を使用) 上で `dnf download
--source` を回す方式。Fedora ホストからは bootstrap だけ実行する。

```bash
# 1. RHEL 9 VM を起動 (Fedora ホスト側、要 sudo)
cd "$CASKET_WORK"/phase-a-rpm/vm
./bootstrap.sh                           # → ssh cloud-user@<IP>

# 2. VM に必要物を転送 (Fedora ホスト)
VMIP=192.168.122.xxx                     # bootstrap.sh の最終行に出る
scp ~/.docker/config.json cloud-user@$VMIP:~/pull-secret.json
scp -r scripts cloud-user@$VMIP:~/

# 3. VM 内でサブスク登録 → 抽出 → fetch (cloud-user)
ssh cloud-user@$VMIP
sudo rhc connect --activation-key <KEY> --organization <ORG>   # or subscription-manager register
cd ~/scripts
./01-extract-rpmdb.sh                    # 7 版分 rpmdb (~3 分、quay からの pull 含む)
./02-fetch-srpms.sh                      # SRPM 一括 DL (~15-30 分)
exit

# 4. Fedora ホストに回収 + pack
rsync -a cloud-user@$VMIP:~/scripts/srpms/    $CASKET_WORK/phase-a-rpm/srpms/
rsync -a cloud-user@$VMIP:~/scripts/rpmdb-tsv/ $CASKET_WORK/phase-a-rpm/rpmdb/
rsync -a cloud-user@$VMIP:~/scripts/{wishlist.txt,missing*.txt,fetch*.log} \
                                              $CASKET_WORK/phase-a-rpm/logs/
./scripts/phase-a-rpm-package.sh -o /mnt/hdd/casket-ocp
```

実測 (7 版、初回):
- VM bootstrap: 5 分 (qcow2 clone + cloud-init)
- 01-extract-rpmdb.sh: 約 3 分 (oc が rhel-coreos を pull + rpmdb 抽出)
- 02-fetch-srpms.sh: 約 15 分 (初回 dnf cache 含む) — ヒット率 7-8 割
- EUS / E4S / fast-datapath 追加リトライ: 約 5 分 (`--releasever=9.X` 指定でほぼ全て解決)
- rsync + pack: 約 3 分

### Phase B パイプライン (redhat-operators 収集)

```bash
cd "$CASKET_WORK"
for V in 4.20 4.19 4.18 4.17 4.16 4.15 4.14; do
  ./scripts/phase-b-discover.sh      -v "$V" \
  && ./scripts/phase-b-fetch-bundles.sh -v "$V" --jobs 6 \
  && ./scripts/phase-b-fetch-source.sh  -v "$V" --jobs 6 \
  && ./scripts/phase-b-resolve-v2.sh    -v "$V" --jobs 8 \
  && ./scripts/phase-b-package.sh       -v "$V" -o /mnt/hdd/casket-ocp \
  || { echo "FAILED $V"; break; }
done
```

Phase B は OCP **minor** で動く (Phase A の patch 単位とは異なる; カタログタグが `v<minor>` のため)。実測: 7 版で約 90 分 (v1 部分; 1 版あたり 5〜20 分、label lookup と FBC 展開がボトルネック)。`phase-b-resolve-v2.sh` は 1 版あたり 20〜30 秒、`phase-b-package.sh` は 1 版あたり 30 秒〜1 分。

#### certified / community カタログ (2026-07-11 追加)

`CATALOG` 環境変数で同じ 5 スクリプトがそのまま
`certified-operator-index` / `community-operator-index` に切り替わる（既定 `redhat`）:

```bash
CATALOG=certified ./scripts/phase-b-discover.sh -v 4.20   # 以降のスクリプトも同様に CATALOG を付けて実行
```

- work dir は `phase-b-certified/<minor>/`・`phase-b-community/<minor>/`、成果物は
  `casket-<date>-ocp<minor>-{certified,community}-operators.sqfs.xz`（redhat は従来どおり無印）。
- 実測 (4.20): certified 185 operators → **800 MB** / community 300 operators
  (github source 217 dirs — OSS 由来で解決率が redhat より高い) → **1.4 GB**。
- **auto-update の対象にはしない**（運用判断 2026-07-11）: community はカタログの
  digest が頻繁に動くためリビルド多発になる。必要な時に手動でビルドする。
- community のコンテナはサードパーティレジストリ (ghcr.io、個人 quay リポジトリ等)
  に散っており、死んだエンドポイントで `oc image info` が数分ハングし得るため
  `phase-b-fetch-source.sh` のラベル照会には `timeout 60` を入れてある。

#### 本番マウントへの差し替え (新しい operator casket を作り直した場合)

```bash
sudo "$CASKET_WORK"/scripts/swap-operators-v2.sh   # fstab 書換 + daemon-reload + restart
```

`systemctl restart` が busy で失敗するマウントがあれば、そのマウントだけ `umount -l` してから `systemctl start` でフォロー (実例: 4.20 で発生)。差し替え対象のファイル名は同 script の `VERS=` と日付グロブを編集して合わせる。

### 作業ディレクトリ

```
$CASKET_WORK/
├── README.md                      ← 概要とドキュメント索引
├── CLAUDE.md  USAGE.md            ← 開発ガイド / 利用手順
├── scripts/                       ← パイプライン本体 (Phase A / A-rpm / B / B-operand) + lib-resolve.sh
├── tests/                         ← ネットワーク不要の回帰テスト (CI で実行)
├── .github/workflows/ci.yml       ← lint + テスト (本体パイプラインは CI 対象外)
├── docs/                          ← 設計・計画ドキュメント
├── decks/                         ← 資料: build_*_deck.py + pptx/pdf + 図 (.gitignore — 社内限定テンプレート依存のため非公開)
├── analysis/                      ← 移行・検証の分析メモ (.gitignore — ホスト固有の調査記録のため非公開)
├── mcp/  opengrok/                ← MCP サーバー / OpenGrok ソースブラウザ
├── ocp<VERSION>/                  ← Phase A バージョン別中間生成物 (.gitignore)
│   ├── 00-discover/               release.json, release-commits.txt, images.tsv, commits.tsv
│   ├── 10-git/                    tarball + fetch.log
│   ├── 40-manifest/               MANIFEST.json
│   └── 50-out/stage/              mksquashfs 入力ツリー
├── phase-a-rpm/                   ← A-rpm 中間生成物
│   ├── vm/                        cloud-init + virt-install bootstrap, VM 内スクリプト
│   ├── srpms/                     843 *.src.rpm (VM から rsync)
│   ├── rpmdb/                     7 版分の rpmdb tsv
│   ├── logs/                      wishlist / missing / fetch ログ
│   └── 50-out/stage/              mksquashfs 入力ツリー
├── phase-b/<minor>/               ← Phase B 中間生成物 (minor 単位)
│   ├── 00-discover/               configs/ (FBC), operators.tsv, bundles.tsv, containers.tsv, labels.tsv, git.tsv, containers-v2.tsv, git-v2.tsv
│   ├── 10-bundles/<op>/<head>/    bundle 展開: manifests/, metadata/
│   ├── 20-git/                    github tarball (v1 + v2 追記、fetch.log + fetch-v2.log)
│   └── 50-out/stage/              mksquashfs 入力ツリー
└── phase-b-operand/<pkg>/<minor>/ ← B-operand 中間生成物 (製品×minor 単位)
    ├── 00-discover/               head bundle, images.tsv (operand 一覧)
    ├── 20-git/                    operand github tarball + fetch ログ
    ├── 30-vendor/<comp>/          Rust 依存の展開済ソース (Cargo.lock 解決, 対象製品のみ)
    └── 50-out/stage/              統合前 stage (phase-b-operand-combine.sh が集約)
```

中間生成物 (`ocp<VERSION>/`, `phase-a-rpm/srpms`, `phase-a-rpm/50-out`) は最終 `.sqfs.xz` 生成後は削除して OK。`phase-a-rpm/rpmdb/` と `phase-a-rpm/logs/` は次回更新時の差分把握に役立つので残すと良い。
