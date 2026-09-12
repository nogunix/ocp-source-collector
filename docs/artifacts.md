# 成果物リファレンス

各フェーズが生成する casket の内容・レイアウト・数値。フェーズの位置づけは [README](../README.md)、生成手順は [pipeline.md](pipeline.md) を参照。

## 成果物 (Phase A)

`/mnt/hdd/casket-ocp/` 配下に各マイナー最新パッチ版を 1 ファイルずつ配置（9 マイナー 4.14–4.22）:

| ファイル | サイズ | images |
|---------|-------:|-------:|
| `casket-20260527-ocp4.14.58.sqfs.xz` | 688 MB | 189 |
| `casket-20260527-ocp4.15.59.sqfs.xz` | 763 MB | 190 |
| `casket-20260527-ocp4.16.55.sqfs.xz` | 659 MB | 190 |
| `casket-20260527-ocp4.17.53.sqfs.xz` | 634 MB | 189 |
| `casket-20260527-ocp4.18.41.sqfs.xz` | 666 MB | 188 |
| `casket-20260527-ocp4.19.31.sqfs.xz` | 698 MB | 189 |
| `casket-20260527-ocp4.20.22.sqfs.xz` | 754 MB | 191 |
| `casket-20260527-ocp4.21.17.sqfs.xz` | 778 MB | 190 |

各 git ソースは **展開済み** (`git/<name>-<short_sha>/...`、GitHub archive の top-level dir を stage 時に剥がす)。展開 + xz 再圧縮のテキスト dedupe で、tarball 同梱版 (旧 `casket-20260523/24`、2.4〜2.9 GB) から **約 73% 縮小** (2026-05-27 リビルド)。旧版はロールバック用に残置。

```
$ file /mnt/hdd/casket-ocp/casket-20260527-ocp4.20.22.sqfs.xz
Squashfs filesystem, little endian, version 4.0, xz compressed, ... inodes
```

各成果物の中身:

```
squashfs-root/
├── git/                          164 ソースツリー (展開済, .tar.gz 同梱せず)
│   ├── agent-installer-api-server-760626123a57/
│   ├── agent-installer-ui-2d9cabc5a1a1/
│   ├── ...
│   ├── INDEX.tsv                 逆引き索引 (dir｜repo｜ref｜version｜components)
│   ├── by-component/<name>       -> ../git/<dir>  (image コンポーネント名で辿る)
│   └── by-repo/<repo>            -> ../git/<dir>  (上流 repo 名で辿る)
└── meta/
    ├── MANIFEST.json             191 image -> ソース対応表 (SHA256 付き)
    ├── README.txt                内部メモ
    ├── release.json              oc adm release info --output=json (生)
    ├── release-commits.txt       oc adm release info --commits (生)
    ├── images.tsv                name TAB pullspec TAB digest
    ├── commits.tsv               name TAB repo_url TAB commit_sha
    └── fetch.log                 ダウンロード結果ログ
```

マウントして確認:

```bash
sudo mount -o loop /mnt/hdd/casket-ocp/casket-20260527-ocp4.20.22.sqfs.xz /mnt/x
jq '.totals' /mnt/x/meta/MANIFEST.json
jq '.images[] | select(.name == "etcd")' /mnt/x/meta/MANIFEST.json
sudo umount /mnt/x
```

または `unsquashfs -l <file>` でファイル一覧、`unsquashfs -d /tmp/out <file>` で展開。

## 成果物 (A-rpm)

同じリリースペイロードのうち `rhel-coreos` イメージだけは commit 情報が使えず
RPM/SRPM 単位で深掘りする必要がある、Phase A の一部という位置づけ（詳細は
[operations.md](operations.md)）。`/mnt/hdd/casket-ocp/casket-20260529-ocp-srpms.sqfs.xz` — **3.5 GB** (`/srv/sources-ocp-srpms` にマウント)

