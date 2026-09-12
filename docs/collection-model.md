# ソースコード収集モデル定義 (collection-model)

> 2026-07-16 なんでも相談会のアクションアイテム「ソースコード収集作業を AI が
> 管理できる形式で保存できるように定義する」の成果物。**この1枚が収集系全体の
> 地図**であり、AI（Claude Code 等）はこのファイルを起点に収集の全操作を再現・
> 監査・拡張できる。個別の手順詳細は各リンク先が正。

## 1. 収集対象のレイヤーモデル

OpenShift の「ソース全部」は 2 起点 × 各2段 + 横断1層 = 5 レイヤーで定義する。

```
起点1: リリースペイロード (quay.io/openshift-release-dev)
  a       ペイロードコンテナの git ソース         (oc adm release info --commits が真実源)
  a-rpm   rhel-coreos の rpmdb → ノードOS SRPM    (1段深掘り)
起点2: operator カタログ (registry.redhat.io/redhat/redhat-operator-index)
  b       全パッケージの operator ソース + bundle  (certified/community カタログも同機構)
  b-operand 全パッケージの CSV relatedImages → operand ソース (1段深掘り)
横断: コンテナイメージ内 RPM
  image-rpms  全収集イメージの rpm-manifest → SRPM (Pyxis API、pull 不要)
```

- operator は CR を管理する係、**operand が製品の実体**（CNV: operator 1 対 operand 61）。
- コンテナ「の上」は git、コンテナ/ノード「の下」(OS) は RPM という素材の違いが
  a↔a-rpm / b,b-operand↔image-rpms の対になっている。

## 2. 機械可読な定義ファイル（何を集めるか）

| ファイル | 意味 | 形式 |
|---|---|---|
| `config/minors.txt` | 追跡する OCP minor 一覧 | 1行1minor |
| `config/phase-b-operand-products.tsv` | b-operand 対象＝**カタログ全パッケージ**（package→infix。curated 9 は短縮名） | TSV |
| `config/phase-a-rpm-minors.txt` | a-rpm 対象 minor | 1行1minor |
| `config/auto-update-phases.txt` | 週次自動 staging の対象フェーズ | 1行1phase |
| `config/static-mounts.tsv` | registry 外の静的 casket（RHEL 2本等） | TSV |
| `config/opengrok-minors.txt` | OpenGrok が索引する minor の whitelist | 1行1minor |

## 3. upstream ソース位置の記録（どこから来たか）

ビルドごとに生成され **casket 内に同梱**される（=自己記述的アーカイブ）:

| 記録 | 場所 | 内容 |
|---|---|---|
| `git/INDEX.tsv` | 各 casket | dir↔repo↔ref↔version↔components（+ by-component/ by-repo/ symlink） |
| `meta/git.tsv` `meta/labels.tsv` | b / b-operand | component↔image↔source_url↔ref↔**解決メソッド**（a:upstream-vcs / b:component-map / c:source-label / f:caa-versions-pin …） |
| `meta/IMAGE_MAP.tsv` | b-operand | image pullspec → component → git dir |
| `meta/bin-to-src.tsv` `by-ocp/` | a-rpm | バイナリNEVRA→ソースNEVR→OCP版 |
| `meta/image-rpms.tsv` | a-rpm(拡張) | イメージ digest → SRPM NEVR（コンテナ内 RPM 層） |
| `COMPONENT_MAP` | `scripts/phase-b-operand-resolve-labels.py` | ラベルから解決できない例外の**明文化**（イメージ名→repo→mode） |

## 4. 鮮度と更新（いつ取り直すか）

- 鮮度シグナル: `scripts/lib-fingerprint.sh`（a=stable channel patch文字列 /
  b,b-operand=index digest / a-rpm=patch束のsha256）。check と build が同一実装を共有。
