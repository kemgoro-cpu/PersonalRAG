# PersonalRAG プロジェクト 引き継ぎサマリ

最終更新: 2026-05-16

## ここまでの作業内容

### 大きな機能追加（フェーズ A〜D）

- **フェーズ A**: パイプライン可視化 + Windows トースト通知
  - `scripts/notify.py`（winotify ラッパ）新規
  - `pipeline.py` が `data/logs/pipeline_state.json` に現在処理中ファイルと recent 20 件を出力
  - 録音 GUI の「録音」タブにパイプライン状態セクション追加
- **フェーズ B**: 録音前メモダイアログ + メタデータ引き継ぎ
  - 録音開始時にタイトル/参加者/テーマを入力するモーダル
  - WAV ファイル名にタイトル反映、`.meta.json` をサイドカー保存
  - `summarize.py` がノート Markdown 先頭に YAML フロントマターを 1 ブロック統合で出力
- **フェーズ C**: サービス管理タブ
  - ttk.Notebook で「録音」「サービス管理」タブに分離
  - Ollama / Pipeline / Open WebUI の状態検知（非同期ポーリング 5 秒間隔）
  - 起動/停止ボタン、PID 管理は `subprocess.Popen` オブジェクト保持
- **フェーズ D**: 簡易ノートビューア
  - `scripts/note_viewer.py` 新規（tkinter）
  - テキスト検索 + セマンティック検索（ChromaDB、遅延 import）
  - `search.py` から `search_lib.py` に検索ロジックを分離

### 追加された運用機能・改善

- **設定 GUI 変更**: 「📁 変更…」ボタンで `recordings_dir` をフォルダ選択ダイアログから変更可能（`update_settings_path` を `config_loader.py` に追加）
- **失敗ファイル隔離**: `retry_max=3` 回失敗で `data/input/failed/` に隔離（`retry_tracker.py` 新規）
  - GUI に「隔離ファイル (N)」ボタン + 再試行/削除/エクスプローラで開く操作
  - リトライ中件数も状態ラベルに表示
- **pipeline.py 単一インスタンス保証**: lock file (`data/logs/pipeline.lock`) + PID 生存チェック
- **state.json ロック衝突リトライ**: WinError 32 を 5 回まで自動リトライ
- **pipeline 停止時のクズファイル対策**: transcribe.py / summarize.py のアトミック書き込み、ingest_db.py の「同 source 削除 → 再投入」で重複防止

### 主なバグ修正

- ホットキー Ctrl+Alt+R が無反応 → `win_hotkey.py` で Windows 標準 API に置換
- 無音判定の 2 秒誤発火 / 60 秒タイムアウト分離
- Tooltip 残留問題 → インスタンス変数 `_tooltip_windows` 管理
- ServiceManager の `_pids` 残骸 → `_processes` 統一
- pipeline 30 秒 OFF 誤判定 → heartbeat thread 追加
- path traversal リスク（`failed_files.json`、`search_lib.py`）→ basename + `is_relative_to` 二重防御
- 各種ラベル表示の混乱（「失敗一覧」→「隔離ファイル」）
- `ingest_db.strip_frontmatter` の YAML リスト `keywords` 解釈バグを修正
- GUI の「リトライ中」件数を、`retry_count.json` 全件ではなく実ファイルが投入フォルダに残るものだけ数えるよう修正

### Git 状態

- main に 20 コミット追加、GitHub に push 済み
- worktree `claude/gracious-banach-010b7d`、`claude/naughty-bardeen-8d9e65` 等が残存

---

## 現在の状態

- **動作**: ローカル運用で録音 → pipeline → 要約 → ChromaDB 投入まで全て稼働確認済み
- **設定**: `paths.recordings_dir: data/recordings` / `paths.input_dir: data/input`（**現状別フォルダ**、NAS パス未設定）
- **未処理 WAV**: 現状 `data/input/` 直下に `rec_2026-05-15_140351.wav` と `.meta.json` が残存
- **失敗ファイル**: `retry_count.json` には古い残骸を含む 11 件が残存。ただし実ファイルが投入フォルダに残るアクティブなリトライ対象は 1 件、隔離 0 件
- **テスト**: pytest で 26 件 PASS（retry_tracker + ingest_db frontmatter）、py_compile チェック通過

