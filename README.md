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

CUDA 12.1 用ホイールを先に入れる（faster-whisper / pyannote が依存）。

```powershell
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

#### 3. 残りの依存ライブラリ

```powershell
pip install -r requirements.txt
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

### B. マイクから録音する

```powershell
python scripts/record_mic.py
# Enter キーで停止
# 録音から文字起こしまで一気にやるなら --transcribe を付与
python scripts/record_mic.py --transcribe
```

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
