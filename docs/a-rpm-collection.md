# a-rpm 収集手順（ノードOS + 拡張 + EUS 補填 + コンテナ内RPM層）

`casket-*-ocp-srpms.sqfs.xz` の中身は**3つの異なる収集ストリームの合流**で、
`phase-a-rpm-package.sh` はその合流結果（`phase-a-rpm/srpms/` に置かれた
`*.src.rpm`）を読むだけ。合流の手順自体はこれまでどこにも書かれておらず、
2026-08-11 の更新時に「素直に build すると 3270 → 795 に激減する」形で問題が表面化した。
この文書はその再発防止。

> **重要**: casket は展開済みツリーのみを持ち `.src.rpm` を残さない（設計）。
> つまり**マウントから収集物を復元することはできない**。`srpms/` を消す前に、
> それが唯一の原本でないことを必ず確認する。

---

## 全体像

```
                                     ┌──────────────────────────┐
 A.  ノードOS ベース (rhel-coreos) ─▶│                          │
 A2. ノードOS 拡張 (…-extensions) ─▶│  phase-a-rpm/srpms/      │─▶ phase-a-rpm-package.sh
 B.  EUS/E4S/FDP 補填            ──▶│  *.src.rpm をここに集約   │    └─ 50-out/stage → .sqfs.xz
 C.  コンテナ内RPM層 (Pyxis+CDN)  ─▶│                          │
                                     └──────────────────────────┘
```

2026-08 時点の実績規模:

| ストリーム | 件数 | 自動化 |
|---|---|---|
| A. ノードOS ベース | 795 | **済** (`01`/`02`) |
| A2. ノードOS 拡張 | 77 SRPM 未取得（2026-08-12 判明） | インベントリ**済**、取得は VM 実行待ち |
| B. EUS 補填 | ~120（`srpms-fill*` 33+45+45） | **済** (`03`) |
| C. コンテナ内RPM層 | 2203 (`srpms-containers/`) | **インベントリのみ**。取得は手動 |
| 合流後の casket | **3315** | — |

**ノードOS は「1イメージ = 1 rpmdb」ではない。** ペイロードには
`rhel-coreos`（ベース）とは別に `rhel-coreos-extensions` があり、4.21 以降は
さらに el10 版が2つ増える。A だけを収集すると kata などが丸ごと落ちる。詳細は A2。

---

## A. ノードOS（RHEL9 VM 内）

VM `rhel9-srpm`（libvirt、autostart 無効、登録済みで永続化）。

```bash
sudo virsh start rhel9-srpm
sudo virsh net-dhcp-leases default          # IP 確認
scp ~/.docker/config.json cloud-user@$VMIP:~/pull-secret.json
scp phase-a-rpm/vm/scripts/*.sh cloud-user@$VMIP:~/scripts/

ssh cloud-user@$VMIP
cd ~/scripts
VERSIONS_OVERRIDE="4.14.58 4.15.59 …"  \
  CACHE_DIR=$HOME/run-<date>/rpmdb-cache \
  TSV_DIR=$HOME/run-<date>/rpmdb-tsv \
  ./01-extract-rpmdb.sh                     # 9版で ~10分

TSV_DIR=$HOME/run-<date>/rpmdb-tsv \
  OUT_DIR=$HOME/run-<date>/srpms \
  WISH=$HOME/run-<date>/wishlist.txt \
  LOG=$HOME/run-<date>/fetch.log \
  MISSING=$HOME/run-<date>/missing.txt \
  ./02-fetch-srpms.sh                       # ~3.5時間
```

**必ず新しい `run-<date>/` に出す。** VM には過去の中断分が残っており
（`~/srpms` 226件、`~/rpmdb-tsv` は旧パッチ）、混ぜると存在しないパッチの
rpmdb を casket に配ることになる。

`VERSIONS_OVERRIDE` は**必ず現行パッチを渡す**。省略時のデフォルトは
ハードコードされた古い値。現行値の取り方:

```bash
CASKET_WORK=$PWD bash -c 'source scripts/lib.sh; source scripts/lib-fingerprint.sh
  while read -r m; do [ -n "$m" ] && fetch_patch "$m"; done \
    < <(grep -v "^#" config/phase-a-rpm-minors.txt | grep -v "^$")'
```

### 罠

- **rpmdb 抽出は VM 必須**。Fedora ホストの新しい rpm では 4.14〜4.18 の
  rpmdb 変換が format mismatch で落ちる（`docs/design-notes.md:14`）
- **`--rpmdb-image=rhel-coreos` 必須**。4.14/4.15 には旧 el8 の
  `machine-os-content` が同居しており、既定ではそちらを引いてしまう
