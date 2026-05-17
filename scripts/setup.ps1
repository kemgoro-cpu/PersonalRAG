# =============================================================================
# PersonalRAG セットアップスクリプト（PowerShell 用）
#
# 使い方:
#   .\scripts\setup.ps1 dev    # 開発機（RTX 3060 6GB）向け
#   .\scripts\setup.ps1 prod   # 本番機（RTX Pro 2000 16GB）向け
#   .\scripts\setup.ps1        # 引数なしの場合は dev を使用
#
# 前提条件:
#   - Python 3.10 以上がインストール済みで PATH が通っていること
#   - Ollama をインストール済みであること（モデル取得に使う）
#   - PowerShell 5.1 以上で実行すること（Windows 11 標準で問題なし）
#
# このスクリプトが行うこと:
#   1. 引数チェック（dev / prod のみ受け付け）
#   2. プロジェクトルートへ移動・ファイル存在確認
#   3. メイン用 仮想環境（.venv）の作成と依存インストール
#   4. Open WebUI 用 仮想環境（.venv-webui）の作成とインストール
#   5. 設定プロファイルの適用（settings.dev.yaml → settings.yaml）
#   6. Ollama モデルの取得（ollama がインストール済みの場合のみ）
#   7. 残り手動作業の案内表示
# =============================================================================

# =============================================================================
# 引数定義
# ValidateSet: "dev" か "prod" 以外が渡されると PowerShell が自動でエラーを出す
# =============================================================================
param(
    [Parameter(Position = 0)]
    [ValidateSet("dev", "prod")]
    [string]$Profile = "dev"
)

# $ErrorActionPreference = "Stop": エラーが起きたら即座に停止する設定
# これがないと、エラーが起きても次の処理に進んでしまう恐れがある
# PowerShell の param ブロックはスクリプト先頭に置く必要があるため、
# 代入は param の後に行う。
$ErrorActionPreference = "Stop"

# =============================================================================
# 進捗ログ用ヘルパー関数
# =============================================================================

# 通常の進捗メッセージ（シアン色）
function Write-Info {
    param([string]$Message)
    Write-Host "[setup] $Message" -ForegroundColor Cyan
}

# 警告メッセージ（黄色）— 続行するが注意が必要なとき
function Write-Warn {
    param([string]$Message)
    Write-Host "[setup 警告] $Message" -ForegroundColor Yellow
}

# エラーメッセージ（赤色）— このあと終了する
function Write-Err {
    param([string]$Message)
    Write-Host "[setup エラー] $Message" -ForegroundColor Red
}

# =============================================================================
# Step 1/12: 引数バリデーション
# （ValidateSet で自動チェックされるため、ここでは確認表示のみ）
# =============================================================================
Write-Info "Step 1/12: 引数を確認しています..."
Write-Info "プロファイル: $Profile"

# =============================================================================
# Step 2/12: プロジェクトルートへ移動してファイル存在確認
# =============================================================================
Write-Info "Step 2/12: プロジェクトルートに移動しています..."

# このスクリプト自身の場所（scripts\）から1つ上がプロジェクトルート
# $PSScriptRoot = このスクリプトが置かれているフォルダの絶対パス（PowerShell 組み込み変数）
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
Write-Info "作業ディレクトリ: $ProjectRoot"

# 必須ファイルの存在確認。無ければセットアップを中断する
if (-not (Test-Path "config\settings.$Profile.yaml")) {
    Write-Err "config\settings.$Profile.yaml が見つかりません。"
    Write-Err "プロジェクトが正しくダウンロードできているか確認してください。"
    exit 1
}

if (-not (Test-Path "requirements.txt")) {
    Write-Err "requirements.txt が見つかりません。"
    Write-Err "プロジェクトが正しくダウンロードできているか確認してください。"
    exit 1
}

Write-Info "必須ファイルの確認 OK"

# =============================================================================
# Step 5/12: メイン venv の作成
# =============================================================================
Write-Info "Step 5/12: メイン仮想環境（.venv）を準備しています..."

