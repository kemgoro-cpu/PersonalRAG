"""test_retry_tracker.py
retry_tracker.py の単体テスト。

テスト対象の関数:
    - load_retry_state: ファイル読み込み（正常・ファイル不在・壊れた JSON）
    - save_retry_state: アトミック書き込み
    - increment_retry_count: カウント加算（新規・既存エントリ）
    - clear_retry_count: カウントクリア（存在するエントリ・存在しないエントリ）
    - append_failed_history: 失敗履歴の追記
    - is_quarantined: 隔離判定（failed 含む・含まない）

実行方法:
    cd C:/Users/kemgo/Documents/Program/PersonalRAG
    .venv/Scripts/python.exe -m pytest tests/test_retry_tracker.py -v
"""

import json
import sys
from pathlib import Path

import pytest

# scripts/ を Python のモジュール検索パスに追加（retry_tracker を import するため）
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from retry_tracker import (
    load_retry_state,
    save_retry_state,
    increment_retry_count,
    clear_retry_count,
    append_failed_history,
    is_quarantined,
)

import logging

# テスト用のロガー（コンソールに出力するだけ）
test_logger = logging.getLogger("test_retry_tracker")
test_logger.setLevel(logging.DEBUG)


# ============================================================
# フィクスチャ
# ============================================================

@pytest.fixture
def tmp_retry_file(tmp_path: Path) -> Path:
    """テスト用の一時 retry_count.json パスを返す（ファイルはまだ存在しない）。"""
    return tmp_path / "retry_count.json"


@pytest.fixture
def tmp_failed_log(tmp_path: Path) -> Path:
    """テスト用の一時 failed_files.json パスを返す（ファイルはまだ存在しない）。"""
    return tmp_path / "failed_files.json"


# ============================================================
# load_retry_state のテスト
# ============================================================

