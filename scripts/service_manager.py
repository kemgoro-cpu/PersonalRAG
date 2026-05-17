"""service_manager.py
フェーズ C: サービス管理モジュール。

Ollama / Pipeline / Open WebUI の 3 サービスについて、
状態検知・起動・停止のロジックを GUI から切り離して集約する。

このモジュールは GUI に依存しないため、単体テストが可能。
"""

from __future__ import annotations

import ctypes
import enum
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Windows の subprocess フラグ: 新しいプロセスグループを作成する。
# GUI 終了時の taskkill が孫プロセスへ過剰に広がらないよう独立化する。
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# Windows のプロセス生存確認で使う定数。
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    """Windows で補助コマンドのコンソール窓を出さないための設定を返す。"""
    if os.name != "nt":
        return None

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return startupinfo


def _hidden_creationflags(extra_flags: int = 0) -> int:
    """Windows で補助コマンドのコンソール窓を抑止する creationflags を返す。"""
    if os.name != "nt":
        return extra_flags
    return extra_flags | CREATE_NO_WINDOW


class ServiceStatus(enum.Enum):
    """サービスの状態を表す列挙型。"""

    RUNNING = "running"   # 稼働中
    STOPPED = "stopped"   # 停止中
    UNKNOWN = "unknown"   # 判定不可（通常は STOPPED 扱い）


@dataclass
class ServiceInfo:
    """1 つのサービスの状態情報。"""

    name: str           # "Ollama" / "Pipeline" / "Open WebUI"
    status: ServiceStatus
    detail: str         # "稼働中" / "停止中" / "応答なし" 等の人間可読テキスト
    pid: int | None = field(default=None)  # ServiceManager が起動した場合のみセット


