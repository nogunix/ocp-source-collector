# docs/

ocp-source-collector のドキュメント索引。

## リファレンス (現行)

| ドキュメント | 内容 |
|---|---|
| [pipeline.md](pipeline.md) | スクリプト構成と実行手順 (Phase A / A-rpm / B / B-operand) |
| [operations.md](operations.md) | リリース追随の運用: check / build / swap / cleanup、auto-update、マウント管理 |
| [artifacts.md](artifacts.md) | 各フェーズの casket 成果物: レイアウト・サイズ・マウント先 |
| [setup.md](setup.md) | 利用形態と新規ホストセットアップ |
| [a-rpm-collection.md](a-rpm-collection.md) | a-rpm 収集手順の詳細 (ノードOS + 拡張 + EUS 補填 + コンテナ内RPM層) |
| [collection-model.md](collection-model.md) | ソースコード収集モデル定義 — 収集系全体の地図 |

## 設計・運用知見

| ドキュメント | 内容 |
|---|---|
| [design-notes.md](design-notes.md) | 各フェーズの設計判断とその理由 |
| [operational-pitfalls.md](operational-pitfalls.md) | 運用で踏んだ地雷と対処 (依存収集 / submodule / OpenGrok / ビルド・ストレージ) |
| [casket-mcp-design.md](casket-mcp-design.md) | casket-mcp サーバの設計メモ |

## データ (生成物)

| ドキュメント | 内容 |
|---|---|
| [submodule-gaps.md](submodule-gaps.md) | マウント上の空 submodule ディレクトリ一覧 (`report-submodule-gaps.py` が生成) |

## アーカイブ (完了済みの計画書)

[archive/](archive/) に移動済み。実装は完了しており、経緯の記録として保存。

| ドキュメント | 内容 |
|---|---|
| [archive/phase-b-operand-plan.md](archive/phase-b-operand-plan.md) | B-operand (旧 Phase D) 計画書 |
| [archive/phase-b-uncollected-plan.md](archive/phase-b-uncollected-plan.md) | Phase B 未収録オペレーター収録計画 |
| [archive/casket-improvements.md](archive/casket-improvements.md) | casket 改善点メモ (2026-06-08 起点、ほぼ全項目完了) |
