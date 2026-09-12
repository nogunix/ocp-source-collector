# ocp-source-collector OpenGrok ソースコードブラウザ

ocp-source-collector が収集した OCP リリースの展開済みソース (Phase A / A-rpm / B / B-operand +
certified/community カタログ) を、Web ブラウザからシンボル検索・クロスリファレンス・
全文検索できる [OpenGrok](https://oracle.github.io/opengrok/) コンテナ。
公式イメージ `docker.io/opengrok/docker` を podman でそのまま使う (カスタムビルド不要)。

設計の経緯と詳細は `analysis/plan-scb-container.md`（非公開）にある。以下は
それ無しで運用できるだけの要点をまとめたもの。

## 構成 (2026-07-11 時点、~54 プロジェクト)

```
/srv/sources-*              casket squashfs マウント (read-only)  ← 入力
        │  stage-sources.sh が symlink 木を生成
        ▼
/srv/opengrok-src/          クリーンな symlink 木
  ├── ocp-<ver>/<comp>       → /srv/sources-ocp<ver>/git/<comp>-<sha>              (A ×9: 4.14.58〜4.22.3)
  ├── operators-<minor>/..   → /srv/sources-ocp<minor>-operators/git/..            (B ×8+)
  ├── certified-<minor>/..   → /srv/sources-ocp<minor>-certified-operators/git/..  (certified ×9)
  ├── community-<minor>/..   → /srv/sources-ocp<minor>-community-operators/git/..  (community ×9)
  ├── layered-<minor>/<prod> → /srv/sources-layered-ocp<minor>/<prod>/git/..       (B-operand ×8)
  └── srpms-<ver>/<pkg>      → /srv/sources-ocp-srpms/srpms/<NEVR>                 (A-rpm, OCP版ごと1プロジェクト)
        │  run-opengrok.sh が staging + 元ソースをマウントして起動
        ▼
podman container casket-ocp-grok   :8080  OpenGrok Web UI  ※ rootless (リポジトリを持つ一般ユーザ) — sudo で起動しないこと
  index は /srv/opengrok-data (SSD, bind mount, ~150-250G) に永続化。HDD は非現実的
  (xref は 1 ソース = 1 個の極小 .gz、索引は数百万 inode の乱アクセスで inode 律速。
  HDD だと索引・検索が実用にならない)。config/opengrok-minors.txt のマイナーに絞って SSD に置く
```

> フェーズ名は 2026-07-11 のリネーム後 (A-rpm=旧B、B=旧C、B-operand=旧D)。
> layered (B-operand) はプロジェクト `layered-<minor>` の製品サブディレクトリ
> `<product>/<clean-name>` に展開。certified/community カタログは 2026-07-11 追加 —
> `stage-sources.sh` が `-certified-operators` / `-community-operators` マウントを
> 検出して `certified-<minor>` / `community-<minor>` プロジェクトに振り分ける。

> **z-stream の索引上限 (2026-07-29)**: `config/opengrok-minors.txt` の
> `keep-patches: N` (本番は **1** = 各マイナーの最新 z-stream のみ) が、パッチ単位の
> プロジェクト (`ocp-<ver>`, `srpms-<ver>`) を絞る。Phase A の swap は加算式で
> 旧パッチのマウントを永久に `live` のまま残す設計 (過去パッチを正確に見られるのが
> casket の価値) で、`casket-cleanup.sh` は `retired` しか消さないため、これが無いと
> プロジェクトが無制限に増える。実測で `ocp-<patch>` 1本 = index 19G + xref 14G =
> **SSD 約 33G** (casket 本体は HDD 2.6G)。上限から外れたパッチもマウントは残るので
> ripgrep / casket-mcp からは従来どおり検索できる（索引だけをスキップする）。
> 外れたパッチの `index`/`xref` は孤児として残るので、`--prune-index` で削除する。

## スクリプト (`scripts/`)

| スクリプト | 役割 |
|-----------|------|
| `stage-sources.sh`  | `/srv/sources-*` から `/srv/opengrok-src/` のクリーン symlink 木を生成 (要 root, sudo 自動)。`--prune-index` で、staging から外れたプロジェクトの `index`/`xref` を削除 |
| `run-opengrok.sh`   | staging を最新化し、必要なソース + index/etc volume をマウントしてコンテナ起動。起動後に suggester 夜間再構築を無効化 |
| `stop-opengrok.sh`  | コンテナ停止・削除 (index volume は既定で保持、`--wipe-index` で削除) |
| `entrypoint-ro.sh`  | read-only squashfs 用のカスタム entrypoint (公式の chown -R を回避) |
| `package-airgap.sh` | エアギャップ配布物 (イメージ + index + スクリプト) を作成 |
| `deploy-airgap.sh`  | 配布物から本番環境で OpenGrok を立ち上げ |

## 日常運用

```bash
# 起動 (staging 再生成 + コンテナ起動)。初回はバックグラウンドでインデックス生成。
# 必ず rootless (通常ユーザー) で実行 — sudo だと root 側 podman にイメージが無く
# docker.io から pull しようとして rate limit に当たる (2026-07-11 実障害)。
# stage-sources.sh は内部で sudo するので、run-opengrok.sh 自体は素で叩いてよい。
./scripts/run-opengrok.sh

# 進捗監視
podman logs -f casket-ocp-grok          # "Sync done" で完了

# 停止 (index は残す → 次回は再インデックス不要)
./scripts/stop-opengrok.sh

# アクセス
#   http://<host>:8080/                  トップ (検索ページ)
#   http://<host>:8080/xref/<project>/   ツリー閲覧
# LAN から見るなら firewall を開ける:
#   sudo firewall-cmd --add-port=8080/tcp [--permanent && sudo firewall-cmd --reload]
```

調整は環境変数で (例): `PORT=8888 INDEXER_JAVA_OPTS=-Xmx12g ./scripts/run-opengrok.sh`。
ソースを足した/入れ替えた後は `./scripts/run-opengrok.sh` を再実行 (incremental 再インデックス)。

**メモリ設定 (casket-host は RAM 60G、index 215G〜・~54 project — 重要)**:

| env | 既定 | 役割 | 推奨 |
|-----|------|------|------|
| `INDEXER_JAVA_OPTS` | `-Xmx8g` | 各 reindex JVM のヒープ。起動時に project 数ぶん**並列**で起動する | **`-Xmx2g`**。`-Xmx8g`×25並列だと RAM を超過し `pthread_create EAGAIN` で一部 project の索引が壊れる |
| `CATALINA_OPTS` | (空=既定≈RAMの25%≈15g) | webapp JVM のヒープ。全文索引のロード・suggester 再構築・検索結果セットに使う | **`-Xmx24g`〜`-Xmx32g`**。既定の 15g だと suggester 再構築や巨大クエリでヒープ枯渇→GC暴走で webapp がハングする |
| `WORKERS` | `4` (`run-opengrok.sh` の既定。イメージ本来の既定は `nproc`=16) | 起動時 sync の**並列 project 数**。project ごとに JVM を1つ起動する | 既定の `4` のままで十分低負荷。ホストが暇でとにかく早く終わらせたいときだけ上げる (`WORKERS=8` 等) |

例 (推奨の更新コマンド): `SKIP_STAGE=1 INDEXER_JAVA_OPTS=-Xmx2g CATALINA_OPTS=-Xmx32g ./scripts/run-opengrok.sh`
(`SKIP_STAGE=1` = symlink 木を作り直さず再利用。`WORKERS` は `run-opengrok.sh` が既定 `4` を渡すため通常は指定不要)。

**重さの根本原因 (2026-07-02 判明)**: コンテナは `run-opengrok.sh` 実行のたび (そして毎回のホスト再起動後) `podman rm -f` で作り直され、全 project (~54) の resync が最大 `WORKERS` 並列 (project ごとに JVM 1個) で走る。既定の `WORKERS`=`nproc`(casket-host では 16) だと 16 JVM が同時に CPU/IO を食い合い、ホスト全体が体感で重くなる。`INDEXER_JAVA_OPTS`/`CATALINA_OPTS` はメモリ枯渇 (EAGAIN・GC暴走) 対策であり、この CPU 競合には効かない。`run-opengrok.sh` は `WORKERS=4` を既定にして緩和済み。索引データ自体は volume 永続で増分のみなので、並列数を下げても1project あたりの時間は変わらず、単に同時実行数が減って総時間が伸びる代わりにピーク負荷が下がる。

> **注意 — operators 等の内容だけ更新した場合**: `run-opengrok.sh` はコンテナ再作成で
> `/opengrok/etc/configuration.xml` がリセットされ、起動時に全 project を**並列**で再索引する。
> 索引データ自体は `/srv/opengrok-data` (SSD bind mount) に永続するので増分は速いが、上記ヒープ設定を誤ると
> EAGAIN / GC暴走を起こす。少数 project だけ更新したいなら単一 JVM の直列索引が安全:
> ```bash
> podman exec -u appuser casket-ocp-grok java -Xmx8g -jar /opengrok/lib/opengrok.jar \
>   -c /usr/local/bin/ctags -s /opengrok/src -d /opengrok/data \
>   -P -H -G -r dirbased -m 256 --leadingWildCards on \
>   --disableRepository Perforce --disableRepository git --canonicalRoot /srv/ \
>   -W /opengrok/etc/configuration.xml -U http://localhost:8080 \
>   --token @/opengrok/etc/webapp_api_token
> ```
> また `/api/v1/search?full=the` のような**超頻出語の全文クエリは結果が数十万件**になり webapp の
> ヒープを枯渇させる。動作確認は具体的なシンボル名・固有語で行うこと (MCP の通常検索は影響なし)。

### 必須の起動オプション (run-opengrok.sh が自動付与)

read-only squashfs を OpenGrok に食わせるための非自明な対処。理由は plan の「落とし穴」節参照。

- `--security-opt label=disable` — source は SELinux `user_home_t`、squashfs は relabel 不可
- カスタム `entrypoint-ro.sh` — 公式 entrypoint の `chown -R` が read-only で失敗するのを回避
- `INDEXER_OPT="--disableRepository Perforce --disableRepository git --canonicalRoot /srv/"`
  - `Perforce`: 全ディレクトリで p4 を 8 秒ハング探索するのを止める
  - `git`: ソース内の埋め込み git 風メタ (ko の `kodata/HEAD`, SRPM の libssh `.git`) の誤認で
    history cache が失敗しプロジェクトが空になるのを止める (我々は history 不要)
  - `--canonicalRoot /srv/`: staging の symlink (→ /srv 配下) を OpenGrok に辿らせる

### 起動レース対策 — 検索が 0 件になる沈黙バグ (2026-07-12 判明)

**症状**: xref (ツリー閲覧) は完全に正常なのに、全文検索・シンボル検索だけが
**何を検索しても 0 件**を返す。`podman logs` に
`IndexNotFoundException: no segments* file found in MMapDirectory@/opengrok/data/index`
が繰り返し出る (末尾に全 project 名が並ぶ)。閲覧が動くので気づきにくい。

**原因**: コンテナ内の `/scripts/start.py` が起動時に流す project sync シーケンス
(`sync.yml`) は、各 project についてまず `POST /api/v1/messages` を叩く。この POST が
Tomcat の REST 層のデプロイ完了に**レースで負ける**と `Connection refused` で失敗し、
シーケンス全体が中断 → project を `indexed` にする最後のステップ
(`opengrok-reindex-project -U <url>`) まで到達しない。OpenGrok の REST 検索は
`ProjectHelper.getAllProjects()` を `isIndexed()==true` だけに絞ってからクエリするため、
全 project が `indexed=false` だと絞り込み結果が空になり、旧来の
`searchSingleDatabase()` 経路 (project 無し構成用) にフォールバック。これが存在しない
ルート直下 `/opengrok/data/index` を Lucene で開こうとして毎回
`IndexNotFoundException` を投げる。xref はこの経路を通らない (ファイルを直接読むだけ)
ので無傷 → 「閲覧は動くのに検索だけ死ぬ」状態になる。

初回フルビルド時はタイミングが合って成功していたため、この罠は
`systemctl restart opengrok` や再起動で**後から**顕在化する。

**対策**: `/opengrok/etc` を `ETC_VOLUME` に永続化する (下記)。初回 sync が完走して
`configuration.xml` に `indexed=true` が書かれれば、それが作り直し・再起動をまたいで
残るので、以後は起動レースが**再顕在化しない** (webapp は永続 config を読んで即検索を返す)。
もし 0 件バグに陥ったら `run-opengrok.sh` を叩き直す (= コンテナ作り直し + フル sync 再実行)
のが正しい復旧経路。かつては専用の `wait-for-ready.sh` で毎起動 sync を再トリガーしていたが、
config 永続化で不要になったため削除した (2026-07-12)。suggester 夜間再構築の無効化は
`run-opengrok.sh` 内 (コンテナ起動直後) で行う。

> **絶対にやらないこと**: 復旧目的で `PUT /api/v1/projects/<name>/indexed` を叩くのは
> 逆効果。このエンドポイントは suggester の全再構築を強制的に誘発し、index に対して
> ランダム read の嵐 (`iostat` で `%util` 99%、CPU 100%超) を数十分発生させ、最終的に
> webapp を **OOM (`java.lang.OutOfMemoryError: Java heap space`)** で落とす。正しい復旧は
> 上記の `run-opengrok.sh` 再実行 (= sync パイプラインの再実行) であって、indexed フラグの
> 手動セットではない。

### 再起動時に全 project を再 sync しない (configuration.xml 永続化, 2026-07-12)

どの project が `indexed=true` かは `/opengrok/etc/configuration.xml` に入るが、
これを書くのは start.py の `save_config()` で、**起動時 do_sync が完走した後**。
`/opengrok/etc` はコンテナ FS 上にあり、起動の種類で挙動が分かれる:

- **`systemctl restart` / 再起動 (= `podman start`、同一コンテナ)**: `/opengrok/etc`
  はコンテナ内に残るので、前回 sync が完走していれば configuration.xml の
  `indexed=true` が生きており、webapp は起動直後から検索を返す。
- **`run-opengrok.sh` (= `podman rm -f` + `run`、作り直し)**: `/opengrok/etc` が
  消える → configuration.xml がゼロから → 全 project を indexed にし直すまで検索が
  死ぬ。ソース入れ替え時に毎回これを踏んでいた。

対策として `run-opengrok.sh` は `/opengrok/etc` を `ETC_VOLUME`
(既定 `/srv/opengrok-etc`) に bind-mount し、**configuration.xml を作り直しでも
永続化**する。これで作り直し後も indexed 済み config が残り、検索が一瞬も落ちない。

> start.py は起動時 do_sync を無条件で1回走らせる (ハードコード)。config 永続化で
> **検索は落ちなくなる**が、この裏の do_sync 自体は公式イメージのままだと消せない
> (消すには vendor の start.py 改造が必要 → fragile なので採らない)。suggester は
> 無効化済みなので churn は index の増分チェックのみ (非ブロッキング)。
> なお初回 (bind 先が空) の一度だけは bare config からのフル sync が走り、その後は
> 永続化された config が使われる。

## エアギャップ配布

index (~215G) を同梱して配り、本番環境では再インデックスせず即提供する方式。
**index は OpenGrok のバージョンに固有** — イメージは digest 固定で配る (違うと起動時に破棄される)。
検証済: volume export → 別 volume に import → 同一イメージ + 同一ソースマウントで起動 → 再インデックスなしで検索可能。

ソース本体 (`casket-*.sqfs.xz`) は一般配布しない —— この airgap 方式は自組織の
別環境へ**一式まとめて持ち込む特殊経路**であり、その場合のみソースも同梱搬送し、
本番側で **同じ `/srv/sources-ocp*` パスにマウント**する (index がそのパスを参照するため)。

### ビルド側 (casket-host)

```bash
# フルインデックス完了後に実行。OUT_DIR に image.tar + index.tar + scripts + MANIFEST を出力。
OUT_DIR=/mnt/hdd/casket-ocp/opengrok-airgap ./scripts/package-airgap.sh
# lean 版 (index 同梱せず本番側で ~3h 再インデックス) なら INCLUDE_INDEX=0
```

### 本番環境

```bash
# 1. casket ソースを /srv/sources-ocp* にマウント (通常の casket 手順、fstab loop mount)
# 2. 配布物を展開し、その中で:
./scripts/deploy-airgap.sh
#    → podman load (image) → index volume を import → staging 生成 → コンテナ起動
# 3. 初回起動で incremental sync (~20-40分の I/O、再 ctags なし)、以後は即提供
```

## トラブルシュート

| 症状 | 原因 / 対処 |
|------|------------|
| xref (閲覧) は動くが全文/シンボル検索が**全部 0 件**、ログに `IndexNotFoundException: no segments*` | 起動レースで全 project が `indexed=false` のまま。`./scripts/run-opengrok.sh` を叩き直す (コンテナ作り直し + sync 再実行)。詳細は上記「起動レース対策」。`PUT .../indexed` での手動復旧は OOM を招くので厳禁 |
| 起動直後にコンテナが Exited (123/126) | `entrypoint-ro.sh` の SELinux ラベル or chown。`--security-opt label=disable` を確認 |
| あるプロジェクトの index が空 (`indexed=false`) | ソース内の埋め込み git 誤認。`--disableRepository git` が効いているか確認 |
| インデックスが終わらない | Perforce 探索。`--disableRepository Perforce` を確認 |
| symlink 配下が index されない | `--canonicalRoot /srv/` と、元 `/srv/sources-*` がコンテナにマウントされているか確認 |
| 本番環境で index が破棄され再インデックスされる | イメージのバージョン不一致。MANIFEST の digest と一致するイメージを使う |
| 管理 API `/api/v1/projects` が 401 | 仕様 (webapp 起動後トークン必須)。Web UI と `/api/v1/search` は公開で動作するため閲覧・検索に支障なし |
| ログに `pthread_create failed (EAGAIN)`、一部 project が検索 0 件 | 並列 reindex の RAM 枯渇。索引データは残るが webapp 登録が失敗。`INDEXER_JAVA_OPTS=-Xmx2g` に下げて再実行、または上記の単一 JVM 直列索引で復旧 |
| webapp が無応答 (HTTP 000)・`java` が高 CPU で RSS がヒープ上限に張り付く | suggester 再構築 or 巨大クエリで webapp ヒープ枯渇→GC暴走。`CATALINA_OPTS=-Xmx32g` を付けて `SKIP_STAGE=1 ./scripts/run-opengrok.sh` で再作成。検証は固有語クエリで |
| `run-opengrok.sh` が docker.io から pull しようとして `toomanyrequests` | `sudo` で実行している。イメージは rootless (ユーザー側) の podman storage にあるため root には見えない。素の (sudo なし) 実行に戻す — 既存の rootless コンテナは sudo 実行では壊れない |