7 OCP 版 (4.14.58〜4.20.22) の `rhel-coreos` イメージから抽出した全 binary RPM の **ソースを 1 ファイルに集約**。重複排除済み 843 SRPM、カバレッジ 1390 / 1392 = **99.86%** (未取得は `redhat-release-9.2-0.15.el9` と同 eula、既に Red Hat repos から消失)。

各 SRPM は **展開済み** (`srpms/<NEVR>/` に spec + 上流ソースツリー + `patches/`、Phase A/B と同じレイアウト)。`.src.rpm` 自体は同梱しない (ソース閲覧用途、rpmbuild 用ではない)。展開 + xz 再圧縮のテキスト dedupe で 8.5G → **3.5G** (2026-05-28 リビルド)。`srpms/` は全版横断で dedup されているため、`by-ocp/<ver>/` シンボリックリンク木でどの OCP 版由来かを可視化 (2026-05-29 追加)。

```
squashfs-root/
├── srpms/                       843 SRPM (展開済・重複排除済)
│   └── systemd-252-51.el9_6.4/
│       ├── systemd.spec         spec (top)
│       ├── systemd-252/         上流ソースツリー (Source* tarball 展開)
│       └── patches/             .patch + 非 tarball 補助ソース
├── by-ocp/<OCP-VER>/<src-name>  → ../../srpms/<NEVR> へのリンク (各版 341〜368 本)
│   └── 4.20.22/systemd          → ../../srpms/systemd-252-51.el9_6.4
└── meta/
    ├── MANIFEST.json            SRPM 索引 + 各 OCP 版の binary RPM 一覧
    ├── bin-to-src.tsv           binary-NEVRA TAB source-NEVR TAB ocp-version (3844 行)
    ├── README.txt
    ├── rpmdb/<V>.tsv            7 版分 rhel-coreos の rpmdb (name TAB epoch TAB ver TAB rel TAB arch)
    ├── wishlist.txt             1392 unique (name-ver-rel) 要求リスト
    ├── missing.txt              初回 fetch で未解決だったもの
    ├── missing3.txt             最終未解決 (2 件)
    └── fetch*.log               dnf download --source のログ
```

`cd /srv/sources-ocp-srpms/by-ocp/4.20.22/` で当該リリースの SRPM を直接ブラウズできる。旧 `casket-20260524` (bundled 8.5G) / `casket-20260528` (by-ocp 無し) はロールバック用に残置。

## 成果物 (Phase B)

`/mnt/hdd/casket-ocp/casket-20260527-ocp<minor>-operators.sqfs.xz` — 各 OCP minor に 1 ファイル (計 7 本、合計 **約 4.6 GB**):

| ファイル | サイズ | operators | bundles 取得 | container 解決 | github 実取得 (v2) |
|---------|-------:|---------:|-------:|---------:|---------:|
| `casket-20260527-ocp4.14-operators.sqfs.xz` | 505 MB | 109 | 109 | 88 | 30 |
| `casket-20260527-ocp4.15-operators.sqfs.xz` | 556 MB | 110 | 110 | 88 | 27 |
| `casket-20260527-ocp4.16-operators.sqfs.xz` | 549 MB | 141 | 141 | 93 | 34 |
| `casket-20260527-ocp4.17-operators.sqfs.xz` | 342 MB | 116 | 116 | 92 | 33 |
| `casket-20260527-ocp4.18-operators.sqfs.xz` | 352 MB | 122 | 122 | 92 | 34 |
| `casket-20260527-ocp4.19-operators.sqfs.xz` | 403 MB | 117 | 117 | 91 | 34 |
| `casket-20260527-ocp4.20-operators.sqfs.xz` | 896 MB | 121 | 121 | 95 | 86 |

**Phase B v2 改善 (2026-05-27 完了)**: v1 では各 minor あたり github 実取得が 1〜6 件と低カバレッジだったが、v2 で CSV メタデータ (`annotations.repository`, `spec.links[]`) からの source URL 復旧と tag/branch fallback (v-prefix 正規化、`release-X.Y` / `main` / `master`) を追加し、27〜86 件まで向上 (合計 278 tarballs / 4.6 GB)。残る非取得は CSV の repository フィールドが `access.redhat.com/containers/...` や `www.redhat.com` の operator (各 minor で 50〜60 件) — github 以外を指しているため v2 でも救えない構造的限界。

