# B-operand（旧称 Phase D）— Layered-product operand source collection

> 文中の「Phase D」は 2026-07-11 のリネームで現「B-operand」。記録保全のため本文は当時の呼称のまま。

OpenShift Platform Plus (OPP) / Red Hat OpenShift AI (RHOAI) / OpenShift
Virtualization (CNV) の **operand ソース**を収集する新フェーズの計画。

策定日: 2026-06-03 / 状態: **PoC 完了(CNV @ 4.20)**。スクリプト3本実装済み

## PoC 結果(CNV @ 4.20、2026-06-03)

`kubevirt-hyperconverged` で discover→fetch→package を通し、検証済み。

| 指標 | 値 |
|------|---:|
| operand images(CSV relatedImages) | 61 |
| ソース解決 | **50 / 61 (82%)** |
| 取得 tarball(repo,ref dedup 後) | 27 / 27 OK・0 fail |
| 成果物 | `casket-20260603-cnv-ocp4.20.sqfs.xz` = **344 MB**(`/mnt/hdd/casket-ocp-test/`) |

解決方法内訳: `a:upstream-vcs`(upstream github URL+commit)36、`b:component-map`
(kubevirt-core→`kubevirt/kubevirt` tag)13、`c:oci-source`1、`z:none`(未解決)11。

**重要な知見**: konflux ビルドの layered product は `org.opencontainers.image.source`
が**内部 gitlab.cee** を指す(52/61)。Phase C の「github のみ」ロジックでは全滅する。
実ソースは `upstream-vcs-url`/`upstream-vcs-ref`/`upstream-version` ラベルにあり、
Phase D はこれを優先する(`scripts/phase-d-resolve-labels.py`)。コア kubevirt(virt-*,
passt, pr-helper 等)は upstream も gitlab を指すため、コンポーネント名→`kubevirt/kubevirt`
+ `upstream-version` の tag マッピングで救済。

未解決 11 件 = OCP base(`ose-coredns`/`ose-csi-*`/`ose-kube-rbac-proxy` 計6、**Phase A と重複**)
+ `virtio-win`/`virt-artifacts-server`(真の no-source)+ `hostpath-csi-driver`/
`hostpath-provisioner`/`kubevirt-ipam-controller`(内部 gitlab のみ、tag 不明)。CNV 固有の
実欠落は実質 5 件。

## downstream / upstream provenance(製品別、2026-06-08 調査)

「Phase D は downstream か upstream か」は**製品ごとに異なる**。各 `meta/labels.tsv` の
解決元 org を実機集計した結果(@4.20):

| 製品 | 解決元 org | 種別 |
|---|---|---|
| ACM | stolostron (47) | **downstream**(RH 公開 org)|
| MCE | stolostron (23), openshift (13) | **downstream**(公開)|
| ACS | stackrox (12) | RH 所有(公開、downstream=upstream)|
| ODF | red-hat-storage (2) | **downstream**(公開)|
| Quay | quay (1) | RH(公開)|
| RHOAI | red-hat-data-services (68) | **downstream**(公開)|
| **CNV** | **kubevirt (38=upstream)** | **upstream**(下記理由で downstream 化不可)|

→ **7製品中6つは既に Red Hat の公開 downstream org から取得済**(`c:source-label` =
`org.opencontainers.image.source`/`source-location` が公開 github を指す)。**CNV のみ upstream**。

### なぜ CNV だけ upstream か / downstream 化できない理由

CNV(virt-* コア)のラベル実値:
```
org.opencontainers.image.source = https://<internal-gitlab>/openshift-virtualization/konflux-builds/v4-20/kubevirt
upstream-vcs-url                = https://<internal-gitlab>/openshift-virtualization/downstream/kubevirt
upstream-vcs-ref                = <downstream commit>   upstream-version = 1.6.5-66-g…(upstream+RHコミット)
```
downstream ソースは **Red Hat 社内 GitLab `<internal-gitlab>`** にあり、
**公開ネットワークから DNS 解決すら不可**(VPN/SSO 必須・公開ミラー無し)。
リゾルバの `gh()` が非 github URL を捨て、公開 upstream `kubevirt/kubevirt`(tag) へ
フォールバックしている。**当環境(非 RH 社内)では CNV の downstream 化は経路が無く不可能。**
RHEL 固有挙動(machine type 等)が要る場合は Phase B(qemu, applied 済)側で見る住み分け。

