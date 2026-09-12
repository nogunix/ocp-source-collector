# Phase B（旧称 Phase C）未収録オペレーター収録計画

> 文中の「Phase C」は 2026-07-11 のリネームで現「Phase B」。記録保全のため本文は当時の呼称のまま。

> 作成: 2026-06-09 / 更新: 2026-06-09（4.18 実パイプライン調査・修正反映）
> 対象: OCP `redhat-operator-index` のうち casket 未収録の OLM オペレーター
> 調査ベース: OCP 4.18 インデックス（discover→fetch-bundles 実行済み、キャッシュ参照）

## 背景 — Phase C は全インデックスを処理済み

`phase-c-discover.sh` はインデックス全体を処理する。4.18 実測: カタログ 153 オペレーター、
default-channel head を持つ 122。収録されるのは「ソースが公開 GitHub の取得可能な
commit/tag に解決できたもの」だけ（本番 4.18 カセット ~34 ディレクトリ）。

## 2つの根本原因（いずれも修正済み）

### 原因1 — ref が解決できない（resolve-v2.sh）

リポジトリ URL は CSV から取れるが、社内ビルドの `revision` sha は公開ミラーに無く、
v2 の既存フォールバック（`v<version>` / `release-<ocp-minor>` / main / master）が
**製品版と公開リポのタグ命名の不一致**で 404 する。

**修正**: `phase-c-resolve-v2.sh` の `dl_one` にリポジトリ別 ref 戦略（case テーブル、Phase D
COMPONENT_MAP と同型）を追加。`ver_minor`（製品版の MAJOR.MINOR）を導出して使用。

| リポジトリ | 変換 | 実測(4.18) |
|---|---|---|
| redhat-developer/gitops-operator | `tags/v<MAJOR.MINOR>.0`（patch切捨て） | v1.20.0 → 200 |
| maistra/istio-operator | `tags/maistra-<ver>-dev`, `tags/maistra-<ver>` | maistra-2.6.16-dev → 200 |
| openshift-knative/serverless-operator | `heads/release-<MAJOR.MINOR>`（製品版から） | release-1.37 → 200 |
| ComplianceAsCode/compliance-operator | `tags/v<ver>`, `heads/<MAJOR.MINOR>` | （RH先行のため近似） |

### 原因2 — containerImage 注釈なしで行ごと脱落（fetch-bundles.sh）★今回発見

`phase-c-fetch-bundles.sh:149` の `[[ -z "$container" ]] && continue` が、CSV に
`containerImage` 注釈を持たないオペレーターを `containers.tsv` から除外していた。
4.18 実測: 122 中 **92 行のみ**、**30 オペレーターが脱落**。脱落分は resolve-v2 に
到達しないため、ref 戦略以前に救済不能だった。脱落30の中に serverless / pipelines /
compliance / file-integrity / sandboxed-containers / windows-machine-config 等が含まれる。

**修正**: `continue` を撤去し、containerImage が空でも行を残す。resolve-v2 は CSV の
`metadata.annotations.repository`（または `spec.links[].url`）から csv_repo を再取得して解決する。

### 原因3 — 空ref で read が列ズレ（resolve-v2.sh download部）★今回発見

csv_repo のみで commit ラベルが無いオペレーターは ref 空。DL_TSV の行が
`src<TAB><TAB>ver<TAB>fname` となり、`IFS=$'\t' read -r src ref ver fname` が
**連続タブを1つに圧縮**（tab は空白文字）して列がズレ、`fname` が空に → 該当29製品の
ダウンロードが全て無効化されていた。

**修正**: DL_TSV 生成時に空 ref を `_NONE_` sentinel に置換、`dl_one` 冒頭で空へ戻す。
これで serverless/pipelines/compliance 等の ref 空ケースが正しく fetch される。

## 4.18 実測サマリ（修正後・全3原因解消）

| 指標 | 値 |
|---|---|
| 本番 4.18（修正前） | 34 source dir |
| **修正後 ユニーク source dir** | **110** |
| 収録 operator（dedup前） | 114 |
| head operator | 122 |
| NO_SOURCE | 6 |
| DL失敗 | 2（openstack=org-root, openshift-builds=www.redhat.com） |

**約 3.2倍（34→110）。** 優先3製品は狙い通り解決:
serverless→release-1.37, gitops→v1.20.0, servicemesh→maistra-2.6.16-dev。
ソース実体も検証済み（tektoncd operator-main, serverless release-1.37 等は本物の source tree）。

品質: 大半は sha/tag/release-branch で版正確。一部（~12–14, compliance/pipelines/kueue/
lws/jobset/node-maintenance/security-profiles/watcher/logic/orchestrator/exploit-iq/
amq-broker 等）は main/master 近似（最新ソース、出荷版と厳密一致せず）。これは既存 v2 と
同じ方針上のトレードオフ。

## 4.18 旧実測（原因調査時）

