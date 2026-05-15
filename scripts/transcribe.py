"""transcribe.py
Step 1: 音声ファイルを文字起こししてテキストファイルに保存するスクリプト。

処理の流れ:
    1. faster-whisper（kotoba-whisper-v2.0-faster）で音声→テキスト変換
    2. pyannote.audio で話者分離（settings.yaml で ON/OFF 切替可能）
    3. 各セグメントに話者ラベルとタイムスタンプを付けて .txt に保存

使い方:
    python scripts/transcribe.py <音声ファイルパス>

例:
    python scripts/transcribe.py data/input/meeting_001.wav

VRAM 注意:
    開発機（VRAM 6GB）では whisper と pyannote の GPU 同時起動で OOM になる場合がある。
    その場合は config/settings.yaml の diarization.device を "cpu" にしてください。
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config_loader import (
    PROJECT_ROOT,
    get_huggingface_token,
    load_settings,
    resolve_path,
)


def format_timestamp(seconds: float) -> str:
    """秒数を `HH:MM:SS` 形式の文字列に変換する。

    Args:
        seconds: 経過秒数（小数可）。

    Returns:
        "01:23:45" のような文字列。
    """
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def transcribe_audio(
    audio_path: Path, whisper_cfg: dict[str, Any]
) -> list[dict[str, Any]]:
    """faster-whisper で音声を文字起こしし、セグメントのリストを返す。

    Args:
        audio_path: 入力音声ファイルのパス。
        whisper_cfg: settings.yaml の whisper セクション。

    Returns:
        各セグメントを表す辞書のリスト。各要素は {"start": float, "end": float, "text": str}。
    """
    # faster-whisper はインポート自体に時間がかかるので、関数内で遅延 import
    from faster_whisper import WhisperModel

    print(f"[whisper] モデルをロード中: {whisper_cfg['model']}")
    print(f"[whisper] device={whisper_cfg['device']}, compute_type={whisper_cfg['compute_type']}")

    model = WhisperModel(
        whisper_cfg["model"],
        device=whisper_cfg["device"],
        compute_type=whisper_cfg["compute_type"],
    )

    print(f"[whisper] 文字起こし開始: {audio_path.name}")
    vad_filter = whisper_cfg.get("vad_filter", False)
    no_speech_threshold = whisper_cfg.get("no_speech_threshold", 0.6)
    print(
        f"[whisper] vad_filter={vad_filter}, no_speech_threshold={no_speech_threshold}"
    )
    segments, info = model.transcribe(
        str(audio_path),
        language=whisper_cfg.get("language", "ja"),
        initial_prompt=whisper_cfg.get("initial_prompt", ""),
        beam_size=5,
        vad_filter=vad_filter,
        no_speech_threshold=no_speech_threshold,
    )
    # segments はジェネレータなので、ここでリスト化して全件確定させる
    seg_list: list[dict[str, Any]] = [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments
    ]
    print(
        f"[whisper] 完了: 言語={info.language} "
        f"(確率={info.language_probability:.2f}) "
        f"セグメント数={len(seg_list)}"
    )

    # モデル参照を破棄して GPU メモリを開放
    del model
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass

    return seg_list


def diarize_audio(
    audio_path: Path, diarization_cfg: dict[str, Any], hf_token: str
) -> list[tuple[float, float, str]]:
    """pyannote.audio で話者分離し、(開始秒, 終了秒, 話者ラベル) のリストを返す。

    Args:
        audio_path: 入力音声ファイルのパス。
        diarization_cfg: settings.yaml の diarization セクション。
        hf_token: Hugging Face トークン（モデル取得に必要）。

    Returns:
        話者区間のリスト。例: [(0.0, 5.2, "SPEAKER_00"), (5.2, 10.1, "SPEAKER_01"), ...]
    """
    from pyannote.audio import Pipeline
    import soundfile as sf
    import torch

    print(f"[diarize] パイプラインをロード中: {diarization_cfg['model']}")
    # pyannote.audio 3.x から引数名が use_auth_token → token に変更されている
    pipeline = Pipeline.from_pretrained(
        diarization_cfg["model"], token=hf_token
    )

    # 設定で device を指定（cuda / cpu）
    device_name = diarization_cfg.get("device", "cpu")
    if device_name == "cuda" and torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
    else:
        pipeline.to(torch.device("cpu"))

    # 音声を事前にメモリへ読み込んで pyannote に dict 形式で渡す。
    # こうすることで pyannote 内部の torchcodec/FFmpeg 依存を回避できる
    # （Windows で FFmpeg DLL が無くても動く）。
    print(f"[diarize] 音声を事前読み込み中: {audio_path.name}")
    waveform_np, sample_rate = sf.read(str(audio_path), dtype="float32")
    waveform = torch.from_numpy(waveform_np)
    # soundfile の返り値: モノラル → (time,) / マルチチャンネル → (time, channels)
    # pyannote が要求する形: (channels, time)
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)  # (1, time)
    else:
        waveform = waveform.T.contiguous()  # (time, ch) → (ch, time)

    print(f"[diarize] 話者分離開始 (device={device_name})")
    result = pipeline(
        {"waveform": waveform, "sample_rate": sample_rate},
        min_speakers=diarization_cfg.get("min_speakers", 1),
        max_speakers=diarization_cfg.get("max_speakers", 6),
    )

    # pyannote.audio のバージョンによって戻り値型が違うため両対応する
    # - 3.x: pipeline(...) → Annotation を直接返す
    # - 4.x: pipeline(...) → DiarizeOutput(.speaker_diarization に Annotation)
    if hasattr(result, "speaker_diarization"):
        annotation = result.speaker_diarization
    elif hasattr(result, "itertracks"):
        annotation = result
    else:
        raise RuntimeError(
            f"想定外の戻り値型: {type(result).__name__}。"
            "pyannote.audio のドキュメントを確認してください。"
        )

    speaker_segments: list[tuple[float, float, str]] = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        speaker_segments.append((turn.start, turn.end, speaker))

    print(f"[diarize] 完了: 話者区間数={len(speaker_segments)}")

    # メモリ開放
    del pipeline
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    return speaker_segments


def assign_speaker_to_segment(
    seg_start: float,
    seg_end: float,
    speaker_segments: list[tuple[float, float, str]],
) -> str:
    """whisper のセグメントに対応する話者ラベルを返す。

    判定ロジック: セグメントの中点が含まれる話者区間のラベルを採用。
    どの区間にも入らない場合は "UNKNOWN" を返す。

    Args:
        seg_start: whisper セグメントの開始秒。
        seg_end: whisper セグメントの終了秒。
        speaker_segments: 話者分離の結果。

    Returns:
        話者ラベル文字列（例 "SPEAKER_00"）。
    """
    midpoint = (seg_start + seg_end) / 2.0
    for start, end, speaker in speaker_segments:
        if start <= midpoint <= end:
            return speaker
    return "UNKNOWN"


def write_transcript(
    output_path: Path,
    segments: list[dict[str, Any]],
    speaker_segments: list[tuple[float, float, str]] | None,
    source_audio: Path,
) -> None:
    """文字起こし結果をテキストファイルとして書き出す。

    フォーマット例:
        # 文字起こし結果
        # source: data/input/meeting_001.wav
        # generated_at: 2026-05-13 10:30:00

        [00:00:00 - 00:00:05] SPEAKER_00: 本日の議題は...

    Args:
        output_path: 出力ファイルのパス。
        segments: whisper のセグメントリスト。
        speaker_segments: 話者分離結果。None なら話者ラベル無しで出力。
        source_audio: 元音声のパス（メタ情報として記録）。
    """
    lines: list[str] = [
        "# 文字起こし結果",
        f"# source: {source_audio}",
        f"# generated_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for seg in segments:
        ts = f"[{format_timestamp(seg['start'])} - {format_timestamp(seg['end'])}]"
        if speaker_segments:
            speaker = assign_speaker_to_segment(
                seg["start"], seg["end"], speaker_segments
            )
            lines.append(f"{ts} {speaker}: {seg['text']}")
        else:
            lines.append(f"{ts} {seg['text']}")

    # --- アトミック書き込み ---
    # pipeline.py が Ctrl+C / taskkill で停止された場合、中途半端な .txt が
    # data/transcripts/ に残ると、次回処理で find_latest_transcript が
    # 不完全ファイルを掴むリスクがある。
    # そこで *.tmp に全文を書き終わってから os.replace で本ファイルに昇格させる。
    # 起動時クリーンアップ（pipeline.py main 冒頭）で残った *.tmp は全削除される。
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp_path, output_path)
    except Exception:
        # 失敗時は中途半端な tmp を残さない（起動時クリーンアップでも消えるが念のため）
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    print(f"[output] 保存しました: {output_path}")


def build_output_path(audio_path: Path, transcripts_dir: Path) -> Path:
    """出力ファイル名を生成する。

    例: meeting_001.wav → meeting_001_2026-05-13_1030.txt
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return transcripts_dir / f"{audio_path.stem}_{timestamp}.txt"


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(
        description="音声ファイルを文字起こししてテキスト保存する"
    )
    parser.add_argument(
        "audio", type=str, help="入力音声ファイルのパス（.wav/.mp3/.m4a など）"
    )
    args = parser.parse_args()

    audio_path = Path(args.audio).resolve()
    if not audio_path.exists():
        print(f"[error] 音声ファイルが見つかりません: {audio_path}", file=sys.stderr)
        return 1

    settings = load_settings()
    whisper_cfg = settings["whisper"]
    diarization_cfg = settings["diarization"]
    transcripts_dir = resolve_path(settings["paths"]["transcripts_dir"])
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    # Step A: 文字起こし
    segments = transcribe_audio(audio_path, whisper_cfg)
    if not segments:
        print("[warn] 文字起こし結果が空でした。無音ファイルの可能性があります。")
        return 2

    # Step B: 話者分離（設定で OFF にも切替可能）
    speaker_segments: list[tuple[float, float, str]] | None = None
    if diarization_cfg.get("enabled", False):
        hf_token = get_huggingface_token()
        if not hf_token:
            print(
                "[warn] HUGGINGFACE_TOKEN が未設定のため話者分離をスキップします。\n"
                "       .env を作成してトークンを設定してください（.env.example 参照）。"
            )
        else:
            try:
                speaker_segments = diarize_audio(
                    audio_path, diarization_cfg, hf_token
                )
            except Exception as e:
                # 話者分離は失敗しても文字起こし結果は保存したいので握りつぶす
                print(f"[warn] 話者分離に失敗したためスキップ: {e}")

    # Step C: 出力
    output_path = build_output_path(audio_path, transcripts_dir)
    write_transcript(output_path, segments, speaker_segments, audio_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
