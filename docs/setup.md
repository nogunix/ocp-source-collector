# 利用形態と新規ホストセットアップ

casket の `.sqfs.xz` を**ファイルとして配布する予定はない**。利用形態は次の2つ:

| 形態 | 必要なもの | 読む節 |
|------|-----------|--------|
| (A) 稼働ホストのサービスを LAN 越しに使う（推奨・ほとんどの人はこれ） | ブラウザ / MCP クライアントのみ | 1 |
| (B) 自分のホストに一式を構築する（ビルドから全部やる） | Linux ホスト + **有効な Red Hat サブスクリプション** | 2〜5 |

## 1. (A) 稼働ホストのサービスを使う

セットアップ不要。casket ホストが提供する2つの入口に接続するだけ:

- **OpenGrok Web UI** — `http://<casket-host>:8080/`。ブラウザでシンボル検索・
  クロスリファレンス・ソース閲覧（project 一覧やレイアウトは
  [../opengrok/README.md](../opengrok/README.md)）
- **casket-mcp** — Claude 等の MCP クライアントから検索・参照。LAN 接続手順
  （HTTP transport の URL、接続例）は [../mcp/README.md](../mcp/README.md)

どちらも read-only。ソースの実体はホスト側の `/srv/sources-*` にあり、
手元へのコピーは発生しない。

## 2. (B) 自分のホストに構築する — 事前診断

```bash
./scripts/casket-doctor.sh --build   # ツール・レジストリ認証・容量を OK/WARN/FAIL で表示
```

以降の手順で迷ったら、まずこれを再実行して FAIL/WARN 行を潰すのが早い。

## 3. ストレージ設計

必要容量の目安（2026-07 時点の全量構成）:

| 項目 | 容量 | 備考 |
|------|-----:|------|
| casket 成果物（全フェーズ + カタログ） | ~45G | ビルドで生成される |
| ビルド中間物 | 一時 ~150G | 完成後削除可 |
| OpenGrok 索引（任意） | ~150-250G（既定）／全量 ~900G | Web UI で検索したい場合のみ。無くても casket-mcp/ripgrep で検索可能。既定は `config/opengrok-minors.txt`（直近 3-4 マイナー）のみ索引 |

SSD/HDD の使い分けは自由（本家ホストは「SSD=ビルド作業+OpenGrok 索引 / HDD=成果物」だが、
これはサイジング判断であって前提ではない）。全マイナーを索引すると ~900G になるため
既定はマイナーを絞って SSD に置く。置き場所を変える環境変数:
`CASKET_WORK`（作業ルート、既定 = このリポジトリの checkout 位置）、
`CASKET_OUT`（成果物、既定 `/mnt/hdd/casket-ocp`）、OpenGrok 索引は `DATA_VOLUME=<path>`。

## 4. ビルドとマウント

**大前提: 有効な Red Hat サブスクリプション。** pull secret の入手
（console.redhat.com）、`registry.redhat.io` からの operator カタログ pull、
A-rpm の RHEL 9 VM 登録、EUS チャネルの SRPM 取得——すべてこれに依存する。
サブスクリプションなしで動くのは Phase A の GitHub 取得部分だけ。

- ツール要件: `oc jq curl awk sha256sum mksquashfs xz rpm2cpio cpio` +
  quay.io / registry.redhat.io 両方の認証が入った pull secret
  （`~/.docker/config.json`、`AUTHFILE` で変更可）
- A-rpm だけはサブスクリプション付き RHEL 9 VM が必要（[pipeline.md](pipeline.md)）
- ビルド〜本番反映の流れは [operations.md](operations.md) の
  check → build → swap → cleanup。swap すればマウントまで自動で、
  再起動後の永続化は `sudo ./scripts/install-mounts-service.sh`（1回だけ）

registry 管理外の casket（別系統の RHEL casket 等）を足す場合のみ
`config/static-mounts.tsv` に「ファイル TAB マウント先」を書いて
`sudo ./scripts/casket-mounts.sh --apply`（[operations.md](operations.md) の
「マウント管理」節参照）。

## 5. 検索基盤（任意）

- **casket-mcp**（[../mcp/README.md](../mcp/README.md)）: マウントさえあれば索引不要で動く。
  まずこれで足りるか試すのがおすすめ
- **OpenGrok**（[../opengrok/README.md](../opengrok/README.md)）: シンボル検索・xref が
  要るなら。索引 ~800G と初回インデックス数時間のコストがある
