"""record_mic.py
Step 1（補助）: PC マイクから音声をリアルタイム録音して WAV に保存するスクリプト。

使い方:
    python scripts/record_mic.py
    → 開始メッセージ表示後、Enter キーで録音停止。
    → data/recordings/ にタイムスタンプ付きの WAV が保存される。

オプション:
    --output-name <名前>  保存ファイル名（拡張子不要）。省略時は日時で自動生成。
    --transcribe          録音終了後にそのまま transcribe.py を呼び出す。

注意:
    録音中は Enter 待ちのループでブロックされる。Ctrl+C でも停止可能（その場合も
    それまでの音声は保存される）。

実装メモ:
    実際の録音ロジックは scripts/recorder.py の Recorder クラスに集約してある。
    本ファイルは CLI 専用のラッパで、Enter キー入力監視と引数処理だけを担う。
    GUI 版 (scripts/record_gui.py) も同じ Recorder を使うため、録音挙動は両者
    で完全に一致する。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from config_loader import PROJECT_ROOT, load_settings, resolve_path
from recorder import Recorder


def wait_for_enter(stop_event: threading.Event) -> None:
    """別スレッドで Enter キー入力を待ち、押されたら stop_event をセットする。"""
    try:
        input()  # Enter 待ち
    except EOFError:
        # 標準入力が閉じられた場合も停止扱い
        pass
    stop_event.set()


def record(output_path: Path, sample_rate: int, channels: int) -> None:
    """マイクから録音し、output_path に WAV として保存する。

    Args:
        output_path: 保存先 WAV ファイルのパス。
        sample_rate: サンプリングレート（Hz）。whisper は 16000 推奨。
        channels: チャンネル数（1=モノラル）。
    """
    # Enter 押下で停止するための共有 Event
    stop_event = threading.Event()
    recorder = Recorder(sample_rate=sample_rate, channels=channels)

    # Enter 待ちスレッド開始
    threading.Thread(target=wait_for_enter, args=(stop_event,), daemon=True).start()

    print(f"[mic] 録音開始（Enter キーで停止）  sr={sample_rate}Hz ch={channels}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    recorder.start(output_path)

    try:
        while not stop_event.is_set():
            # 0.2 秒ごとに Event と Recorder のエラー状態をチェック
            stop_event.wait(timeout=0.2)
            err = recorder.last_error()
            if err is not None:
                raise err
    except KeyboardInterrupt:
        print("\n[mic] Ctrl+C を検知、停止します。", file=sys.stderr)
    finally:
        recorder.stop()

    print(f"[mic] 保存しました: {output_path}")


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(description="マイクから録音して WAV 保存")
    parser.add_argument(
        "--output-name",
        type=str,
        default=None,
        help="保存ファイル名（拡張子不要）。省略時は日時で自動生成。",
    )
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="録音終了後に transcribe.py を呼び出して文字起こしを行う。",
    )
    args = parser.parse_args()

    settings = load_settings()
    rec_cfg = settings["recording"]
    recordings_dir = resolve_path(settings["paths"]["recordings_dir"])

    # ファイル名生成
    if args.output_name:
        filename = f"{args.output_name}.wav"
    else:
        filename = f"rec_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.wav"
    output_path = recordings_dir / filename

    # 録音実行
    record(
        output_path=output_path,
        sample_rate=rec_cfg["sample_rate"],
        channels=rec_cfg["channels"],
    )

    # オプション: 続けて文字起こし
    if args.transcribe:
        transcribe_script = PROJECT_ROOT / "scripts" / "transcribe.py"
        print(f"\n[mic] 文字起こしを開始: {transcribe_script.name}")
        # subprocess で別プロセス起動（マイクと whisper の依存衝突を避ける）
        return subprocess.call(
            [sys.executable, str(transcribe_script), str(output_path)]
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
