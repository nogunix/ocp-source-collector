# ocp-source-collector 利用者ガイド

OpenShift / RHCOS のソースコードを `/srv/sources-*` から閲覧するためのリファレンス。

## マウントポイント一覧

`/etc/fstab` で常時マウントされている。再起動後も自動マウント、`ro` (read-only)。

| マウントポイント | 内容 | 由来 |
|---|---|---|
| `/srv/sources-ocp4.14.58` 〜 `/srv/sources-ocp4.20.22` | OCP コンポーネントの github ソース (各 minor 1 マウント、計 8) | Phase A |
| `/srv/sources-ocp-srpms` | rhel-coreos の SRPM 展開ソース (全 7 OCP 版 union, 843 SRPM) | A-rpm |
| `/srv/sources-ocp4.14-operators` 〜 `/srv/sources-ocp4.20-operators` | OperatorHub redhat-operators の bundle + ソース (各 minor 1 マウント、計 8) | Phase B |
| `/srv/sources-rhel9` / `/srv/sources-rhel10` | 既存 RHEL casket (参考) | 別系統 |

全マウント一覧:

```bash
mount | grep /srv/sources-
```

---

## レイアウト早見表 (3 phase 共通: すべて展開済み、`cd` で即閲覧可)

### Phase A — OCP コンポーネントソース

`/srv/sources-ocp<X.Y.Z>/`

```
git/<name>-<short_sha>/   ← 各コンポーネントの github ソース (commit ピンナップ済み)
  cmd/ pkg/ ...           ← github archive の top-level dir を strip 済み、いきなりソース
meta/
  MANIFEST.json           image -> tarball 対応表 (sha256 付き)
  commits.tsv             name TAB repo_url TAB commit_sha
  images.tsv              name TAB pullspec TAB digest
  release.json            oc adm release info --output=json
```

### A-rpm — SRPM 展開ソース

`/srv/sources-ocp-srpms/`

```
srpms/<NEVR>/             NEVR = name-version-release (例: bash-5.1.8-9.el9)
  <name>.spec             RPM spec
  <tarball-top-dir>/      上流ソース展開済み (Source* tarball 由来、複数なら複数 subdir)
  patches/                .patch + 補助ファイル
meta/
  MANIFEST.json           SRPM 索引 + 各 OCP 版の binary RPM 一覧
  rpmdb/<OCPver>.tsv      name TAB epoch TAB ver TAB rel TAB arch
  README.txt
```

`.src.rpm` 自体は同梱されていない (rpmbuild が必要なら rhel-coreos rpmdb から再取得)。

### Phase B — Operator bundle + ソース

`/srv/sources-ocp<X.Y>-operators/`

```
catalog/<operator>/catalog.json    File-Based Catalog (FBC) verbatim
bundles/<operator>/<head>/
  manifests/                       CSV + CRDs (OLM payload)
  metadata/                        annotations.yaml
git/<name>-<short_sha>/             github source (展開済み)
meta/
  MANIFEST.json
  operators.tsv                    name TAB channel TAB head_bundle TAB head_image
  containers.tsv                   name TAB head_bundle TAB containerImage TAB csv_version
  labels.tsv                       containerImage TAB source_url TAB vcs_ref
  git.tsv                          name TAB source_url TAB vcs_ref TAB tarball|NO_SOURCE
```

---

## よくある操作

### あるパッケージのソースを開く

```bash
# OCP コンポーネント (例: 4.20.22 の etcd)
cd /srv/sources-ocp4.20.22/git/etcd-*/
ls

# SRPM (例: bash)
cd /srv/sources-ocp-srpms/srpms/bash-5.1.8-9.el9/bash-5.1/
ls

# operator (例: 4.20 の OpenShift GitOps)
cd /srv/sources-ocp4.20-operators/git/gitops-operator-*/
```

### パッケージ名がうろ覚え

```bash
# Phase A: コンポーネント一覧
ls /srv/sources-ocp4.20.22/git/ | sort

# A-rpm: 利用可能な SRPM 一覧
ls /srv/sources-ocp-srpms/srpms/ | sort

# 部分一致検索 (どの phase でも)
ls /srv/sources-ocp-srpms/srpms/ | grep -i kernel
```

### 全 SRPM 横断で grep (例: ある関数定義を探す)

```bash
grep -r "function_name" /srv/sources-ocp-srpms/srpms/ 2>/dev/null | head
```

squashfs はメモリにキャッシュされるので、繰り返しの grep は十分速い。とはいえ初回はディスク読みが入るので `--include` で絞ると速い:

```bash
grep -r --include='*.c' "func_name" /srv/sources-ocp-srpms/srpms/
```

### あるバイナリ RPM がどの SRPM から来たか

```bash
# rpmdb から逆引き (NEVR を取得)
grep -i '^cri-o' /srv/sources-ocp-srpms/meta/rpmdb/4.20.22.tsv

# → 出た name-ver-rel が srpms/<NEVR>/ に対応
ls /srv/sources-ocp-srpms/srpms/cri-o-*
```

### コンポーネントの commit hash を確認 (Phase A)

```bash
grep ^etcd /srv/sources-ocp4.20.22/meta/commits.tsv
# → etcd  https://github.com/openshift/etcd  <full_sha>
```

ディレクトリ名 `etcd-<short_sha>` の short_sha は `commits.tsv` の最初の 12 文字。

### operator の CSV を見る (Phase B)

```bash
cd /srv/sources-ocp4.20-operators/bundles/openshift-gitops-operator/
ls */manifests/*.clusterserviceversion.yaml
```

CSV は YAML / JSON 1 ファイル。`yq` / `jq` で属性抽出可能:

```bash
yq '.spec.install.spec.deployments[].spec.template.spec.containers[].image' \
    bundles/openshift-gitops-operator/*/manifests/*.clusterserviceversion.yaml
```

### MANIFEST.json で機械的に索引

```bash
# Phase A: あるイメージの tarball ファイル名
jq '.images[] | select(.name=="etcd") | .tarball' \
   /srv/sources-ocp4.20.22/meta/MANIFEST.json

# A-rpm: SRPM の sha256
jq '.srpms[] | select(.nevr=="bash-5.1.8-9.el9") | .sha256' \
   /srv/sources-ocp-srpms/meta/MANIFEST.json

# Phase B: あるイメージの source_url
jq '.git[] | select(.operator=="openshift-gitops-operator")' \
   /srv/sources-ocp4.20-operators/meta/MANIFEST.json
```

---

## 制約と注意

| 項目 | 内容 |
|---|---|
| `ro` マウント | 全 casket 読み取り専用。書き込みたい場合は `cp -r` で別パスへ |
| A-rpm 補完 | 一部 bin-RPM (2 件、`redhat-release-9.2-*`) は EUS チャネル消失で未収録 |
| Phase B source 解決 | CSV repository が `access.redhat.com/containers/...` の operator は github tarball を取得不可 (各 minor で 50〜60 件)。`meta/git.tsv` に `NO_SOURCE` と記載 |
| Phase A の patch 単位 | Phase A は **OCP patch** バージョン (例: 4.20.22)、Phase B は **minor** (例: 4.20)。マウント名で区別する |
| ファイル属性 | `mksquashfs -all-root` で全ファイル `root:root` 所有、`a+r` 権限 |
| 直接 `rpm -i` 不可 | A-rpm は SRPM 自体は入っていない (展開ソースのみ)。`rpmbuild` したい場合は rhel-coreos rpmdb から再取得 |

---

## トラブル時

| 症状 | 対処 |
|---|---|
| `/srv/sources-*` が空 / マウント外れた | `sudo systemctl restart srv-sources\\x2d<...>\\.mount` (パスを systemd-escape で生成) |
| `ls` が遅い | 初回ディレクトリ読みは遅め。2 回目以降キャッシュで高速化 |
| `grep -r` でメモリ食う | `--include` で絞る、または `git/<one>` のように対象を限定 |
| ファイルが壊れている疑い | `unsquashfs -s /mnt/hdd/casket-ocp/casket-*.sqfs.xz` で squashfs 自体の整合性確認 |

---

## 参考: 中身を別マシンに持ち出す

```bash
# 単一 SRPM ディレクトリをコピー
cp -r /srv/sources-ocp-srpms/srpms/bash-5.1.8-9.el9 /tmp/

# casket ファイル自体を別ホストへ転送 (3.5G、A-rpm の場合)
scp /mnt/hdd/casket-ocp/casket-20260528-ocp-srpms.sqfs.xz user@otherhost:/path/
# 転送先で:
#   sudo mount -o loop /path/casket-20260528-ocp-srpms.sqfs.xz /mnt/sources
```

`.sqfs.xz` は内部 xz 圧縮の squashfs (外側 xz ラップなし)。`mount -t squashfs` でそのまま読める。

---

## 関連ドキュメント

- `README.md` — システム全体の構成、ビルド手順、設計判断
- `CLAUDE.md` — メンテナ向けプロジェクト規約
- 各 casket 内 `meta/README.txt` — phase 個別の簡易ガイド