> **解決オーバーホール (2026-06-09, 完了)**: 上表は旧ロールアウト (2026-05-27, github 取得 27〜86 件) の本番値で、現在の本番値ではない。その後、解決を抑制していた **3 つの独立したバグ**を修正し、**4.18 で 34 → 110 ユニーク source dir** に増加（検証済み）。
> 1. `phase-b-fetch-bundles.sh`: CSV に `containerImage` 注釈の無い operator を `containers.tsv` から落としていた（30/122 件除外＝serverless/pipelines/compliance 等）。
> 2. `phase-b-resolve-v2.sh`: repository 別の ref 戦略（gitops `v<MAJ.MIN>.0` / maistra `maistra-<ver>-dev` / serverless `release-<製品minor>` / compliance branch）と、空 ref の列ズレ（`IFS=$'\t' read` が連続タブを圧縮）を `_NONE_` sentinel で修正。
> 3. `phase-b-package.sh`: meta/MANIFEST/INDEX に `git-v2.tsv` を優先採用。
>
> 純粋ロジックは `scripts/lib-resolve.sh` に分離し `tests/` で回帰テスト（CI で実行）。詳細は [phase-b-uncollected-plan.md](archive/phase-b-uncollected-plan.md)。残る困難 2 件は版マッピング不能な pipelines (製品 v1.22 ↔ tektoncd v0.79) と compliance (RH が公開 tag より先行)。

各成果物の中身:

```
squashfs-root/
├── catalog/<operator>/catalog.json    File-Based Catalog (FBC) を verbatim
│                                      (registry.redhat.io/redhat/redhat-operator-index:v<minor> 由来)
├── bundles/<operator>/<head>/
│   ├── manifests/                     CSV + CRDs (OLM payload)
│   └── metadata/                      bundle annotations.yaml
├── git/<name>-<short_sha>/             github source (展開済; v1 labels + v2 CSV/branch fallback の合算)
└── meta/
    ├── MANIFEST.json                  operator/container/git の全情報
    ├── operators.tsv                  name TAB channel TAB head_bundle TAB head_image
    ├── containers.tsv                 name TAB head_bundle TAB containerImage TAB csv_version
    ├── labels.tsv                     containerImage TAB source_url TAB vcs_ref
    ├── git.tsv                        name TAB source_url TAB vcs_ref TAB tarball (or NO_SOURCE)
    ├── git-v2.tsv                     v2 rescues CSV repo + smart fallback strategy
    └── {bundles,git,git-v2}-fetch.log
```

サイズの不均一は古い minor の FBC catalog (4.14〜4.16) が ansible-automation-platform 等で 100 MB 級の長い upgrade history を含むため。

**v2 でも取りこぼす operator**: CSV/label の repository フィールドが github 以外 (`access.redhat.com/containers/<...>`, `www.redhat.com`) を指す operator は v2 でも取得不可。これは Red Hat 社内 brew/cachito ビルドで公開 git tag に紐づかないものが多く、再現性のある github source 取得は構造的に不可能。各 minor で 50〜60 件残る。

## 成果物 (B-operand)

Phase B と同じ operator カタログをもう1段深く見るサブフェーズ（詳細は [operations.md](operations.md)）。
`/mnt/hdd/casket-ocp/casket-<date>-layered-ocp<minor>.sqfs.xz` — **1 マイナー = 統合 1 casket** (全 9 マイナー 4.14–4.22、各 **0.8〜1.1 GB**)。`/srv/sources-layered-ocp<minor>/{cnv,acs,mce,acm,rhoai,odf,quay}/` にマウントし、各 product 配下に `bundle/`・`git/`・`meta/`。

Phase B が operator の**制御イメージ**ソースを採るのに対し、こちらは各レイヤード製品の **operand 群**(CSV `spec.relatedImages`) の上流 github ソースを採る。対象 7 製品:

