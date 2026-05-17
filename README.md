# PersonalRAG — ローカル完結のパーソナル知識ベース

音声を録音 → 文字起こし → 要約 → ベクター DB に蓄積し、Open WebUI からチャットで検索できるシステム。**完全ローカル動作**（外部 API 不使用）で社外秘の会話も安心。

## 動作環境

| 項目 | 開発機 | 本番機 |
|---|---|---|
| GPU | RTX 3060 (VRAM 6GB) | RTX Pro 2000 Blackwell (VRAM 16GB) |
| RAM | 32GB | 32GB |
| 想定 LLM | `gemma3:4b` | `gemma4:e4b-it-q4_K_M`（VRAM 約9.6GB） |
| 想定文字起こし | faster-whisper-large-v3 (int8_float16) | faster-whisper-large-v3 (float16) |

> ⚠ **VRAM 16GB でも Gemma 4 26B（18GB）は VRAM オーバー**。本番機は量子化版 `gemma4:e4b-it-q4_K_M`（9.6GB）を使います。文字起こしと LLM は絶対に同時起動しない設計です。

---

## ディレクトリ構成

```
PersonalRAG/
├── data/                   # ← .gitignore で除外（社外秘の可能性）
│   ├── input/              # 音声ファイル投入先
│   │   └── processed/      # 処理済み音声の退避先
│   ├── input_text/         # テキストファイル投入先（Teams/.vtt/.txt/.md）
│   │   └── processed/      # 処理済みテキストの退避先
│   ├── recordings/         # マイク録音の保存先
│   ├── transcripts/        # 文字起こし結果 (.txt)
│   ├── notes/              # 要約・ToDo (.md)
│   ├── chromadb/           # ベクター DB の永続化
│   └── logs/               # pipeline.log
├── scripts/
│   ├── config_loader.py    # 共通: 設定読み込み
│   ├── transcribe.py       # Step 1: 音声 → 文字起こし
│   ├── import_transcript.py # Step 1-alt: テキスト取り込み（Teams/.vtt/.txt/.md）
│   ├── record_mic.py       # Step 1 補助: マイク録音
│   ├── summarize.py        # Step 2: テキスト → 要約・ToDo
│   ├── ingest_db.py        # Step 3: Markdown → ChromaDB
│   ├── search.py           # Step 3 動作確認: CLI 検索
│   ├── pipeline.py         # Step 4: フォルダ監視で全自動化
│   ├── setup.sh            # セットアップスクリプト（Git Bash 用）
│   └── setup.ps1           # セットアップスクリプト（PowerShell 用）
├── config/
│   ├── settings.yaml       # 現在の設定（.gitignore で管理外。下記プロファイルから生成）
│   ├── settings.dev.yaml   # 開発機 (RTX 3060 6GB) 向けプロファイル
│   ├── settings.prod.yaml  # 本番機 (RTX Pro 2000 16GB) 向けプロファイル
│   └── prompts/
│       └── summarize.txt   # 要約プロンプト
├── .env.example            # 環境変数テンプレート
├── .gitignore
├── PersonalRAG.cmd         # インストール不要のダブルクリック起動
├── requirements.txt
└── README.md
```

---

## セットアップ手順

### クイックセットアップ（推奨）

スクリプト 1 つで venv 作成・依存インストール・設定適用・モデル取得を一括実行します。

#### Git Bash

```bash
cd /c/Users/kemgo/Documents/Program/PersonalRAG
./scripts/setup.sh dev      # 開発機の場合
# または
./scripts/setup.sh prod     # 本番機の場合
```

#### PowerShell

```powershell
cd C:\Users\kemgo\Documents\Program\PersonalRAG
.\scripts\setup.ps1 dev     # 開発機の場合
# または
.\scripts\setup.ps1 prod    # 本番機の場合
```

> PowerShell でスクリプト実行が `セキュリティエラー` になる場合は、実行ポリシーを一時的に変更してください:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```

### セットアップ後の手動作業

スクリプト完了後、以下を手動で行ってください。

1. **`.env` ファイルを作成**（`.env.example` をコピーして HF トークンを記入）

   ```powershell
   Copy-Item .env.example .env
   # .env をテキストエディタで開いて HUGGINGFACE_TOKEN=hf_xxx の xxx 部分を書き換える
   ```

2. **Hugging Face で利用規約に同意**（話者分離モデルに必要）
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - 各ページの「Agree and access repository」ボタンを押す

3. **`OLLAMA_KEEP_ALIVE=0` 環境変数を設定**（任意、VRAM 競合回避）

   ```powershell
   # システム環境変数に設定（PC を再起動するとOllama に反映）
   setx OLLAMA_KEEP_ALIVE "0"
   ```

### 環境別の差分

dev と prod で異なる設定値の一覧です。

| 設定 | dev (RTX 3060 6GB) | prod (RTX Pro 2000 16GB) | 変更理由 |
|---|---|---|---|
| LLM | `gemma3:4b` | `gemma4:e4b-it-q4_K_M` | 16GB では量子化 Gemma 4（約9.6GB）が安定動作。26B（18GB）は VRAM オーバー |
| Whisper 量子化 | `int8_float16` | `float16` | 16GB は VRAM 余裕があるため精度優先の float16 に変更 |
| 話者分離 device | `cpu` | `cuda` | 16GB では GPU で話者分離してもメモリに余裕がある |

### 現在のプロファイルを確認する

セットアップスクリプトを実行すると `data/logs/active_profile.txt` に現在のプロファイル名と適用日時が記録されます。

**Git Bash:**
```bash
cat data/logs/active_profile.txt
```

**PowerShell:**
```powershell
Get-Content data\logs\active_profile.txt
```

開発機と本番機で別の設定を使っているとき、いまどちらの設定で動作しているか即座に確認できます。

### 開発機・本番機の切替

設定ファイルをコピーするだけで切り替えられます（スクリプトを再実行しなくても OK）。

```bash
# Git Bash の場合
cp config/settings.dev.yaml config/settings.yaml   # 開発機用に戻す
cp config/settings.prod.yaml config/settings.yaml  # 本番機用に切り替え
```

```powershell
# PowerShell の場合
Copy-Item config\settings.dev.yaml config\settings.yaml   # 開発機用に戻す
Copy-Item config\settings.prod.yaml config\settings.yaml  # 本番機用に切り替え
```

> `config/settings.yaml` は `.gitignore` で Git 管理外になっています。
> 環境別プロファイル（`settings.dev.yaml` / `settings.prod.yaml`）は Git で管理します。

---

### 詳細手順（自動セットアップが失敗したとき用）

以下の手順を上から順に実行してください。

#### 1. Python 仮想環境

**PowerShell の場合:**
```powershell
cd C:\Users\kemgo\Documents\Program\PersonalRAG
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Git Bash の場合:**
```bash
cd /c/Users/kemgo/Documents/Program/PersonalRAG
python -m venv .venv
source .venv/Scripts/activate
```

