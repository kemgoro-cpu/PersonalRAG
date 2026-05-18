"""Remote service control agent.

Run this on the remote PC. It watches a NAS/shared control directory for service
commands from the desktop UI and publishes Ollama / Pipeline / Open WebUI status
back to the same directory.

Usage:
    python scripts/remote_service_agent.py
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from config_loader import PROJECT_ROOT, load_settings, resolve_path
from remote_services import (
    ACTION_REFRESH,
    ACTION_START,
    ACTION_STOP,
    SERVICE_ALL,
    SERVICE_NAMES,
    atomic_write_json,
    get_commands_dir,
    get_control_dir,
    get_status_file,
    now_iso,
    serialize_service_info,
)
from service_manager import ServiceManager


def setup_logger(log_dir: Path) -> logging.Logger:
    """Configure console and file logging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("remote_service_agent")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_dir / "remote_service_agent.log", encoding="utf-8")
    fh.setFormatter(formatter)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def publish_status(
    manager: ServiceManager,
    status_file: Path,
    *,
    last_command: dict[str, Any] | None = None,
) -> None:
    """Publish service status for the desktop UI."""
    infos = manager.check_all()
    payload = {
        "updated_at": now_iso(),
        "controller": "running",
        "services": [serialize_service_info(info) for info in infos],
        "last_command": last_command,
    }
    atomic_write_json(status_file, payload)


def execute_command(
    manager: ServiceManager,
    command: dict[str, Any],
) -> dict[str, Any]:
    """Execute one remote command and return a result payload."""
    action = str(command.get("action", ""))
    service = str(command.get("service", ""))
    if action not in {ACTION_START, ACTION_STOP, ACTION_REFRESH}:
        raise ValueError(f"unknown action: {action}")
    if service not in {*SERVICE_NAMES, SERVICE_ALL}:
        raise ValueError(f"unknown service: {service}")

    if action == ACTION_REFRESH:
        return {"ok": True, "message": "状態を更新しました"}

    if service == SERVICE_ALL:
        results = manager.start_all() if action == ACTION_START else manager.stop_all()
        ok = all(result[0] for result in results.values()) if results else True
        message = "; ".join(f"{name}: {result[1]}" for name, result in results.items())
        return {"ok": ok, "message": message or "対象サービスはありません", "results": results}

    if action == ACTION_START:
        start_map = {
            "Ollama": manager.start_ollama,
            "Pipeline": manager.start_pipeline,
            "Open WebUI": manager.start_open_webui,
        }
        ok, message = start_map[service]()
        return {"ok": ok, "message": message}

    ok, message = manager.stop_service(service)
    return {"ok": ok, "message": message}


def process_commands(
    manager: ServiceManager,
    commands_dir: Path,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Process pending commands in order."""
    commands_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = commands_dir / "processed"
    failed_dir = commands_dir / "failed"
    processed_dir.mkdir(exist_ok=True)
    failed_dir.mkdir(exist_ok=True)

    last_result: dict[str, Any] | None = None
    for command_path in sorted(commands_dir.glob("*.json")):
        try:
            command = json.loads(command_path.read_text(encoding="utf-8"))
            result = execute_command(manager, command)
            last_result = {
                "id": command.get("id", command_path.stem),
                "processed_at": now_iso(),
                "action": command.get("action"),
                "service": command.get("service"),
                **result,
            }
            logger.info(
                "command %s %s: %s",
                command.get("action"),
                command.get("service"),
                result.get("message"),
            )
            done_path = processed_dir / f"{command_path.stem}.done.json"
            atomic_write_json(done_path, {"command": command, "result": last_result})
            command_path.unlink()
        except Exception as exc:
            last_result = {
                "id": command_path.stem,
                "processed_at": now_iso(),
                "ok": False,
                "message": str(exc),
            }
            logger.warning("command failed %s: %s", command_path.name, exc)
            failed_path = failed_dir / command_path.name
            try:
                shutil.move(str(command_path), str(failed_path))
            except Exception:
                pass
    return last_result


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="NAS経由のリモートサービス制御エージェント")
    parser.add_argument("--once", action="store_true", help="1回だけ状態更新とコマンド処理を行う")
    parser.add_argument("--interval", type=float, default=3.0, help="ポーリング間隔（秒）")
    args = parser.parse_args()

    settings = load_settings()
    logs_dir = resolve_path(settings.get("paths", {}).get("logs_dir", "data/logs"))
    logger = setup_logger(logs_dir)
    control_dir = get_control_dir(settings)
    commands_dir = get_commands_dir(control_dir)
    status_file = get_status_file(control_dir)
    manager = ServiceManager(PROJECT_ROOT, settings)
    logger.info("remote service agent started: %s", control_dir)

    last_command: dict[str, Any] | None = None
    try:
        while True:
            latest = process_commands(manager, commands_dir, logger)
            if latest is not None:
                last_command = latest
            publish_status(manager, status_file, last_command=last_command)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("remote service agent stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