| 製品 | パッケージ | ライセンス |
|------|-----------|-----------|
| CNV (OpenShift Virtualization) | `kubevirt-hyperconverged` | OCP 同梱 |
| MCE (Multicluster Engine) | `multicluster-engine` | OCP 同梱 |
| ACS (Advanced Cluster Security) | `rhacs-operator` | Platform Plus |
| ACM (Advanced Cluster Management) | `advanced-cluster-management` | Platform Plus |
| RHOAI (OpenShift AI) | `rhods-operator` | 別売 |
| ODF (OpenShift Data Foundation) | `odf-operator` | Platform Plus |
| Quay | `quay-operator` | Platform Plus |

ソース解決は `phase-b-operand-resolve-labels.py` が operand image ごとに **`upstream-vcs-url` → `component-map` → `oci-source` → `source-location` → `url`** の順で github URL + ref を引く (ODF/Quay は公開 git に umbrella/main しか無いため `COMPONENT_MAP` で代替)。解決数 (operand 解決 / 総数) は例: 4.18 で cnv 54/59・mce 38/39・acs 12/12・acm 50/64・rhoai 89/100・odf 3/3・quay 1/10。DL 失敗は全マイナー共通で `openshift/*`・`rh-openjdk` (Phase A に既収録のため重複回避)、Quay は公開 git が umbrella のみで 1/10、ACM 4.14/4.16 のみラベル方式差で低解決 (要追加調査)。詳細は [phase-b-operand-plan.md](archive/phase-b-operand-plan.md)。

```
squashfs-root/                         (casket-<date>-layered-ocp<minor>)
├── cnv/
│   ├── git/<name>-<ref>/              operand 上流ソース (展開済)
│   │   └── by-component/ by-repo/ INDEX.tsv   逆引き索引
│   ├── bundle/                        head bundle の manifests/metadata
│   └── meta/                          MANIFEST.json, images.tsv, git.tsv
├── acs/ mce/ acm/ rhoai/ odf/ quay/   同構造
└── README.txt
```

dedup ディレクトリ名で実体 repo が隠れる問題 (例: kubevirt 本体が無関係名の下に集約) は、各 product の `by-component/`・`by-repo/`・`INDEX.tsv` (Phase A/B と同じ `build-source-index.py`) で発見可能。旧・製品別 4.20 casket は `/mnt/hdd/casket-ocp-test/superseded-perproduct-20260603/` に退避。

## 数字サマリ

| 項目 | 値 |
|------|----|
| OCP バージョン | stable-4.14 〜 stable-4.20 の各最新パッチ (計 7 本) |
| Phase A: 1 版あたりイメージ数 | 188〜191 |
| Phase A: 1 版あたり squashfs.xz | 634〜778 MB（展開レイアウト, 2026-05-27〜; tarball 同梱版から約 73% 減） |
| Phase A: 全 7 版バッチ所要時間 | 約 12 分（逐次・1 版あたり ~2 分） |
| A-rpm: 1 版あたり binary RPM 数 | 518〜568 (rhel-coreos) |
| A-rpm: SRPM 数 (7 版 union, 重複排除) | 843 |
| A-rpm: squashfs.xz | 3.5 GB (展開レイアウト, 2026-05-28〜) |
| A-rpm: 取得カバレッジ | 1390 / 1392 = 99.86% |
| A-rpm: 所要時間 (初回 + リトライ) | 約 25 分 (VM bootstrap 別) |
| Phase B: 1 版あたり operator 数 | 109〜141 |
| Phase B: 1 版あたり head bundle 取得 | 109〜141 |
| Phase B: 1 版あたり squashfs.xz | 342〜896 MB (古い minor の FBC + v2 git tarballs) |
| Phase B: github source 解決 (v2 合計実取得) | 7 版で 278 件 (v1 の 27 件から 10 倍以上に改善) |
| Phase B: 全 7 版バッチ所要時間 | v1 約 90 分 + v2 追加 約 15 分 |
| 合計サイズ (A + A/RPM + B) | 約 32 GB |
