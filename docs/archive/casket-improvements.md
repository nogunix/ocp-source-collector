# casket 改善点メモ

起点: 2026-06-08 OCP-V New→Old VM移行調査（分析メモは非公開）中に発見した、casket利用上の問題点と改善案。

## ステータス (2026-06-08)

- **#1 / #3 → 実装・本番反映済み。** `scripts/build-source-index.py` を新設し、
  Phase A/C/D の package スクリプトに組み込み（staging 直後に実行）。各 `git/` 直下に
  `INDEX.tsv`（dir｜repo｜ref｜version｜components）＋ `by-component/<name>`・
  `by-repo/<repo>` の相対シンボリックリンク木を**追加生成**（ディレクトリ名は不変＝可逆）。
  既存 casket への後付けは `scripts/repackage-add-index.sh`（overlayfs で索引を重ねて
  再 mksquashfs）＋ `scripts/swap-source-index.sh`（fstab 差し替え＋mount 再起動、
  既定 dry-run / `--apply` で実行）。全 24 casket を 2026-06-08 に `casket-20260608-*`
  として作り直し・本番差し替え済み（旧ファイルはロールバック用に温存）。
  採用したのは下記改善案の **2（by-component/by-repo 木）＋ 3（INDEX.tsv）**。
  案1（dir名を repo basename に改名）は破壊的なため不採用、ディレクトリ名は据え置き。
- **#2 → 部分対応。** `method=b:component-map` かつ ref 空が「tag 取得（NO_SOURCE ではない）」
  である旨を Phase D README に明記。git.tsv への実取得タグ列の追加は未着手。

以下は当時の問題記述（経緯として残置）。

## 1. 【最重要】dedup後のディレクトリ名が実体repoを表さない

### 症状
`(repo, tag)` または `(repo, commit)` で重複排除する際、生成ディレクトリ名は
**「そのキーで最初に見えたコンポーネント名」**になる（Phase A/C/D 共通仕様）。
このため、複数の無関係コンポーネントが同一 `kubevirt/kubevirt` リポジトリにマップされる
Phase D の `component-map "tag"` で、実体と名前が乖離する。

実例（CNV）:

| マウント上のディレクトリ名 | 実体 | 中身 |
|---|---|---|
| `cnv/git/passt-network-binding-plugin-cni-v1.4.1/` (4.18) | **kubevirt/kubevirt @ v1.4.1 全体** | `pkg/virt-config/`, `feature-gates.go`, `virt-launcher` 等 |
| `cnv/git/libguestfs-tools-v1.6.5/` (4.20) | **kubevirt/kubevirt @ v1.6.5 全体** | 同上 |

→ virt-api/virt-controller/virt-handler/virt-launcher/virt-operator/passt/libguestfs-tools
   等はすべてこの1ディレクトリに集約されている（dedup自体は正しい）。
→ **KubeVirt本体という最重要ソースが、無関係な名前の下に「隠れて」しまい、
   ソースブラウズで発見不能**。実際、本調査でも当初「NO_SOURCEで未収録」と誤判定した。

### 影響
- 利用者（顧客・サポート）が目的のソースに辿り着けない。
- OpenGrok等のインデクサでもプロジェクト名が誤表示される。

### 改善案（いずれか）
1. **dedupディレクトリ名を repo basename ベースにする**
   例: `kubevirt-v1.4.1/`, `containerized-data-importer-1.61.5/`。
   コミット時は `kubevirt-<short_sha>/`。最も直感的。
2. **component → dir のシンボリックリンク木を mount 上に追加**（Phase B `by-ocp/` と同方式）
   例: `cnv/by-component/virt-launcher -> ../git/kubevirt-v1.4.1`。
   既存ディレクトリ名を変えずに発見性だけ上げられる。
3. 最低限、`git/README` か `git/INDEX.tsv`（component→実dir→repo）を mount 直下に置く。

## 2. git.tsv の tarball 列が無関係名を指し、ref 空で NO_SOURCE に見える

### 症状
`meta/git.tsv` で kubevirt/kubevirt 系コンポーネントの行が:

```
virt-api  https://github.com/kubevirt/kubevirt  (ref空)  passt-network-binding-plugin-cni-v1.4.1.tar.gz  1.4.1  b:component-map
```

- tarball 列＝無関係な passt 名（#1と同根）。
- ref 空＋version のみ → 一見 NO_SOURCE と誤読しやすい（実際は tag `v1.4.1` で取得成功）。

### 改善案
- 解決済みの**実取得タグ（v1.4.1）と実DL URL**を git.tsv に明示列追加（traceability）。
- tarball/dir 列を #1 の repo basename 命名に合わせる。
- `method=b:component-map` かつ ref 空は「tag取得」であることをヘッダ/READMEで明記
  （現状 NO_SOURCE は `z:none`/tarball=`NO_SOURCE` と別物だが紛らわしい）。

