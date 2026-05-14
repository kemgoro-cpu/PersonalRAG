"""service_manager.py
フェーズ C: サービス管理モジュール。

Ollama / Pipeline / Open WebUI の 3 サービスについて、
状態検知・起動・停止のロジックを GUI から切り離して集約する。

このモジュールは GUI に依存しないため、単体テストが可能。
"""

from __future__ import annotations

import enum
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Windows の subprocess フラグ: 新しいプロセスグループを作成する。
# GUI 終了時の taskkill が孫プロセスへ過剰に広がらないよう独立化する。
CREATE_NEW_PROCESS_GROUP = 0x00000200


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
        - 起動した Popen のみが停止対象（外部起動のサービスは触らない）
        - PID ではなく Popen を保持することで、PID 再利用による誤 kill を防ぐ

    停止:
        - proc.poll() で生死確認後、taskkill /PID <pid> /T /F で子プロセスごと強制終了
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

        # Ollama の URL（settings の llm.host から取得、デフォルト localhost:11434）
        self._ollama_base_url: str = settings.get("llm", {}).get(
            "host", "http://localhost:11434"
        ).rstrip("/")

        # Open WebUI の URL（settings の openwebui.base_url から取得）
        self._webui_base_url: str = settings.get("openwebui", {}).get(
            "base_url", "http://localhost:3000"
        ).rstrip("/")

    # ------------------------------------------------------------------
    # 状態検知
    # ------------------------------------------------------------------

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
                return ServiceInfo(name="Ollama", status=ServiceStatus.RUNNING,
                                   detail="稼働中", pid=pid)
        except Exception:
            pass
        return ServiceInfo(name="Ollama", status=ServiceStatus.STOPPED, detail="停止中")

    def check_pipeline(self) -> ServiceInfo:
        """Pipeline の稼働状態を状態ファイルの更新時刻で確認する。

        pipeline_state.json の updated_at が現在時刻から 30 秒以内なら RUNNING。
        ファイルが存在しない・パース失敗・30 秒超はすべて STOPPED 扱い。
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

            if diff_seconds <= 30:
                with self._lock:
                    proc = self._processes.get("Pipeline")
                    pid = proc.pid if proc is not None else None
                return ServiceInfo(name="Pipeline", status=ServiceStatus.RUNNING,
                                   detail="稼働中", pid=pid)
            else:
                # 状態ファイルは存在するが古い → 停止しているとみなす
                return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED,
                                   detail=f"停止中（最終更新 {int(diff_seconds)}s 前）")

        except Exception as exc:
            logger.debug(f"Pipeline 状態チェック例外: {exc}")
        return ServiceInfo(name="Pipeline", status=ServiceStatus.STOPPED, detail="停止中")

    def check_open_webui(self) -> ServiceInfo:
        """Open WebUI の稼働状態を HTTP で確認する。

        GET /health が 200 なら RUNNING。
        /health が存在しないバージョン（404）では GET / でフォールバックし、
        200 が返れば RUNNING とみなす。
        それ以外の例外・タイムアウトはすべて STOPPED 扱い。
        """
        try:
            import requests
            # まず /health を試す
            try:
                resp = requests.get(f"{self._webui_base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    with self._lock:
                        proc = self._processes.get("Open WebUI")
                        pid = proc.pid if proc is not None else None
                    return ServiceInfo(name="Open WebUI", status=ServiceStatus.RUNNING,
                                       detail="稼働中", pid=pid)
                if resp.status_code == 404:
                    # /health がないバージョン → / でフォールバック
                    resp2 = requests.get(f"{self._webui_base_url}/", timeout=2.0)
                    if resp2.status_code == 200:
                        with self._lock:
                            proc = self._processes.get("Open WebUI")
                            pid = proc.pid if proc is not None else None
                        return ServiceInfo(name="Open WebUI", status=ServiceStatus.RUNNING,
                                           detail="稼働中", pid=pid)
            except requests.exceptions.ConnectionError:
                pass  # 下の STOPPED へ落ちる
        except Exception as exc:
            logger.debug(f"Open WebUI 状態チェック例外: {exc}")
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
                creationflags=CREATE_NEW_PROCESS_GROUP,
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

        Returns:
            (成功フラグ, メッセージ)
        """
        pythonw = self.project_root / ".venv" / "Scripts" / "pythonw.exe"
        pipeline_script = self.project_root / "scripts" / "pipeline.py"

        if not pythonw.exists():
            return False, f"pythonw.exe が見つかりません: {pythonw}\n.venv を作成してください。"
        if not pipeline_script.exists():
            return False, f"pipeline.py が見つかりません: {pipeline_script}"

        try:
            proc = subprocess.Popen(
                [str(pythonw), str(pipeline_script)],
                creationflags=CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                self._processes["Pipeline"] = proc
            logger.info(f"Pipeline 起動: PID={proc.pid}")
            return True, f"Pipeline を起動しました（PID={proc.pid}）"

        except Exception as exc:
            logger.warning(f"Pipeline 起動例外: {exc}")
            return False, f"Pipeline の起動に失敗しました: {exc}"

    def start_open_webui(self) -> tuple[bool, str]:
        """Open WebUI をバックグラウンドで起動する。

        .venv-webui/Scripts/open-webui.exe を使って port 3000 で起動する。
        Popen オブジェクトを self._processes["Open WebUI"] に保存する。

        Returns:
            (成功フラグ, メッセージ)
        """
        webui_exe = self.project_root / ".venv-webui" / "Scripts" / "open-webui.exe"

        if not webui_exe.exists():
            return False, (
                f"open-webui.exe が見つかりません: {webui_exe}\n"
                ".venv-webui に Open WebUI をインストールしてください。\n"
                "例: python -m venv .venv-webui && .venv-webui\\Scripts\\pip install open-webui"
            )

        try:
            proc = subprocess.Popen(
                [str(webui_exe), "serve", "--port", "3000"],
                creationflags=CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with self._lock:
                self._processes["Open WebUI"] = proc
            logger.info(f"Open WebUI 起動: PID={proc.pid}")
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

        自分が起動した Popen のみ停止する。外部で起動されたサービスは触らない。
        Popen.poll() で生死を確認してから taskkill /T /F で強制終了する。
        既に終了していた場合は _processes から除去して「停止済み」を返す。

        Args:
            name: サービス名（"Ollama" / "Pipeline" / "Open WebUI"）

        Returns:
            (成功フラグ, メッセージ)
        """
        with self._lock:
            proc = self._processes.get(name)

        if proc is None:
            return False, f"{name} は外部起動のため停止できません（このGUIからは起動していません）"

        # poll() で生死確認: None = まだ生きている、それ以外 = 終了済み
        if proc.poll() is not None:
            # 既に終了している → _processes から除去して「停止済み」扱い
            with self._lock:
                self._processes.pop(name, None)
            logger.info(f"{name} は既に終了していました（PID={proc.pid}）")
            return True, f"{name} は既に停止していました（PID={proc.pid}）"

        pid = proc.pid
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                # 停止確認のため最大 5 秒待機
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"{name} の終了を 5 秒待ったがタイムアウト: PID={pid}")

            with self._lock:
                # 停止済みなので Popen を管理辞書から削除
                self._processes.pop(name, None)

            if result.returncode == 0:
                logger.info(f"{name} 停止: PID={pid}")
                return True, f"{name} を停止しました（PID={pid}）"
            else:
                # 既に終了していた可能性がある
                err_out = (result.stderr or "").strip()
                logger.warning(f"{name} taskkill 失敗: {err_out}")
                return False, f"{name} の停止に失敗しました（既に終了している可能性）: {err_out}"

        except Exception as exc:
            logger.warning(f"{name} 停止例外: {exc}")
            return False, f"{name} の停止中にエラーが発生しました: {exc}"

    def stop_all(self) -> dict[str, tuple[bool, str]]:
        """自分が起動したすべてのサービスを停止する。

        Returns:
            {サービス名: (成功, メッセージ)} の辞書。起動していないサービスは含まない。
        """
        results: dict[str, tuple[bool, str]] = {}
        with self._lock:
            names_to_stop = list(self._processes.keys())

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