> ⚠ Git Bash では `.\.venv\Scripts\Activate.ps1` は動きません（`\` がエスケープ文字として消えるため）。必ず `source .venv/Scripts/activate` を使ってください。

#### 2. PyTorch (CUDA 版) をインストール

PyTorch は GPU 世代に合わせた CUDA wheel を先に入れる（faster-whisper / pyannote が依存）。

**開発機 (RTX 3060 / Ampere):**

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

**本番機 (RTX Pro 2000 Blackwell):**

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
```

#### 3. 残りの依存ライブラリ

```powershell
pip install -r requirements.txt
```

Open WebUI は別 venv に検証済みバージョンを入れる。

```powershell
.\.venv-webui\Scripts\python.exe -m pip install open-webui==0.9.5
```

#### 4. Ollama インストール & モデル取得

[https://ollama.com/download/windows](https://ollama.com/download/windows) からインストール後、PowerShell で:

**開発機 (6GB):**
```powershell
ollama pull gemma3:4b
ollama pull nomic-embed-text
```

**本番機 (16GB):**
```powershell
ollama pull gemma4:e4b-it-q4_K_M   # 量子化 Gemma 4（約9.6GB）
ollama pull nomic-embed-text
```

#### Ollama の VRAM 自動解放（必須設定）

文字起こしや Open WebUI と VRAM を奪い合わないため、未使用時に即アンロード設定にします。

```powershell
# システム環境変数に設定（PCを再起動するとOllamaに反映）
setx OLLAMA_KEEP_ALIVE "0"
```

#### 5. Hugging Face トークン取得（話者分離用）

1. [https://huggingface.co](https://huggingface.co) でアカウント作成
2. [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) の利用規約に同意
3. [Settings → Access Tokens](https://huggingface.co/settings/tokens) で Read トークン発行
4. `.env.example` を `.env` にコピーして `HUGGINGFACE_TOKEN=hf_xxx` を記入

> 話者分離が不要なら `config/settings.yaml` の `diarization.enabled: false` で OFF にできます（トークン不要）。

#### 6. Open WebUI（pip 版、Docker 不要）

会社 PC で Docker Desktop が使えない環境にも対応するため、pip 版を採用しています。
**メインの `.venv` とは別の venv** に入れて依存衝突を避けます。

**Git Bash:**
```bash
cd /c/Users/kemgo/Documents/Program/PersonalRAG
python -m venv .venv-webui
source .venv-webui/Scripts/activate
pip install open-webui
open-webui serve --port 3000
```

**PowerShell:**
```powershell
cd C:\Users\kemgo\Documents\Program\PersonalRAG
python -m venv .venv-webui
.\.venv-webui\Scripts\Activate.ps1
pip install open-webui
open-webui serve --port 3000
```

→ ブラウザで `http://localhost:3000` を開いて初回アカウント作成（メアド/PW はローカル管理のみ）。

> 初回起動はモデル一覧取得などで 30〜60 秒かかります。コンソールに `Uvicorn running on http://0.0.0.0:3000` と出れば準備完了です。

**毎回の起動手順** (PC 再起動後など):
```bash
cd /c/Users/kemgo/Documents/Program/PersonalRAG
source .venv-webui/Scripts/activate
open-webui serve --port 3000
```
このターミナルは閉じずに開いたままにしておきます（Ctrl+C で停止）。

---

## 使い方

### A. 既存の音声ファイルを処理する

```powershell
# 単発実行（Step 1 → 2 → 3 を手動で順番に）
python scripts/transcribe.py data/input/sample.wav
python scripts/summarize.py data/transcripts/sample_2026-05-13_1030.txt
python scripts/ingest_db.py data/notes/sample_2026-05-13_1030.md
```

または **フォルダ監視で全自動化**:

```powershell
python scripts/pipeline.py
```

別ターミナルで `data/input/` に音声ファイルをコピーすると、自動で文字起こし → 要約 → DB 投入されます。

#### パイプライン状態の確認方法

`pipeline.py` は処理の進捗を `data/logs/pipeline_state.json` に随時書き出しています。

```json
{
  "updated_at": "2026-05-15T14:30:22+09:00",
  "current": {"file": "rec_xxx.wav", "step": "summarize", "started_at": "..."},
  "queue": ["meeting_notes.docx"],
  "recent": [
    {"file": "rec_xxx.wav", "result": "success", "finished_at": "...", "note_path": "..."},
    {"file": "broken.wav",  "result": "failed",  "finished_at": "...", "error": "..."}
  ]
}
```

**GUI から確認する（おすすめ）**: 録音 GUI のメインウィンドウ下部に「パイプライン状態」セクションがあります。
- 「現在:」— 処理中のファイル名とステップ（文字起こし中 / 要約中 / DB 投入中）が表示されます
- 「最近の処理（直近 24h）:」— 直近 24 時間の成功・失敗件数が表示されます
- 「詳細...」ボタン — 最近の処理一覧が表示され、成功行をクリックするとノートを既定アプリで開けます

**通知**: 処理が完了または失敗すると Windows トースト通知が届きます。
成功通知は `config/settings.yaml` の `pipeline.notify_on_success: false` で抑制できます。
失敗通知は常に表示されます（気付けるよう抑制不可）。

```yaml
pipeline:
  notify_on_success: true   # false にすると成功時のトーストを出さない
  state_file: data/logs/pipeline_state.json  # 状態ファイルの場所
```

### B. マイクから録音する

```powershell
python scripts/record_mic.py
# Enter キーで停止
# 録音から文字起こしまで一気にやるなら --transcribe を付与
python scripts/record_mic.py --transcribe
```

#### B-1. 録音ボタン GUI（おすすめ）

毎回 PowerShell で CLI を叩くのが面倒な場合は、GUI ランチャを使うと便利です。
ウィンドウのボタン、グローバルホットキー、タスクトレイのいずれからでも
録音をトグルできます。停止すると WAV が `recordings_dir` に保存されます
（ローカル単体運用のデフォルトでは `input_dir` と同じ場所なので自動処理されます）。

```powershell
# 初回のみ依存ライブラリを追加インストール
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

##### 起動

普段はリポジトリ直下の `PersonalRAG.cmd` をダブルクリックします。録音 GUI が開き、録音・ホットキー・トレイ常駐・サービス管理タブをそのまま使えます。

インストーラや exe 化は不要です。会社 PC ではこのフォルダを置いたまま、`PersonalRAG.cmd` のショートカットだけをデスクトップに作る運用がおすすめです。

従来どおり `scripts\record_gui.cmd` を直接ダブルクリックしても起動できます。

##### デスクトップショートカット作成

1. エクスプローラで `PersonalRAG.cmd` を右クリック → 「ショートカットの作成」
2. 生成された `PersonalRAG.cmd - ショートカット` をデスクトップへ移動
3. （任意）右クリック → プロパティ → アイコン変更

##### 操作

| 操作 | 方法 |
|---|---|
| マイクデバイス選択 | ウィンドウ上部のプルダウン（停止中のみ操作可） |
| 録音開始/停止 | ウィンドウの大きなボタン / ホットキー `Ctrl+Alt+R` / トレイメニュー |
| ウィンドウを隠す | ×ボタン → タスクトレイに常駐（プロセスは終了しない） |
| ウィンドウを戻す | トレイアイコンをクリック → 「表示」 |
| 完全終了 | トレイアイコン右クリック → 「終了」 |

##### 「⚠ 音声が入っていません」と出たら

GUI が 10 秒間連続で無音を検知すると警告を表示します。原因は大抵マイクの選択ミスです。

1. 録音を停止する
2. GUI のプルダウンで正しいマイクを選び直す（または Windows のサウンド設定で既定デバイスを切替）
3. もう一度録音開始ボタンを押す

> 録音中に Windows のサウンド設定でマイクを切り替えても、その録音セッションには反映されません（一度停止 → 再開始が必要）。これは sounddevice ライブラリの仕様です。

##### 録音前メモの使い方

「録音開始」ボタン（またはホットキー・トレイメニュー）を押すと、録音が始まる前に **メモ入力ダイアログ** が表示されます。

```
┌─ 録音情報の入力（任意） ─────────────────────────┐
│ タイトル: [打ち合わせ-XYZ案件            ] ▼     │  ← 過去5件がプルダウンで選べる
│ 参加者:   [田中, 佐藤                    ]       │
│ テーマ:   [設計レビュー                  ]       │
│                                                   │
│  [ 録音開始 ]  [ メモなしで開始 ]  [ キャンセル ] │
└───────────────────────────────────────────────────┘
```

| ボタン | 動作 |
|---|---|
| 録音開始 | 入力内容を保存してから録音を開始する |
| メモなしで開始 | メタ情報を保存せず即録音（従来と同じ）|
| キャンセル / × | 録音せずに閉じる |

**入力は任意です**。すべて空欄のまま「録音開始」を押しても録音できます。

**ファイル名への反映**:

タイトルを入力すると WAV ファイル名に自動的に付加されます。

- 入力あり: `rec_2026-05-15_143022_打ち合わせ-XYZ案件.wav`
- 入力なし: `rec_2026-05-15_143022.wav`（従来どおり）

Windows で使えない文字（`/ : * ? " < > |` など）は自動的に `_` に置き換えられます。50 文字を超える部分は切り詰められます。

**メタ情報の Markdown ノートへの反映**:

パイプラインで要約が生成されると、ノートの先頭に入力内容が YAML フロントマターとして挿入されます。

```markdown
---
title: 打ち合わせ-XYZ案件
participants: 田中, 佐藤
topic: 設計レビュー
recorded_at: '2026-05-15T14:30:22+09:00'
---

---
source: rec_2026-05-15_143022_打ち合わせ-XYZ案件_2026-05-15_1430.txt
date: 2026-05-15
...
---

# 要約 (2026-05-15)
...
```

このフロントマターは Open WebUI の Knowledge 検索でタイトルや参加者名がヒットしやすくなるために追加されます。ChromaDB 投入時にはフロントマターは自動的に除外されるため、検索ノイズにはなりません。

**タイトル履歴**:

過去 5 件のタイトルが Combobox のプルダウンに残ります。同じタイトルを再選択すると先頭（最新）に移動します。履歴は `data/.gui_history.json` に保存されます（`.gitignore` で Git 管理外）。

##### 無音録音の自動削除

GUI で開始した録音セッション中に **一度も音声を検知できなかった** 場合、停止時に WAV ファイルを自動削除します。これは「マイクが切れていたまま録音してしまったゴミファイル」が pipeline.py に拾われて空の文字起こしに無駄な時間を使うのを防ぐためです。1 秒でも喋った録音は通常通り保存されます。

> CLI 版 (`python scripts/record_mic.py`) はこの自動削除を行いません（意図的に録音している前提なので、サプライズで消えると困るため）。

##### 設定

`config/settings.yaml` の `recording` セクションで以下を調整できます。

```yaml
recording:
  hotkey: "ctrl+alt+r"          # ホットキー。例: "ctrl+shift+f12"
  silence_threshold: 0.001       # 無音判定の振幅しきい値（0.0-1.0）
  silence_timeout: 5.0           # この秒数連続無音で警告
```

##### 注意

- ホットキーは Windows 標準 API (`RegisterHotKey`) を直接使っているため、追加ライブラリの誤検知問題はありません
- 既に他アプリ（OBS など）が同じキーを押さえているときはホットキー登録に失敗します（GUI 起動時に警告ダイアログ）。`config/settings.yaml` の `recording.hotkey` を別キーに変更してください
- 管理者権限のアプリが前面のときはホットキーが効かないことがあります（Windows UIPI の仕様）
- 録音中にタスクマネージャから強制終了すると、最後の数秒の音声と WAV ヘッダが失われ再生不能になることがあります
- 最小化中の無音警告は `winotify` ライブラリによる Windows 10/11 のシステムトーストで通知します。`pip install -r requirements.txt` を再実行して `winotify` を入れてください。未インストールでもステータス赤字とトレイ tooltip 更新は出るので、最低限気付くことはできます

##### 録音保存先を変更する

録音 GUI の「録音」タブにある「📁 変更...」ボタンから保存先を変更できます。

**GUI から変更する手順**:

1. 録音タブの「保存先:」の右の「📁 変更...」ボタンを押す
2. フォルダ選択ダイアログが開くので、保存先を選択して「OK」
3. フォルダが存在しない場合は「作成しますか？」の確認ダイアログが出る
4. 確認ダイアログで「はい」→ `settings.yaml` への書き込み確認ダイアログが出る（コメント消失の警告あり）
5. 「はい」で確定 → 「再起動してください」のメッセージが出る
6. GUI を一度閉じて再起動すると新しい保存先が反映される

> 書き戻し前に `config/settings.yaml.bak` が自動作成されます。コメントが消えた場合は `settings.yaml.bak` を参照してください。

**settings.yaml を手動で変更する場合**（コメントを保持したいとき）:

```yaml
paths:
  recordings_dir: data/input         # ← pipeline に拾わせるなら input_dir と同じにする
```

**NAS / SMB パスを設定する場合の注意**:

- UNC パス（`\\nas-server\share\PersonalRAG\input`）を直接指定できます
- NAS が起動していないと録音を保存できないため、保存先として設定する前に疎通確認を行ってください
- `chromadb_dir` は NAS に置かないでください（SQLite のロック競合で DB 破損リスクあり）
- NAS への認証情報は Windows の「資格情報マネージャー」に保存しておくと、再起動後も自動接続されます

#### B-4. ノートビューア（簡易表示）

Open WebUI を起動せずに `data/notes/*.md` を一覧・プレビュー・検索できる軽量ビューアです。

##### 起動方法

- `scripts\note_viewer.cmd` をダブルクリック（コンソール非表示で起動）
- または録音 GUI の「📖 ノートを開く」ボタンを押す

##### 画面構成

```
┌─ 左ペイン (1/3) ──────────┐ ┌─ 右ペイン (2/3) ───────────────┐
│ [検索キーワード] [検索]    │ │ メタ情報                       │
│ ○テキスト ○セマンティック  │ │   タイトル: 打ち合わせ-XYZ案件 │
│                            │ │   参加者:   田中, 佐藤         │
│ ノート一覧                 │ │   テーマ:   設計レビュー       │
│   2026-05-15 打ち合わせ... │ │   録音日時: 2026-05-15...      │
│   2026-05-14 仕様レビュー  │ │   日付:     2026-05-15         │
│   ...                      │ │ ──────────────────────────── │
│                            │ │ # 要約 (2026-05-15)            │
│  [更新] [エディタで開く]   │ │ ...本文プレビュー...           │
└────────────────────────────┘ └────────────────────────────────┘
```

**左ペイン**:
- ノート一覧はファイルの更新日時降順で表示
- フロントマターに `title` キーがあればそれを表示、なければファイル名（拡張子なし）を表示
- 「更新」ボタンで一覧を再読み込み
- 「エディタで開く」ボタンで選択中ノートを既定アプリで開く（Windows 専用）

**右ペイン**:
- フロントマターのメタ情報（タイトル・参加者・テーマ・録音日時・日付）を上部に表示
- ノートの本文（フロントマター以降の Markdown テキスト）を下部のテキストウィジェットに表示
- 読み取り専用（テキスト選択・コピーは可能）

##### テキスト検索 vs セマンティック検索

| 検索モード | 速度 | 精度 | 必要なもの |
|---|---|---|---|
| テキスト（デフォルト）| 即時 | キーワードが完全一致している必要あり | なし（即使える）|
| セマンティック (ChromaDB) | 初回 3〜5 秒 | 意味が似た文章でもヒットする | ChromaDB と Ollama の起動 + `ingest_db.py` での投入済みデータ |

**テキスト検索**: 「田中」「ECU」「納期」などの具体的なキーワードを探すときに向いています。大文字小文字は無視します。フロントマターも含めた全文が対象です。

**セマンティック検索**: 「車両試験の課題について話した会議」のように、キーワードそのものではなく**意味**で探したいときに向いています。ChromaDB に投入済みのノートだけがヒット対象になります。Ollama が起動していない場合や ChromaDB にデータがない場合は空結果になります（クラッシュしません）。

> セマンティック検索の初回選択時に ChromaDB の読み込みで数秒かかります。2 回目以降は速くなります。

##### フロントマターが無い古いノートについて

フェーズ B 以前に作成されたノート（フロントマターなし）も問題なく表示できます。
この場合、メタ情報欄はすべて「—」になり、ノート本文が全文プレビューされます。

#### B-3. サービス管理タブ

録音 GUI の「サービス管理」タブから、3 つのサービスを一括管理できます。

```
┌─ サービス管理 ────────────────────────────────────────┐
│ Ollama:       ● 稼働中    稼働中(PID=...) [ 停止 ]   │  ← 外部起動も停止可
│ Pipeline:     ● 稼働中    稼働中        [ 停止    ]   │
│ Open WebUI:   ○ 停止中    停止中        [ 起動    ]   │
│                                                       │
│   [ すべて起動 ]  [ すべて停止 ]                      │
│                                                       │
│ 注意: 文字起こし中に Open WebUI でチャットすると      │
│       Ollama/Gemma と VRAM 競合の恐れあり。           │
└───────────────────────────────────────────────────────┘
```

##### 状態の見方

| 表示 | 意味 |
|---|---|
| ● 稼働中（緑） | サービスが応答している |
| ○ 停止中（グレー） | サービスが応答していない |
| 確認中... | 初回ポーリング待ち（5 秒以内に更新）|

状態は 5 秒おきに自動で更新されます（バックグラウンドスレッドがポーリング）。

##### 各サービスの起動コマンド

| サービス | 起動元 |
|---|---|
| Ollama | `ollama serve`（システムにインストール済みの `ollama` コマンド） |
| Pipeline | `.venv\Scripts\pythonw.exe scripts\pipeline.py`（コンソール非表示） |
| Open WebUI | `.venv-webui\Scripts\open-webui.exe serve --port 3000` |

Open WebUI をこのタブから起動すると、HTTP 応答が確認できたあとに
`scripts\sync_webui.py` をバックグラウンドで 1 回実行し、`data/notes/` の
未同期 `.md` を Knowledge に回収します。結果は
`data/logs/sync_webui_stdout.log` / `data/logs/sync_webui_stderr.log` に出力されます。
GUI 起動中は `data/notes/` も軽く監視しており、新規・更新 `.md` が 5 秒ほど
安定したあと、Open WebUI が稼働していれば同じ同期処理に回します。Open WebUI
停止中の変更は保留され、次に Open WebUI が起動したタイミングで回収されます。

##### VRAM 競合の警告について

文字起こし（Whisper）は VRAM を大量に使います。Open WebUI の画面表示自体は
大きな GPU 負荷ではありませんが、チャットで回答を生成すると背後の Ollama/Gemma
が動き、Whisper と VRAM を奪い合って Out of Memory エラーが発生する場合があります。
**Open WebUI でチャットしたい場合は、先に Pipeline を停止してください。**

詳しくは「運用ルール（VRAM 競合回避のため必読）」の表を参照してください。

##### GUI 終了時の挙動

GUI を閉じても、このタブから起動した Pipeline と Open WebUI は **継続します**。
処理を中断されると困るため、意図的にこの仕様にしています。

サービスを停止したい場合は、GUI を閉じる前に「停止」ボタンまたは「すべて停止」を
押してください。

##### 既知の制約

- **Ollama** は通常システムサービスとして常駐しているため、GUI からの起動に失敗しても
  「稼働中」と表示されます（既存プロセスを検知するため）
- **外部で起動されたサービス** も、GUI が PID を検出できる場合は「停止」できます。
  Pipeline は `pipeline.lock` とこのプロジェクトの `pipeline.py`、Ollama / Open WebUI は
  待受ポートから PID を検出し、その PID だけを `taskkill /T /F` で停止します
- **Open WebUI の初回起動**はモデル一覧取得などで 30〜60 秒かかります。起動後しばらくは
  「起動中」と表示され、5 秒おきのポーリングで更新されます
- GUI を強制終了（タスクマネージャ等）すると孫プロセスが残る場合があります。
  残った場合はタスクマネージャから手動で終了してください

#### B-3-1. 失敗ファイルの管理

pipeline.py は処理に連続失敗したファイルを `data/input/failed/` に隔離します。
これにより「同じファイルを何度も再処理して無限ループ化する」問題を防ぎます。

##### 仕組み

1. ファイルの処理が失敗するたびに `data/logs/retry_count.json` でリトライ回数を +1 する
2. リトライ回数が `retry_max`（デフォルト 3 回）に達したら:
   - ファイルを `data/input/failed/` に移動（音声は `.meta.json` も一緒に移動）
   - `data/logs/failed_files.json` に失敗履歴を永続記録
   - トースト通知「✗ 連続失敗のため隔離: <ファイル名>」を表示
3. 処理が成功した場合はリトライカウントをリセット（0 から再カウント）

##### 「失敗一覧」ダイアログの使い方

録音 GUI の「録音」タブ下部に「失敗一覧 (N)」ボタンがあります（N は隔離済み件数）。
件数が 0 のときはボタンが非活性になります。

ボタンを押すと以下のダイアログが開きます:

```
┌─ 隔離された失敗ファイル ─────────────────────────────────┐
│ ファイル名                    隔離日時         失敗回数 最後のエラー  │
│ rec_2026-05-13_120126.wav    2026-05-15 14:35    3    transcribe 失敗 │
│ meeting_notes.docx           2026-05-15 14:40    3    summarize 失敗  │
│ ...                                                               │
├────────────────────────────────────────────────────────────────────┤
│  [ 再試行 ]  [ 削除 ]  [ エクスプローラで開く ]         [ 閉じる ] │
└────────────────────────────────────────────────────────────────────┘
```

| ボタン | 動作 |
|---|---|
| 再試行 | 選択ファイルを `data/input/` に戻す → pipeline が拾って再処理開始 |
| 削除 | 確認後に物理削除（元に戻せないので注意） |
| エクスプローラで開く | `data/input/failed/` フォルダをエクスプローラで開く |

「再試行」ボタンを押すと、リトライカウントもリセットされます。
もし同じエラーで再び失敗した場合は、再度 3 回失敗後に隔離されます。

##### リトライ上限の設定変更

`config/settings.yaml` の `pipeline.retry_max` で変更できます:

```yaml
pipeline:
  retry_max: 3                          # 3 回失敗で隔離（デフォルト）
  retry_count_file: data/logs/retry_count.json   # リトライ回数の記録先
  failed_files_log: data/logs/failed_files.json  # 失敗履歴の永続記録先
```

##### 失敗の典型的な原因と対処

| エラー内容 | 原因 | 対処 |
|---|---|---|
| `transcribe 失敗` | VRAM 不足 / faster-whisper クラッシュ | GPU を解放してから「再試行」。`compute_type: int8_float16` に変更してみる |
| `summarize 失敗` | Ollama が停止している / VRAM 不足 | `ollama serve` で Ollama を起動してから「再試行」 |
| `ingest_db 失敗` | ChromaDB がロックされている / ディスクフル | 他のプロセスを終了 / ディスク空きを確保してから「再試行」 |
| `import_transcript 失敗` | .docx が壊れている / 文字コードエラー | ファイル内容を確認してから「再試行」。修復不能なら「削除」 |

#### B-3-2. 処理中の停止について

pipeline.py を `Ctrl+C` やタスクマネージャの強制終了で止めた場合に、中途半端な
ファイルや ChromaDB の不整合が残らないよう設計してあります。

##### 停止方法ごとの挙動

| 停止方法 | 何が起きるか | 残骸 |
|---|---|---|
| `Ctrl+C`（推奨） | `finally` 節で lock file 削除 + heartbeat 停止 | なし |
| タスクマネージャ強制終了 | `finally` が走らず lock file が残る場合あり | 次回起動時に lock 検証 + tmp クリーンアップで自動回復 |
| OS シャットダウン | 同上 | 同上 |

##### アトミック書き込み

`transcribe.py`（Step 1）と `summarize.py`（Step 2）は出力を一度 `*.tmp` に書き、
完全に書き終わってから `os.replace` で本ファイル名へ昇格させます。これにより、
書き込み途中で殺されても「中途半端な `.txt` / `.md` が次の処理で拾われる」事故が起きません。

万が一 `*.tmp` が残った場合は、次回 `pipeline.py` 起動時の自動クリーンアップで削除されます
（`config/settings.yaml` の `pipeline.cleanup_tmp_on_startup: true` がデフォルト）。

##### ChromaDB の整合性（Step 3）

`ingest_db.py` は投入時に「同じ `source_file` メタを持つ既存チャンクを `delete` してから
新規チャンクを `add`」する delete-then-add 戦略を取っています。これにより、

- 同じノートを再処理しても重複チャンクが増えない
- 前回チャンク数 > 今回チャンク数の場合でも、孤児チャンクが残らない

Step 3 の途中（delete 完了後・add 完了前）で停止された場合のみ、該当ノートが
ChromaDB から一時的に消えますが、次回 pipeline 起動時に同じノートが再処理されて
復元されます（「重複より欠落の方がマシ」というトレードオフ）。

### B-2. 既存テキスト（Teams、iPhone、メモ等）を取り込む

Teams 会議トランスクリプト（.docx）、iPhone ボイスメモの起こし（.txt）、Zoom 録画のキャプション（.vtt）など、
既に文字起こし済みのテキストファイルを RAG に取り込めます。

#### 単発取り込み（CLI）

```powershell
python scripts/import_transcript.py data/input_text/teams_meeting.docx
python scripts/summarize.py data/transcripts/teams_meeting_2026-05-14_XXXX.txt
python scripts/ingest_db.py data/notes/teams_meeting_2026-05-14_XXXX.md
```

#### 自動取り込み（フォルダ監視）

```powershell
python scripts/pipeline.py
# data/input_text/ に .docx / .txt / .vtt / .md をコピーすると自動で
# 要約 → ChromaDB 投入まで完了。処理済みファイルは data/input_text/processed/ へ退避。
```

#### 対応形式

| 形式 | 想定ソース | 話者・タイムスタンプ |
|---|---|---|
| `.docx` | Microsoft Teams 会議トランスクリプト | 実名で抽出 |
| `.vtt` | Zoom / Teams ライブキャプション | 抽出（`<v 名前>` タグから） |
| `.txt` | iPhone ボイスメモ起こし、メモ全般 | 無し（プレーン本文） |
| `.md` | Markdown メモ | 無し（プレーン本文） |

### C. CLI で類似検索（動作確認）

```powershell
python scripts/search.py "先週の会議で決まった納期は？"
```

### D. Open WebUI で対話的に質問する

1. 別ターミナルで `open-webui serve --port 3000` を起動しておく
2. ブラウザで `http://localhost:3000` を開く
3. 右上アイコン → Settings → **Connections** → Ollama URL に `http://localhost:11434` を設定（pip 版は同一マシン上で動くので `localhost` で OK）
4. 左メニュー **Workspace → Knowledge** → 新規 Knowledge を作成
5. `data/notes/` の `.md` ファイルを **すべてアップロード**（ドラッグ&ドロップ可）
6. 新規チャットで右上の `+` から作成した Knowledge を選択 → 質問

> 自前 ChromaDB と Open WebUI 内蔵 RAG は **並行運用**しています。CLI 検索は自前 DB、チャットは Open WebUI 内蔵 DB を使う形です。

### E. Open WebUI 自動同期（手動アップロード不要にする）

pipeline.py が音声・テキストを処理すると、完了後に自動で WebUI Knowledge にアップロードします。
Open WebUI が停止していて同期に失敗した分は、次にサービス管理タブから Open WebUI を
起動したときにも自動回収されます。
GUI 起動中に `data/notes/` へ手動で追加・更新した `.md` も、書き込みが安定したあと
Open WebUI 稼働中なら自動同期されます。
初回だけ以下のセットアップが必要です。

#### 初回セットアップ手順

```bash
# 1. venv を有効化して requests をインストール
source .venv/Scripts/activate   # Git Bash
# または
.\.venv\Scripts\Activate.ps1    # PowerShell
pip install -r requirements.txt

# 2. .env に API キーを追記
#    .env.example の OPENWEBUI_API_KEY 行を参考に、実際のキーを .env に貼る
#    キー取得: WebUI を開いて 右上アバター → Settings → Account → API Keys

# 3. WebUI を別ターミナルで起動
source .venv-webui/Scripts/activate
open-webui serve --port 3000

# 4. Knowledge を作成して ID を確認
python scripts/sync_webui.py --create-knowledge "PersonalRAG"
# → 表示された ID を config/settings.yaml の openwebui.knowledge_id に貼る
```

`config/settings.yaml` の該当行を書き換えます:

```yaml
openwebui:
  enabled: true
  knowledge_id: "ここに表示された ID を貼る"
```

#### 初回バルクアップロード（既存 notes/ を一括 sync）

```bash
python scripts/sync_webui.py
# → data/notes/ の全 .md が WebUI Knowledge にアップロードされる
```

#### 手動で特定ファイルを sync

```bash
python scripts/sync_webui.py data/notes/meeting_2026-05-14_1030.md
# → そのファイルだけアップロードされる
```

#### Knowledge 一覧確認（ID を調べたいとき）

```bash
python scripts/sync_webui.py --list-knowledges
```

#### 全件再アップロード（インデックスをリセットしたいとき）

```bash
python scripts/sync_webui.py --reupload-all
```

#### WebUI が停止していた場合の回収

```bash
# WebUI を起動してから実行すると、停止中に処理されたファイルをまとめてアップロード
python scripts/sync_webui.py
```

> 重複防止インデックスは `data/.webui_synced.json` に保存されます（`data/` 配下のため `.gitignore` で Git 管理外になっています）。

---

### F. リモートPC運用構成（社内NAS共有・パターンB）

GPU を持つ別の本番 PC で重い処理（文字起こし・要約・DB・Open WebUI ホスティング）を動かし、手元 PC からは「録音」と「Teams/iPhone のテキスト投入」「ブラウザでの検索チャット」だけを行う構成。社内 LAN・固定 IP・社内 NAS（社外秘データを置いてよい場所）が前提です。

#### アーキテクチャ

```
[手元PC]                                              [リモートPC (RTX Pro 2000 16GB)]
record_mic.py で録音 ─┐                            ┌─ pipeline.py 監視
Teams .docx / .txt   ─┤                            │   transcribe → summarize → ingest_db
                      ▼                            │                                   → sync_webui
       \\<NAS>\PersonalRAG\input\          ───────▶│  Open WebUI (--host 0.0.0.0:3000)
       \\<NAS>\PersonalRAG\input_text\     ───────▶│
                                                   │
ブラウザ http://<REMOTE-IP>:3000 ◀──────HTTP───────┘
```

**ポイント**:
- 両PCが NAS の同じパス（例 `\\nas-server\share\PersonalRAG\input\`）を `data/input/` として参照する
- リモートPC内の `ChromaDB` だけは絶対にローカル SSD に置く（SQLite ロック競合で破損するため）
- `scripts/config_loader.py` の `resolve_path()` が UNC パスを素通しするため、`settings.yaml` に直接 `\\nas-server\...` と書ける

#### F-1. NAS 上に共有フォルダを作る

NAS 上で `PersonalRAG\input\` と `PersonalRAG\input_text\` を作成し、リモートPC・手元 PC の両方の Windows ユーザーアカウントに **「変更」権限**を付与（Everyone は不可、社外秘データ漏洩防止）。

#### F-2. リモートPC側の設定

1. 既存の「クイックセットアップ → prod プロファイル」を完了させる
2. `config/settings.yaml` の `paths` セクションを編集:
   ```yaml
   paths:
     # 両PCが触る分は NAS パスに
     input_dir: \\nas-server\share\PersonalRAG\input
     input_text_dir: \\nas-server\share\PersonalRAG\input_text
     processed_dir: \\nas-server\share\PersonalRAG\input\processed
     processed_text_dir: \\nas-server\share\PersonalRAG\input_text\processed

     # 中間ファイル・DB はリモートPCのローカルディスクに残す
     transcripts_dir: data/transcripts
     notes_dir: data/notes
     chromadb_dir: data/chromadb        # ← 絶対 NAS に置かない（SQLite ロック対策）
     recordings_dir: data/input         # ← リモートでは未使用
     logs_dir: data/logs
   ```
3. Open WebUI を **LAN 公開**で起動:
   ```powershell
   .\.venv-webui\Scripts\Activate.ps1
   open-webui serve --port 3000 --host 0.0.0.0
   ```
4. Windows ファイアウォール:
   - 「Windows Defender ファイアウォール」→「詳細設定」→「受信の規則」→「新規」
   - 種類「ポート」→ TCP `3000` → 許可 → プロファイル「**ドメイン**」「**プライベート**」のみチェック（**パブリックは外す**）→ 名前「Open WebUI LAN」
5. `python scripts/pipeline.py` で監視開始（NAS パスを watch）

#### F-3. 手元 PC 側の設定

1. リポジトリ clone + **最小依存だけ**インストール（Ollama / faster-whisper / pyannote は不要）:
   ```powershell
   cd C:\path\to\your\workspace
   git clone https://github.com/kemgoro-cpu/PersonalRAG
   cd PersonalRAG
   python -m venv .venv-client
   .\.venv-client\Scripts\Activate.ps1
   pip install sounddevice soundfile numpy pyyaml python-dotenv
   ```
2. `config/settings.yaml` を新規作成（または `settings.dev.yaml` をコピーして編集）:
   ```yaml
   paths:
     recordings_dir: \\nas-server\share\PersonalRAG\input    # ← NAS の input と同じ
     # 以下は record_mic.py からは参照されないが、エラー回避のため一応書いておく
     input_dir: data/input
     input_text_dir: data/input_text
     processed_dir: data/input/processed
     processed_text_dir: data/input_text/processed
     transcripts_dir: data/transcripts
     notes_dir: data/notes
     chromadb_dir: data/chromadb
     logs_dir: data/logs

   recording:
     sample_rate: 16000
     channels: 1
     format: wav
   ```
3. **NAS への認証情報を Windows に覚えさせる**: エクスプローラのアドレスバーに `\\nas-server\share\` と入力 → 認証ダイアログで「資格情報を記憶する」にチェック

#### F-4. 日常運用

| 操作 | 手元PCで | リモートPCで |
|---|---|---|
| マイク録音 | `python scripts/record_mic.py` | 自動で処理開始 |
| Teams 議事録投入 | エクスプローラで `\\nas-server\share\PersonalRAG\input_text\` にドラッグ | 自動で処理開始 |
| 検索・チャット | ブラウザで `http://<REMOTE-IP>:3000` | （何もしない） |
| ログ確認 | 必要時のみ RDP でリモートにログイン | RDP で `data/logs/pipeline.log` |

#### F-5. 動作確認チェックリスト

1. ✅ 手元 PC のエクスプローラで `\\nas-server\share\PersonalRAG\input\` が開ける
2. ✅ リモートPCで `python scripts/pipeline.py` を起動した状態で、手元 PC から NAS へ .wav をコピーすると即座に処理が走る
3. ✅ 手元 PC で `python scripts/record_mic.py` を実行し、録音停止後に NAS に .wav が保存される
4. ✅ 手元 PC のブラウザで `http://<REMOTE-IP>:3000` が開ける（Open WebUI ログイン画面が出る）
5. ✅ Open WebUI のチャットで先ほどの録音内容が Knowledge から引用されて回答される

#### F-6. 注意点

- **`chromadb_dir` を NAS に置かない**: SQLite が NAS（SMB）上だとロックが正しく機能せず DB 破損リスクあり。**必ずリモートPCのローカル SSD** に置く
- **NAS が落ちると pipeline が一時停止**: NAS 復旧後にリモートPCで `pipeline.py` を再起動すれば、起動時キャッチアップで未処理ファイルが自動回収される
- **Open WebUI を LAN に晒すリスク**: WebUI 自身のログイン認証で保護されるが、Firewall ルールで「パブリック」プロファイルは必ず外す（社外 Wi-Fi 接続時に外部公開されないように）

---

## 運用ルール（VRAM 競合回避のため必読）

| 状況 | やってよい / ダメ |
|---|---|
| `pipeline.py` 実行中に Open WebUI でチャット | ❌ Gemma を奪い合って OOM |
| 文字起こし中に Ollama にリクエスト | ❌ whisper と Gemma の同時起動で OOM |
| `pipeline.py` 停止 → Open WebUI でチャット | ✅ |
| Open WebUI 未使用時に `pipeline.py` 起動 | ✅ |

**推奨運用**: 仕事中は `pipeline.py` を立ち上げっぱなしにし、Open WebUI でチャットしたい時だけ Ctrl+C で停止する。

---

## モデル切り替えチートシート

環境別プロファイルをコピーするだけで切り替えられます（`settings.yaml` を直接編集する必要はありません）。

```bash
# Git Bash
cp config/settings.dev.yaml config/settings.yaml   # 開発機 (6GB): gemma3:4b
cp config/settings.prod.yaml config/settings.yaml  # 本番機 (16GB): gemma4:e4b-it-q4_K_M
```

どうしても手動で特定のモデルだけ変えたい場合は `config/settings.yaml` を直接編集します。

```yaml
llm:
  model: gemma3:4b               # 開発機 (6GB)
  # model: gemma4:e4b-it-q4_K_M  # 本番機 (16GB)、量子化 Gemma 4（9.6GB）
  # model: gemma3:1b              # さらに軽量にしたいとき（精度は落ちる）
```

---

## トラブルシューティング

### `CUDA out of memory` が出る

| 場面 | 対策 |
|---|---|
| Step 1（文字起こし）で OOM | `settings.yaml` の `whisper.compute_type` を `int8` に変更 |
| Step 1 で diarization が OOM | `diarization.device: cpu` に変更（処理は遅くなる） |
| Step 2（LLM）で OOM | LLM モデルを 1 ランク軽量化（27b → 12b → 4b） |

### `Ollama' is not running` エラー

PowerShell で `ollama list` を実行し、コマンドが通るか確認。通らなければ Ollama アプリを起動。

### 文字起こしの精度が悪い

`settings.yaml` の `whisper.initial_prompt` に業務固有の専門用語を追加してください（既に自動車開発用語が入っています）。

### pyannote が `gated repository` エラー

Hugging Face のモデルページで利用規約に同意していない可能性。[pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) と [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) の両方に対して `Agree and access repository` ボタンを押してください。

---

## 今後の拡張予定

- [ ] 週次サマリースクリプト（過去 7 日の notes を Gemma で要約）
- [ ] Open WebUI Pipelines で自前 ChromaDB を直接参照
- [ ] Slack / メール への ToDo 自動転送
- [ ] 話者ラベルを人名に手動マッピングする補助 UI
