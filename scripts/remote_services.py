"""NAS-backed remote service control helpers.

The desktop UI writes command JSON files into a shared control directory. A
lightweight agent on the remote PC consumes those commands, uses ServiceManager,
and publishes service status back to the same directory.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import resolve_path


SERVICE_NAMES = ["Ollama", "Pipeline", "Open WebUI"]
SERVICE_ALL = "All"
ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_REFRESH = "refresh"


def now_iso() -> str:
    """Return current local ISO timestamp."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def get_control_dir(settings: dict[str, Any]) -> Path:
    """Resolve the NAS/shared service control directory."""
    value = settings.get("paths", {}).get("remote_control_dir", "data/control")
    return resolve_path(value)


def get_commands_dir(control_dir: Path) -> Path:
    """Return the command inbox path."""
    return control_dir / "commands"


def get_status_file(control_dir: Path) -> Path:
    """Return the published remote service status file path."""
    return control_dir / "service_status.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def create_service_command(
    control_dir: Path,
    *,
    action: str,
    service: str,
) -> Path:
    """Create a command file for the remote service agent."""
    if action not in {ACTION_START, ACTION_STOP, ACTION_REFRESH}:
        raise ValueError(f"unknown action: {action}")
    if service not in {*SERVICE_NAMES, SERVICE_ALL}:
        raise ValueError(f"unknown service: {service}")

    command_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    payload = {
        "id": command_id,
        "created_at": now_iso(),
        "action": action,
        "service": service,
    }
    path = get_commands_dir(control_dir) / f"{command_id}.json"
    atomic_write_json(path, payload)
    return path


def read_service_status(control_dir: Path) -> dict[str, Any] | None:
    """Read the remote service status JSON if available."""
    status_file = get_status_file(control_dir)
    if not status_file.exists():
        return None
    return json.loads(status_file.read_text(encoding="utf-8"))


def serialize_service_info(info: Any) -> dict[str, Any]:
    """Convert ServiceInfo-like objects to JSON-serializable dicts."""
    status = getattr(info, "status", "")
    return {
        "name": getattr(info, "name", ""),
        "status": getattr(status, "value", str(status)),
        "detail": getattr(info, "detail", ""),
        "pid": getattr(info, "pid", None),
    }
