# gitlaw-jp

日本の法律の改正履歴・沿革を構造的なディレクトリ・ファイル実体とGitで追跡可能なコミットデータに変換する試み。

- `src/`: バルクデータをGitのコミット・ブランチデータへ直接変換するPythonスクリプト
- `laws/`: ファイル実体が保管されています。
  - `{law_id[:3]}/{law_id}/`: e-gov 法令APIの法令IDに基づいています。先頭3文字は和暦になっています。
    - `current.json`:
    - `current.xml`: all_xml.zip内に存在する各法令・版のXMLをそのまま各コミットが適用しています。当ファイルの来歴を見ることで法令の差分を確認することが可能です。

## ブランチについて

1. 公布`<run_id>/promulgations`と施行`<run_id>/enforcements`はブランチを分ける
2. 公布ブランチでは改正法＝commitの単位としたい。
    - 改正法は複数の法律の条文を一気に変える場合がある。
     ![](https://laws.e-gov.go.jp/docs/_next/static/media/210-amendment-of-multiple-laws.a7d76cae.svg)
    - 改正法＝commit単位は個人的に直感的だと思うので。
3. 施行ブランチでは各法令の条文の溶け込み・各法令の版をcommitの単位としている。
   - 同じ改正法の同じ方に対する変更はそのまま同じ時に適用されるわけではない
   - 例えば、制度上緩和措置として「この業界だけは猶予を与える」ということは多いが、法律でも同じで、同じ改正法でも〇〇法は〇月から施行、〇〇法は〇月から施行が起きる。
   - またN段ロケットと呼ばれるものもあり、同じ改正規定が同じ法に対しても、段階的に改正を適用することがある。
4. 公布-施行ブランチの対応の両立
   - これが一番の課題で、これについては今後どのような形であれば自然か、扱いやすいかなどは考えていくつもり。
   - 今のところ、版単位のcommitを最も小さい単位として、公布ブランチ側では改正法ごとに束ねるmerge commit的なcommitを生成している。

現在実装されている親子関係、並び順、`as_of` の扱い、モデル化しない立法過程については [Gitグラフモデル](docs/git-graph-model.md) を参照してください。

## 使用方法

- python: uvを利用することが一番簡単です
- e-gov 法令APIから`all_xml.zip`と`XMLSchemaForJapanaeseLaw_v3.xsd`をダウンロードし、`payload/`ディレクトリに配置してください
  - `--input-zip`と`--xsd-path`で変更が可能です。
- デフォルトでは`法律`のみを対象にします。`--law-type 政令`のように追加でき、`--all-law-types`で全種別を対象にできます。
- main.pyがエントリーポイントになっています。
  - `plan`: runs配下にapplyで適用されるcommit/branchに関する情報を構築し、jsonファイルを含むディレクトリとして`runs/<run_id>`配下に出力します。
    - 詳細はパイプラインを確認してください。
    - `dataset_id`はzipのhash値に基づきます。`run_id`は`日時_dataset_id`です。
    - 既存runを明示的に上書きする場合は`--force`を利用します。
  - `apply`: planで生成された`runs/<run_id>/manifest.json`を--run-manifestで渡し、実際にcommit/branchを生成します。
    - 既存refを明示的に更新する場合は`--force-refs`を利用します。
  - `full`: plan/applyの両方をまとめて行います。この場合--run-manifestは必要ありません。
  - 詳しくは`-h`または`--help`

## 入力ソース

`--source` で入力データのフォーマットを切り替えます(デフォルト `auto` = zip 構造で自動判別)。

| `--source` | 想定 zip | 説明 |
|---|---|---|
| `auto` | - | `all_law_list.csv` を含めば e-Gov、含まずに `Act/`, `Constitution/`, ... を含めば JAHIS `lawdb` と判定 |
| `egov` | e-Gov 法令API `all_xml.zip` | 従来挙動。CSV + `{law_id}_{revision_id}/{law_id}_{revision_id}.xml` |
| `lawdb` | 名古屋大学 JAHIS `law_data.zip` | 明治19〜平成29(2017年)の歴史的法令。`{LawType}/{law_id}/{law_id}{type}{seq}.xml` |

lawdb には改正履歴メタデータ (`改正法令名`, `改正法令番号`, `改正法令公布日`) が
存在しないため、各 XML 版が独立した "改正イベント" として扱われます。
公布ブランチと施行ブランチで `1版 = 1 commit` の関係が成立し、コミットメッセージの
`[gitlaw][公布][...]`/`[gitlaw][施行][...]` には law 名・公布日が記録されます。

例: lawdb 用の典型コマンド
```
python main.py plan --input-zip payload/law_data.zip \
                    --source lawdb --all-law-types
```

### lawdb 利用時の制約

- 施行日メタデータが無いため、`effective_date` は公布日で近似します。`--as-of` による
  施行投影の絞り込みはこの近似値に依存します。
- `law_id` は e-Gov の法令IDではなく JAHIS の採番 (`119` など) です。`--law-id` は
  この値で指定します。
- lawdb の XSD は e-Gov と別系のため、`--snapshot-parse-xml` による `xml_mvp` 抽出には
  対応していません。指定時はプレースホルダの `xml_mvp` を出力し、警告ログを出します。

## パイプライン

- `01_ingest`: 入力Zipの識別と`run_id`の決定
- `02_validate`: CSV / XSDスキーマの検証
- `03_normalize_versions`: CSV / XML から`LawVersion`を生成
- `04_build_timelines`: 法令ごとの版列(`LawTimeline`)を構築
- `05_graph_plan`: `EnforcementUnit` / `AmendmentEvent`からcommit graph planを構築
- `06_git_sink`: planをそのままGit object / refsに反映

## Appendix

- e-gov 法令API
  - <https://laws.e-gov.go.jp/bulkdownload/>
  - <https://laws.e-gov.go.jp/api/2/redoc/#tag/laws-api/operation/get-laws>
  - <https://laws.e-gov.go.jp/api/2/swagger-ui>
- 法令データドキュメンテーション: <https://laws.e-gov.go.jp/docs/>
- 日本法令索引: <https://hourei.ndl.go.jp/>

## ライセンス

MIT