class ServiceManager:
    """3 つのサービス（Ollama / Pipeline / Open WebUI）を管理するクラス。

    状態検知:
        - 各 check_xxx() メソッドが HTTP または状態ファイルで確認する
        - ネットワーク呼び出しはすべて例外を握りつぶして STOPPED を返す
          （GUI ループを止めないため）

    起動:
        - subprocess.Popen で各サービスを起動し、Popen オブジェクトを self._processes に保存
        - 起動した Popen は self._processes に保存する
        - PID ではなく Popen を保持することで、PID 再利用による誤 kill を防ぐ

    停止:
        - 管理中 Popen または検出済み外部 PID を taskkill /PID <pid> /T /F で停止
        - 既に終了していた場合は停止済みとして _processes から除去する
    """

    def __init__(self, project_root: Path, settings: dict[str, Any]) -> None:
        """
        Args:
            project_root: プロジェクトのルートディレクトリ（config_loader.PROJECT_ROOT）。
                          各 venv へのパス解決に使う。
            settings: load_settings() で読み込んだ設定辞書。
        """
        self.project_root = project_root
        self.settings = settings

        # 自分が起動したサービスの Popen オブジェクトを管理する辞書
        # キー: サービス名 ("Ollama" / "Pipeline" / "Open WebUI")
        # 値: subprocess.Popen オブジェクト
        # PID（int）ではなく Popen を保持することで、PID 再利用による誤 kill を防ぐ
        # （Popen.poll() で「自分が起動したプロセスがまだ生きているか」を直接確認できる）
        self._processes: dict[str, subprocess.Popen] = {}

        # _processes への同時アクセスを防ぐスレッドロック
        self._lock = threading.Lock()
        # Open WebUI 起動後の notes 自動同期を多重起動しないためのフラグ
        self._webui_sync_in_progress = False
        self._notes_auto_sync_thread: threading.Thread | None = None
        self._notes_auto_sync_stop = threading.Event()
        self._notes_file_states: dict[str, tuple[int, int, float]] = {}
        self._notes_pending_paths: set[str] = set()
        self._notes_poll_interval_seconds = 5.0
        self._notes_stable_seconds = 5.0

        # パイプライン状態ファイルのパス（Pipeline の稼働判定に使う）
        state_file_rel: str = settings.get("pipeline", {}).get(
            "state_file", "data/logs/pipeline_state.json"
        )
        # resolve_path を使わず直接解決（循環 import 回避のため）
        state_path = Path(state_file_rel)
        if state_path.is_absolute():
            self._pipeline_state_file = state_path
        else:
            self._pipeline_state_file = project_root / state_file_rel
        self._logs_dir = self._pipeline_state_file.parent

        # pipeline.py が作成する lock file。状態ファイルだけではなく、
        # lock file 内の PID が生存しているかも確認して誤表示を防ぐ。
        lock_file_rel: str = settings.get("pipeline", {}).get(
            "lock_file", "data/logs/pipeline.lock"
        )
        lock_path = Path(lock_file_rel)
        if lock_path.is_absolute():
            self._pipeline_lock_file = lock_path
        else:
            self._pipeline_lock_file = project_root / lock_file_rel

        # Ollama の URL（settings の llm.host から取得、デフォルト localhost:11434）
        self._ollama_base_url: str = settings.get("llm", {}).get(
            "host", "http://localhost:11434"
        ).rstrip("/")

        # Open WebUI の URL（settings の openwebui.base_url から取得）
        self._webui_base_url: str = settings.get("openwebui", {}).get(
            "base_url", "http://localhost:3000"
        ).rstrip("/")

        notes_dir_rel: str = settings.get("paths", {}).get("notes_dir", "data/notes")
        notes_path = Path(notes_dir_rel)
        if notes_path.is_absolute():
            self._notes_dir = notes_path
        else:
            self._notes_dir = project_root / notes_dir_rel

    # ------------------------------------------------------------------
    # 状態検知
    # ------------------------------------------------------------------

    @staticmethod
    def _is_pid_running(pid: int) -> bool:
        """PID のプロセスが生存しているか確認する。

        Windows では ctypes で OpenProcess/GetExitCodeProcess を呼ぶ。
        追加依存を増やさず、tasklist より軽く GUI の定期ポーリングに使える。
        """
        if pid <= 0:
            return False

        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False

        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def _read_pipeline_lock_pid(self) -> int | None:
        """pipeline lock file から PID を読む。読めない場合は None を返す。"""
        try:
            text = self._pipeline_lock_file.read_text(encoding="utf-8").strip()
            return int(text)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _port_from_url(url: str) -> int | None:
        """URL からポート番号を取得する。取得できない場合は None。"""
        parsed = urlparse(url)
        if parsed.port is not None:
            return parsed.port
        if parsed.scheme == "http":
            return 80
        if parsed.scheme == "https":
            return 443
        return None

    @staticmethod
    def _find_listening_pids(port: int) -> list[int]:
        """指定 TCP ポートを LISTENING している PID を netstat から取得する。"""
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                creationflags=_hidden_creationflags(),
                startupinfo=_hidden_startupinfo(),
            )
        except Exception:
            return []

        pids: set[int] = set()
        suffix = f":{port}"
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if "LISTENING" not in line.upper():
                continue

            parts = line.split()
            if len(parts) < 5:
                continue

            local_address = parts[1]
            pid_text = parts[-1]
            if not local_address.endswith(suffix):
                continue
            try:
                pids.add(int(pid_text))
            except ValueError:
                continue

        return sorted(pid for pid in pids if ServiceManager._is_pid_running(pid))

    def _find_project_script_pids(self, script_name: str) -> list[int]:
        """このプロジェクト配下の指定スクリプトを実行中の PID を探す。"""
        if os.name != "nt":
            return []

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        "Get-CimInstance Win32_Process | "
                        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                creationflags=_hidden_creationflags(),
                startupinfo=_hidden_startupinfo(),
            )
        except Exception:
            return []

        if result.returncode != 0 or not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        rows = data if isinstance(data, list) else [data]
        project_text = str(self.project_root).lower()
        script_text = script_name.lower()
        pids: set[int] = set()

        for row in rows:
            if not isinstance(row, dict):
                continue
            command_line = str(row.get("CommandLine") or "").lower()
            if project_text not in command_line or script_text not in command_line:
                continue
            try:
                pid = int(row.get("ProcessId"))
            except (TypeError, ValueError):
                continue
            if self._is_pid_running(pid):
                pids.add(pid)

        return sorted(pids)

    def _detect_external_pids(self, name: str) -> list[int]:
        """外部起動サービスの停止対象 PID を検出する。"""
        pids: set[int] = set()

        if name == "Pipeline":
            lock_pid = self._read_pipeline_lock_pid()
            if lock_pid is not None and self._is_pid_running(lock_pid):
                pids.add(lock_pid)
            pids.update(self._find_project_script_pids("pipeline.py"))
        elif name == "Ollama":
            port = self._port_from_url(self._ollama_base_url)
            if port is not None:
                pids.update(self._find_listening_pids(port))
        elif name == "Open WebUI":
            port = self._port_from_url(self._webui_base_url)
            if port is not None:
                pids.update(self._find_listening_pids(port))

        return sorted(pid for pid in pids if self._is_pid_running(pid))

    def check_ollama(self) -> ServiceInfo:
        """Ollama の稼働状態を HTTP で確認する。

        GET http://localhost:11434/api/tags が 200 なら RUNNING。
        タイムアウト（2 秒）・接続失敗・例外はすべて STOPPED 扱い。
        """
        try:
            import requests
            resp = requests.get(f"{self._ollama_base_url}/api/tags", timeout=2.0)
            if resp.status_code == 200:
                with self._lock:
                    proc = self._processes.get("Ollama")
                    pid = proc.pid if proc is not None else None
                if pid is None:
                    port = self._port_from_url(self._ollama_base_url)
                    pids = self._find_listening_pids(port) if port is not None else []
                    pid = pids[0] if pids else None
                    detail = f"稼働中（外部起動 PID={pid}）" if pid else "稼働中"
                else:
                    detail = f"稼働中（PID={pid}）"
                return ServiceInfo(name="Ollama", status=ServiceStatus.RUNNING,
                                   detail=detail, pid=pid)
        except Exception:
            pass
        return ServiceInfo(name="Ollama", status=ServiceStatus.STOPPED, detail="停止中")

    def check_pipeline(self) -> ServiceInfo:
        """Pipeline の稼働状態を状態ファイルと lock PID で確認する。

        pipeline_state.json の updated_at が新しく、かつこの GUI が起動した
        Popen または pipeline.lock の PID が生存している場合のみ RUNNING。
        状態ファイルだけが新しい状態を誤って「外部起動」と表示しないため。
        """
        try:
            if not self._pipeline_state_file.exists():
                return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED,
                                   detail="停止中（状態ファイルなし）")

            import json
            text = self._pipeline_state_file.read_text(encoding="utf-8")
            data = json.loads(text)

            updated_at_str: str = data.get("updated_at", "")
            if not updated_at_str:
                return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED,
                                   detail="停止中（更新時刻不明）")

            updated_at = datetime.fromisoformat(updated_at_str)
            # タイムゾーン情報がなければ UTC とみなす
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            diff_seconds = (now - updated_at).total_seconds()

            if diff_seconds > 30:
                # 状態ファイルは存在するが古い → 停止しているとみなす
                return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED,
                                   detail=f"停止中（最終更新 {int(diff_seconds)}s 前）")

            with self._lock:
                proc = self._processes.get("Pipeline")
                if proc is not None and proc.poll() is None:
                    return ServiceInfo(name="Pipeline", status=ServiceStatus.RUNNING,
                                       detail="稼働中", pid=proc.pid)
                if proc is not None and proc.poll() is not None:
                    self._processes.pop("Pipeline", None)

            lock_pid = self._read_pipeline_lock_pid()
            if lock_pid is not None and self._is_pid_running(lock_pid):
                return ServiceInfo(name="Pipeline", status=ServiceStatus.RUNNING,
                                   detail=f"稼働中（外部起動 PID={lock_pid}）",
                                   pid=lock_pid)

            return ServiceInfo(
                name="Pipeline",
                status=ServiceStatus.STOPPED,
                detail="停止中（状態ファイルは新しいが lock PID 無効）",
            )

        except Exception as exc:
            logger.debug(f"Pipeline 状態チェック例外: {exc}")
        return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED, detail="停止中")

    def check_open_webui(self) -> ServiceInfo:
        """Open WebUI の稼働状態を HTTP で確認する。

        GET /health が 200 なら RUNNING。
        /health が存在しないバージョン（404）では GET / でフォールバックし、
        200 が返れば RUNNING とみなす。
        GUI から起動したプロセスが HTTP 待受前なら UNKNOWN（起動中）として扱う。
        """
        managed_pid: int | None = None
        managed_exit_code: int | None = None
        with self._lock:
            proc = self._processes.get("Open WebUI")
            if proc is not None:
                managed_exit_code = proc.poll()
                if managed_exit_code is None:
                    managed_pid = proc.pid
                else:
                    self._processes.pop("Open WebUI", None)

        try:
            import requests
            # まず /health を試す
            try:
                resp = requests.get(f"{self._webui_base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    if managed_pid is None:
                        port = self._port_from_url(self._webui_base_url)
                        pids = self._find_listening_pids(port) if port is not None else []
                        pid = pids[0] if pids else None
                        detail = f"稼働中（外部起動 PID={pid}）" if pid else "稼働中"
                    else:
                        pid = managed_pid
                        detail = f"稼働中（PID={pid}）"
                    return ServiceInfo(name="Open WebUI", status=ServiceStatus.RUNNING,
                                       detail=detail, pid=pid)
                if resp.status_code == 404:
                    # /health がないバージョン → / でフォールバック
                    resp2 = requests.get(f"{self._webui_base_url}/", timeout=2.0)
                    if resp2.status_code == 200:
                        if managed_pid is None:
                            port = self._port_from_url(self._webui_base_url)
                            pids = self._find_listening_pids(port) if port is not None else []
                            pid = pids[0] if pids else None
                            detail = f"稼働中（外部起動 PID={pid}）" if pid else "稼働中"
                        else:
                            pid = managed_pid
                            detail = f"稼働中（PID={pid}）"
                        return ServiceInfo(name="Open WebUI", status=ServiceStatus.RUNNING,
                                           detail=detail, pid=pid)
            except requests.exceptions.ConnectionError:
                pass  # 下の STOPPED へ落ちる
        except Exception as exc:
            logger.debug(f"Open WebUI 状態チェック例外: {exc}")

        if managed_pid is not None:
            return ServiceInfo(
                name="Open WebUI",
                status=ServiceStatus.UNKNOWN,
                detail=f"起動中（PID={managed_pid}、HTTP 応答待ち）",
                pid=managed_pid,
            )

        port = self._port_from_url(self._webui_base_url)
        pids = self._find_listening_pids(port) if port is not None else []
        if pids:
            pid = pids[0]
            return ServiceInfo(
                name="Open WebUI",
                status=ServiceStatus.UNKNOWN,
                detail=f"起動中（外部起動 PID={pid}、HTTP 応答待ち）",
                pid=pid,
            )

        if managed_exit_code is not None:
            return ServiceInfo(
                name="Open WebUI",
                status=ServiceStatus.STOPPED,
                detail=f"停止中（終了コード={managed_exit_code}）",
            )
        return ServiceInfo(name="Open WebUI", status=ServiceStatus.STOPPED, detail="停止中")

    def check_all(self) -> list[ServiceInfo]:
        """3 サービスをすべてチェックして結果リストを返す。

        Returns:
            [Ollama の ServiceInfo, Pipeline の ServiceInfo, Open WebUI の ServiceInfo]
        """
        return [
            self.check_ollama(),
            self.check_pipeline(),
            self.check_open_webui(),
        ]

    def _queue_open_webui_pending_sync(self) -> bool:
        """Open WebUI 起動後に未同期 notes の Knowledge 同期を 1 回だけ予約する。"""
        webui_cfg = self.settings.get("openwebui", {})
        if not webui_cfg.get("enabled", False) or not webui_cfg.get("knowledge_id", ""):
            return False

        with self._lock:
            if self._webui_sync_in_progress:
                return False
            self._webui_sync_in_progress = True

        thread = threading.Thread(
            target=self._run_open_webui_pending_sync_when_ready,
            daemon=True,
            name="open-webui-pending-sync",
        )
        thread.start()
        return True

    def _run_open_webui_pending_sync_when_ready(self) -> None:
        """Open WebUI の HTTP 応答を待ってから sync_webui.py をバックグラウンド実行する。"""
        try:
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                if self.check_open_webui().status == ServiceStatus.RUNNING:
                    break
                time.sleep(5.0)
            else:
                logger.warning("Open WebUI 起動後の Knowledge 同期をスキップ: HTTP 応答待ちタイムアウト")
                return

            python = self.project_root / ".venv" / "Scripts" / "python.exe"
            sync_script = self.project_root / "scripts" / "sync_webui.py"
            if not python.exists() or not sync_script.exists():
                logger.warning(
                    f"Open WebUI Knowledge 同期をスキップ: python={python.exists()}, "
                    f"sync_webui.py={sync_script.exists()}"
                )
                return

            self._logs_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = self._logs_dir / "sync_webui_stdout.log"
            stderr_path = self._logs_dir / "sync_webui_stderr.log"
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                proc = subprocess.Popen(
                    [str(python), str(sync_script)],
                    cwd=str(self.project_root),
                    creationflags=_hidden_creationflags(CREATE_NEW_PROCESS_GROUP),
                    startupinfo=_hidden_startupinfo(),
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            returncode = proc.wait()
            if returncode == 0:
                logger.info("Open WebUI 起動後の Knowledge 同期が完了しました")
            else:
                logger.warning(
                    f"Open WebUI 起動後の Knowledge 同期に失敗しました "
                    f"(returncode={returncode}, stderr={stderr_path})"
                )
        finally:
            with self._lock:
                self._webui_sync_in_progress = False

    def start_notes_auto_sync(self) -> bool:
        """GUI 起動中に data/notes の新規・更新 .md を自動同期する監視を開始する。"""
        webui_cfg = self.settings.get("openwebui", {})
        if not webui_cfg.get("enabled", False) or not webui_cfg.get("knowledge_id", ""):
            return False

        with self._lock:
            if self._notes_auto_sync_thread is not None and self._notes_auto_sync_thread.is_alive():
                return False
            self._notes_auto_sync_stop.clear()
            self._notes_file_states = self._collect_notes_file_states(time.monotonic())
            self._notes_pending_paths.clear()
            self._notes_auto_sync_thread = threading.Thread(
                target=self._notes_auto_sync_loop,
                daemon=True,
                name="notes-auto-sync",
            )
            self._notes_auto_sync_thread.start()

        logger.info(f"notes 自動同期監視を開始: {self._notes_dir}")
        if self.check_open_webui().status == ServiceStatus.RUNNING:
            self._queue_open_webui_pending_sync()
        return True

    def stop_notes_auto_sync(self) -> None:
        """notes 自動同期監視スレッドを停止する。"""
        self._notes_auto_sync_stop.set()
        thread = self._notes_auto_sync_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _collect_notes_file_states(self, now: float) -> dict[str, tuple[int, int, float]]:
        """現在の .md ファイル状態を収集する。値は (size, mtime_ns, changed_at)。"""
        states: dict[str, tuple[int, int, float]] = {}
        if not self._notes_dir.exists():
            return states

        for md_path in self._notes_dir.rglob("*.md"):
            try:
                stat = md_path.stat()
                key = str(md_path.resolve())
            except OSError:
                continue
            states[key] = (stat.st_size, stat.st_mtime_ns, now)
        return states

    def _scan_notes_for_stable_changes(self, now: float) -> bool:
        """新規・更新 .md が安定したら True を返す。削除は同期対象にしない。"""
        current = self._collect_notes_file_states(now)

        for key in set(self._notes_file_states) - set(current):
            self._notes_file_states.pop(key, None)
            self._notes_pending_paths.discard(key)

        for key, (size, mtime_ns, _changed_at) in current.items():
            previous = self._notes_file_states.get(key)
            if previous is None or previous[0] != size or previous[1] != mtime_ns:
                self._notes_file_states[key] = (size, mtime_ns, now)
                self._notes_pending_paths.add(key)

        stable_paths = [
            key
            for key in self._notes_pending_paths
            if key in self._notes_file_states
            and now - self._notes_file_states[key][2] >= self._notes_stable_seconds
        ]
        if not stable_paths:
            return False

        return True

    def _clear_stable_notes_pending(self, now: float) -> None:
        """同期を予約できた変更だけ pending から外す。Open WebUI 停止中は保留する。"""
        stable_paths = [
            key
            for key in self._notes_pending_paths
            if key in self._notes_file_states
            and now - self._notes_file_states[key][2] >= self._notes_stable_seconds
        ]
        for key in stable_paths:
            self._notes_pending_paths.discard(key)

    def _notes_auto_sync_loop(self) -> None:
        """data/notes を軽くポーリングし、安定した変更を Knowledge 同期へ渡す。"""
        while not self._notes_auto_sync_stop.wait(self._notes_poll_interval_seconds):
            try:
                if not self._scan_notes_for_stable_changes(time.monotonic()):
                    continue
                if self.check_open_webui().status != ServiceStatus.RUNNING:
                    logger.info("notes 変更を検知しましたが Open WebUI 停止中のため同期を保留します")
                    continue
                if self._queue_open_webui_pending_sync():
                    self._clear_stable_notes_pending(time.monotonic())
            except Exception as exc:
                logger.warning(f"notes 自動同期監視中に例外: {exc}")

    # ------------------------------------------------------------------
    # 起動
    # ------------------------------------------------------------------

    def start_ollama(self) -> tuple[bool, str]:
        """Ollama サービスを起動する。

        既に外部で常駐していてポートが使用中の場合は「警告のみ・成功扱い」とする。
        Popen オブジェクトを self._processes["Ollama"] に保存する（PID ではなく
        Popen を保持することで PID 再利用による誤 kill を防ぐ）。

        Popen 直後に即死した場合（外部 Ollama とのポート衝突が典型）は
        _processes には保存せず、外部プロセス扱いとして「成功扱い」で返す。

        Returns:
            (成功フラグ, メッセージ)
        """
        try:
            proc = subprocess.Popen(
                ["ollama", "serve"],
                creationflags=_hidden_creationflags(CREATE_NEW_PROCESS_GROUP),
                startupinfo=_hidden_startupinfo(),
                # 標準出力/エラーは捨てる（GUI を巻き込まないため）
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"Ollama 起動要求: PID={proc.pid}")

            # 0.5 秒待って即死していないか確認する
            # （"port already in use" で即終了する場合の典型的な検知）
            time.sleep(0.5)
            if proc.poll() is not None:
                # 即死 → 外部 Ollama が動いているとみなし、_processes には保存しない
                logger.info(f"Ollama PID={proc.pid} が即終了: 外部起動と判断")
                return True, "Ollama はすでに外部プロセスとして起動しています（このGUIでは管理しません）"

            # 正常起動 → Popen を保存
            with self._lock:
                self._processes["Ollama"] = proc

            # 残り 1 秒待ってから起動確認（起動直後は状態が安定しない）
            time.sleep(1.0)
            info = self.check_ollama()
            if info.status == ServiceStatus.RUNNING:
                return True, f"Ollama を起動しました（PID={proc.pid}）"
            # 起動に時間がかかることがある → 楽観的に成功扱い
            return True, f"Ollama の起動を要求しました（PID={proc.pid}、初期化中）"

        except FileNotFoundError:
            return False, "ollama コマンドが見つかりません。Ollama をインストールしてください。"
        except Exception as exc:
            # "port already in use" 等の場合も起動試行は「成功」とみなす
            err_str = str(exc).lower()
            if "address" in err_str or "port" in err_str or "bind" in err_str:
                return True, "Ollama はすでに起動しています（外部プロセスとして稼働中）"
            logger.warning(f"Ollama 起動例外: {exc}")
            return False, f"Ollama の起動に失敗しました: {exc}"

    def start_pipeline(self) -> tuple[bool, str]:
        """Pipeline（pipeline.py）をバックグラウンドで起動する。

        pythonw.exe を使うことでコンソールウィンドウを出さずに起動する。
        Popen オブジェクトを self._processes["Pipeline"] に保存する。

        多重起動防止:
            Popen する前に check_pipeline() で稼働中かどうか確認する。
            既に RUNNING なら新規 Popen をせずに「稼働中」メッセージを返す。
            これにより、ボタン連打でプロセスが 7 個並列起動する事故を防ぐ。
            pipeline.py 自身の lock file 防止（Bug 1 修正）と 2 重に防御する。

        Returns:
            (成功フラグ, メッセージ)
        """
        pythonw = self.project_root / ".venv" / "Scripts" / "pythonw.exe"
        pipeline_script = self.project_root / "scripts" / "pipeline.py"

        if not pythonw.exists():
            return False, f"pythonw.exe が見つかりません: {pythonw}\n.venv を作成してください。"
        if not pipeline_script.exists():
            return False, f"pipeline.py が見つかりません: {pipeline_script}"

        # --- 既存稼働チェック（Popen する前に確認） ---
        # state.json の updated_at が 30 秒以内なら既に別プロセスで動いているとみなす。
        # Whisper モデルロード直後は heartbeat がまだ走っていないため、
        # 自分が起動した Popen が生きているかも合わせて確認する。
        current_info = self.check_pipeline()
        if current_info.status == ServiceStatus.RUNNING:
            logger.info("Pipeline は既に稼働中のため新規起動をスキップします")
            return True, "Pipeline は既に稼働中です"

        # 自分が既に Popen を保持していて、そのプロセスがまだ生きている場合も重複起動しない
        with self._lock:
            existing_proc = self._processes.get("Pipeline")
        if existing_proc is not None and existing_proc.poll() is None:
            logger.info(
                f"Pipeline Popen が既に存在します (PID={existing_proc.pid})。新規起動をスキップします"
            )
            return True, f"Pipeline は既に起動中です（PID={existing_proc.pid}）"

        try:
            proc = subprocess.Popen(
                [str(pythonw), str(pipeline_script)],
                creationflags=_hidden_creationflags(CREATE_NEW_PROCESS_GROUP),
                startupinfo=_hidden_startupinfo(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                self._processes["Pipeline"] = proc
            logger.info(f"Pipeline 起動: PID={proc.pid}")

            # 0.5 秒待って即死していないか確認する（lock file 競合で exit 1 する場合の検知）
            time.sleep(0.5)
            if proc.poll() is not None:
                with self._lock:
                    self._processes.pop("Pipeline", None)
                logger.warning(f"Pipeline PID={proc.pid} が即終了: 多重起動ガードが発動した可能性")
                return False, "Pipeline の起動に失敗しました（多重起動ガードにより即終了した可能性があります）"

            return True, f"Pipeline を起動しました（PID={proc.pid}）"

        except Exception as exc:
            logger.warning(f"Pipeline 起動例外: {exc}")
            return False, f"Pipeline の起動に失敗しました: {exc}"

    def start_open_webui(self) -> tuple[bool, str]:
        """Open WebUI をバックグラウンドで起動する。

        .venv-webui/Scripts/open-webui.exe を使って openwebui.base_url の port で起動する。
        Popen オブジェクトを self._processes["Open WebUI"] に保存する。

        Returns:
            (成功フラグ, メッセージ)
        """
        webui_exe = self.project_root / ".venv-webui" / "Scripts" / "open-webui.exe"
        webui_port = self._port_from_url(self._webui_base_url) or 3000

        if not webui_exe.exists():
            return False, (
                f"open-webui.exe が見つかりません: {webui_exe}\n"
                ".venv-webui に Open WebUI をインストールしてください。\n"
                "例: python -m venv .venv-webui && .venv-webui\\Scripts\\pip install open-webui==0.9.5"
            )

        current_info = self.check_open_webui()
        if current_info.status == ServiceStatus.RUNNING:
            self._queue_open_webui_pending_sync()
            return True, "Open WebUI は既に稼働中です"
        if current_info.status == ServiceStatus.UNKNOWN and current_info.pid is not None:
            self._queue_open_webui_pending_sync()
            return True, f"Open WebUI は起動中です（PID={current_info.pid}）"

        try:
            env = os.environ.copy()
            # Open WebUI は起動時にブロック文字のバナーを出力することがある。
            # Windows の既定 cp932 では UnicodeEncodeError で落ちるため UTF-8 を強制する。
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            self._logs_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = self._logs_dir / "open_webui_stdout.log"
            stderr_path = self._logs_dir / "open_webui_stderr.log"
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                proc = subprocess.Popen(
                    [str(webui_exe), "serve", "--port", str(webui_port)],
                    cwd=str(self.project_root),
                    creationflags=_hidden_creationflags(CREATE_NEW_PROCESS_GROUP),
                    startupinfo=_hidden_startupinfo(),
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            with self._lock:
                self._processes["Open WebUI"] = proc
            logger.info(f"Open WebUI 起動: PID={proc.pid}")
            time.sleep(0.5)
            if proc.poll() is not None:
                with self._lock:
                    self._processes.pop("Open WebUI", None)
                return False, (
                    f"Open WebUI の起動に失敗しました（終了コード={proc.returncode}）。"
                    f" 詳細は {stderr_path} を確認してください。"
                )
            self._queue_open_webui_pending_sync()
            return True, f"Open WebUI を起動しました（PID={proc.pid}、初期化に 30〜60 秒かかります）"

        except Exception as exc:
            logger.warning(f"Open WebUI 起動例外: {exc}")
            return False, f"Open WebUI の起動に失敗しました: {exc}"

    def start_all(self) -> dict[str, tuple[bool, str]]:
        """3 サービスをすべて順番に起動する。

        各 start_xxx() が Popen オブジェクトを self._processes に保存する。

        Returns:
            {"Ollama": (成功, メッセージ), "Pipeline": ..., "Open WebUI": ...}
        """
        return {
            "Ollama": self.start_ollama(),
            "Pipeline": self.start_pipeline(),
            "Open WebUI": self.start_open_webui(),
        }

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------

    def stop_service(self, name: str) -> tuple[bool, str]:
        """指定サービスを停止する。

        自分が起動した Popen に加え、外部起動でも PID を検出できるものは停止する。
        停止対象はサービスごとに限定して検出し、taskkill /T /F で子プロセスごと終了する。

        Args:
            name: サービス名（"Ollama" / "Pipeline" / "Open WebUI"）

        Returns:
            (成功フラグ, メッセージ)
        """
        with self._lock:
            proc = self._processes.get(name)

        pids: set[int] = set()
        ended_managed_pid: int | None = None
        if proc is not None:
            # poll() で生死確認: None = まだ生きている、それ以外 = 終了済み
            if proc.poll() is None:
                pids.add(proc.pid)
            else:
                # 既に終了している → _processes から除去
                ended_managed_pid = proc.pid
                with self._lock:
                    self._processes.pop(name, None)
                logger.info(f"{name} は既に終了していました（PID={proc.pid}）")

        detected_pids = set(self._detect_external_pids(name))
        pids.update(detected_pids)
        requested_pids = set(pids)
        pids = {pid for pid in pids if self._is_pid_running(pid)}

        if not pids:
            with self._lock:
                self._processes.pop(name, None)
            if ended_managed_pid is not None or requested_pids:
                pid_text = ", ".join(str(pid) for pid in sorted(requested_pids))
                if ended_managed_pid is not None and not pid_text:
                    pid_text = str(ended_managed_pid)
                return True, f"{name} は既に停止していました（PID={pid_text}）"
            return False, f"{name} の停止対象 PID が見つかりません"

        stopped: list[int] = []
        failed: list[str] = []
        try:
            for pid in sorted(pids):
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=_hidden_creationflags(),
                    startupinfo=_hidden_startupinfo(),
                )

                if result.returncode == 0:
                    stopped.append(pid)
                    continue

                err_out = (result.stderr or result.stdout or "").strip()
                if not self._is_pid_running(pid):
                    # 検出から taskkill 実行までの間に終了していた場合は成功扱い。
                    # Windows の taskkill は「プロセスが見つかりません」をエラーにするため、
                    # ここで吸収しないとユーザーには停止失敗に見えてしまう。
                    stopped.append(pid)
                    logger.info(f"{name} は taskkill 前後に既に終了: PID={pid}")
                    continue
                failed.append(f"PID={pid}: {err_out}")
                logger.warning(f"{name} taskkill 失敗: PID={pid}, {err_out}")

            if proc is not None and proc.pid in stopped:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"{name} の終了を 5 秒待ったがタイムアウト: PID={proc.pid}"
                    )

            with self._lock:
                # 停止済みなので Popen を管理辞書から削除
                self._processes.pop(name, None)

            if failed:
                detail = "\n".join(failed)
                return False, f"{name} の停止に一部失敗しました:\n{detail}"

            pid_text = ", ".join(str(pid) for pid in stopped)
            logger.info(f"{name} 停止: PID={pid_text}")
            return True, f"{name} を停止しました（PID={pid_text}）"

        except Exception as exc:
            logger.warning(f"{name} 停止例外: {exc}")
            return False, f"{name} の停止中にエラーが発生しました: {exc}"

    def stop_all(self) -> dict[str, tuple[bool, str]]:
        """稼働中として検出できるすべてのサービスを停止する。

        Returns:
            {サービス名: (成功, メッセージ)} の辞書。稼働中でないサービスは含まない。
        """
        results: dict[str, tuple[bool, str]] = {}
        names_to_stop = [
            info.name for info in self.check_all()
            if info.status in {ServiceStatus.RUNNING, ServiceStatus.UNKNOWN}
        ]

        for name in names_to_stop:
            results[name] = self.stop_service(name)
        return results

    # ------------------------------------------------------------------
    # クリーンアップ
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """自分が起動したプロセスをすべて終了する。

        atexit から呼ぶことで、GUI が閉じたときに残プロセスを後片付けする。
        注意: Pipeline と Open WebUI は「GUI 終了後も継続させたい」という
        運用ポリシーがあるため、GUI 側でこのメソッドを呼ぶかどうかを慎重に判断すること。
        """
        with self._lock:
            names_to_stop = list(self._processes.keys())

        for name in names_to_stop:
            try:
                ok, msg = self.stop_service(name)
                if ok:
                    logger.info(f"cleanup: {name} を停止しました")
                else:
                    logger.debug(f"cleanup: {name} の停止をスキップ ({msg})")
            except Exception as exc:
                logger.debug(f"cleanup: {name} 停止中の例外を無視: {exc}")
