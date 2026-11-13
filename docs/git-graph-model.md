# Gitグラフモデル

## 目的

gitlaw-jpのGitグラフは、日本の法令について次の2種類の履歴を表現する。

1. **公布投影**: ある改正法が、どの法令・どの版に関係するか
2. **施行投影**: 各法令の溶け込み後本文が、いつ有効な状態へ移ったか

このグラフは法令本文の状態履歴を対象とする。法案提出、委員会審議、採決などの立法過程そのものは、現時点ではグラフの構成要素に含めない。

## 用語

- `LawVersion`: ある法令の溶け込み後本文の1版
- `LawTimeline`: 同じ法令IDに属する `LawVersion` の時系列
- `AmendmentEvent`: 改正法令番号と公布日で束ねた公布イベント
- `EnforcementUnit`: 1つの法令版を施行順へ投影する最小単位
- `projection`: 同じ入力データを公布または施行の観点で並べたもの

Gitコミットは法的事実そのものではなく、上記のドメインイベントを検索・比較できる形へ投影した表現である。

## 生成するref

1回のrunにつき、次の2つのbranch refを生成する。

```text
refs/heads/<run_id>/<promulgation-branch-prefix>
refs/heads/<run_id>/<enforcement-branch-prefix>
```

デフォルトは次のとおり。

```text
refs/heads/<run_id>/promulgations
refs/heads/<run_id>/enforcements
```

Git sinkは `refs/heads/dev` が存在すれば、そのtipを両投影の共通の基点として使う。存在しなければroot commitから生成する。

## 公布投影

公布投影は、改正法をイベント単位として表現する。

```text
dev: A
      
\       P1 -- P2 -------- M1
              \          \
               `----------`-- P3 -- M2

P*: 対象法令版を適用する公布準備commit
M*: AmendmentEventを表す公布commit
```

実際の親関係は、イベントごとに次のように構築する。

1. 直前の公布tipをイベントのbaseとする。
2. 対象となる `EnforcementUnit` を決定順に並べ、公布準備commitとして直列に積む。
3. イベントのbaseと、公布準備commit列のtipを親に持つ公布commitを作る。
4. 公布commitのtreeには、公布準備commit列のtipのtreeを採用する。

最初のイベントは専用のbase commitを持たないため、計画上の公布commitは公布準備tipだけを親に持つ。2件目以降の公布commitは通常2-parent mergeになる。

### 公布準備commit

1つの対象法令版につき、次を更新する。

```text
laws/<law_id先頭3文字>/<law_id>/current.xml
laws/<law_id先頭3文字>/<law_id>/current.json
```

`current.xml` は入力ZIP内の溶け込み後本文、`current.json` は法令ID、版ID、施行日、改正イベントなどのsidecar metadataである。

### 公布commit

公布commit自体はファイルを追加更新せず、対象となる公布準備commit群を1つの `AmendmentEvent` として束ねる。コミットメッセージとtrailersに次を記録する。

- 改正法令名・改正法令番号
- 公布日
- 対象法令数・対象unit数
- `Amendment-Id`
- `Projection: promulgation`

## 施行投影

施行投影は、法令本文の有効状態を全法令横断の時系列として直列化する。

```text
dev: A -- E1 -- E2 -- E3 -- E4

E*: EnforcementUnitを適用する施行commit
```

各施行commitは、公布準備commitと同じ `current.xml` と `current.json` を更新する。ただしコミットの順序と意味は施行日を基準とする。

`--as-of YYYY-MM-DD` が指定された場合、次の条件を満たすunitだけを含める。

- 未施行フラグが付いていない
- 解釈可能な施行日が `as_of` 以下

`full` 実行で `as_of` が省略された場合は、Gitへ投影する時点の日本時間の日付を使う。`plan` だけを実行し、`as_of` も省略した場合は全unitを計画対象とする。

## 決定性と順序

同じ入力と設定から同じグラフ計画を得られるよう、順序は明示的に決める。

