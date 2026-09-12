# casket-mcp 設計メモ（ocp-source-collector MCP サーバ）

casket マウント (`/srv/sources-*`) を MCP ツールとして公開し、Claude 等から
OpenShift/CNV/RHEL のソースを検索・参照できるようにする。起点: 2026-06-08。

## 方針（確定）

- **backend = ハイブリッド**: 検索の「シンボル定義/参照/全文」は OpenGrok REST、
  「ナビゲーション・repo解決・ファイル読取・Phase D の全文」は FS + ripgrep + 新索引層。
- **transport = 両対応**: 同一実装で stdio（ローカル）と HTTP（LAN共有）を切替。
- **read-only**: 全ツール読み取りのみ。パスは `/srv/sources-*` 配下に限定（traversal 防止）。
- 実装: Python + 公式 `mcp` SDK の **FastMCP**（デコレータでツール定義）。

## アーキテクチャ

```
Claude Code / Desktop ──(MCP: stdio or HTTP)──▶ casket-mcp (FastMCP)
                                                  ├─ OpenGrok REST  http://localhost:8080/api/v1
                                                  │     def / symbol / full / path  (Phase A/B/C 索引)
                                                  └─ FS + ripgrep   /srv/sources-*
                                                        by-repo / by-component / INDEX.tsv（新索引層）
                                                        read_file / grep（Phase D 含む全マウント）
```

### バックエンド振り分け（ルーティング）

| 操作 | 第一候補 | フォールバック / 補完 |
|---|---|---|
| シンボル定義 (`def`) | OpenGrok REST | 無ければ ripgrep で `type X struct`/`func X` 近似 |
| 参照検索 (xref/symbol) | OpenGrok REST | （rgでは近似不可 → "OpenGrok停止中"と返す） |
| 全文検索 | OpenGrok REST（索引済みマイナー） | 未索引マイナーは ripgrep |
| repo/コンポーネント解決 | FS: `by-repo/`,`by-component/`,`INDEX.tsv` | — |
| ファイル読取・dir一覧 | FS 直 | — |
| パターン grep（範囲指定） | ripgrep | — |

> **重要な前提**: 索引の切れ目は**フェーズではなくマイナー**。b-operand も
> `layered-<minor>` として索引済みで、上の「Phase D は ripgrep」は解消済み。
> 代わりに `config/opengrok-minors.txt` が索引対象マイナーを絞り（全量だと ~900G）、
> さらに `keep-patches: N` が Phase A / a-rpm のパッチを最新 N 本に制限する。
> **索引対象外のマイナー・パッチは ripgrep 経路**で、これは容量判断による恒久的な設計。
> また OpenGrok コンテナは常駐が前提（停止中は検索系が degrade）。

## ツール表面（tool surface）

```
# ナビゲーション / 解決（FS・常に可用）
list_versions()                         -> 利用可能な OCP minor/patch と Phase 一覧（マウント名から）
list_components(version, phase?)        -> INDEX.tsv/images.tsv からコンポーネント一覧
resolve_repo(repo, version)             -> by-repo/ 経由で実パス（dedup命名の隠れを解決）
resolve_component(name, version)        -> by-component/ 経由で実パス

# 検索（ハイブリッド）
search_symbol(name, project?)           -> 定義位置 [{path,line,snippet}]（OpenGrok def）
search_refs(name, project?)             -> 参照元（OpenGrok symbol/xref）
search_text(query, project?|path?, max) -> 全文（OpenGrok優先 / Phase D は rg）
grep(pattern, path, glob?, max)         -> ripgrep（範囲指定・Phase D 含む）

# 参照（FS）
read_file(path, start?, end?)           -> ファイル/範囲読取
list_dir(path)                          -> ディレクトリ一覧

# 今回の調査で有用だったもの（任意・第2段）
diff_file(path_a, path_b)               -> 2版間 unified diff（API差分等）
```

戻り値は LLM が扱いやすい構造化 JSON（`path:line` を含め、Claude Code でクリック可能に）。

## transport 切替

FastMCP の `mcp.run(transport=...)` で同一コードから両対応:

```python
# casket_mcp.py 末尾
import sys
mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
if mode == "http":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8765)
else:
    mcp.run(transport="stdio")
```

### Claude Code への登録

```bash
# ローカル(stdio)。$CASKET_WORK はこのリポジトリのチェックアウト先（既定 ~/casket-work）
claude mcp add casket -- python "$CASKET_WORK"/mcp/casket_mcp.py

# あるいは .mcp.json（リポジトリ同梱・チーム共有、絶対パスで記載）
{ "mcpServers": {
    "casket": { "command": "python",
                "args": ["/path/to/casket-work/mcp/casket_mcp.py"] } } }

# LAN共有(HTTP)。server: python casket_mcp.py http
{ "mcpServers": {
    "casket": { "type": "http", "url": "http://casket-host:8765/mcp" } } }
```

HTTP公開時は OpenGrok 同様 firewall を開ける（`firewall-cmd --add-port=8765/tcp`）。

## OpenGrok REST 参照（v1）

- `GET /api/v1/projects` → プロジェクト名一覧（`ocp-4.18` 等）
- `GET /api/v1/search?def=<sym>&projects=<p>&maxresults=N` → 定義
- 同 `?symbol=` 参照 / `?full=` 全文 / `?path=` パス
- レスポンス: `{ "resultCount":N, "results": { "<file>": [ {"lineNumber","line","tag"} ] } }`
  （フィールド名は稼働インスタンスで要確認 — バージョン差あり）

## 実装計画

```
mcp/
├── casket_mcp.py        FastMCP 本体（ツール定義＋ルーティング）
├── backends.py          opengrok_search() / rg_search() / fs_resolve()
├── requirements.txt     mcp[cli]  （ripgrep/curl は OS 側）
└── README.md            登録手順・運用（OpenGrok起動依存・firewall）
```

段階:
1. **第1段（FS+rg 中核）**: list/resolve/read/grep/search_text(rg) — OpenGrok 無しでも全 Phase 動く。
2. **第2段（OpenGrok 連携）**: search_symbol/refs/text を REST 優先に。停止検知でrg degrade。
3. **第3段（任意）**: diff_file、Phase D の OpenGrok 索引追加でフォールバック降格。

## 設計上の注意

- パス検証: 受け取った `path` は `realpath` して `/srv/sources-*` 配下のみ許可（外部読取防止）。
- 大きすぎる結果の抑制: `maxresults`/`read_file` の range 必須化で context 溢れ防止。
- by-repo/by-component は **2026-06-08 の索引層**前提（[[casket-production-layout]]）。未再ビルドの
  casket には無いが、全24本は反映済みなので可。
- OpenGrok 未起動時に検索系を黙って劣化させず、レスポンスに `"backend":"ripgrep(opengrok down)"` を明示。
```