## 3. mount 上に「逆引き（dir → どのoperand画像か）」が無い

`meta/MANIFEST.json` を読まないと、ある git ディレクトリがどの operand 由来か分からない。
#1案2の by-component シンボリックリンク木があれば解消。Phase A/C にも同様に有効。

## 4. （任意）vendored と本体の重複

`kubevirt.io/api` が ~19 operand に vendor され、さらに kubevirt 本体も収録。
squashfs xz が実バイト重複は吸収するため容量影響は軽微だが、
「APIスキーマの正典は本体 `staging/src/kubevirt.io/api`」である旨を README に書くと混乱が減る。

## 5. Phase B SRPM: パッチ未適用で downstream機能がソースツリーに見えない

### 症状
`srpms/<NEVR>/` は spec + 展開ソース + `patches/` を置くが、**パッチは未適用**。
RHEL独自機能はパッチで足されるため、ソースツリーを browse しても見えない。

実例: machine type `pc-q35-rhel9.6.0` は
`qemu-kvm-9.1.0-15.el9_6.18/qemu-9.1.0/` には**存在せず**、
`patches/0025-redhat-Add-rhel9.6.0-machine-type.patch` にしかない。
（今回の qemu machine type 調査で patches/ も grep する必要があった。）

### 影響
- 「ソースを読めば分かる」と思った downstream 専用挙動（machine type, RHEL固有CVE修正等）を
  見落とす。OpenGrok インデックスにも未適用状態で載る。

### 改善案
- `srpms/<NEVR>/README` か meta に「パッチは未適用。downstream差分は `patches/` 参照」を明記。 ✅
- （任意・重い）`%prep` 相当で `patches/` を当てた applied ツリーを別途用意。

### applied-tree モード実装（2026-06-08, コード）
`scripts/phase-b-extract-one.sh` に **`PHASE_B_APPLY=1`** モードを追加（既定オフ＝従来のraw抽出のまま安全）。
オン時は `.src.rpm` を一時 `_topdir` に `rpm -i` → `rpmbuild --nodeps -bp` で %prep（%setup+%autopatch等）を実行し、
**適用済みソースツリー**を `<NEVR>/<tree>` に stage（`<NEVR>/.patches-applied` マーカー、`patches/` も参照用に保持）。
%prep 失敗時は raw 抽出へ自動フォールバック（`.patches-unapplied`）。rpm4(`BUILD/<tree>`)/rpm6(`BUILD/<n>-<v>-build/`＋SPECPARTS) 両レイアウト対応。
合成SRPMで機構検証済（patch適用→ツリーに反映、マーカー付与）。`phase-b-package.sh` 実行時に env を渡せば伝播。

### 本番反映（2026-06-08, 完了）
`rhel9-srpm` VM は**消滅ではなく shut off だった**（`sudo virsh list --all` に存在、qcow2健在、SCA登録済）。起動して収集物を確認したところ
`~/scripts/srpms/` に **843 src.rpm が全収集済**だった → 再DL不要。手順:
1. VM起動・SSH(cloud-user)・843 src.rpm をホスト `phase-b/srpms/`(8.5G)へ rsync。
2. ホスト(rpm6)で `PHASE_B_APPLY=1 phase-b-package.sh`（`KEEP_STAGE`再利用）→ 適用抽出。
   `%autopatch -p1` 形式は適用成功(675)、%prep失敗(168, redhat-rpm-config等のマクロ/ツール差)は raw fallback。
3. `%autosetup -S git` が生む `.git`(167個・8.6G)を除去（スクリプトに恒久化）→ 14G→**5.3G**。
4. `casket-20260608-ocp-srpms.sqfs.xz`(5.3G) を swap（旧20260529保持、fstab backup `/etc/fstab.bak-20260608-srpms-applied`）。
5. 検証: `/srv/sources-ocp-srpms/srpms/qemu-kvm-9.1.0-15.el9_6.18/qemu-9.1.0/hw/i386/pc_q35.c:675`
   に `pc_q35_rhel_machine_9_6_0_options`(RHEL-9.6.0 PC) が**ソース上で可視**（従来patches/のみ）。

> 注: applied 675 / unapplied 168。unapplied は %prep が host(rpm6)で失敗した分で raw ツリー＋`.patches-unapplied`
> マーカー（patches/ 参照）。RHEL VM(rpm4)上で抽出すれば成功率は上がる見込み（qemu等の本命は applied 済）。
> OpenGrok の `srpms` プロジェクトは旧内容を索引中 → 反映には再インデックス要（次回 run-opengrok.sh）。

## 6. Phase B SRPM の by-ocp が 4.21 を欠く（Phase A/C/D は 4.21 あり）