- **`zsh` から `lib.sh` を source しない**。`${BASH_SOURCE[0]}` が解決されず
  `CASKET_WORK` が `$HOME` に化け、指紋も registry の書き込み先もずれる

---

## A2. ノードOS 拡張（`rhel-coreos-extensions`）

`01` が読むのはベース rpmdb だけで、これは**ノードOSの半分**でしかない。
オプション機能はペイロードの別イメージ `rhel-coreos-extensions` に素の RPM
として同梱され、

```
MachineConfig{Spec:{Extensions: ["sandboxed-containers"]}}
  → machine-config-operator が "kata-containers" に翻訳
  → rpm-ostree でノードに載る
```

という経路でノードに入る。**ベース rpmdb には一切現れない**ので a-rpm から
不可視だった。4.20.32 で 162 RPM / 10 extension、うち 89 バイナリ
（23 SRPM）が casket のどこにも無かった。

発覚のきっかけは kata（OSC の operator が
`sandboxed-containers-operator/controllers/daemonset_reconcile.go:34` で
まさにこのイメージを実行時に解決している）だが、穴は kata だけではない:
`kernel-rt` `usbguard` `libreswan` `pacemaker`/`pcs` `crun-wasm` `wasmedge`
`fence-agents` なども同じ。

### インベントリ（ホスト側、VM 不要）

```bash
scripts/phase-a-rpm-extensions-inventory.sh          # 全マイナー、~10分
scripts/phase-a-rpm-extensions-inventory.sh -v 4.20.32   # 単発
```

`oc image extract` で `/usr/share/rpm-ostree/extensions/` を取り出し、
`rpm -qp` で **`01` と同じ5列**の tsv を吐く。VM も subscription も不要
（RPM を読むだけで install しないため）。出力は
`phase-a-rpm/rpmdb-extensions-<date>/`:

| ファイル | 内容 |
|---|---|
| `<patch>-extensions.tsv` | name\|epoch\|version\|release\|arch |
| `meta/extensions-map.tsv` | patch\|extension\|package |
| `meta/extensions-missing.txt` | **プールに無い SRPM = これから取るもの** |
| `meta/extensions-deferred.txt` | 意図的に未収集のペイロードタグ |
| `cache/<digest>/` | 展開済み RPM（再実行は7秒） |

2026-08-12 実測（9マイナー）: 70/70/102/80/84/162/162/162/153 RPM、
プール未収録の SRPM **77**（kata は各マイナー1つずつ計8バージョン）。

### 取得

tsv を VM の `$TSV_DIR` に置けば `02`/`03` がそのまま食う。**`01` の出力と
同じディレクトリに混ぜてよい** — ファイル名の `-extensions` サフィックスは
`02` の minor 導出でも `by-ocp` のキーでも正しく扱われ、by-ocp 上は
`by-ocp/<patch>-extensions/` という別ツリーになる（既定インストールされる
パッケージ集合と混ざらないように、意図的にこうしている）。

```bash
scp phase-a-rpm/rpmdb-extensions-<date>/*-extensions.tsv cloud-user@$VMIP:~/run-<date>/rpmdb-tsv/
# あとは A と同じ 02 → B の 03
```

### 罠

- **補助ファイルを `*.tsv` グロブに晒さない。** `02` も
  `phase-a-rpm-by-ocp.sh` も入力を `"$DIR"/*.tsv` の素のグロブで読む。3列の
  `extensions-map.tsv` を5列のパッケージ一覧と同じ階層に置くと、黙って
  パッケージ一覧として解釈され `by-ocp/extensions-map/` が生える。だから
  補助出力は `meta/` に隔離してある
- **rhaos の dist tag はペイロードのマイナーと一致しない。** 4.20.32 の
  extensions は `kata-containers-3.31.0-4.rhaos4.19.el9` を積んでおり、SRPM は
  `rhocp-4.19` の source repo にある。`02` は tsv のファイル名だけでなく
  **中身の `rhaos<MAJ>.<MIN>` からも** rhocp マイナーを導出する（2026-08-12 追加）。
  これが無いと「単に見つからない」形で静かに落ちる
- **el9 と el10 は別イメージ。** 下の「積み残し」参照

## B. EUS / E4S / FDP 補填（同じ VM 内）

`02` の `missing.txt` に残るのは、ほぼ**マイクロリリース**（`el9_2` / `el9_4`
/ `el9_6` …）。通常の repo には無く、**`--releasever` を tag に合わせて
EUS/E4S を引く**必要がある。2026-08-11 の実測では未解決 477 件のうち
**471 件が `el9_N`、6 件が `el9fdp`** で、どちらも下記でカバーできる。

```bash
cd ~/scripts
cp $HOME/run-<date>/missing.txt .    # 03 系は ~/scripts/missing.txt を読む
./03-fetch-eus.sh                    # EUS + E4S + fast-datapath
./03c.sh                             # EUS のみで取りこぼしを再試行
```

