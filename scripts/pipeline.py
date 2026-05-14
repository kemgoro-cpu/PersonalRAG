"""pipeline.py
Step 4: data/input/ フォルダを監視し、新規音声ファイルを検知したら自動で
transcribe → summarize → ingest_db を順次実行するスクリプト。

使い方:
    python scripts/pipeline.py
    → 起動するとフォルダ監視を開始。Ctrl+C で停止。

設計の肝（VRAM 競合回避）:
    各 Step を subprocess.run() で別プロセスとして起動することで、
    Python プロセス終了時に GPU メモリが完全解放される。
    これにより whisper（Step 1）と Gemma（Step 2）の VRAM 衝突を回避する。

注意:
    Open WebUI でチャット中は、Gemma を奪い合って OOM になるため
    本スクリプトを停止しておくこと（README に運用ルールとして記載）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config_loader import PROJECT_ROOT, load_settings, resolve_path
from notify import notify


def setup_logger(log_dir: Path) -> logging.Logger:
    """ログ出力を設定する。コンソールとファイルの両方に出す。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"

    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.INFO)
    # 二重登録防止
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def _now_iso() -> str:
    """現在時刻を ISO8601 文字列で返す（タイムゾーン付き）。"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def write_state(
    state_file: Path,
    current: dict[str, Any] | None,
    queue: list[str],
    recent: list[dict[str, Any]],
    logger: logging.Logger,
) -> None:
    """パイプラインの処理状態を JSON ファイルに書き出す。

    書き込みはアトミック（一時ファイルへ書いて os.replace でリネーム）にして、
    読み手（record_gui.py）と競合しても壊れた JSON が見えないようにする。
    書き込み失敗はログ警告だけ出して pipeline 本処理は止めない。

    Args:
        state_file: 書き出し先のパス（例: data/logs/pipeline_state.json）。
        current: 現在処理中のエントリ。何も処理していなければ None。
                 キー: "file", "step", "started_at"
        queue: これから処理待ちのファイル名リスト。
        recent: 最近の処理結果リスト（最新 20 件）。
                キー: "file", "result"("success"|"failed"), "finished_at",
                      "note_path"(success 時のみ), "error"(failed 時のみ)
        logger: ロガー。
    """
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        # recent をメモリ上でも 20 件に切り詰める（無限増加を防ぐ）
        # この関数はすべての append 後に呼ばれるため、ここで 1 回処理すれば十分
        if len(recent) > 20:
            del recent[:-20]
        payload = {
            "updated_at": _now_iso(),
            "current": current,
            "queue": queue,
            # recent は最新 20 件だけ保持（古いものは捨てる）
            "recent": recent,
        }
        # 一時ファイルに書いてからリネーム（アトミック書き込み）
        tmp_file = state_file.with_suffix(".json.tmp")
        tmp_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_file, state_file)
    except Exception as e:
        # ディスクフル・権限エラー等は warning だけ出して続行
        logger.warning(f"状態ファイルの書き込み失敗（処理は継続します）: {e}")


def wait_until_stable(path: Path, wait_seconds: int, logger: logging.Logger) -> bool:
    """ファイルが書き込み中でないことを確認する（サイズが安定するまで待つ）。

    録音アプリが書き込み中のファイルを誤って処理しないためのガード。

    Args:
        path: 対象ファイル。
        wait_seconds: 何秒間サイズが変わらなければ安定とみなすか。
        logger: ロガー。

    Returns:
        True なら安定確認 OK、False なら一定回数試してもダメ。
    """
    last_size = -1
    stable_count = 0
    for _ in range(60):  # 最大 60 回（最大約 60 * wait_seconds 秒待つ）
        if not path.exists():
            return False
        try:
            current_size = path.stat().st_size
        except OSError:
            time.sleep(wait_seconds)
            continue

        if current_size == last_size and current_size > 0:
            stable_count += 1
            if stable_count >= 1:
                return True
        else:
            stable_count = 0
        last_size = current_size
        time.sleep(wait_seconds)

    logger.warning(f"ファイルサイズが安定せずタイムアウト: {path.name}")
    return False


def run_step(
    script_name: str, args: list[str], logger: logging.Logger
) -> bool:
    """指定スクリプトを別プロセスで実行する。

    Args:
        script_name: scripts/ 配下のファイル名（例 "transcribe.py"）。
        args: スクリプトに渡す引数のリスト。
        logger: ロガー。

    Returns:
        正常終了なら True、失敗なら False。
    """
    script_path = PROJECT_ROOT / "scripts" / script_name
    cmd = [sys.executable, str(script_path), *args]
    logger.info(f"実行: {' '.join(cmd)}")
    try:
        # check=False にしてリターンコードで判定（例外発生で全体停止を避ける）
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            return True
        logger.error(f"{script_name} が異常終了 (returncode={result.returncode})")
        return False
    except Exception as e:
        logger.error(f"{script_name} の起動に失敗: {e}")
        return False


def find_latest_transcript(transcripts_dir: Path, audio_stem: str) -> Path | None:
    """直近で生成された transcript を audio stem 基準で特定する。

    transcribe.py は出力名を `<audio_stem>_<YYYY-MM-DD_HHMM>.txt` にしているため、
    `<audio_stem>_*.txt` で最も新しいファイルを返す。
    """
    candidates = sorted(
        transcripts_dir.glob(f"{audio_stem}_*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_latest_note(notes_dir: Path, transcript_stem: str) -> Path | None:
    """transcript から生成された note を特定する。

    summarize.py は `<transcript_stem>.md` を出力するため一意に決まる。
    """
    candidate = notes_dir / f"{transcript_stem}.md"
    return candidate if candidate.exists() else None


def process_audio(
    audio_path: Path,
    settings: dict[str, Any],
    logger: logging.Logger,
    state_file: Path | None = None,
    queue: list[str] | None = None,
    recent: list[dict[str, Any]] | None = None,
) -> None:
    """1 つの音声ファイルに対して Step 1 → 2 → 3 を順次実行する。

    Args:
        audio_path: 処理対象の音声ファイル。
        settings: load_settings() で読み込んだ設定辞書。
        logger: ロガー。
        state_file: パイプライン状態ファイルの書き出し先。None なら状態ファイルを書かない。
        queue: 処理待ちファイル名のリスト（状態ファイルに記録するため渡す）。
        recent: 最近の処理結果リスト（状態ファイルに記録するため渡す）。
    """
    # デフォルト値（呼び出し元から渡されない場合）
    if queue is None:
        queue = []
    if recent is None:
        recent = []

    logger.info(f"=== 処理開始: {audio_path.name} ===")

    # --- 状態ファイル: 処理開始（transcribe ステップ）を記録 ---
    started_at = _now_iso()
    if state_file is not None:
        write_state(
            state_file,
            current={"file": audio_path.name, "step": "transcribe", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    transcripts_dir = resolve_path(settings["paths"]["transcripts_dir"])
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    processed_dir = resolve_path(settings["paths"]["processed_dir"])
    processed_dir.mkdir(parents=True, exist_ok=True)

    # 設定から通知オプションを取得（デフォルトは有効）
    notify_on_success: bool = settings.get("pipeline", {}).get("notify_on_success", True)

    # 書き込み中ファイルでないことを確認
    if not wait_until_stable(
        audio_path, settings["pipeline"]["stable_wait_seconds"], logger
    ):
        logger.warning(f"スキップ: {audio_path.name}")
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "ファイルが安定しないためスキップ",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ stable_wait 失敗: {audio_path.name}", "warning")
        return

    # Step 1: 文字起こし
    if not run_step("transcribe.py", [str(audio_path)], logger):
        logger.error("Step 1 失敗のため後続をスキップ")
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "transcribe 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ transcribe 失敗: {audio_path.name}", "error")
        return

    transcript_path = find_latest_transcript(transcripts_dir, audio_path.stem)
    if not transcript_path:
        logger.error(
            f"transcript が見つかりません（audio_stem={audio_path.stem}）。後続をスキップ"
        )
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "transcript ファイルが見つからない",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ transcript なし: {audio_path.name}", "error")
        return
    logger.info(f"transcript: {transcript_path.name}")

    # --- 状態ファイル: summarize ステップへ切替 ---
    if state_file is not None:
        write_state(
            state_file,
            current={"file": audio_path.name, "step": "summarize", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    # Step 2: 要約
    if not run_step("summarize.py", [str(transcript_path)], logger):
        logger.error("Step 2 失敗のため Step 3 をスキップ")
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "summarize 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ summarize 失敗: {audio_path.name}", "error")
        return

    note_path = find_latest_note(notes_dir, transcript_path.stem)
    if not note_path:
        logger.error("note が見つかりません。Step 3 をスキップ")
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "note ファイルが見つからない",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ note なし: {audio_path.name}", "error")
        return
    logger.info(f"note: {note_path.name}")

    # --- 状態ファイル: ingest ステップへ切替 ---
    if state_file is not None:
        write_state(
            state_file,
            current={"file": audio_path.name, "step": "ingest", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    # Step 3: ChromaDB 投入
    if not run_step("ingest_db.py", [str(note_path)], logger):
        logger.error("Step 3 失敗")
        if state_file is not None:
            recent.append({
                "file": audio_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "ingest_db 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ ingest_db 失敗: {audio_path.name}", "error")
        return

    # Step 5: Open WebUI Knowledge への自動同期（任意・失敗しても続行）
    # この同期は WebUI が起動していない場合でも pipeline を止めない設計にしている。
    # sync に失敗した分は後から「python scripts/sync_webui.py」で回収できる。
    if settings.get("openwebui", {}).get("enabled", False):
        if not run_step("sync_webui.py", [str(note_path)], logger):
            logger.warning(
                f"Open WebUI 同期失敗（後で sync_webui.py で回収可能）: {note_path.name}"
            )

    # 処理済み音声を退避（重複処理防止）
    try:
        dest = processed_dir / audio_path.name
        # 既に同名ファイルがあれば上書きせず連番にする
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            for n in range(1, 999):
                alt = processed_dir / f"{stem}_{n:03d}{suffix}"
                if not alt.exists():
                    dest = alt
                    break
        shutil.move(str(audio_path), str(dest))
        logger.info(f"退避: {audio_path.name} → {dest.relative_to(PROJECT_ROOT)}")
    except Exception as e:
        logger.error(f"退避失敗: {e}")

    # --- 状態ファイル: 処理完了を記録 ---
    if state_file is not None:
        recent.append({
            "file": audio_path.name,
            "result": "success",
            "finished_at": _now_iso(),
            "note_path": str(note_path),
        })
        write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)

    # 成功時のトースト通知（設定で notify_on_success: false にすると抑制できる）
    if notify_on_success:
        notify("PersonalRAG", f"✓ 要約完了: {audio_path.name}", "info")

    logger.info(f"=== 処理完了: {audio_path.name} ===\n")


class AudioFileHandler(FileSystemEventHandler):
    """data/input/ に新規音声ファイルが投入されたら処理する watchdog ハンドラ。"""

    def __init__(
        self,
        settings: dict[str, Any],
        logger: logging.Logger,
        state_file: Path | None = None,
        recent: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.logger = logger
        self.state_file = state_file
        # recent リストはメイン関数から渡してもらい、audio/text 両ハンドラで共有する
        self.recent: list[dict[str, Any]] = recent if recent is not None else []
        self.watch_extensions: set[str] = {
            ext.lower() for ext in settings["pipeline"]["watch_extensions"]
        }
        # 既に処理キューに入れたパスを記録（重複イベント抑止）
        self._processing: set[str] = set()

    def on_created(self, event: Any) -> None:
        self._handle(event)

    def on_moved(self, event: Any) -> None:
        # ドラッグ&ドロップだと「一時ファイル作成→リネーム」になるケースがある
        self._handle(event, use_dest=True)

    def _handle(self, event: Any, use_dest: bool = False) -> None:
        if event.is_directory:
            return
        path_str = event.dest_path if use_dest else event.src_path
        path = Path(path_str)

        # processed/ サブフォルダの変動は無視
        if "processed" in path.parts:
            return

        if path.suffix.lower() not in self.watch_extensions:
            return

        if path_str in self._processing:
            return
        self._processing.add(path_str)
        try:
            # queue には「自分以外の処理中パス」を渡す。
            # watchdog はイベントを直列処理するため、実際の待ち行列は取得できない。
            # ここで渡す値は「同時に _handle が再帰的に呼ばれた場合の並行ファイル」であり、
            # 正確な待ち行列ではない（将来課題: スレッドセーフな queue 管理に移行）。
            queue: list[str] = [
                Path(p).name for p in self._processing if p != path_str
            ]
            process_audio(
                path, self.settings, self.logger,
                state_file=self.state_file,
                queue=queue,
                recent=self.recent,
            )
        finally:
            self._processing.discard(path_str)


def process_text(
    text_path: Path,
    settings: dict[str, Any],
    logger: logging.Logger,
    state_file: Path | None = None,
    queue: list[str] | None = None,
    recent: list[dict[str, Any]] | None = None,
) -> None:
    """1 つのテキストファイルに対して Step 1-alt → 2 → 3 を順次実行する。

    process_audio と同じ構造で、Step 1 だけ import_transcript.py に置き換えている。
    これにより音声フローとテキストフローを完全に分離でき、リグレッションリスクを
    最小化できる。

    Args:
        text_path: 処理対象のテキストファイル（.txt / .vtt / .docx / .md）。
        settings: load_settings() で読み込んだ設定辞書。
        logger: ロガー。
        state_file: パイプライン状態ファイルの書き出し先。None なら状態ファイルを書かない。
        queue: 処理待ちファイル名のリスト（状態ファイルに記録するため渡す）。
        recent: 最近の処理結果リスト（状態ファイルに記録するため渡す）。
    """
    # デフォルト値（呼び出し元から渡されない場合）
    if queue is None:
        queue = []
    if recent is None:
        recent = []

    logger.info(f"=== テキスト処理開始: {text_path.name} ===")

    # --- 状態ファイル: 処理開始（import_transcript ステップ）を記録 ---
    started_at = _now_iso()
    if state_file is not None:
        write_state(
            state_file,
            current={"file": text_path.name, "step": "transcribe", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    transcripts_dir = resolve_path(settings["paths"]["transcripts_dir"])
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    processed_text_dir = resolve_path(settings["paths"]["processed_text_dir"])
    processed_text_dir.mkdir(parents=True, exist_ok=True)

    # 設定から通知オプションを取得（デフォルトは有効）
    notify_on_success: bool = settings.get("pipeline", {}).get("notify_on_success", True)

    # 書き込み中ファイルでないことを確認（コピー中のファイルを誤処理しないため）
    if not wait_until_stable(
        text_path, settings["pipeline"]["stable_wait_seconds"], logger
    ):
        logger.warning(f"スキップ: {text_path.name}")
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "ファイルが安定しないためスキップ",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ stable_wait 失敗: {text_path.name}", "warning")
        return

    # Step 1-alt: テキスト → 正規化 transcript
    if not run_step("import_transcript.py", [str(text_path)], logger):
        logger.error("Step 1-alt 失敗のため後続をスキップ")
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "import_transcript 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ import_transcript 失敗: {text_path.name}", "error")
        return

    # import_transcript.py も transcribe.py と同じ命名規則で出力するため、
    # 同じ find_latest_transcript 関数で探せる
    transcript_path = find_latest_transcript(transcripts_dir, text_path.stem)
    if not transcript_path:
        logger.error(
            f"transcript が見つかりません（text_stem={text_path.stem}）。後続をスキップ"
        )
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "transcript ファイルが見つからない",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ transcript なし: {text_path.name}", "error")
        return
    logger.info(f"transcript: {transcript_path.name}")

    # --- 状態ファイル: summarize ステップへ切替 ---
    if state_file is not None:
        write_state(
            state_file,
            current={"file": text_path.name, "step": "summarize", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    # Step 2: 要約
    if not run_step("summarize.py", [str(transcript_path)], logger):
        logger.error("Step 2 失敗のため Step 3 をスキップ")
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "summarize 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ summarize 失敗: {text_path.name}", "error")
        return

    note_path = find_latest_note(notes_dir, transcript_path.stem)
    if not note_path:
        logger.error("note が見つかりません。Step 3 をスキップ")
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "note ファイルが見つからない",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ note なし: {text_path.name}", "error")
        return
    logger.info(f"note: {note_path.name}")

    # --- 状態ファイル: ingest ステップへ切替 ---
    if state_file is not None:
        write_state(
            state_file,
            current={"file": text_path.name, "step": "ingest", "started_at": started_at},
            queue=queue,
            recent=recent,
            logger=logger,
        )

    # Step 3: ChromaDB 投入
    if not run_step("ingest_db.py", [str(note_path)], logger):
        logger.error("Step 3 失敗")
        if state_file is not None:
            recent.append({
                "file": text_path.name,
                "result": "failed",
                "finished_at": _now_iso(),
                "error": "ingest_db 失敗",
            })
            write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)
        notify("PersonalRAG", f"✗ ingest_db 失敗: {text_path.name}", "error")
        return

    # Step 5: Open WebUI Knowledge への自動同期（任意・失敗しても続行）
    # 音声フローと同じ設計。WebUI が停止中でも pipeline は完走する。
    if settings.get("openwebui", {}).get("enabled", False):
        if not run_step("sync_webui.py", [str(note_path)], logger):
            logger.warning(
                f"Open WebUI 同期失敗（後で sync_webui.py で回収可能）: {note_path.name}"
            )

    # 処理済みテキストを退避（重複処理防止）
    try:
        dest = processed_text_dir / text_path.name
        # 同名ファイルがあれば連番を付けて上書き防止
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            for n in range(1, 999):
                alt = processed_text_dir / f"{stem}_{n:03d}{suffix}"
                if not alt.exists():
                    dest = alt
                    break
        shutil.move(str(text_path), str(dest))
        logger.info(f"退避: {text_path.name} → {dest.relative_to(PROJECT_ROOT)}")
    except Exception as e:
        logger.error(f"退避失敗: {e}")

    # --- 状態ファイル: 処理完了を記録 ---
    if state_file is not None:
        recent.append({
            "file": text_path.name,
            "result": "success",
            "finished_at": _now_iso(),
            "note_path": str(note_path),
        })
        write_state(state_file, current=None, queue=queue, recent=recent, logger=logger)

    # 成功時のトースト通知（設定で notify_on_success: false にすると抑制できる）
    if notify_on_success:
        notify("PersonalRAG", f"✓ 要約完了: {text_path.name}", "info")

    logger.info(f"=== テキスト処理完了: {text_path.name} ===\n")


class TextFileHandler(FileSystemEventHandler):
    """data/input_text/ に新規テキストファイルが投入されたら処理する watchdog ハンドラ。

    AudioFileHandler と同じパターンで実装しており、
    監視対象の拡張子と呼び出す処理関数だけが異なる。
    """

    def __init__(
        self,
        settings: dict[str, Any],
        logger: logging.Logger,
        state_file: Path | None = None,
        recent: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.logger = logger
        self.state_file = state_file
        # recent リストはメイン関数から渡してもらい、audio/text 両ハンドラで共有する
        self.recent: list[dict[str, Any]] = recent if recent is not None else []
        self.text_extensions: set[str] = {
            ext.lower() for ext in settings["pipeline"]["text_extensions"]
        }
        # 既に処理キューに入れたパスを記録（重複イベント抑止）
        self._processing: set[str] = set()

    def on_created(self, event: Any) -> None:
        self._handle(event)

    def on_moved(self, event: Any) -> None:
        # ドラッグ&ドロップだと「一時ファイル作成→リネーム」になるケースがある
        self._handle(event, use_dest=True)

    def _handle(self, event: Any, use_dest: bool = False) -> None:
        if event.is_directory:
            return
        path_str = event.dest_path if use_dest else event.src_path
        path = Path(path_str)

        # processed/ サブフォルダの変動は無視
        if "processed" in path.parts:
            return

        if path.suffix.lower() not in self.text_extensions:
            return

        if path_str in self._processing:
            return
        self._processing.add(path_str)
        try:
            # queue には「自分以外の処理中パス」を渡す。
            # watchdog は直列処理のため正確な待ち行列は取得できない（将来課題）。
            queue: list[str] = [
                Path(p).name for p in self._processing if p != path_str
            ]
            process_text(
                path, self.settings, self.logger,
                state_file=self.state_file,
                queue=queue,
                recent=self.recent,
            )
        finally:
            self._processing.discard(path_str)


def process_existing_files(
    input_dir: Path,
    settings: dict[str, Any],
    logger: logging.Logger,
    extensions_key: str = "watch_extensions",
    state_file: Path | None = None,
    recent: list[dict[str, Any]] | None = None,
) -> None:
    """起動時、既に input/ にあるファイル（processed/ 除く）を順次処理する。

    音声・テキスト両方で使えるよう extensions_key を引数にして汎化している。
    デフォルトは音声用の "watch_extensions"。テキスト用は "text_extensions" を渡す。

    Args:
        input_dir: 監視対象ディレクトリ。
        settings: 設定辞書。
        logger: ロガー。
        extensions_key: settings["pipeline"] の中の、対象拡張子リストのキー名。
        state_file: パイプライン状態ファイルの書き出し先。
        recent: 最近の処理結果リスト（ハンドラと共有して累積させる）。
    """
    if recent is None:
        recent = []

    extensions = {ext.lower() for ext in settings["pipeline"][extensions_key]}
    candidates = [
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    ]
    if not candidates:
        return

    # 処理の種類を判断するために extensions_key を使う
    is_text_mode = extensions_key == "text_extensions"
    mode_label = "テキスト" if is_text_mode else "音声"

    logger.info(f"起動時の未処理{mode_label}ファイル {len(candidates)} 件を処理します。")
    for i, path in enumerate(candidates):
        # 残りの未処理ファイルを queue として渡す
        queue = [p.name for p in candidates[i + 1:]]
        if is_text_mode:
            process_text(path, settings, logger,
                         state_file=state_file, queue=queue, recent=recent)
        else:
            process_audio(path, settings, logger,
                          state_file=state_file, queue=queue, recent=recent)


def main() -> int:
    """エントリポイント。"""
    settings = load_settings()
    input_dir = resolve_path(settings["paths"]["input_dir"])
    log_dir = resolve_path(settings["paths"]["logs_dir"])
    input_dir.mkdir(parents=True, exist_ok=True)

    # テキスト取り込み用ディレクトリを作成する（無ければ自動生成）
    text_input_dir = resolve_path(settings["paths"]["input_text_dir"])
    text_input_dir.mkdir(parents=True, exist_ok=True)

    logger = setup_logger(log_dir)
    logger.info(f"音声フォルダ監視を開始: {input_dir}")
    logger.info(f"テキスト監視も開始: {text_input_dir}")
    logger.info("Ctrl+C で終了します。")

    # --- パイプライン状態ファイルの設定 ---
    # settings に state_file がなければデフォルトパスを使う
    state_file_rel = settings.get("pipeline", {}).get(
        "state_file", "data/logs/pipeline_state.json"
    )
    state_file = resolve_path(state_file_rel)
    logger.info(f"状態ファイル: {state_file}")

    # recent リストは音声・テキスト両ハンドラで共有して累積させる
    recent: list[dict[str, Any]] = []

    # 起動直後に「待機中」状態を書き出しておく
    write_state(state_file, current=None, queue=[], recent=recent, logger=logger)

    # 起動時のキャッチアップ処理（音声）
    process_existing_files(
        input_dir, settings, logger,
        extensions_key="watch_extensions",
        state_file=state_file, recent=recent,
    )

    # 起動時のキャッチアップ処理（テキスト）
    process_existing_files(
        text_input_dir, settings, logger,
        extensions_key="text_extensions",
        state_file=state_file, recent=recent,
    )

    audio_handler = AudioFileHandler(settings, logger, state_file=state_file, recent=recent)
    text_handler = TextFileHandler(settings, logger, state_file=state_file, recent=recent)

    observer = Observer()
    # 音声フォルダの監視（既存フロー）
    observer.schedule(audio_handler, str(input_dir), recursive=False)
    # テキストフォルダの監視（新規フロー）
    observer.schedule(text_handler, str(text_input_dir), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("停止シグナルを受信、終了します...")
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