---

## 次にやるべきこと

### 優先度: 高（運用開始のために必須）

- **NAS / SMB パスの決定と設定変更**
  - 手元 PC: `paths.recordings_dir` を NAS パスに（GUI の「📁 変更…」で可）
  - リモート PC: `paths.input_dir` を同じ NAS パスに（手動で `settings.yaml` 編集）
  - 両側で pipeline.py / 録音 GUI を再起動

### 優先度: 中（動作確認と微調整）

- **リトライ中 WAV の挙動を観察**
  - 2026-05-15 の `transcribe.py` 失敗は多くが `returncode=3221226505`（`0xC0000409`）で同じ系統
  - 現在 `data/input/` に残る実ファイルは `rec_2026-05-15_140351.wav` のみ
  - 何度処理しても同じエラーで失敗するなら 3 回到達で `failed/` に隔離される予定
  - 何が原因で失敗しているか `data/logs/pipeline.log` を見てトリアージ
- **停止時クズ対策の動作確認**
  - 同じノートを 2 回 ingest して ChromaDB に重複チャンクが増えないことを確認（`scripts/search.py "クエリ"` で同じ内容が複数ヒットしないか）

### 優先度: 低（任意改善）

- **worktree のクリーンアップ**
  - `claude/gracious-banach-010b7d` / `claude/naughty-bardeen-8d9e65` は使い終わったので閉じてよい

---

## 未解決の論点

### 仕様レベル

- **`recordings_dir` と `input_dir` の関係**: NAS 構成への移行を前提とした「分離設計」のままで、ローカル運用を主とするユーザーには紛らわしい余地が残る
- **失敗ファイルの命名衝突**: 隔離時の `_001`/`_002` 連番は採用済みだが、同名ファイルが連続失敗で 999 を超えた場合の挙動は未定義（実用上はほぼ起きないが）
- **メタ JSON の引き継ぎが transcript 経由**: `pipeline.py` が WAV 隣の `.meta.json` を transcript 隣にコピーする実装。録音 → 文字起こしの中で 1 ファイルだけ介在するので壊れにくいが、複数の文字起こし結果が同 audio に対して並存する場合の扱いは要検討

### コード品質

- **`config_loader.update_settings_path` のテスト隔離が弱い**（Codex 既指摘、軽微）: `PROJECT_ROOT` モンキーパッチに依存。`settings_path` 引数を取れるシグネチャに拡張する余地
- **`failed_files.json` のガベージコレクションが未実装**: 削除ボタンを押すまでエントリが残るので、長期運用で肥大化。30 日経過自動削除等のロジックが将来課題
- **`Treeview iid` の同名衝突対応** は済んでいるが、`failed_files.json` 側で同 source の `append` が累積する設計は維持。再録音 → 失敗 → 隔離が繰り返されると `errors` が無限に伸びる
- **CI なし**: 各種 import 漏れバグ（`_pids` 参照漏れ等）を `py_compile` だけでは検出できない。`tests/` 配下のテストは `retry_tracker` のみで、`pipeline.py` / `record_gui.py` の統合テストは未整備

### 運用上の判断保留

- **失敗ファイルの自動再試行ロジックの妥当性**: 現状は pipeline.py 起動時の `process_existing_files` で `data/input/` に残っているファイルを毎回拾い直すため、停止/起動を繰り返すとカウントが進む。**ユーザーが意図的に停止しただけのケースでも失敗扱いになる**ため、`stable_wait` 失敗等の一過性エラーと永続エラーの区別が現状はカウント上で同じ重みになっている
- **VRAM 競合の手動回避ルール**: README に「文字起こし中は Open WebUI 停止」と明記されているが、フェーズ C のサービス管理タブから自動的にロックする仕組みはなし。警告ラベルのみ
- **`gemma4:e4b-it-q4_K_M` の動作確認**: 本番機（RTX Pro 2000 16GB）想定、開発機では未検証

### 既知の既存バグ

- 現時点で handoff に残す既知バグはなし。`ingest_db.strip_frontmatter` の YAML リスト `keywords` 解釈バグは修正済み