> 社内アクセスがある環境で再ビルドするなら、`gh()` を「gitlab.cee も許可＋トークン clone」に
> 拡張すれば CNV も downstream 化できる(その環境限定)。それ以外の6製品は現状のまま downstream。

### 実装済みスクリプト

- `scripts/phase-d-discover.sh -p <pkg> -v <minor>` — FBC 抽出→head bundle→CSV→`images.tsv`
- `scripts/phase-d-fetch-source.sh -p <pkg> -v <minor> [--jobs N] [--limit N]` — ラベル解決→tarball 取得
- `scripts/phase-d-resolve-labels.py` — upstream 優先リゾルバ(`COMPONENT_MAP` に kubevirt-core)
- `scripts/phase-d-package.sh -p <pkg> -v <minor> -i <infix> [-o <dir>]` — 展開 stage→mksquashfs xz

ハマりどころ: bash `IFS=$'\t' read` はタブを空白扱いし**空フィールドを詰める**。ref/version
が空の行で列ずれするため、git.tsv 生成と DL リストは awk + `-` センチネルで処理(修正済み)。

## 配信形態(統合 + 全マイナー展開)

7製品を**1マイナー=統合1 casket**で配信: `casket-<date>-layered-ocp<minor>.sqfs.xz` を
`/srv/sources-layered-ocp<minor>/{cnv,acs,mce,acm,rhoai,odf,quay}/`(各 product 配下に
bundle/git/meta)。統合 stage は各 `phase-d/<pkg>/<minor>/50-out/stage` を `cp -al` で
1ツリーに集約して mksquashfs(`phase-d-combine.sh`)。

**全8マイナー(4.14–4.21)に展開済み(2026-06-04)**。各 0.8–1.1G。4.20=20260603、
他=20260604。旧・製品別 4.20 casket は `/mnt/hdd/casket-ocp-test/superseded-perproduct-20260603/`
に退避。

ビルド手順(マイナー1本):
```
for pkg in <7 packages>: phase-d-discover.sh -p $pkg -v $MINOR
                         phase-d-fetch-source.sh -p $pkg -v $MINOR --jobs 8
                         phase-d-package.sh -p $pkg -v $MINOR -i <infix> --stage-only
phase-d-combine.sh -v $MINOR -o /mnt/hdd/casket-ocp
```

### 全マイナー解決数(operand 解決 / 総数)

| minor | cnv | acs | mce | acm | rhoai | odf | quay |
|------|---|---|---|---|---|---|---|
| 4.14 | 42/49 | 12/12 | 32/33 | 10/46※ | 42/49 | 3/3 | 1/10 |
| 4.15 | 48/55 | 11/11 | 34/35 | 47/55 | 39/49 | 3/3 | 1/10 |
| 4.16 | 53/60 | 12/12 | 38/39 | 13/56※ | 89/100 | 3/3 | 1/10 |
| 4.17 | 53/58 | 12/12 | 37/38 | 50/64 | 89/100 | 3/3 | 1/10 |
| 4.18 | 54/59 | 12/12 | 38/39 | 50/64 | 89/100 | 3/3 | 1/10 |
| 4.19 | 55/60 | 12/12 | 38/39 | 50/64 | 89/100 | 2/2 | 1/10 |
| 4.20 | 50/61 | 12/12 | 38/39 | 50/64 | 89/100 | 2/2 | 1/10 |
| 4.21 | 61/64 | 12/12 | 38/39 | 50/64 | 89/100 | 3/4 | 1/10 |

