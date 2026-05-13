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
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from config_loader import PROJECT_ROOT, load_settings, resolve_path


def wait_for_enter(stop_event: threading.Event) -> None:
    """別スレッドで Enter キー入力を待ち、押されたら stop_event をセットする。"""
    try:
        input()  # Enter 待ち
    except EOFError:
        # 標準入力が閉じられた場合も停止扱い
        pass
    stop_event.set()


def record(
    output_path: Path, sample_rate: int, channels: int
) -> None:
    """マイクから録音し、output_path に WAV として保存する。

    Args:
        output_path: 保存先 WAV ファイルのパス。
        sample_rate: サンプリングレート（Hz）。whisper は 16000 推奨。
        channels: チャンネル数（1=モノラル）。
    """
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    stop_event = threading.Event()

    def callback(
        indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        """sounddevice から呼ばれるコールバック。受信した音声バッファを queue に積む。"""
        if status:
            # オーバーラン等の警告。録音は継続させたいので print のみ
            print(f"[mic] status={status}", file=sys.stderr)
        audio_queue.put(indata.copy())

    # Enter 待ちスレッド開始
    threading.Thread(target=wait_for_enter, args=(stop_event,), daemon=True).start()

    print(f"[mic] 録音開始（Enter キーで停止）  sr={sample_rate}Hz ch={channels}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # WAV を逐次書き込み（メモリに溜め込まないことで長時間録音にも対応）
    with sf.SoundFile(
        str(output_path),
        mode="w",
        samplerate=sample_rate,
        channels=channels,
        subtype="PCM_16",
    ) as wav_file:
        with sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            callback=callback,
            dtype="float32",
        ):
            try:
                while not stop_event.is_set():
                    try:
                        # 0.5秒ごとに queue をチェックしつつ書き込み
                        chunk = audio_queue.get(timeout=0.5)
                        wav_file.write(chunk)
                    except queue.Empty:
                        continue
            except KeyboardInterrupt:
                print("\n[mic] Ctrl+C を検知、停止します。", file=sys.stderr)

            # queue に残った音声バッファを書き出す
            while not audio_queue.empty():
                try:
                    wav_file.write(audio_queue.get_nowait())
                except queue.Empty:
                    break

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
