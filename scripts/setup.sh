#!/usr/bin/env bash
# =============================================================================
# PersonalRAG セットアップスクリプト（Git Bash 用）
#
# 使い方:
#   ./scripts/setup.sh dev    # 開発機（RTX 3060 6GB）向け
#   ./scripts/setup.sh prod   # 本番機（RTX Pro 2000 16GB）向け
#   ./scripts/setup.sh        # 引数なしの場合は dev を使用
#
# 前提条件:
#   - Python 3.10 以上がインストール済みで PATH が通っていること
#   - Ollama をインストール済みであること（モデル取得に使う）
#   - Git Bash（Windows Terminal や VS Code のターミナルで起動）から実行すること
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

# set -e: 途中でエラーが起きたら即座にスクリプトを停止する
# これがないと、エラーが起きても次の処理に進んでしまう恐れがある
set -e

# =============================================================================
# 進捗ログ用ヘルパー関数
# ANSI カラーコード: \033[36m = シアン, \033[33m = 黄, \033[31m = 赤, \033[0m = リセット
# =============================================================================

# 通常の進捗メッセージ（シアン色）
info() {
    echo -e "\033[36m[setup]\033[0m $1"
}

# 警告メッセージ（黄色）— 続行するが注意が必要なとき
warn() {
    echo -e "\033[33m[setup 警告]\033[0m $1"
}

# エラーメッセージ（赤色）— このあと exit 1 で停止する
error() {
    echo -e "\033[31m[setup エラー]\033[0m $1"
}

# =============================================================================
# Step 1/12: 引数バリデーション
# =============================================================================
info "Step 1/12: 引数を確認しています..."

# 引数が空なら dev をデフォルトとして使う
PROFILE="${1:-dev}"

# dev か prod 以外が渡されたらエラー終了
if [[ "$PROFILE" != "dev" && "$PROFILE" != "prod" ]]; then
    error "引数が正しくありません: '$PROFILE'"
    echo ""
    echo "使い方:"
    echo "  ./scripts/setup.sh dev    # 開発機（RTX 3060 6GB）向け"
    echo "  ./scripts/setup.sh prod   # 本番機（RTX Pro 2000 16GB）向け"
    exit 1
fi

info "プロファイル: $PROFILE"

# =============================================================================
# Step 2/12: プロジェクトルートへ移動してファイル存在確認
# =============================================================================
info "Step 2/12: プロジェクトルートに移動しています..."

# このスクリプト自身の場所（scripts/）から1つ上がプロジェクトルート
# BASH_SOURCE[0] = このスクリプトのパス。dirname で「scripts/」を取得し、
# その1つ上（..）= プロジェクトルートに cd する
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"
info "作業ディレクトリ: $PROJECT_ROOT"

# 必須ファイルの存在確認。無ければセットアップを中断する
if [[ ! -f "config/settings.${PROFILE}.yaml" ]]; then
    error "config/settings.${PROFILE}.yaml が見つかりません。"
    error "プロジェクトが正しくダウンロードできているか確認してください。"
    exit 1
fi

if [[ ! -f "requirements.txt" ]]; then
    error "requirements.txt が見つかりません。"
    error "プロジェクトが正しくダウンロードできているか確認してください。"
    exit 1
fi

info "必須ファイルの確認 OK"

# =============================================================================
# Step 3/12: 省略（Step 2 と統合済み）
# Step 4/12: 省略（Step 2 と統合済み）
# Step 5/12: メイン venv の作成
# =============================================================================
info "Step 5/12: メイン仮想環境（.venv）を準備しています..."

# 仮想環境（venv）= Python の依存ライブラリを他のプロジェクトと分離するための箱
# 既に存在すればスキップして再利用（2回目以降のセットアップで時間短縮）
if [[ ! -d ".venv" ]]; then
    info ".venv を新規作成します..."
    python -m venv .venv
    info ".venv の作成完了"
else
    info "既存の .venv を再利用します（スキップ）"
fi

# =============================================================================
# Step 6/12: PyTorch CUDA 版のインストール
# =============================================================================
info "Step 6/12: PyTorch（CUDA 版）をインストールしています..."
info "※ファイルサイズが大きいため時間がかかります（数分〜10分）"

# activate を使わず直接 .venv の python を呼ぶ
# 理由: bash スクリプト内で source activate すると親シェルの状態を変えてしまうことがあり、
#       直接パス指定のほうがスクリプトとして安全で明示的
# -m pip: pip を直接呼ばず python 経由で実行することで、
#         PATH の状況に関わらず必ず .venv 内の pip が使われることを保証する
# --index-url: PyTorch 公式の CUDA 12.1 用ホイール配布サーバーから取得する指定
./.venv/Scripts/python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# =============================================================================
# Step 7/12: requirements.txt の依存ライブラリをインストール
# =============================================================================
info "Step 7/12: requirements.txt から依存ライブラリをインストールしています..."

./.venv/Scripts/python.exe -m pip install -r requirements.txt

info "メイン venv のインストール完了"

# =============================================================================
# Step 8/12: Open WebUI 用 venv の作成
# =============================================================================
info "Step 8/12: Open WebUI 用仮想環境（.venv-webui）を準備しています..."