- カタログ 153 / head 122 / 旧 containers.tsv 92（→修正後 122 へ）
- 脱落30のうち **GitHub csv_repo を持つ = 18**（救済候補）、非GitHub 0、repository注釈なし 12
- repository 注釈なし12 も大半は `spec.links` に GitHub あり（下表）

## 救済対象の全体像（4.18）

### A. csv_repo が GitHub（18製品）— fix#2 で resolve-v2 に到達

serverless, openshift-pipelines, compliance, file-integrity, sandboxed-containers,
windows-machine-config, jobset, lws, node-maintenance, fence-agents-remediation,
machine-deletion-remediation, logic-operator(×2), orchestrator, rhtpa, exploit-iq,
amq-broker(rhel8/rhel9)。
openshift/* ・ medik8s/* は release ブランチ/タグ/sha が揃い解決容易。

### B. repository 注釈なし12製品 — spec.links 経由の内訳（実測）

| グループ | 数 | 製品 | 対処 |
|---|---|---|---|
| B1 追加実装ゼロ・版正確 | 3 | dpu, nbde-tang-server, numaresources | `release-4.18` ブランチ存在（200） |
| B2 追加実装ゼロ・最新近似 | 5 | cincinnati, devworkspace, kueue, security-profiles, watcher | main/master のみ200（版は最新近似） |
| B3 org-root リンクのみ | 1 | openstack | `openstack-k8s-operators/` 止まり。Phase D 方式の手動リポマッピング要 |
| B4 github リンク無し | 3 | ansible-automation-platform, ansible-cloud-addons, pf-status-relay | 制御イメージの `io.openshift.build.source-location` ラベル抽出が要。ansible は公開ソース無しの公算大 |

B1+B2 の8製品は**追加コード不要**（fix#2＋既存 spec.links フォールバックで自動収集）。

## 困難な2製品（厳密一致不可、近似のみ）

| 製品 | 理由 |
|---|---|
| openshift-pipelines (tektoncd/operator) | 製品版 v1.22 ↔ upstream v0.79 が完全非対応。版逆引き写像なし。要 midstream リポ調査 |
| compliance (ComplianceAsCode) | RH 出荷 v1.9.0 が公開リポに未 tag/branch化（公開最新 v1.8.2）。master 近似のみ |

## 解決見込み（4.18）

| 区分 | 数 | 備考 |
|---|---|---|
| 既存収録 | ~34 | 維持 |
| A群（GitHub csv_repo） | ~16/18 | 困難2を除き大半解決 |
| B1+B2群 | 8 | 自動収集（3つ版正確/5つ近似） |
| **合計見込み** | **~46–50** | 現状比 +35〜45% |
| 残課題 | 4 | openstack(B3) + ansible×2/pf-status-relay(B4) |

## 実装ステップ

1. **済** `phase-c-resolve-v2.sh` に per-family ref 戦略（原因1）
2. **済** `phase-c-fetch-bundles.sh` の空containerImage行ドロップ撤去（原因2）
3. **済** `phase-c-resolve-v2.sh` の空ref sentinel 修正（原因3）
4. **済** 4.18 full 再実行 → 110 ユニークdir（34→110）確定
5. 4.18 を package → 検証マウント（`phase-c-package.sh -v 4.18`）
6. 全8マイナー（4.14–4.21）再実行＋再パッケージ: 各 minor で discover→fetch-bundles→fetch-source→resolve-v2→package、`swap-operators-extracted.sh`（同名スワップ・fstab不変）

### 後回し（次の課題）

- openstack: 構成リポ群の手動マッピング（Phase D 寄り）
- ansible×2 / pf-status-relay: relatedImages の制御イメージ build ラベル抽出ロジック（費用対効果低）
- pipelines: 製品版↔upstream版 対応表 or RH midstream リポ調査
- compliance: 公開タグ化を待つ or master 近似で妥協

## 検証コマンド（再現用）

```bash
# 特定オペレーターの config だけ抽出（全インデックス不要）
mkdir -p /tmp/cfg/<op>
oc image extract --registry-config=~/.docker/config.json --filter-by-os=linux/amd64 \
  --path "/configs/<op>/:/tmp/cfg/<op>/" registry.redhat.io/redhat/redhat-operator-index:v4.18

# head bundle CSV の repository / spec.links / containerImage
oc image extract ... --path "/manifests/:/tmp/b/<op>/" <bundle-image>
python3 -c "import yaml; d=yaml.safe_load(open('<csv>'));
ann=d['metadata']['annotations']; print('repo=',ann.get('repository'));
print('links=',[l['url'] for l in d.get('spec',{}).get('links',[])])"

# ref 候補の存在確認
curl -sIL -o /dev/null -w '%{http_code}\n' \
  https://github.com/<owner>/<repo>/archive/refs/heads/release-4.18.tar.gz
```
```bash
# zsh 注意: 未クォート変数は単語分割されない。ループは printf '%s\n' ... | while read で。
```