class TestLoadRetryState:
    """load_retry_state のテストクループ。"""

    def test_file_not_exist_returns_empty(self, tmp_path: Path) -> None:
        """ファイルが存在しない場合は空 dict を返す。"""
        path = tmp_path / "nonexistent.json"
        result = load_retry_state(path)
        assert result == {}

    def test_broken_json_returns_empty(self, tmp_path: Path) -> None:
        """壊れた JSON ファイルがあっても空 dict を返す（例外を投げない）。"""
        path = tmp_path / "broken.json"
        path.write_text("{ this is not valid json }", encoding="utf-8")
        result = load_retry_state(path)
        assert result == {}

    def test_json_array_returns_empty(self, tmp_path: Path) -> None:
        """JSON がリスト（配列）形式の場合も空 dict を返す（型保証）。"""
        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = load_retry_state(path)
        assert result == {}

    def test_valid_json_returns_content(self, tmp_path: Path) -> None:
        """有効な JSON dict ファイルは内容をそのまま返す。"""
        path = tmp_path / "valid.json"
        data = {
            "rec_xxx.wav": {
                "count": 2,
                "last_error": "transcribe 失敗",
                "first_failed_at": "2026-05-15T14:00:00+09:00",
                "last_failed_at": "2026-05-15T14:30:00+09:00",
            }
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        result = load_retry_state(path)
        assert result == data


# ============================================================
# increment_retry_count のテスト
# ============================================================

class TestIncrementRetryCount:
    """increment_retry_count のテストグループ。"""

    def test_new_entry_starts_at_one(self, tmp_retry_file: Path) -> None:
        """新規エントリのカウントは 1 から始まる。"""
        count = increment_retry_count(tmp_retry_file, "file.wav", "transcribe 失敗", test_logger)
        assert count == 1

        state = load_retry_state(tmp_retry_file)
        assert state["file.wav"]["count"] == 1
        assert state["file.wav"]["last_error"] == "transcribe 失敗"
        assert "first_failed_at" in state["file.wav"]

    def test_existing_entry_increments(self, tmp_retry_file: Path) -> None:
        """既存エントリのカウントが正しく +1 される。"""
        # 2 回呼ぶと 2 になる
        increment_retry_count(tmp_retry_file, "file.wav", "transcribe 失敗", test_logger)
        count = increment_retry_count(tmp_retry_file, "file.wav", "transcribe 失敗", test_logger)
        assert count == 2

    def test_first_failed_at_not_changed_on_second_call(self, tmp_retry_file: Path) -> None:
        """2 回目以降の失敗で first_failed_at が変わらないことを確認。"""
        increment_retry_count(tmp_retry_file, "file.wav", "err1", test_logger)
        state_first = load_retry_state(tmp_retry_file)
        first_time = state_first["file.wav"]["first_failed_at"]

        increment_retry_count(tmp_retry_file, "file.wav", "err2", test_logger)
        state_second = load_retry_state(tmp_retry_file)
        assert state_second["file.wav"]["first_failed_at"] == first_time

    def test_last_error_updates(self, tmp_retry_file: Path) -> None:
        """last_error が最後のエラー内容で更新されることを確認。"""
        increment_retry_count(tmp_retry_file, "file.wav", "err1", test_logger)
        increment_retry_count(tmp_retry_file, "file.wav", "err2", test_logger)
        state = load_retry_state(tmp_retry_file)
        assert state["file.wav"]["last_error"] == "err2"

    def test_multiple_files_independent(self, tmp_retry_file: Path) -> None:
        """複数ファイルのカウントが独立していることを確認。"""
        increment_retry_count(tmp_retry_file, "file_a.wav", "err", test_logger)
        increment_retry_count(tmp_retry_file, "file_a.wav", "err", test_logger)
        increment_retry_count(tmp_retry_file, "file_b.wav", "err", test_logger)
        state = load_retry_state(tmp_retry_file)
        assert state["file_a.wav"]["count"] == 2
        assert state["file_b.wav"]["count"] == 1


# ============================================================
# clear_retry_count のテスト
# ============================================================

class TestClearRetryCount:
    """clear_retry_count のテストグループ。"""

    def test_existing_entry_is_removed(self, tmp_retry_file: Path) -> None:
        """既存エントリが削除されることを確認（べき等）。"""
        increment_retry_count(tmp_retry_file, "file.wav", "err", test_logger)
        clear_retry_count(tmp_retry_file, "file.wav", test_logger)
        state = load_retry_state(tmp_retry_file)
        assert "file.wav" not in state

    def test_nonexistent_entry_does_not_raise(self, tmp_retry_file: Path) -> None:
        """存在しないエントリを削除しようとしても例外が出ない。"""
        # 何も起こらないことを確認（例外なし）
        clear_retry_count(tmp_retry_file, "nonexistent.wav", test_logger)

    def test_other_entries_preserved(self, tmp_retry_file: Path) -> None:
        """対象エントリを削除しても他のエントリが残ることを確認。"""
        increment_retry_count(tmp_retry_file, "file_a.wav", "err", test_logger)
        increment_retry_count(tmp_retry_file, "file_b.wav", "err", test_logger)
        clear_retry_count(tmp_retry_file, "file_a.wav", test_logger)
        state = load_retry_state(tmp_retry_file)
        assert "file_a.wav" not in state
        assert "file_b.wav" in state


# ============================================================
# append_failed_history のテスト
# ============================================================

class TestAppendFailedHistory:
    """append_failed_history のテストグループ。"""

    def test_creates_file_and_appends(self, tmp_failed_log: Path) -> None:
        """ファイルが存在しない場合は新規作成して追記する。"""
        entry = {
            "file": "rec_xxx.wav",
            "errors": ["transcribe 失敗"] * 3,
            "first_failed_at": "2026-05-15T14:00:00+09:00",
            "moved_at": "2026-05-15T14:35:00+09:00",
            "moved_to": "data/input/failed/rec_xxx.wav",
        }
        append_failed_history(tmp_failed_log, entry, test_logger)

        data = json.loads(tmp_failed_log.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["file"] == "rec_xxx.wav"

    def test_appends_to_existing(self, tmp_failed_log: Path) -> None:
        """既存ファイルにエントリが追記されることを確認。"""
        entry1 = {"file": "file1.wav", "errors": [], "first_failed_at": "", "moved_at": "", "moved_to": ""}
        entry2 = {"file": "file2.wav", "errors": [], "first_failed_at": "", "moved_at": "", "moved_to": ""}
        append_failed_history(tmp_failed_log, entry1, test_logger)
        append_failed_history(tmp_failed_log, entry2, test_logger)

        data = json.loads(tmp_failed_log.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["file"] == "file1.wav"
        assert data[1]["file"] == "file2.wav"

    def test_broken_json_is_overwritten(self, tmp_failed_log: Path) -> None:
        """壊れた JSON があった場合は上書きして新規エントリを追記する（データロスより継続を優先）。"""
        tmp_failed_log.write_text("not a valid json", encoding="utf-8")
        entry = {"file": "file.wav", "errors": [], "first_failed_at": "", "moved_at": "", "moved_to": ""}
        append_failed_history(tmp_failed_log, entry, test_logger)

        data = json.loads(tmp_failed_log.read_text(encoding="utf-8"))
        assert len(data) == 1


# ============================================================
# is_quarantined のテスト
# ============================================================

class TestIsQuarantined:
    """is_quarantined のテストグループ。"""

    def test_path_with_failed_returns_true(self) -> None:
        """path.parts に "failed" が含まれる場合は True。"""
        path = Path("data/input/failed/rec_xxx.wav")
        assert is_quarantined(path) is True

    def test_absolute_path_with_failed_returns_true(self) -> None:
        """絶対パスでも "failed" が含まれれば True。"""
        path = Path("C:/Users/kemgo/Documents/PersonalRAG/data/input/failed/rec.wav")
        assert is_quarantined(path) is True

    def test_path_without_failed_returns_false(self) -> None:
        """path.parts に "failed" が含まれない場合は False。"""
        path = Path("data/input/rec_xxx.wav")
        assert is_quarantined(path) is False

    def test_processed_path_returns_false(self) -> None:
        """processed/ 配下は "failed" を含まないので False。"""
        path = Path("data/input/processed/rec_xxx.wav")
        assert is_quarantined(path) is False

    def test_filename_with_failed_but_not_in_dir_returns_false(self) -> None:
        """ファイル名に 'failed' という文字列が含まれていても、ディレクトリパーツにない場合は False。

        例: "failed_recording.wav" は隔離されていない。
        is_quarantined は path.parts に "failed" というコンポーネントが含まれるかを見るため、
        ファイル名の一部である場合とは区別される。
        """
        # 注意: Path("data/input/failed_recording.wav").parts には
        # "failed_recording.wav" が含まれ、"failed" は含まれない
        path = Path("data/input/failed_recording.wav")
        assert is_quarantined(path) is False