# Open WebUI はメインの .venv とは別環境に入れる
# 理由: open-webui は独自の依存ライブラリを大量に持ち、
#       メインの venv に入れると whisper や pyannote と競合する可能性がある
if [[ ! -d ".venv-webui" ]]; then
    info ".venv-webui を新規作成します..."
    python -m venv .venv-webui
    info ".venv-webui の作成完了"
else
    info "既存の .venv-webui を再利用します（スキップ）"
fi

# =============================================================================
# Step 9/12: Open WebUI のインストール
# =============================================================================
info "Step 9/12: open-webui をインストールしています..."
info "※初回は時間がかかります（数分）"

./.venv-webui/Scripts/python.exe -m pip install open-webui

info "Open WebUI のインストール完了"

# =============================================================================
# Step 10/12: 設定プロファイルの適用
# =============================================================================
info "Step 10/12: 設定プロファイルを適用しています..."
info "  config/settings.${PROFILE}.yaml → config/settings.yaml"

# cp コマンドで環境別プロファイルを settings.yaml にコピー
# 既存の settings.yaml があれば上書きされる（意図通り）
cp "config/settings.${PROFILE}.yaml" "config/settings.yaml"

info "設定プロファイルの適用完了 (${PROFILE})"

# =============================================================================
# Step 10.5/12: 現在のプロファイル名を data/logs/active_profile.txt に記録
# 目的: 後から「いまどちらの設定で動いているか」をファイル1つで確認できるようにする
# =============================================================================
info "Step 10.5/12: 現プロファイルを data/logs/active_profile.txt に記録しています..."

# data/logs/ ディレクトリが存在しない場合は作成する
# -p: 途中のディレクトリが無くても一括作成してくれるオプション
mkdir -p data/logs

# { ... } > ファイル: 複数行をまとめてファイルに書き出すリダイレクト構文
# date '+%Y-%m-%d %H:%M:%S': "2026-05-14 12:34:56" 形式で現在時刻を取得
{
    echo "$PROFILE"
    echo "applied_at: $(date '+%Y-%m-%d %H:%M:%S')"
} > data/logs/active_profile.txt

info "記録完了: data/logs/active_profile.txt"

# =============================================================================
# Step 11/12: Ollama モデルの取得
# =============================================================================
info "Step 11/12: Ollama モデルを取得しています..."

# command -v ollama: ollama コマンドが存在するか確認する
# 存在しない場合は warn だけ出してスキップ（ollama は手動インストールが必要なため）
if ! command -v ollama &> /dev/null; then
    warn "ollama コマンドが見つかりません。Ollama がインストールされていない可能性があります。"
    warn "https://ollama.com/download/windows からインストール後、手動で以下を実行してください:"
    if [[ "$PROFILE" == "dev" ]]; then
        warn "  ollama pull gemma3:4b"
    else
        warn "  ollama pull gemma4:e4b-it-q4_K_M"
    fi
    warn "  ollama pull nomic-embed-text"
else
    # プロファイルに応じて LLM モデルを切り替える
    if [[ "$PROFILE" == "dev" ]]; then
        info "開発機用モデル（gemma3:4b）を取得します..."
        ollama pull gemma3:4b
    else
        # prod: Gemma 4 E4B 量子化版（VRAM 約9.6GB、本番機16GBで安定動作）
        info "本番機用モデル（gemma4:e4b-it-q4_K_M）を取得します..."
        info "※モデルサイズが大きいため、ダウンロードに時間がかかります"
        ollama pull gemma4:e4b-it-q4_K_M
    fi

    # 埋め込みモデルは dev/prod 共通で使う
    info "埋め込みモデル（nomic-embed-text）を取得します..."
    ollama pull nomic-embed-text

    info "Ollama モデルの取得完了"
fi

# =============================================================================
# Step 12/12: 完了メッセージと残り手動作業の案内
# =============================================================================
info "Step 12/12: セットアップ完了！"

echo ""
echo "============================================================"
echo "  セットアップ完了 (プロファイル: ${PROFILE})"
echo "============================================================"
echo ""
echo "残り手動作業（以下を順番に行ってください）:"
echo ""
echo "  1. .env ファイルを作成してください:"
echo "       cp .env.example .env"
echo "       # .env を開いて HUGGINGFACE_TOKEN=hf_xxx の xxx 部分を書き換える"
echo ""
echo "  2. Hugging Face で話者分離モデルの利用規約に同意してください:"
echo "       https://huggingface.co/pyannote/speaker-diarization-3.1"
echo "       https://huggingface.co/pyannote/segmentation-3.0"
echo "       ※ 上記ページを開いて「Agree and access repository」ボタンを押す"
echo ""
echo "  3. OLLAMA_KEEP_ALIVE=0 の設定を推奨します（VRAM 競合回避のため）:"
echo "       # PowerShell で実行（PCを再起動後に反映）:"
echo "       setx OLLAMA_KEEP_ALIVE \"0\""
echo ""
echo "設定確認: config/settings.yaml（現在は ${PROFILE} プロファイルが適用済み）"
echo ""
echo "起動方法:"
echo "  パイプライン:  .venv/Scripts/python scripts/pipeline.py"
echo "  Open WebUI:   .venv-webui/Scripts/open-webui serve --port 3000"
echo "============================================================"