※ 4.14/4.16 の ACM のみ解決率が低い。古い版のラベル方式差(`org.opencontainers.image.source`
未設定で `url`/`source-location` 違い)が原因の可能性。要追加調査(他は安定して 47–50/64）。
DL 失敗は全マイナー共通で `openshift/*`・`rh-openjdk`(Phase A 重複)。

## 横展開結果(2026-06-03、@ 4.20)

CNV に続き 6 製品を採取。下表は製品別の収集内訳(統合前の個別 casket サイズ)。

| 製品 | pkg | version | operand | 解決 | git dirs | casket |
|------|------|------|---:|---:|---:|---:|
| CNV | kubevirt-hyperconverged | 4.20.15 | 61 | 50 | 27 | 344M |
| ACS | rhacs-operator | 4.10.3 | 12 | 12 | 4 | 60M |
| MCE | multicluster-engine | 2.11.1 | 39 | 38 | 29 | 155M |
| ACM | advanced-cluster-management | 2.16.1 | 64 | 50 | 43 | 151M |
| RHOAI | rhods-operator | 2.25.6 | 100 | 89 | 38 | 273M |
| ODF | odf-operator | 4.20.13 | 2 | 2 | 2 | 11M |
| Quay | quay-operator | 3.16.4 | 10 | 1 | 1 | 9.3M |

実 repo 例: ACS=stackrox/*、MCE=openshift/assisted-*・hive・hypershift・stolostron/*、
ACM=stolostron/*(policy/observability/search/governance/console)、RHOAI=
red-hat-data-services/*(kserve, vllm, kubeflow, modelmesh, odh-dashboard, trustyai,
data-science-pipelines…)+ opendatahub-io/notebooks。

**製品ごとに source URL のラベルキーが異なる**ことが判明し、リゾルバを拡張(優先順):
`upstream-vcs-url`(CNV konflux)→ COMPONENT_MAP(kubevirt-core)→
`org.opencontainers.image.source`(ACM/MCE)→ `source-location`(ACS)→ `url`(RHOAI)
→ tag フォールバック。

ダウンロード失敗(MCE 2 / ACM 3 / RHOAI 18)は**全て `openshift/*`・`rh-openjdk`**
= OCP payload base 共有イメージで、downstream commit が public github に無い。これらは
**Phase A(`/srv/sources-ocp4.20.22`)で収集済み**なので実損失ではない。

### ODF / Quay(COMPONENT_MAP 対応済み、2026-06-03)

ODF/Quay の operand は github ソースラベルが無い(`url`=docs/catalog.redhat.com)ため、
`COMPONENT_MAP` を `name -> (repo, mode)` 構造に拡張して対応:

| 製品 | コンポーネント | repo | mode | 結果 |
|------|------|------|------|---:|
| ODF | odf-rhel9-operator | red-hat-storage/odf-operator | upstream-commit | OK(12M) |
| ODF | odf-console | red-hat-storage/odf-console | upstream-commit | OK(1.4M) |
| Quay | quay | quay/quay | tag(v3.16.4) | OK(13M) |

- `upstream-commit` = `upstream-vcs-ref` の commit(github に存在)で取得。`tag` = downstream
  commit が public github に無いため version tag で取得。`,` 区切りの重複 ref は先頭を採用。
- ODF=2/2、Quay=1/10 解決。`casket-20260603-{odf,quay}-ocp4.20.sqfs.xz`(11M/9.3M)を
  本番マウント(`/srv/sources-{odf,quay}-ocp4.20`)。
- **Quay の clair/quay-builder/quay-operator は構造的に不可**: RH 内部ビルドの版・commit が
  public upstream github(clair v4.x / quay-builder v3.2 / quay-operator v3.7)と不一致。
- **ODF の実ストレージソース**(ceph/rook/noobaa/ocs)は **スコープ外で確定(2026-06-03)**。
  技術的には別パッケージ(`ocs-operator`/`rook-ceph-operator`/`cephcsi-operator`/
  `odf-csi-addons-operator`/`noobaa`)を Phase D で個別採取すれば回収可能だが、ceph 本体など
  規模が大きく casket が狙う「OpenShift プラットフォームのコード」からは外縁のため採らない。
  ODF は umbrella(odf-operator + odf-console)止まりとする。

## 計画(以下は当初設計。CNV PoC で妥当性確認済み)


## 命名方針(2026-06-03 確定)

リポジトリ名は `ocp-source-collector` (旧 `casket-ocp`。2026-09 リネーム)。
内部変数 (`CASKET_WORK`) やアーカイブ名 (`casket-*`) は casket フォーマットを
指すため従来どおり。
README/CLAUDE.md の説明文は Phase D 着手時に「OCP リリースペイロード」→「OpenShift
エコシステム(OCP 本体 + layered products)」へ拡張する。

ファイル名は製品 infix で区別:
- OPP/CNV(OCP minor 紐づき): `casket-<date>-<product>-ocp<minor>.sqfs.xz`("ocp<minor>" は対応 OCP 版を示す)
- RHOAI(版独立): `casket-<date>-rhoai-<2.x>.sqfs.xz`("ocp" は付かない)

## 背景・課題

Phase C (現 Phase B) は `redhat-operator-index:v<minor>` の全オペレータを走査
するが、採取しているのは各オペレータの **head bundle の `containerImage` 1個**
= オペレータ制御部のソースのみ。OPP/RHOAI/CNV のような layered product の実体は
CSV の `spec.relatedImages` に列挙された **operand 群**(別イメージ)にあり、現状は
未展開。Phase D はこれを Phase A の「イメージ→ラベル→GitHub archive」手法で回収する。

## スコープ(確定)

| 項目 | 決定 |
|------|------|
| 深さ | operand 全部(CSV `relatedImages` 全展開) |
| 対象 | RHOAI / ACM+MCE / ACS / ODF+Quay / CNV |
| 展開範囲 | 最新 1 minor で PoC(OPP/CNV)→ 検証後に横展開 |
| RHOAI 版 | 最新 stable(チャネル head bundle、OCP minor 非依存) |
| PoC 起点 | CNV @ 最新 minor(回収率が高く検証が早い)。起点 minor は 4.20/4.21 で要確定 |

### 対象製品とパッケージ

| 製品 | カタログ内パッケージ | operand ソース回収の見込み |
|------|------|------|
| RHOAI | `rhods-operator` | 部分的(内部 git 混在) |
| ACM + MCE | `advanced-cluster-management` + `multicluster-engine` | 中(operand 最多・最大サイズ) |
| ACS | `rhacs-operator` | 中〜高(stackrox は OSS) |
| ODF + Quay | `odf-operator`(ceph/rook/noobaa)/ `quay-operator`(clair 等) | 中 |
| CNV | `kubevirt-hyperconverged` | 高(kubevirt/CDI が真の OSS) |

## アーキテクチャ(Phase A/C の資産を再利用)

手法・ラベル優先順・(repo,sha) dedupe・展開済みレイアウト・mksquashfs xz は既存と同一。
Phase C との差分は「1 image」→「relatedImages 全件(+ operator 本体)」への一般化のみ。

```bash
phase-d-discover.sh     -p <pkg> -v <minor>                 # head_bundle_image を pull → CSV 抽出
                                                            #  → .spec.relatedImages[] を images.tsv 化
phase-d-fetch-source.sh -p <pkg> -v <minor> [--jobs N] [--limit N]
                                                            # oc image info で各 image のラベル
                                                            #  → git.tsv → GitHub tarball
                                                            #  (phase-c-fetch-source のラベルロジック流用)
phase-d-package.sh      -p <pkg> -v <minor>                 # 展開 stage → mksquashfs -comp xz
```

### ディレクトリ構成(Phase A/C 準拠)

```
phase-d/<pkg>/<minor>/
├── 00-discover/   release/bundle CSV, images.tsv, labels.tsv, git.tsv
├── 20-git/        <name>-<short_sha>/...   (GitHub archive 展開済み)
└── 50-out/stage/  git/<name>-<sha>/  + meta/
```

### 成果物(per-product。ACM が大きいので分離)

- `casket-<date>-cnv-ocp<minor>.sqfs.xz`
- `casket-<date>-acm-ocp<minor>.sqfs.xz`（+MCE 同梱）
- `casket-<date>-acs-ocp<minor>.sqfs.xz`
- `casket-<date>-odf-ocp<minor>.sqfs.xz` / `casket-<date>-quay-ocp<minor>.sqfs.xz`
- `casket-<date>-rhoai-<2.x>.sqfs.xz`（版独立なのでファイル名も RHOAI 版基準）

## 工程(PoC)

| # | 作業 | 検証ポイント |
|---|------|------|
| 1 | `phase-d-discover.sh` 実装 → CNV で relatedImages 抽出 | image 件数・digest 解決 |
| 2 | `phase-d-fetch-source.sh`(phase-c-fetch-source 流用)→ `--limit` でスモーク | ラベル→github 解決率 |
| 3 | フル fetch → 回収率を git.tsv で集計 | NO_SOURCE 件数 |
| 4 | `phase-d-package.sh` → .sqfs.xz 生成・`file`/マウント確認 | サイズ・展開レイアウト |
| 5 | CNV 確定後、ACS→ODF/Quay→ACM→RHOAI の順に横展開 | 製品別の構造的限界把握 |

## 注意・構造的限界

- **回収率**: kubevirt/CDI(CNV)・stackrox(ACS)・ceph/rook/noobaa/clair(ODF/Quay)は
  真の OSS で高い見込み。RHOAI と ACM は内部 git(gitlab.cee 等)混在で Phase C と同様に
  部分回収(`access.redhat.com` / 内部ホストは救済不可)。
- **サイズ**: ACM が operand 最多。relatedImages は数十〜百超のことがあり、PoC の数値を
  見てから ACM の本採取を判断。
- **dedupe**: 製品間・operand 間で同一 (repo,sha) は1本化(Phase A/C と同じ `.work.tsv` 方式)。
- **マウント運用**: 既存の `mount-new-minor.sh` / swap 系と同パターンで fstab 追加
  (`/srv/sources-<product>-ocp<minor>`)。systemd unit 名は `systemd-escape -p` で生成。

## 次のステップ

1. CNV を本番ディレクトリ `/mnt/hdd/casket-ocp/` へ配置 + fstab マウント(`mount-new-minor.sh` と同パターン)。
2. 他製品へ横展開: ACS(`rhacs-operator`)→ ODF/Quay → ACM+MCE → RHOAI(`rhods-operator`, stable)。
   各製品でラベル傾向を確認し、必要なら `COMPONENT_MAP` を追加。
3. RHOAI/ACM は内部 git 比率が高い見込み。回収率を見て `phase-d-resolve-labels.py` を拡張。
4. CNV の `hostpath-*`/`ipam` は upstream repo が分かれば `COMPONENT_MAP` で追加救済可能。

## 決定済み

- PoC 起点 minor = **4.20**(2026-06-03 実施)。
- ファイル名 infix = 製品略称(`-i cnv` 等)。

## 2026-08-01: 取得漏れの一斉修正と全 minor 再ビルド

「OpenGrok が `layered-4.18/service-registry-operator` を索引していない」という
報告が起点。索引バグではなく**収集の穴**だった。詳細は CLAUDE.md の
"B-operand source coverage" 節に集約してある。ここには数字だけ残す。

修正前 → 後（実ソースを持つ製品数）:

| minor | 前 | 後 |
|---|---|---|
| 4.18 | 49 / 146 | **121** / 146 |
| 4.20 | 52 / 149 | **120** / 149 |
| 4.22 | 49 / 140 | **111** / 140 |

4.18 の operand image 1355 件では、resolved 769 のうち **767 が実際にツリーを持つ**
（exact 457 / approximate 310 / 取得失敗 2）。修正前は「resolved」でも
ダウンロードが 404 で落ちる行が大半だった。

### この作業で確定した注意点

- **`source_resolved` は coverage ではない**。repo URL が決まっただけで、ラベルの
  commit が public github に無い（konflux 内部 SHA）ケースが非常に多い。
  MANIFEST は `source_exact` / `source_approx` / `source_fetch_failed` に3分割し、
  `meta/git-fetched.tsv` に実際に取得した URL と `exact` を記録するようにした。
  CVE 用途でツリーを信用する前にこのファイルを見ること。
- **取得できなかった `openshift/*` を Phase A で代替できると考えないこと**。
  取得不能 82 repo のうち payload にあるのは **17 だけ**。metallb / sriov-\* /
  velero / ViaQ/\* / migtools/\* は layered 専用。
- **候補チェーンの救済はほぼ branch 頭**。4.18 の取得不能 156 repo を実測したところ
  156 件とも何かは取れたが、**155 件は main/master/release-X**。だから
  approximate として明示的に記録する設計にした。
- **`COMPONENT_MAP` は版数が上流タグに一致するものだけ足す**。main/master にしか
  当たらない製品（amq-broker, strimzi, noobaa, 3scale, Quay, Kuadrant の
  サブコンポーネント）は、repo の同定はできても版数体系が別なので**意図的に未マップ**。
  理由はコード中のコメントに残してある。
- `konflux-ci/mintmaker` は KMM 4 image が source label に持つ**ビルドbot**。
  `INFRA_REPOS` で拒否し `z:infra-label` として名指しする（黙って捨てない）。

### 所要時間の実測

全 9 minor で約14時間、**平均 1.5 時間 / minor**。序盤のサンプルから「8時間/minor」と
見積もったのは過大だった。大半は取得済み tarball の SKIP で進む。