`sources-ocp-srpms/by-ocp/` は 4.14〜**4.20 まで**。一方 layered/Phase A/C は 4.21 まで提供。
→ 4.21 の SRPM/qemu クロス参照（例: 4.21 の qemu machine type 確認）が**できない**。
Phase B 4.21 収集は PAUSED（[[phase-b-421-progress]]）が原因。再開して 4.21 を追加すれば解消。

## 7. OpenGrok: 起動毎に 215G 索引volumeへ `chown -R`（~20分）

### 症状
`opengrok/scripts/entrypoint-ro.sh` が **起動毎**に `chown -R appuser:appgroup /opengrok/data`
を実行。索引volumeは ~215G あり、tomcat/REST 配信開始まで**毎回20分級**ブロックされる
（podman top で PID4 chown が数分継続、REST 401/無応答が続くのを確認）。

### 改善（実装＋検証済 2026-06-08）
所有者が既に `appuser` なら -R をスキップする冪等化。初回（podman作成の root所有）/
airgap配備（root展開）時のみ実行、通常の再起動は即スキップ。`FORCE_DATA_CHOWN=1` で強制。
→ 検証: `podman restart` で「already owned by appuser; skipping recursive chown」、
chownプロセス無し、**REST が数秒で応答**（従来 ~20分）。`def` 検索も即結果。

## 優先度 / 実装状況

| # | 改善 | 優先 | 範囲 | 状況 |
|---|---|---|---|---|
| 1 | dedupディレクトリ発見性（by-component/by-repo リンク木） | **高** | Phase A/C/D | ✅ 実装＋**本番反映済**(casket-20260608-*, 24本live 2026-06-08) |
| 2 | git.tsv の component-map=tag 明記（NO_SOURCE誤読防止） | 中 | Phase D | ✅ README注記済 |
| 3 | mount上の逆引きインデックス（git/INDEX.tsv） | 中 | Phase A/C/D | ✅ 実装＋本番反映済 |
| 4 | vendored vs 本体の注記 | 低 | Phase D (CNV) | ⬜ 未 |
| 5 | SRPMパッチ未適用 → applied-treeモード（PHASE_B_APPLY=1） | 中 | Phase B | ✅ **本番反映済**(casket-20260608-ocp-srpms 5.3G live; applied675/unapplied168; pc-q35-rhel9.6.0 可視) |
| 6 | SRPM by-ocp の 4.21 欠落（Phase B再開） | 中 | Phase B | ✅ 解決（`by-ocp/` は 4.14–4.22 の 9 マイナーを収録） |
| 7 | OpenGrok 起動毎 chown -R（~20分）の冪等化 | 中 | OpenGrok | ✅ 実装＋本番検証済（~20分→数秒） |

### 実装内容（2026-06-08）
- 新規 `scripts/build-source-index.py`: staged `meta/MANIFEST.json`（A/C/D形式 自動判定）から
  `git/INDEX.tsv`（dir｜repo｜ref｜version｜components）と `by-component/<名前>`・`by-repo/<repo名>`
  の相対シンボリックリンク木を生成。dedup命名で隠れた実体（例 `by-repo/kubevirt` → 本体ツリー、
  `by-component/virt-launcher` → 同）を発見可能にする。
- `scripts/package.sh`(A) / `phase-c-package.sh`(現 `phase-b-package.sh`) / `phase-d-package.sh`(現 `phase-b-operand-package.sh`) が staging 後に上記を呼ぶ＋README追記。
- `scripts/phase-b-package.sh`: patches未適用の注記を README に追加（#5）。
- 既存 git/ dir 名は**変更しない**（追加のみ・可逆）。squashfsは相対symlinkを保持するためmount後に解決。
- 反映には対象casketの再ビルド（repackage）が必要。#4 は未対応。

### 本番反映（2026-06-08）
中間ワークは削除済のため全re-fetchを避け、`scripts/repackage-add-index.sh` で
**overlayfs**（read-onlyなcasketマウント＋生成indexを重ねて再mksquashfs、ソース実体は再利用）
により24本を `casket-20260608-*.sqfs.xz` として再生成（`scratch/rebuild-all.sh`、約80分、全OK）。
`scripts/swap-source-index.sh --apply` でfstab差し替え＋mount unit再起動（旧版は残置でロールバック可、
`/etc/fstab.bak-20260608-preindex`）。全24マウントがindex入りでlive。
検証例: `/srv/sources-layered-ocp4.20/cnv/by-repo/kubevirt` → kubevirt本体(`pkg/virt-config`)に解決。

> 補足: #1 は「dedup は filename に first-seen component を使う」という既存設計
> （`CLAUDE.md` のアーキ注記）に起因。dir名は変えず発見性レイヤを足す方式で対応。
> #1〜#4 は 2026-06-08 New→Old VM移行調査、#5〜#6 はその qemu machine type 確認中に判明。
