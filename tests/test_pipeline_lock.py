"""pipeline lock の回帰テスト。

同時起動で pipeline.py が複数本残ると、GUI の状態表示やファイル処理が
実態とズレる。lock file は原子的に取得し、稼働中 PID があれば拒否する。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pipeline import acquire_lock, release_lock


def test_acquire_lock_writes_current_pid(tmp_path: Path) -> None:
    """lock file がない場合、自プロセス PID を書いて取得できる。"""
    lock_file = tmp_path / "pipeline.lock"
    logger = logging.getLogger("test_pipeline_lock")

    assert acquire_lock(lock_file, logger) is True
    assert lock_file.read_text(encoding="utf-8") == str(os.getpid())

    release_lock(lock_file, logger)
    assert not lock_file.exists()


def test_acquire_lock_rejects_running_pid(tmp_path: Path) -> None:
    """生存中 PID の lock file があれば多重起動を拒否する。"""
    lock_file = tmp_path / "pipeline.lock"
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    logger = logging.getLogger("test_pipeline_lock")

    assert acquire_lock(lock_file, logger) is False
    assert lock_file.read_text(encoding="utf-8") == str(os.getpid())


def test_acquire_lock_replaces_stale_pid(tmp_path: Path) -> None:
    """終了済み扱いの PID が残っている場合は lock を作り直す。"""
    lock_file = tmp_path / "pipeline.lock"
    lock_file.write_text("99999999", encoding="utf-8")
    logger = logging.getLogger("test_pipeline_lock")

    assert acquire_lock(lock_file, logger) is True
    assert lock_file.read_text(encoding="utf-8") == str(os.getpid())


def test_release_lock_keeps_other_owner_lock(tmp_path: Path) -> None:
    """別 PID が所有している lock file は削除しない。"""
    lock_file = tmp_path / "pipeline.lock"
    lock_file.write_text("99999999", encoding="utf-8")
    logger = logging.getLogger("test_pipeline_lock")

    release_lock(lock_file, logger)

    assert lock_file.exists()
    assert lock_file.read_text(encoding="utf-8") == "99999999"
