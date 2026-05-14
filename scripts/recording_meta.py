"""recording_meta.py
録音前メモの管理ロジック（サニタイズ・履歴・meta.json 保存）。

record_gui.py から GUI に依存しない純粋な関数だけを切り出したモジュール。
こうすることで record_gui.py の import 時に tkinter / sounddevice / win_hotkey 等の
GUI 系ライブラリを初期化しなくても、これらの関数だけをテストできる。

record_gui.py はこのモジュールを import して使う。
test_phase_b.py はこのモジュールを直接 import してテストする。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

# ロガー（呼び出し元モジュール名ではなく、このモジュール名でログを出す）
logger = logging.getLogger(__name__)

# --- タイトル履歴ファイルのパス ---
# PROJECT_ROOT は config_loader から取得する
from config_loader import PROJECT_ROOT

HISTORY_FILE: Path = PROJECT_ROOT / "data" / ".gui_history.json"
# 履歴に保持する件数（最新 5 件のみ）
HISTORY_MAX = 5

# Windows で使えないファイル名文字のパターン（バックスラッシュ・スラッシュ等）
_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
# Windows 予約デバイス名（大文字・小文字を無視して比較する）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
# ファイル名に使えるタイトルの最大文字数（Unicode 文字単位）
_TITLE_MAX_LEN = 50


def sanitize_title(title: str) -> str:
    """タイトル文字列を Windows ファイル名として安全な形に変換する。

    変換ルール:
        - 使用不可文字（\\/:*?"<>| および制御文字）→ _ に置換
        - 末尾の . と空白を除去（Windows の仕様）
        - Windows 予約デバイス名（CON/PRN 等）と完全一致する場合は末尾に _ を追加
        - 50 文字で切る（Unicode 文字単位）

    Args:
        title: ユーザーが入力したタイトル文字列。

    Returns:
        サニタイズ済みの文字列。空になった場合は空文字列を返す。
    """
    # 不正文字を _ に置換
    sanitized = _INVALID_CHARS_RE.sub("_", title)
    # 末尾の . と空白を除去（Windows の仕様上、ファイル名末尾の . は無視される）
    sanitized = sanitized.rstrip(". ")
    # 50 文字に切る（Unicode 文字単位）
    sanitized = sanitized[:_TITLE_MAX_LEN]
    # 末尾の . と空白を再度除去（切り詰めた結果が . で終わる場合があるため）
    sanitized = sanitized.rstrip(". ")
    # Windows 予約名と完全一致する場合は末尾に _ を付けて予約名でなくする
    if sanitized.upper() in _WINDOWS_RESERVED:
        sanitized = sanitized + "_"
    return sanitized


def load_title_history() -> list[str]:
    """タイトル履歴を HISTORY_FILE から読み込む。

    ファイルが存在しない場合や JSON が壊れている場合は空リストを返す。

    Returns:
        タイトルのリスト（最新が先頭、最大 HISTORY_MAX 件）。
    """
    try:
        if not HISTORY_FILE.exists():
            return []
        text = HISTORY_FILE.read_text(encoding="utf-8")
        data = json.loads(text)
        titles = data.get("titles", [])
        # 型安全のため str のリストに限定する
        return [str(t) for t in titles if isinstance(t, str)]
    except Exception as exc:
        # JSON 壊れ・権限エラーなどは警告ログだけ出してフォールバック
        logger.warning(f"タイトル履歴の読み込みに失敗しました（空リストで続行）: {exc}")
        return []


def save_title_history(titles: list[str]) -> None:
    """タイトル履歴を HISTORY_FILE に保存する。

    保存に失敗しても呼び出し元を止めない（警告ログだけ出す）。

    Args:
        titles: 保存するタイトルのリスト（最大 HISTORY_MAX 件）。
    """
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"titles": titles[:HISTORY_MAX]}
        HISTORY_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"タイトル履歴の保存に失敗しました: {exc}")


def add_title_to_history(new_title: str, history: list[str]) -> list[str]:
    """タイトルを履歴リストの先頭に追加し、重複を除去して最大件数に揃えて返す。

    同じタイトルが既にリスト内にある場合は、既存のものを削除してから先頭に追加する
    （= 最近使った順に並ぶ）。

    Args:
        new_title: 追加するタイトル文字列（空文字列の場合は追加しない）。
        history: 既存の履歴リスト。

    Returns:
        更新後の履歴リスト（最大 HISTORY_MAX 件）。
    """
    if not new_title.strip():
        # 空文字列・空白のみは履歴に追加しない
        return history
    # 既存の同名エントリを削除してから先頭に追加する
    updated = [t for t in history if t != new_title]
    updated.insert(0, new_title)
    return updated[:HISTORY_MAX]


def save_meta_json(wav_path: Path, title: str, participants: str, topic: str) -> None:
    """WAV ファイルと同じ場所にサイドカーメタ JSON を保存する。

    保存パスは `{wav_stem}.meta.json`（例: rec_2026-05-15_143022_打ち合わせ.meta.json）。
    保存に失敗しても録音処理は止めない（警告ログだけ出す）。

    スキーマ:
        {
            "title": "...",
            "participants": "...",
            "topic": "...",
            "recorded_at": "ISO8601 形式の文字列"
        }

    Args:
        wav_path: 録音した WAV ファイルのパス。
        title: タイトル（空文字列可）。
        participants: 参加者（空文字列可）。
        topic: テーマ（空文字列可）。
    """
    meta_path = wav_path.parent / (wav_path.stem + ".meta.json")
    payload = {
        "title": title,
        "participants": participants,
        "topic": topic,
        # ISO8601 形式（タイムゾーン付き）で記録
        "recorded_at": datetime.now().astimezone().isoformat(),
    }
    try:
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"meta.json 保存: {meta_path.name}")
    except Exception as exc:
        logger.warning(f"meta.json の保存に失敗しました: {exc}")