# 仮想環境（venv）= Python の依存ライブラリを他のプロジェクトと分離するための箱
# 既に存在すればスキップして再利用（2回目以降のセットアップで時間短縮）
if (-not (Test-Path ".venv")) {
    Write-Info ".venv を新規作成します..."
    python -m venv .venv
    Write-Info ".venv の作成完了"
} else {
    Write-Info "既存の .venv を再利用します（スキップ）"
}

# =============================================================================
# Step 6/12: PyTorch CUDA 版のインストール
# =============================================================================
Write-Info "Step 6/12: PyTorch（CUDA 版）をインストールしています..."
Write-Info "※ファイルサイズが大きいため時間がかかります（数分〜10分）"

# & 演算子: スペースを含むパスや変数で表されたコマンドを実行するときに使う
# activate を使わず直接 .venv の pip を呼ぶ
# 理由: Activate.ps1 を実行すると現在のシェル環境を変えてしまうことがあり、
#       直接パス指定のほうがスクリプトとして安全で明示的
# --index-url: PyTorch 公式の CUDA 用ホイール配布サーバーから取得する指定
# dev は RTX 3060 の既存実績を優先して CUDA 12.1、prod は Blackwell 世代に合わせて CUDA 12.8 を使う。
if ($Profile -eq "prod") {
    $TorchIndexUrl = "https://download.pytorch.org/whl/cu128"
    Write-Info "本番機用 PyTorch wheel（CUDA 12.8）を使います。"
} else {
    $TorchIndexUrl = "https://download.pytorch.org/whl/cu121"
    Write-Info "開発機用 PyTorch wheel（CUDA 12.1）を使います。"
}
& .\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url $TorchIndexUrl

# =============================================================================
# Step 7/12: requirements.txt の依存ライブラリをインストール
# =============================================================================
Write-Info "Step 7/12: requirements.txt から依存ライブラリをインストールしています..."

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Info "メイン venv のインストール完了"

# =============================================================================
# Step 8/12: Open WebUI 用 venv の作成
# =============================================================================
Write-Info "Step 8/12: Open WebUI 用仮想環境（.venv-webui）を準備しています..."

# Open WebUI はメインの .venv とは別環境に入れる
# 理由: open-webui は独自の依存ライブラリを大量に持ち、
#       メインの venv に入れると whisper や pyannote と競合する可能性がある
if (-not (Test-Path ".venv-webui")) {
    Write-Info ".venv-webui を新規作成します..."
    python -m venv .venv-webui
    Write-Info ".venv-webui の作成完了"
} else {
    Write-Info "既存の .venv-webui を再利用します（スキップ）"
}

# =============================================================================
# Step 9/12: Open WebUI のインストール
# =============================================================================
Write-Info "Step 9/12: open-webui をインストールしています..."
Write-Info "※初回は時間がかかります（数分）"

$OpenWebUIVersion = "0.9.5"
Write-Info "検証済みバージョン open-webui==$OpenWebUIVersion をインストールします。"
& .\.venv-webui\Scripts\python.exe -m pip install "open-webui==$OpenWebUIVersion"

Write-Info "Open WebUI のインストール完了"

# =============================================================================
# Step 10/12: 設定プロファイルの適用
# =============================================================================
Write-Info "Step 10/12: 設定プロファイルを適用しています..."
Write-Info "  config\settings.$Profile.yaml → config\settings.yaml"

# Copy-Item: ファイルをコピーするコマンド。-Force を付けると既存ファイルも上書き
Copy-Item "config\settings.$Profile.yaml" "config\settings.yaml" -Force

Write-Info "設定プロファイルの適用完了 ($Profile)"

# =============================================================================
# Step 10.5/12: 現在のプロファイル名を data\logs\active_profile.txt に記録
# 目的: 後から「いまどちらの設定で動いているか」をファイル1つで確認できるようにする
# =============================================================================
Write-Info "Step 10.5/12: 現プロファイルを data\logs\active_profile.txt に記録しています..."

