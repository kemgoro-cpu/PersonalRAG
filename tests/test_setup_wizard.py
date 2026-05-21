from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETUP_PATH = PROJECT_ROOT / "scripts" / "setup.py"


def load_setup_module() -> Any:
    spec = importlib.util.spec_from_file_location("personalrag_setup", SETUP_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    (project_root / "config").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "config" / "settings.dev.yaml", project_root / "config" / "settings.dev.yaml")
    shutil.copy2(PROJECT_ROOT / "config" / "settings.prod.yaml", project_root / "config" / "settings.prod.yaml")
    return project_root


def load_settings(project_root: Path) -> dict[str, Any]:
    return yaml.safe_load((project_root / "config" / "settings.yaml").read_text(encoding="utf-8"))


def test_local_prod_default_enables_local_service_management(tmp_path: Path) -> None:
    setup = load_setup_module()
    project_root = make_project(tmp_path)

    tasks = setup.apply_configuration(project_root, setup.SetupAnswers())
    settings = load_settings(project_root)

    assert settings["ui"]["local_service_management"] is True
    assert settings["openwebui"]["base_url"] == "http://localhost:3000"
    assert settings["openwebui"]["enabled"] is False
    assert (project_root / ".env").read_text(encoding="utf-8").count("HUGGINGFACE_TOKEN=") == 1
    assert any("Hugging Face token" in task for task in tasks)


def test_remote_server_creates_fixed_shared_layout_and_connection(tmp_path: Path) -> None:
    setup = load_setup_module()
    project_root = make_project(tmp_path)
    shared_root = tmp_path / "share"
    answers = setup.SetupAnswers(
        mode="remote-server",
        profile="prod",
        shared_root=str(shared_root),
        remote_host="rag-gpu",
        webui_port=3333,
        openwebui_api_key="sk-test",
        knowledge_id="knowledge-test",
    )

    setup.apply_configuration(project_root, answers)
    settings = load_settings(project_root)

    assert settings["paths"]["input_dir"] == str(shared_root / "input")
    assert settings["pipeline"]["state_file"] == str(shared_root / "status" / "pipeline_state.json")
    assert settings["openwebui"]["base_url"] == "http://localhost:3333"
    assert settings["openwebui"]["bind_host"] == "0.0.0.0"
    assert settings["openwebui"]["enabled"] is True
    for relative in [
        "input",
        "input/processed",
        "input_text",
        "input_text/processed",
        "status",
        "summaries",
        "control",
        "control/commands",
    ]:
        assert (shared_root / relative).is_dir()
    connection = json.loads((shared_root / "control" / "connection.json").read_text(encoding="utf-8"))
    assert connection["remote_host"] == "rag-gpu"
    assert connection["webui_url"] == "http://rag-gpu:3333"


def test_remote_client_reads_connection_and_uses_shared_paths(tmp_path: Path) -> None:
    setup = load_setup_module()
    project_root = make_project(tmp_path)
    shared_root = tmp_path / "share"
    (shared_root / "control").mkdir(parents=True)
    (shared_root / "control" / "connection.json").write_text(
        json.dumps({"remote_host": "rag-gpu", "webui_port": 3333}),
        encoding="utf-8",
    )
    args = setup.parse_args(["remote-client", str(shared_root), "--non-interactive", "--skip-install"])
    answers = setup.collect_answers(args)

    setup.apply_configuration(project_root, answers)
    settings = load_settings(project_root)

    assert answers.remote_host == "rag-gpu"
    assert answers.webui_port == 3333
    assert settings["paths"]["recordings_dir"] == str(shared_root / "input")
    assert settings["paths"]["remote_pipeline_state_file"] == str(
        shared_root / "status" / "pipeline_state.json"
    )
    assert settings["ui"]["local_service_management"] is False
    assert settings["openwebui"]["base_url"] == "http://rag-gpu:3333"


def test_remote_client_uses_client_dependency_installer(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    setup = load_setup_module()
    calls: list[Path] = []

    monkeypatch.setattr(setup, "install_client_dependencies", lambda root: calls.append(root))
    monkeypatch.setattr(
        setup,
        "install_main_dependencies",
        lambda *_args: (_ for _ in ()).throw(AssertionError("main installer should not run")),
    )
    monkeypatch.setattr(
        setup,
        "install_open_webui",
        lambda *_args: (_ for _ in ()).throw(AssertionError("webui installer should not run")),
    )

    setup.install_dependencies(tmp_path, setup.SetupAnswers(mode="remote-client"))

    assert calls == [tmp_path]