- 法令内の版: revision IDの日付prefix、施行日、revision ID
- 公布イベント: 改正法の公布日、改正法令番号、group key
- 施行unit: 施行日、法令ID、revision ID、phase index
- 改正法令番号がない版: 法令IDとrevision IDごとの独立イベント

コミット日時は、改正法公布日、施行日、revision IDの日付prefix、1970-01-01の順で解決する。libgit2上で負のtimestampを安定して保持できないため、1970年より前の日時はGit timestampをUnix epochへ丸め、法令上の日付はメタデータに残す。

## 不変条件

- `plan` はGit repositoryを変更しない。
- `apply` は保存済みgraph planを読み、計画されたcommitとrefだけを生成する。
- 各projectionは独立したrefを持つ。
- 同じ `LawVersion` は公布・施行の両投影に現れ得るが、コミットの意味と親関係は異なる。
- 公布投影は `as_of` で削らず、施行投影だけを時点フィルタする。
- 既存の生成済みrefは、明示的に `--force-refs` を指定しない限り更新しない。

## モデル化しないもの

現行モデルでは、次の情報をGitの親子関係として表現しない。

- 法案提出と法案の版
- 衆参各院の委員会・本会議での審議
- 修正案、採決、附帯決議
- 審議資料、会議録、議員・会派
- 政策上または制度上の因果関係

これらがなくても、公布された改正法と施行された溶け込み後本文の履歴は構成できる。

将来取り込む場合も、まず `AmendmentEvent` へ外部識別子とURLを関連付ける。

```text
Bill-Id: ...
Diet-Session: ...
Submitted-At: ...
Passed-At: ...
Source-URL: ...
```

立法過程をcommitとして組み込むのは、データの網羅性、同一性、版管理方法が確立してから別projectionとして検討する。法令本文の状態グラフへ不完全な審議情報を直接混ぜない。

## 現時点の制約

- `branch_model` はmanifest上のメタデータであり、現在は `dual` 以外の構築アルゴリズムを実装していない。
- 公布準備commitはイベント内で直列であり、対象法令ごとの独立branchではない。
- 同日施行のunitも1commitずつ直列化するため、法的な同時性は親関係ではなく日付メタデータで表現する。
- e-Govの溶け込み後本文を使うため、改正規定そのもののXML差分を直接適用しているわけではない。
- 入力に改正法令番号がない場合、複数法令を同じ公布イベントへ束ねることはできない。
- lawdb sourceは改正履歴メタデータを持たないため、各版を独立イベントとして扱い、
  `effective_date`は公布日で近似する。したがって施行順序は公布順に一致し、真の施行日とは
  一致しない場合がある。
- lawdb sourceの`law_id`はJAHIS採番であり、e-Govの法令IDとは体系が異なる。
- lawdb sourceはe-Gov XSDと互換でないため`xml_mvp`をパースせず、プレースホルダを出力する。

## 今後の設計判断

現行実装を変更する場合は、少なくとも次を独立した判断事項として扱う。

### 公布commitをmergeにするか

現在の2-parent公布commitは、Git上で競合する開発履歴を統合するためではなく、複数の法令版を「同じ改正法による変更」として束ねるための合成commitである。次の選択肢がある。

- 現行維持: `git log --merges` で改正法を発見できる
- 直列event commit: グラフは単純になるが、改正法と版適用の区別をmetadataだけで表す
- 法令ごとのside branch: 対象法令の独立性は上がるが、大規模な改正法では親数とグラフが複雑になる

### 同日施行を束ねるか

現在は同日でも `EnforcementUnit` ごとにcommitを作る。日単位のbatch commitにすると同時性を表しやすい一方、法令版1件を最小単位として追跡しにくくなる。

### runごとのrefと安定refを併存させるか

run refは入力データと計画の再現性に向いている。利用者向けに常に最新状態を示すrefが必要なら、immutableなrun refとは別に `latest/promulgations` などの安定refを設ける必要がある。

これらは入力sourceの追加や国会審議データの有無とは分離して決定できる。