# data\logs\ ディレクトリが存在しない場合は作成する
# -Force: 既に存在してもエラーにしない。| Out-Null: 作成成功メッセージを非表示にする
New-Item -ItemType Directory -Force -Path "data\logs" | Out-Null

# @(...) | Set-Content: 複数行をまとめてファイルに書き出す PowerShell の書き方
# Get-Date -Format: 日時を指定フォーマットで取得（例: "2026-05-14 12:34:56"）
# -Encoding UTF8: 日本語が含まれていても文字化けしないように UTF-8 で保存
@(
    $Profile
    "applied_at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
) | Set-Content -Path "data\logs\active_profile.txt" -Encoding UTF8

Write-Info "記録完了: data\logs\active_profile.txt"

# =============================================================================
# Step 11/12: Ollama モデルの取得
# =============================================================================
Write-Info "Step 11/12: Ollama モデルを取得しています..."

# Get-Command: 指定したコマンドが使えるか確認する
# -ErrorAction SilentlyContinue: コマンドが見つからなくてもエラーを表示しない
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    # プロファイルに応じて LLM モデルを切り替える
    if ($Profile -eq "dev") {
        Write-Info "開発機用モデル（gemma3:4b）を取得します..."
        ollama pull gemma3:4b
    } else {
        # prod: Gemma 4 E4B 量子化版（VRAM 約9.6GB、本番機16GBで安定動作）
        Write-Info "本番機用モデル（gemma4:e4b-it-q4_K_M）を取得します..."
        Write-Info "※モデルサイズが大きいため、ダウンロードに時間がかかります"
        ollama pull gemma4:e4b-it-q4_K_M
    }

    # 埋め込みモデルは dev/prod 共通で使う
    Write-Info "埋め込みモデル（nomic-embed-text）を取得します..."
    ollama pull nomic-embed-text

    Write-Info "Ollama モデルの取得完了"
} else {
    Write-Warn "ollama コマンドが見つかりません。Ollama がインストールされていない可能性があります。"
    Write-Warn "https://ollama.com/download/windows からインストール後、手動で以下を実行してください:"
    if ($Profile -eq "dev") {
        Write-Warn "  ollama pull gemma3:4b"
    } else {
        Write-Warn "  ollama pull gemma4:e4b-it-q4_K_M"
    }
    Write-Warn "  ollama pull nomic-embed-text"
}

# =============================================================================
# Step 12/12: 完了メッセージと残り手動作業の案内
# =============================================================================
Write-Info "Step 12/12: セットアップ完了！"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  セットアップ完了 (プロファイル: $Profile)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "残り手動作業（以下を順番に行ってください）:"
Write-Host ""
Write-Host "  1. .env ファイルを作成してください:"
Write-Host "       Copy-Item .env.example .env"
Write-Host "       # .env を開いて HUGGINGFACE_TOKEN=hf_xxx の xxx 部分を書き換える"
Write-Host ""
Write-Host "  2. Hugging Face で話者分離モデルの利用規約に同意してください:"
Write-Host "       https://huggingface.co/pyannote/speaker-diarization-3.1"
Write-Host "       https://huggingface.co/pyannote/segmentation-3.0"
Write-Host "       ※ 上記ページを開いて「Agree and access repository」ボタンを押す"
Write-Host ""
Write-Host "  3. OLLAMA_KEEP_ALIVE=0 の設定を推奨します（VRAM 競合回避のため）:"
Write-Host "       # PowerShell で実行（PCを再起動後に反映）:"
Write-Host '       setx OLLAMA_KEEP_ALIVE "0"'
Write-Host ""
Write-Host "設定確認: config\settings.yaml（現在は $Profile プロファイルが適用済み）"
Write-Host ""
Write-Host "起動方法:"
Write-Host "  パイプライン:  .\.venv\Scripts\python.exe scripts\pipeline.py"
Write-Host "  Open WebUI:   .\.venv-webui\Scripts\open-webui.exe serve --port 3000"
Write-Host "============================================================" -ForegroundColor Green
