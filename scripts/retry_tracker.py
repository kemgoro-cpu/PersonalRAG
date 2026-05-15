"""retry_tracker.py
リトライ回数管理と失敗履歴の永続化ユーティリティ。

pipeline.py から呼び出して、処理失敗ファイルのリトライ回数を追跡し、
上限到達時に data/input/failed/ への隔離と failed_files.json への記録を行う。

設計ポイント:
- retry_count.json  : ファイル単位のリトライ回数（一時的な状態）
- failed_files.json : 隔離済みファイルの永続的な失敗履歴（監査ログ）
- すべての書き込みはアトミック（tmp + os.replace）
- ファイル不在・壊れた JSON は空として扱いフォールバック
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# 内部ユーティリティ
# ============================================================

def _now_iso() -> str:
    """現在時刻を ISO8601 文字列で返す（タイムゾーン付き）。"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _atomic_write(
    target: Path,
    data: Any,
    logger: logging.Logger,
    max_retries: int = 5,
) -> bool:
    """JSON データを target にアトミック書き込みする。

    一時ファイルに書いてから os.replace でリネームするため、
    途中でクラッシュしても target が壊れた状態にならない。
    Windows の WinError 32（ファイルロック）をリトライで吸収する。

    Args:
        target:      書き込み先のパス。
        data:        JSON シリアライズ可能なデータ。
        logger:      ロガー。
        max_retries: リトライ上限（デフォルト 5 回）。

    Returns:
        成功なら True。失敗なら False（警告ログ出力済み）。
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"アトミック書き込み準備失敗 ({target.name}): {e}")
        return False

    # os.replace でリネーム（WinError 32 をリトライで吸収）
    for attempt in range(1, max_retries + 1):
        try:
            os.replace(tmp, target)
            return True
        except PermissionError as e:
            logger.warning(f"アトミック書き込みリトライ {attempt}/{max_retries}: {e}")
            time.sleep(0.1)
        except OSError as e:
            if "[WinError 32]" in str(e) or getattr(e, "winerror", None) == 32:
                logger.warning(f"アトミック書き込みリトライ {attempt}/{max_retries}: {e}")
                time.sleep(0.1)
            else:
                logger.warning(f"アトミック書き込み失敗（OSError）: {e}")
                return False

    logger.warning(
        f"アトミック書き込みを {max_retries} 回試みたが失敗しました: {target.name}"
    )
    return False


# ============================================================
# 公開 API
# ============================================================

def load_retry_state(retry_count_file: Path) -> dict[str, dict]:
    """retry_count.json を読み込んで返す。

    ファイルが存在しない・壊れている場合は空の dict を返す（例外を投げない）。

    Args:
        retry_count_file: retry_count.json のパス。

    Returns:
        ファイル名 → {count, last_error, first_failed_at, last_failed_at} の dict。
        例: {"rec_xxx.wav": {"count": 2, "last_error": "transcribe 失敗", ...}}
    """
    if not retry_count_file.exists():
        return {}
    try:
        text = retry_count_file.read_text(encoding="utf-8")
        data = json.loads(text)
        # 辞書であることを保証（壊れた JSON だと list になる場合もある）
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        # JSON パース失敗・権限エラー等はすべて空として扱う
        return {}


def save_retry_state(
    retry_count_file: Path,
    state: dict,
    logger: logging.Logger,
) -> None:
    """retry_count.json を上書き保存する。

    Args:
        retry_count_file: 書き込み先のパス。
        state:            load_retry_state() で取得した dict（更新済み）。
        logger:           ロガー。
    """
    _atomic_write(retry_count_file, state, logger)


def increment_retry_count(
    retry_count_file: Path,
    file_name: str,
    error: str,
    logger: logging.Logger,
) -> int:
    """指定ファイルのリトライ回数を +1 して保存し、更新後の count を返す。

    エントリが存在しない場合は新規作成（count=1 から開始）。
    first_failed_at は最初の失敗時だけ記録し、2 回目以降は変えない。

    Args:
        retry_count_file: retry_count.json のパス。
        file_name:        対象ファイル名（例: "rec_xxx.wav"）。
        error:            エラー内容（例: "transcribe 失敗"）。
        logger:           ロガー。

    Returns:
        更新後のリトライ回数 (int)。失敗しても 1 以上の値を返す。
    """
    state = load_retry_state(retry_count_file)
    now = _now_iso()

    if file_name in state:
        # 既存エントリを更新
        entry = state[file_name]
        entry["count"] = entry.get("count", 0) + 1
        entry["last_error"] = error
        entry["last_failed_at"] = now
        # first_failed_at は変更しない
    else:
        # 新規エントリを作成
        state[file_name] = {
            "count": 1,
            "last_error": error,
            "first_failed_at": now,
            "last_failed_at": now,
        }

    count = state[file_name]["count"]
    save_retry_state(retry_count_file, state, logger)
    logger.info(
        f"リトライカウント更新: {file_name} → {count} 回目 (エラー: {error})"
    )
    return count


def clear_retry_count(
    retry_count_file: Path,
    file_name: str,
    logger: logging.Logger,
) -> None:
    """処理成功時に retry_count.json から該当エントリを削除する。

    エントリが存在しない場合は何もしない（べき等）。

    Args:
        retry_count_file: retry_count.json のパス。
        file_name:        対象ファイル名。
        logger:           ロガー。
    """
    state = load_retry_state(retry_count_file)
    if file_name in state:
        del state[file_name]
        save_retry_state(retry_count_file, state, logger)
        logger.info(f"リトライカウントをクリア（処理成功）: {file_name}")


def append_failed_history(
    failed_files_log: Path,
    entry: dict,
    logger: logging.Logger,
) -> None:
    """failed_files.json に失敗エントリを追記する。

    failed_files.json のスキーマ（リスト形式）:
    [
      {
        "file": "rec_xxx.wav",
        "errors": ["transcribe 失敗", "transcribe 失敗", "transcribe 失敗"],
        "first_failed_at": "...",
        "moved_at": "...",
        "moved_to": "data/input/failed/rec_xxx.wav"
      },
      ...
    ]

    Args:
        failed_files_log: failed_files.json のパス。
        entry:            追記するエントリ dict。
        logger:           ロガー。
    """
    # 既存の失敗履歴を読み込む（ファイルがなければ空リスト）
    history: list[dict] = []
    if failed_files_log.exists():
        try:
            text = failed_files_log.read_text(encoding="utf-8")
            parsed = json.loads(text)
            if isinstance(parsed, list):
                history = parsed
        except Exception:
            # 壊れた JSON は空として扱い、上書きする（データロスよりも追記継続を優先）
            logger.warning(
                f"failed_files.json の読み込みに失敗（空として上書き）: {failed_files_log.name}"
            )

    history.append(entry)
    _atomic_write(failed_files_log, history, logger)
    logger.info(f"失敗履歴に追記: {entry.get('file', '?')} → {failed_files_log.name}")


def is_quarantined(file_path: Path) -> bool:
    """ファイルが failed/ サブフォルダ配下にあるか確認する。

    path.parts に "failed" が含まれていれば隔離済みと判定する。
    watchdog の _handle() で failed/ 配下のイベントをスキップするために使う。

    Args:
        file_path: 確認するファイルのパス。

    Returns:
        隔離済みなら True、そうでなければ False。
    """
    return "failed" in file_path.parts