- **3週次自動 staging**（2026-09-12 に週次から変更）: `casket-auto-update.timer`（金曜23:30 JST起床、3週に1回だけ実行→
  2.5〜3日かけて staged 完了→人手 swap）。swap/cleanup は常に人手（`docs/operations.md`）。
- カタログ digest は毎日回転する（treadmill）。「swap 直後に STALE 表示」は正常。

## 5. 網羅性の不変条件（取り切れたかの機械検証）

**教訓**: 静かな `continue` が2回大規模欠落を生んだ（2026-06-09 の containerImage 30/122、
2026-07-17 の FBC 形式ドリフト 50/153）。スポットチェックで網羅を主張しない。

| 検証 | 実装 |
|---|---|
| カタログ全pkg vs operators.tsv の分母突き合わせ | `phase-b-discover.sh` が `uncovered.txt` に名指し出力、0件なら "coverage: all N captured" |
| FBC 形式の正規化 | 4形式対応（catalog.json / 分割JSON / 任意名 / catalog.yaml+datetime）→ 全て NDJSON 化 |
| b-operand 期待製品 vs 実収録 | `casket-mcp coverage_report`（missing_b_operand_products + known_gaps） |
| 収集不能の名指し | `known_gaps`（pipelines/compliance の版マッピング等）+ `image-rpms-unfetchable.txt` |

## 6. 取得メカニズム（どう取るか）

| 対象 | 手段 |
|---|---|
| git ソース | GitHub archive（commit 優先、tag fallback。`dl_one`） |
| SRPM (ノードOS) | RHEL9 VM `rhel9-srpm` + dnf `--releasever` 別 source repo |
| SRPM (コンテナ内) | **CDN 直**: `repoquery --arch=src --location` + `curl --cert <entitlement>`（el8/el9/el10/layered 全ストリーム。`dnf download` は src arch を弾くので不可） |
| イメージの RPM 一覧 | **Pyxis API**（pull 不要。payload は image_id / registry.redhat.io は manifest_list_digest） |
| 再パッケージ | overlayfs 増分（`repackage-srpms-refresh.sh` — 70G を再展開しない） |

## 7. AI 運用時の約束事

1. 収集系の変更時は**分母チェックの出力を必ず確認**（uncovered.txt / coverage_report）。
2. 想定外形式は `continue` せず計数して WARN（silent-skip 禁止）。
3. 本番 swap・cleanup は必ず人間の `--apply`。staging までが AI/自動の範囲。
4. 同一UTC日の再ビルドは成果物を同名上書きする → swap 後に registry の同一パス
   重複エントリを `registry.py remove`（cleanup が live を消す事故防止）。
5. レジストリ (`state/registry.json`) は `scripts/registry.py` 経由のみ。手編集禁止。

## 8. TODO（形式化の残項目）

- [x] GitHub Actions で週次リンク健全性チェック（2026-07-18 実装）:
      `scripts/export-upstream-sources.sh` が全 casket の INDEX.tsv を
      `state/upstream-sources.tsv`（repo↔ref↔kind↔初出casket）に平坦化し、
      `.github/workflows/upstream-link-check.yml`（土曜09:00 JST）が
      `scripts/check-upstream-links.py` で API（repo存在/rename）+ codeload
      HEAD（ref再取得可否）を検査。収集済み実体は casket 内で安全なので、
      検出は「再取得可能性の予兆」。収集セットが変わったら export を再実行して
      マニフェストをコミットすること。
- [ ] z:none 残件の系統監査: 全 layered の labels.tsv から z:none × vcs-ref 有り
      を集め、GitHub commit-hash 検索 API（`/search/commits?q=hash:<sha>`）で
      属リポを自動特定 → COMPONENT_MAP 半自動生成（OSC/netobserv で人手検証した
      パターンの自動化。レート 30 req/min なので夜間バッチ向き）
- [ ] Tree-sitter バックエンドの検討（大和氏提言: ctags 出力形式との互換を保ち
      つつ解析品質を上げる。casket-mcp の search_symbol 高度化の選択肢）