`03-fetch-eus.sh` は `el9_2→9.2`, `el9_4→9.4`, `el9_6→9.6`, `el9_0→9.0`,
`el9→9` の対応で `dnf download --source --releasever=<rv>` を回し、
`el9fdp` は fast-datapath repo を 9.2/9.4/9.6 で試す。出力は `--destdir srpms`
なので **A と同じディレクトリに積み増しされる**。

> `02-fetch-srpms.sh` は起動時に EUS/E4S を**無効化する**（releasever pin が
> 無いと CDN が 404 を返し、`set -e` でスクリプトごと死ぬため）。
> B はその後に、pin 付きで明示的に引き直す工程。順序を入れ替えないこと。

---

## C. コンテナ内RPM層 ⚠️ **未自動化**

ノードOS の外側、**コンテナイメージの中**に入っている RPM の層。
virt-launcher の qemu/libvirt、ODF の ceph などがここに該当する。
a-rpm の 3315 件のうち **2203 件がこれ**で、最大のストリーム。

### C-1. インベントリ（スクリプトあり）

```bash
CASKET_WORK=$PWD scripts/phase-a-rpm-image-inventory.py [--limit N] [--pool DIR]
```

Pyxis API（`catalog.redhat.com`、**イメージ pull も認証も不要**）から各イメージの
RPM マニフェストを引く。出力は `phase-a-rpm/` 配下:

| ファイル | 内容 |
|---|---|
| `pyxis-cache/<digest>.json` | 生レスポンス（再実行は無料） |
| `image-rpms.tsv` | digest \| repo \| srpm_nevr |
| `image-rpms-missing.txt` | **プールに無い SRPM NEVR = これから取るもの** |
| `image-rpms-unresolved.tsv` | digest 解決失敗（理由付き） |

digest の探索順は `manifest_list_digest` → `image_id` →
`manifest_schema2_digest`（2026-07-17 検証）。

**入力は b / b-operand の `images.tsv` そのものなので、製品を足したら
インベントリを回し直す必要がある。** 回さない限り射程は伸びない。実例:
osc は 2026-07-16 に b-operand へ追加されたが、インベントリの最終実行は
それより前だったため、`sandboxed-containers-operator/*/00-discover/images.tsv`
の8イメージが `image-rpms.tsv` に 8/8 不在のままになっていた（2026-08-12 確認）。
コードの不具合ではなく実行順の問題で、再実行するだけで埋まる。

### C-2. 取得（**スクリプト無し** — ここが積み残し）

`docs/collection-model.md:77` にメカニズムだけが1行残っている:

> **CDN 直**: `repoquery --arch=src --location` + `curl --cert <entitlement>`
> （el8/el9/el10/layered 全ストリーム。`dnf download` は src arch を弾くので不可）

- `dnf download --source` が使えないのは、**src arch を弾かれる**ため。
  `repoquery --location` で CDN の実 URL を得て `curl` で直接落とす
- entitlement 証明書は `/etc/pki/entitlement/*.pem`
- el8 / el10 / layered（fast-datapath 等）を含む**全ストリーム**が対象。
  実際 `srpms-containers/` には `acl-2.2.53-1.el8.src.rpm` のような el8 も入っている

**この工程を再実行可能な形にするのが残作業。** 2026-07 の 2203 件は手作業で
集められ、その具体的なコマンド列は記録されていない。上記メカニズムからの
再構成は可能だが、未検証。

---

## 合流とパッケージング

A/B/C の `.src.rpm` を `phase-a-rpm/srpms/` に集めてから:

```bash
scripts/phase-a-rpm-package.sh -o /mnt/hdd/casket-ocp
```

`srpms/` と `rpmdb/`（`<patch>.tsv` 群）の**両方**が無いと die する。
`rpmdb-<date>/` のようなサフィックス付きでは読まれないので、
シンボリックリンクを張るかリネームすること。

### 増分更新なら overlay を使う（推奨）

全量を集め直す必要がない場合、**マウント済み casket の上に差分だけ重ねる**方が
桁違いに安い。既存 2520 件を保持したまま新規分だけ足せる:

```bash
scripts/repackage-srpms-refresh.sh \
  -m /srv/sources-ocp-srpms \
  -s $PWD/phase-a-rpm/srpms-<date> \
  -r $PWD/phase-a-rpm/rpmdb-<date> \
  -o /mnt/hdd/casket-ocp/casket-<date>-ocp-srpms.sqfs.xz
```

既存 NEVR は自動スキップ。`-r` に新しい rpmdb を渡すと `by-ocp/` と
`meta/bin-to-src.tsv` が現行パッチ構成で再生成され、`meta/rpmdb/` も差し替わる。
2026-08-11 の実績: 新規 45 件のみ展開し、3315 SRPM / 4983 バイナリ / unresolved 0。

