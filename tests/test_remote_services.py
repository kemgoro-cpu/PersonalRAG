from __future__ import annotations

from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from remote_service_agent import execute_command
from remote_services import (
    ACTION_START,
    ACTION_STOP,
    SERVICE_ALL,
    create_service_command,
    read_service_status,
)


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def start_pipeline(self) -> tuple[bool, str]:
        self.calls.append(("start", "Pipeline"))
        return True, "pipeline started"

    def stop_service(self, name: str) -> tuple[bool, str]:
        self.calls.append(("stop", name))
        return True, f"{name} stopped"

    def start_all(self) -> dict[str, tuple[bool, str]]:
        self.calls.append(("start", SERVICE_ALL))
        return {"Pipeline": (True, "pipeline started")}

    def stop_all(self) -> dict[str, tuple[bool, str]]:
        self.calls.append(("stop", SERVICE_ALL))
        return {"Pipeline": (True, "pipeline stopped")}


def test_create_service_command_writes_json(tmp_path: Path) -> None:
    command_path = create_service_command(
        tmp_path,
        action=ACTION_START,
        service="Pipeline",
    )

    assert command_path.parent == tmp_path / "commands"
    text = command_path.read_text(encoding="utf-8")
    assert '"action": "start"' in text
    assert '"service": "Pipeline"' in text


def test_read_service_status_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_service_status(tmp_path) is None


def test_execute_command_routes_single_service() -> None:
    manager = FakeManager()

    result = execute_command(manager, {"action": ACTION_STOP, "service": "Pipeline"})

    assert result["ok"] is True
    assert manager.calls == [("stop", "Pipeline")]


def test_execute_command_routes_all_services() -> None:
    manager = FakeManager()

    result = execute_command(manager, {"action": ACTION_START, "service": SERVICE_ALL})

    assert result["ok"] is True
    assert manager.calls == [("start", SERVICE_ALL)]
