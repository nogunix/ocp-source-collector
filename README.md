# ocp-source-collector — OpenShift ソースコード収集システム

[![ci](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/nogunix/ocp-source-collector/actions/workflows/ci.yml)

*[English README](README.en.md) — 本書が正本で、英語版は短めの概要。*

OpenShift のソースコード収集システム。
OCP リリースペイロードの全コンポーネントイメージから upstream の git ソースを収集し、内部 xz 圧縮 squashfs、`.sqfs.xz`で 1 ファイルにまとめる。

以下の `$CASKET_WORK` はこのリポジトリのチェックアウト先（作業ルート）を指す環境変数
（`scripts/lib.sh` で既定 `~/casket-work`、上書き可）。パス例はこれで一般化してある
——このリポジトリを clone した人なら誰でもそのまま動く想定。

## 収集対象

4つは対等な独立フェーズではなく、**2つの起点 × 2段階の深さ**: A/B がそれぞれの
取得元（リリースペイロード／operatorカタログ）を直接採り、A-rpm/B-operandは
「同じ取得元をもう1段深く見る」サブフェーズ。

| フェーズ | 範囲 | 状態 |
|-------|------|------|
| **A** | コンポーネント git ソース (GitHub archive tarball) | ✅ 完了 |
| **A-rpm** | rhel-coreos (machine-os) の SRPM ── A と同じリリースペイロードの1イメージをRPM単位で深掘り | ✅ 完了 |
| **B** | OperatorHub (redhat-operators) の FBC + bundle manifests + github source | ✅ 完了 (全 9 マイナー稼働中) |
| **B-operand** | レイヤード製品 (CNV/ACS/MCE/ACM/RHOAI/ODF/Quay) の operand ソース ── B と同じoperatorカタログをもう1段深掘り | ✅ 完了 (全 9 マイナー稼働中) |

## データの置き場所

| 場所 | 中身 | 寿命・扱い |
|------|------|-----------|
| **`/srv/sources-*`** | **ソースを読むならここ**。全 casket の read-only マウント（`cd` で即閲覧、casket-mcp / OpenGrok もここを見る） | 常設。マウント構成は下記2ファイルから自動再構築される |
| `/mnt/hdd/casket-ocp/*.sqfs.xz` | 成果物本体（内部 xz 圧縮 squashfs）。1 casket = 1 ファイル | 不変。旧世代は registry 上 retired → `casket-cleanup.sh` が削除 |
| `state/registry.json` + `config/static-mounts.tsv` | **マウントの単一の真実源**（registry = ライフサイクル管理対象、static = RHEL・カタログ等の手動管理分）。fstab には書かない | `casket-mounts.sh` が実マウントと同期（boot 時は systemd service） |
| `$CASKET_WORK/{ocp*,phase-*}/` | ビルド中間物（DL した tarball、展開 stage 等） | **使い捨て**。casket 完成後に削除してよい（SSD 節約） |
| `/srv/opengrok-data/` | OpenGrok 索引（SSD、~150-250G） | 再生成可能だが数時間かかるので消さない。`config/opengrok-minors.txt` のマイナーのみ索引 |
| `/mnt/hdd/casket/` | RHEL 版 casket（本リポジトリの管理外、参照のみ） | — |

ディスクの使い分け: **SSD = ビルド作業と OpenGrok 索引（速さが効く領域）/ HDD = 成果物（読み取り主体の大容量）**。
これはこのホスト（SSD 1.9T / HDD 7.3T）のサイジング判断であって前提ではない —
置き場所はすべて可変（`CASKET_WORK`、`casket-build.sh -o`、OpenGrok は `DATA_VOLUME`）。
OpenGrok 索引は全マイナーを索引すると ~900G になるため、既定では
`config/opengrok-minors.txt`（直近 3-4 マイナー）に絞って SSD に置く方式にしている
（未収録マイナーは casket-mcp の ripgrep で検索可能）。全量を索引したいなら
`DATA_VOLUME` を HDD 側に向ける。
サイズ感: A ×9 ~6.4G / A-rpm 4.1G / B ×9 ~5.6G / B-operand ×9 ~9.5G / certified+community ×18 ~19G。

## リポジトリ構成

```
ocp-source-collector/
├── scripts/                 収集・ビルド・運用スクリプト (本体)
│   ├── discover.sh / fetch-git.sh / manifest.sh / package.sh   Phase A パイプライン
│   ├── phase-a-rpm-*.sh                                        A-rpm パイプライン
│   ├── phase-b-*.sh                                            Phase B / B-operand パイプライン
│   ├── casket-build.sh / casket-swap.sh / casket-check.sh ...  リリース運用ドライバ
│   ├── collect-deps.py / collect-submodules.py                 依存・submodule 収集
│   ├── build-source-index.py                                   ソース索引生成
│   ├── lib.sh / lib-*.sh                                       共通ライブラリ
│   └── registry.py / deplib.py / submodulelib.py               Python ライブラリ
├── config/                  対象バージョン・製品定義
│   ├── minors.txt                     収集対象マイナー (A/B/B-operand 共通)
│   ├── phase-a-rpm-minors.txt         A-rpm 対象マイナー
│   ├── phase-b-operand-products.tsv   B-operand 対象製品
│   └── opengrok-minors.txt            OpenGrok 索引対象マイナー
├── docs/                    設計・運用ドキュメント
├── mcp/                     casket-mcp サーバー (MCP 経由でソース検索)
├── opengrok/                OpenGrok ソースブラウザ (Web UI)
├── tests/                   テストスイート (CI で実行)
├── systemd/                 自動更新用 systemd unit
├── containers/              SRPM 収集用コンテナ定義
├── ansible/                 ホストセットアップ playbook
└── .github/workflows/       CI 定義
```

## クイックスタート

```bash
cd "$CASKET_WORK"
V=4.20.22       # 任意の OCP バージョン

./scripts/discover.sh   -v "$V" -a x86_64
./scripts/fetch-git.sh  -v "$V" --jobs 6
./scripts/manifest.sh   -v "$V"
./scripts/package.sh    -v "$V" -o /mnt/hdd/casket-ocp
```

他フェーズの手順・オプション・所要時間は [docs/pipeline.md](docs/pipeline.md) を参照。

## ドキュメント索引

| ドキュメント | 内容 |
|------------|------|
| [docs/setup.md](docs/setup.md) | **利用形態ガイド**: 稼働ホストのサービス利用（推奨）or 自ホスト構築のセットアップ |
| [USAGE.md](USAGE.md) | **利用者ガイド**: マウントポイント一覧、レイアウト早見表、よくある操作 |
| [docs/pipeline.md](docs/pipeline.md) | パイプライン手順: スクリプト構成、各フェーズの実行方法、作業ディレクトリ |
| [docs/operations.md](docs/operations.md) | 運用: 新リリース追随の check → build → swap → cleanup |
| [docs/artifacts.md](docs/artifacts.md) | 成果物リファレンス: 各フェーズの casket 内容・レイアウト・数字サマリ |
| [docs/design-notes.md](docs/design-notes.md) | 設計判断とその理由 |
| [docs/operational-pitfalls.md](docs/operational-pitfalls.md) | 運用で踏んだ地雷と対処 |
| [docs/collection-model.md](docs/collection-model.md) | ソースコード収集モデル定義 — 収集系全体の地図 |
| [docs/casket-mcp-design.md](docs/casket-mcp-design.md) | casket-mcp 設計メモ |
| [mcp/README.md](mcp/README.md) | casket-mcp: ソースを検索・参照する MCP サーバー |
| [opengrok/README.md](opengrok/README.md) | OpenGrok ソースコードブラウザ (Web UI) |
| [CLAUDE.md](CLAUDE.md) | 開発ガイド (Claude Code 向けアーキテクチャ注記) |
| [CHANGELOG.md](CHANGELOG.md) | 変更履歴 |

## 必要ツール

`oc`, `jq`, `curl`, `awk`, `sha256sum`, `mksquashfs`, `xz`。
**有効な Red Hat サブスクリプションが前提**（pull secret、registry.redhat.io、
A-rpm の RHEL VM 登録・EUS ソース取得に必要。既定 `~/.docker/config.json`、`AUTHFILE` で上書き可）。