登録と切り替え:

```bash
CASKET_WORK=$PWD bash -c 'source scripts/lib.sh; source scripts/lib-fingerprint.sh
  python3 "$REGISTRY_PY" add --phase a-rpm --unit all \
    --fingerprint "$(phase_a_rpm_fingerprint)" \
    --artifact-path <out.sqfs.xz> --mount-path "$(mount_path_for a-rpm all)"'
sudo scripts/casket-swap.sh --phase a-rpm --unit all --apply
```

登録前に、**casket の `by-ocp/` のパッチ集合が現在の上流と一致すること**を
確認する。指紋は上流から計算されるので、抽出後に z-stream が動いていると
中身と指紋がズレる。

---

## 積み残し

0. **el10 ノードOS が未収集（4.21/4.22）。ただし opt-in なので優先度は低い。**
   ペイロードには `rhel-coreos-10` と `rhel-coreos-10-extensions` があり、
   ラベルは `com.coreos.osname=rhcos` /
   `coreos.build.manifest-list-tag=4.22-10.2-…-node-image` — 名前が違うだけで
   **本物の RHCOS（RHEL 10.2 版）**。しかし `01` は `--rpmdb-image=rhel-coreos`
   固定、A2 は el9 タグのみを対象にしている。4.22.8 の el10 extensions だけで
   215 RPM / 9 extension（`wasm` は無い）。

   **既定ノードOSではない。** MCO の `pkg/osimagestream/streams.go`
   (`GetBuiltinDefaultStreamName`) が `releaseVersion.Major() == 4` なら
   `rhel-9` を返すので、**OCP 4.x は 4.21/4.22 も含めて既定は el9**、el10 が
   既定になるのは OCP 5 から。el10 は `OSImageStream` の `spec.default` を
   明示的に上書きしたクラスタだけが踏む。
   `oc adm release info` の `displayVersions.machine-os` は 4.21/4.22 で 10.2
   を返すが、**あれは既定ストリームの指標ではない**（2026-08-12 に一度これを
   根拠に「既定は el10」と誤判定した。判定は MCO のソースで行うこと）。

   2026-08-12 に el9 のみ先行と決めた意図的な保留で、A2 の実行ごとに
   `meta/extensions-deferred.txt` へタグ名が書き出されるので黙殺にはならない。

   **これは「タグを足すだけ」では終わらない。** 読む側と取る側で難易度が違う:

   - **読む側は RHEL 10 不要**（2026-08-12 実測）。Fedora ホストの rpm 6.0.2 で
     `oc adm release info --rpmdb --rpmdb-image=rhel-coreos-10` が通り、489
     パッケージ（kernel 6.12.0-211.39.1.el10_2 等）が取れる。A の「VM 必須」は
     *古い* rpmdb を新しい rpm で読めない方向の罠（4.14–4.18）で、el10 は逆向き。
     コード変更は確かに2箇所（`01` の `--rpmdb-image` 複数対応、A2 の
     `TAG_COLLECT`）
   - **取る側が本番。** `02`/`03` の `dnf download --source` は使えない。RHEL 9 の
     subscription-manager が生成する `redhat.repo` に `rhel-10-*` は存在せず、
     `03` の `--releasever` ピンも効かない（あれは既存 URL の `$releasever` を
     書き換えるだけで、パスには `content/dist/rhel9/` のように製品が literal で
     入っている）

   選択肢は2つ。**(A)** RHEL 10 VM を新設して `02`/`03` を流用。**(B)** C-2 の
   CDN 直取得をスクリプト化する（下の #1）。B を推す根拠は実績で、casket の
   el10 SRPM 282 件は**全部** `srpms-containers/`＝stream C 由来、`dnf` 経路
   由来は 0 件。CDN 直は RHEL 9 VM のまま el10 を引けている。**el10 対応と #1 は
   同じ作業に帰着する。**

   **なお 4.14/4.15 の `machine-os-content`（旧 el8）はこれとは別で、
   従来どおり意図的な除外**（上の A の罠を参照）
1. **C-2（CDN 直取得）のスクリプト化** — 最大ストリーム 2203 件の取得手順が
   1行のメカニズム記述しか無い。ここが埋まるまで、a-rpm を**一から**作り直すことはできない
2. `phase-a-rpm-package.sh` が `rpmdb/` 固定名を要求する件（`-r` 相当のオプションが無い）
3. `01-extract-rpmdb.sh` の `VERSIONS` デフォルトが古いハードコード
   （`VERSIONS_OVERRIDE` で回避しているが、既定値自体を config 由来にすべき）
