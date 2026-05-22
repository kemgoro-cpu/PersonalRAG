from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPEN_WEBUI_VERSION = "0.9.5"
MODES = {"local", "remote-server", "remote-client"}
PROFILES = {"dev", "prod"}


@dataclass
class SetupAnswers:
    mode: str = "local"
    profile: str = "prod"
    shared_root: str = ""
    remote_host: str = ""
    webui_port: int = 3000
    huggingface_token: str = ""
    openwebui_api_key: str = ""
    knowledge_id: str = ""
    proxy_url: str = ""
    set_ollama_keep_alive: bool = False


def info(message: str) -> None:
    print(f"[setup] {message}")


def warn(message: str) -> None:
    print(f"[setup 警告] {message}")


def ask_choice(prompt: str, choices: list[str], default: str) -> str:
    choice_text = " / ".join(
        f"{choice}{' (既定)' if choice == default else ''}" for choice in choices
    )
    while True:
        value = input(f"{prompt} [{choice_text}]: ").strip()
        if not value:
            return default
        normalized = value.lower()
        if normalized in choices:
            return normalized
        print(f"  次のいずれかで入力してください: {', '.join(choices)}")


def ask_text(prompt: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else " [Enterで後から]"
    if secret:
        value = getpass.getpass(f"{prompt}{suffix}: ").strip()
    else:
        value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default


def ask_bool(prompt: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("  y または n で入力してください。")


def ask_int(prompt: str, default: int, *, min_value: int, max_value: int) -> int:
    while True:
        value = ask_text(prompt, str(default))
        try:
            parsed = int(value)
        except ValueError:
            print("  数値で入力してください。")
            continue
        if min_value <= parsed <= max_value:
            return parsed
        print(f"  {min_value} から {max_value} の範囲で入力してください。")


def normalize_mode_alias(mode: str | None, profile: str | None) -> tuple[str | None, str | None]:
    if mode in {"dev", "prod"}:
        return "local", mode
    return mode, profile


def build_webui_url(host: str, port: int) -> str:
    host = host.strip().rstrip("/")
    if not host:
        host = "localhost"
    if host.startswith("http://") or host.startswith("https://"):
        return f"{host}:{port}" if ":" not in host.split("//", 1)[1] else host
    return f"http://{host}:{port}"


def normalize_proxy_url(proxy_url: str) -> str:
    proxy_url = proxy_url.strip()
    if not proxy_url:
        return ""
    if "://" not in proxy_url:
        return f"http://{proxy_url}"
    return proxy_url


def ask_proxy_url() -> str:
    while True:
        proxy_url = normalize_proxy_url(
            ask_text("プロキシURLを入力してください（例: http://proxy:port）", "")
        )
        if proxy_url:
            return proxy_url
        print("  プロキシを使う場合は URL を入力してください。")


def shared_path(shared_root: str, *parts: str) -> str:
    root = shared_root.rstrip("\\/")
    separator = "\\" if "\\" in root else "/"
    return root + separator + separator.join(parts)


def is_absolute_or_unc(path_text: str) -> bool:
    return path_text.startswith("\\\\") or Path(path_text).is_absolute()


def resolve_config_path(project_root: Path, path_text: str) -> Path:
    if is_absolute_or_unc(path_text):
        return Path(path_text)
    return project_root / path_text


def load_connection(shared_root: str) -> dict[str, Any] | None:
    if not shared_root:
        return None
    connection_path = Path(shared_path(shared_root, "control", "connection.json"))
    if not connection_path.exists():
        return None
    try:
        return json.loads(connection_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_answers(args: argparse.Namespace) -> SetupAnswers:
    mode, profile = normalize_mode_alias(args.mode, args.profile)
    proxy_url = normalize_proxy_url(args.proxy_url)
    if args.non_interactive:
        if not mode:
            raise SystemExit("--non-interactive では mode を指定してください。")
        shared_root = args.shared_root or ""
        remote_host = args.remote_host or ""
        webui_port = args.webui_port
        if mode in {"remote-server", "remote-client"} and shared_root:
            connection = load_connection(shared_root)
            if connection and not remote_host:
                remote_host = str(connection.get("remote_host", "") or "")
            if connection and args.webui_port == 3000:
                try:
                    webui_port = int(connection.get("webui_port", webui_port))
                except (TypeError, ValueError):
                    pass
        if mode == "remote-server" and not remote_host:
            remote_host = os.environ.get("COMPUTERNAME", "remote-pc")
        answers = SetupAnswers(
            mode=mode,
            profile=profile or "prod",
            shared_root=shared_root,
            remote_host=remote_host,
            webui_port=webui_port,
            huggingface_token=args.huggingface_token or "",
            openwebui_api_key=args.openwebui_api_key or "",
            knowledge_id=args.knowledge_id or "",
            proxy_url=proxy_url,
            set_ollama_keep_alive=args.set_ollama_keep_alive,
        )
        validate_answers(answers)
        return answers

    print("")
    print("PersonalRAG セットアップウィザード")
    print("質問に答えるだけで、必要な設定ファイルとフォルダを作成します。")
    print("")

    mode = mode or ask_choice(
        "利用形態を選んでください",
        ["local", "remote-server", "remote-client"],
        "local",
    )

    if mode == "remote-client":
        selected_profile = profile or "prod"
    else:
        selected_profile = profile or ask_choice(
            "プロファイルを選んでください",
            ["prod", "dev"],
            "prod",
        )

    if not proxy_url and not args.skip_install:
        if ask_bool("pip など外部アクセス用コマンドにプロキシが必要ですか", False):
            proxy_url = ask_proxy_url()

    shared_root = args.shared_root or ""
    remote_host = args.remote_host or ""
    webui_port = args.webui_port

    if mode in {"remote-server", "remote-client"}:
        shared_root = shared_root or ask_text(
            "NAS/共有フォルダのルートを入力してください",
            r"\\NAS\share\PersonalRAG",
        )
        connection = load_connection(shared_root)
        if connection and not remote_host:
            remote_host = str(connection.get("remote_host", "") or "")
        if connection and args.webui_port == 3000:
            try:
                webui_port = int(connection.get("webui_port", webui_port))
            except (TypeError, ValueError):
                pass
        if mode == "remote-server":
            remote_host = remote_host or ask_text(
                "手元PCから見えるリモート処理PCのホスト名/IPを入力してください",
                os.environ.get("COMPUTERNAME", "remote-pc"),
            )
        else:
            remote_host = remote_host or ask_text(
                "Open WebUI を開くリモート処理PCのホスト名/IPを入力してください",
                "remote-pc",
            )
        webui_port = ask_int(
            "Open WebUI のポートを入力してください",
            webui_port,
            min_value=1,
            max_value=65535,
        )

    needs_server_values = mode in {"local", "remote-server"}
    hf_token = ""
    api_key = ""
    knowledge_id = ""
    set_keep_alive = False
    if needs_server_values:
        hf_token = args.huggingface_token or ask_text(
            "Hugging Face token を入力してください",
            "",
            secret=True,
        )
        api_key = args.openwebui_api_key or ask_text(
            "Open WebUI API key を入力してください",
            "",
            secret=True,
        )
        knowledge_id = args.knowledge_id or ask_text(
            "Open WebUI Knowledge ID を入力してください",
            "",
        )
        set_keep_alive = ask_bool(
            "OLLAMA_KEEP_ALIVE=0 をユーザー環境変数に設定しますか",
            True,
        )

    answers = SetupAnswers(
        mode=mode,
        profile=selected_profile,
        shared_root=shared_root,
        remote_host=remote_host,
        webui_port=webui_port,
        huggingface_token=hf_token,
        openwebui_api_key=api_key,
        knowledge_id=knowledge_id,
        proxy_url=proxy_url,
        set_ollama_keep_alive=set_keep_alive,
    )
    validate_answers(answers)
    return answers


def validate_answers(answers: SetupAnswers) -> None:
    if answers.mode not in MODES:
        raise SystemExit(f"不明な利用形態です: {answers.mode}")
    if answers.profile not in PROFILES:
        raise SystemExit(f"不明なプロファイルです: {answers.profile}")
    if answers.mode in {"remote-server", "remote-client"} and not answers.shared_root:
        raise SystemExit("リモート構成では共有ルートが必要です。")
    if answers.webui_port <= 0 or answers.webui_port > 65535:
        raise SystemExit("Open WebUI のポートは 1-65535 で指定してください。")
    if answers.proxy_url and not answers.proxy_url.startswith(("http://", "https://")):
        raise SystemExit("プロキシURLは http:// または https:// で始まる値を指定してください。")


def venv_python(project_root: Path, venv_name: str = ".venv") -> Path:
    if os.name == "nt":
        return project_root / venv_name / "Scripts" / "python.exe"
    return project_root / venv_name / "bin" / "python"


def create_venv(project_root: Path, venv_name: str = ".venv") -> Path:
    python_path = venv_python(project_root, venv_name)
    if python_path.exists():
        info(f"既存の {venv_name} を再利用します。")
        return python_path
    info(f"{venv_name} を作成しています...")
    subprocess.run([sys.executable, "-m", "venv", venv_name], cwd=project_root, check=True)
    return python_path


def add_pip_proxy(args: list[str], proxy_url: str) -> list[str]:
    normalized_proxy = normalize_proxy_url(proxy_url)
    if not normalized_proxy:
        return args
    if any(arg == "--proxy" or arg.startswith("--proxy=") for arg in args):
        return args
    return [*args, f"--proxy={normalized_proxy}"]


def run_pip(
    python_path: Path,
    args: list[str],
    project_root: Path,
    proxy_url: str = "",
) -> None:
    pip_args = add_pip_proxy(args, proxy_url)
    subprocess.run([str(python_path), "-m", "pip", *pip_args], cwd=project_root, check=True)


def install_main_dependencies(project_root: Path, profile: str, proxy_url: str = "") -> None:
    python_path = create_venv(project_root)
    run_pip(python_path, ["install", "--upgrade", "pip"], project_root, proxy_url)
    if profile == "prod":
        torch_index = "https://download.pytorch.org/whl/cu128"
        torch_version = "2.11.0"
    else:
        torch_index = "https://download.pytorch.org/whl/cu121"
        torch_version = "2.5.1"
    info(f"PyTorch CUDA 版をインストールします: torch=={torch_version}")
    run_pip(
        python_path,
        [
            "install",
            f"torch=={torch_version}",
            f"torchaudio=={torch_version}",
            "--index-url",
            torch_index,
        ],
        project_root,
        proxy_url,
    )
    run_pip(python_path, ["install", "-r", "requirements.txt"], project_root, proxy_url)


def install_client_dependencies(project_root: Path, proxy_url: str = "") -> None:
    python_path = create_venv(project_root)
    run_pip(python_path, ["install", "--upgrade", "pip"], project_root, proxy_url)
    run_pip(python_path, ["install", "-r", "requirements-client.txt"], project_root, proxy_url)


def install_open_webui(project_root: Path, proxy_url: str = "") -> None:
    python_path = create_venv(project_root, ".venv-webui")
    run_pip(python_path, ["install", "--upgrade", "pip"], project_root, proxy_url)
    run_pip(
        python_path,
        ["install", f"open-webui=={OPEN_WEBUI_VERSION}"],
        project_root,
        proxy_url,
    )


def external_command_env(proxy_url: str) -> dict[str, str] | None:
    normalized_proxy = normalize_proxy_url(proxy_url)
    if not normalized_proxy:
        return None
    env = os.environ.copy()
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        env[key] = normalized_proxy
    return env


def pull_ollama_models(profile: str, proxy_url: str = "") -> None:
    if shutil.which("ollama") is None:
        warn("ollama コマンドが見つかりません。Ollama インストール後にモデル取得を実行してください。")
        return
    llm_model = "gemma4:e4b-it-q4_K_M" if profile == "prod" else "gemma3:4b"
    for model in [llm_model, "nomic-embed-text"]:
        info(f"Ollama モデルを取得しています: {model}")
        subprocess.run(["ollama", "pull", model], check=True, env=external_command_env(proxy_url))


def set_ollama_keep_alive() -> None:
    if os.name != "nt":
        warn("OLLAMA_KEEP_ALIVE の自動設定は Windows の setx のみ対応です。")
        return
    subprocess.run(["setx", "OLLAMA_KEEP_ALIVE", "0"], check=False)


def install_dependencies(project_root: Path, answers: SetupAnswers) -> None:
    proxy_url = normalize_proxy_url(answers.proxy_url)
    if answers.mode == "remote-client":
        install_client_dependencies(project_root, proxy_url)
        return
    install_main_dependencies(project_root, answers.profile, proxy_url)
    install_open_webui(project_root, proxy_url)
    pull_ollama_models(answers.profile, proxy_url)
    if answers.set_ollama_keep_alive:
        set_ollama_keep_alive()


def apply_configuration(project_root: Path, answers: SetupAnswers) -> list[str]:
    import yaml

    template_path = project_root / "config" / f"settings.{answers.profile}.yaml"
    if not template_path.exists():
        raise FileNotFoundError(f"設定テンプレートが見つかりません: {template_path}")
    settings = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"設定テンプレートが不正です: {template_path}")

    configure_settings(settings, answers)
    ensure_directories(project_root, settings, answers)
    write_settings(project_root, settings)
    write_env(project_root, answers)
    write_active_profile(project_root, answers)
    if answers.mode == "remote-server":
        write_connection_file(answers)
    return build_remaining_tasks(answers)


def configure_settings(settings: dict[str, Any], answers: SetupAnswers) -> None:
    paths = settings.setdefault("paths", {})
    pipeline = settings.setdefault("pipeline", {})
    ui = settings.setdefault("ui", {})
    openwebui = settings.setdefault("openwebui", {})

    if answers.mode == "local":
        ui["local_service_management"] = True
        openwebui["base_url"] = f"http://localhost:{answers.webui_port}"
        openwebui.pop("bind_host", None)
    elif answers.mode == "remote-server":
        root = answers.shared_root
        paths["input_dir"] = shared_path(root, "input")
        paths["processed_dir"] = shared_path(root, "input", "processed")
        paths["input_text_dir"] = shared_path(root, "input_text")
        paths["processed_text_dir"] = shared_path(root, "input_text", "processed")
        paths["recordings_dir"] = shared_path(root, "input")
        paths["published_notes_dir"] = shared_path(root, "summaries")
        paths["remote_pipeline_state_file"] = shared_path(root, "status", "pipeline_state.json")
        paths["remote_control_dir"] = shared_path(root, "control")
        pipeline["state_file"] = shared_path(root, "status", "pipeline_state.json")
        ui["local_service_management"] = False
        openwebui["base_url"] = f"http://localhost:{answers.webui_port}"
        openwebui["bind_host"] = "0.0.0.0"
    elif answers.mode == "remote-client":
        root = answers.shared_root
        paths["recordings_dir"] = shared_path(root, "input")
        paths["input_dir"] = shared_path(root, "input")
        paths["processed_dir"] = shared_path(root, "input", "processed")
        paths["input_text_dir"] = shared_path(root, "input_text")
        paths["processed_text_dir"] = shared_path(root, "input_text", "processed")
        paths["published_notes_dir"] = shared_path(root, "summaries")
        paths["remote_pipeline_state_file"] = shared_path(root, "status", "pipeline_state.json")
        paths["remote_control_dir"] = shared_path(root, "control")
        ui["local_service_management"] = False
        openwebui["base_url"] = build_webui_url(answers.remote_host, answers.webui_port)
        openwebui.pop("bind_host", None)

    if answers.mode in {"local", "remote-server"}:
        if answers.knowledge_id and answers.openwebui_api_key:
            openwebui["enabled"] = True
            openwebui["knowledge_id"] = answers.knowledge_id
        else:
            openwebui["enabled"] = False
            if answers.knowledge_id:
                openwebui["knowledge_id"] = answers.knowledge_id


def ensure_directories(project_root: Path, settings: dict[str, Any], answers: SetupAnswers) -> None:
    paths = settings.get("paths", {})
    directory_keys = [
        "input_dir",
        "processed_dir",
        "transcripts_dir",
        "notes_dir",
        "chromadb_dir",
        "recordings_dir",
        "logs_dir",
        "published_notes_dir",
        "remote_control_dir",
        "input_text_dir",
        "processed_text_dir",
    ]
    for key in directory_keys:
        value = paths.get(key)
        if value:
            resolve_config_path(project_root, str(value)).mkdir(parents=True, exist_ok=True)
    state_file = paths.get("remote_pipeline_state_file") or settings.get("pipeline", {}).get(
        "state_file", "data/logs/pipeline_state.json"
    )
    resolve_config_path(project_root, str(state_file)).parent.mkdir(parents=True, exist_ok=True)
    control_dir = paths.get("remote_control_dir")
    if control_dir:
        resolve_config_path(project_root, str(control_dir),).joinpath("commands").mkdir(
            parents=True, exist_ok=True
        )
    if answers.mode in {"remote-server", "remote-client"}:
        for part in ["input", "input/processed", "input_text", "input_text/processed", "status", "summaries", "control", "control/commands"]:
            Path(shared_path(answers.shared_root, *part.split("/"))).mkdir(parents=True, exist_ok=True)


def write_settings(project_root: Path, settings: dict[str, Any]) -> None:
    import yaml

    settings_path = project_root / "config" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(settings, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(project_root: Path, answers: SetupAnswers) -> None:
    env_path = project_root / ".env"
    values = read_env(env_path)
    values.setdefault("HUGGINGFACE_TOKEN", "")
    values.setdefault("OPENWEBUI_API_KEY", "")
    if answers.huggingface_token:
        values["HUGGINGFACE_TOKEN"] = answers.huggingface_token
    if answers.openwebui_api_key:
        values["OPENWEBUI_API_KEY"] = answers.openwebui_api_key
    content = [
        "# PersonalRAG local secrets",
        "# Empty values are allowed. Fill them later when the feature is needed.",
    ]
    ordered_keys = ["HUGGINGFACE_TOKEN", "OPENWEBUI_API_KEY"]
    ordered_keys.extend(sorted(key for key in values if key not in ordered_keys))
    for key in ordered_keys:
        content.append(f"{key}={values.get(key, '')}")
    content.append("")
    env_path.write_text("\n".join(content), encoding="utf-8")


def write_active_profile(project_root: Path, answers: SetupAnswers) -> None:
    log_dir = project_root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "active_profile.txt").write_text(
        "\n".join(
            [
                answers.profile,
                f"mode: {answers.mode}",
                f"applied_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_connection_file(answers: SetupAnswers) -> None:
    control_dir = Path(shared_path(answers.shared_root, "control"))
    control_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": "remote-server",
        "shared_root": answers.shared_root,
        "remote_host": answers.remote_host,
        "webui_port": answers.webui_port,
        "webui_url": build_webui_url(answers.remote_host, answers.webui_port),
        "created_at": datetime.now().astimezone().isoformat(),
    }
    (control_dir / "connection.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_remaining_tasks(answers: SetupAnswers) -> list[str]:
    tasks: list[str] = []
    if answers.mode in {"local", "remote-server"} and not answers.huggingface_token:
        tasks.append("話者分離を使う場合は Hugging Face token を .env に追記してください。")
    if answers.mode in {"local", "remote-server"} and not (
        answers.openwebui_api_key and answers.knowledge_id
    ):
        tasks.append("Open WebUI Knowledge 自動同期を使う場合は API key と Knowledge ID を設定してください。")
    if answers.mode == "remote-server":
        tasks.append("Windows Firewall で TCP 3000 をドメイン/プライベートにだけ許可してください。")
        tasks.append("リモートPCで scripts/remote_service_agent.py を起動してください。")
    if answers.mode == "remote-client":
        tasks.append("NAS/共有フォルダの資格情報を Windows に記憶させてください。")
        tasks.append(f"ブラウザで {build_webui_url(answers.remote_host, answers.webui_port)} が開けるか確認してください。")
    return tasks


def invoke_apply_with_venv(project_root: Path, answers: SetupAnswers) -> list[str]:
    python_path = venv_python(project_root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".json") as f:
        json.dump(asdict(answers), f, ensure_ascii=False)
        answers_path = Path(f.name)
    try:
        result = subprocess.run(
            [str(python_path), str(Path(__file__).resolve()), "--apply-only", str(answers_path)],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    finally:
        try:
            answers_path.unlink()
        except OSError:
            pass
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []


def print_summary(answers: SetupAnswers, remaining_tasks: list[str]) -> None:
    print("")
    print("============================================================")
    print("セットアップ完了")
    print("============================================================")
    print(f"利用形態: {answers.mode}")
    print(f"プロファイル: {answers.profile}")
    if answers.mode in {"remote-server", "remote-client"}:
        print(f"共有ルート: {answers.shared_root}")
        print(f"Open WebUI: {build_webui_url(answers.remote_host, answers.webui_port)}")
    print("")
    print("起動方法:")
    if answers.mode == "remote-client":
        print("  録音GUI: PersonalRAG.cmd をダブルクリック")
    else:
        print("  録音GUI: PersonalRAG.cmd をダブルクリック")
        print("  Pipeline: .\\.venv\\Scripts\\python.exe scripts\\pipeline.py")
        print("  Open WebUI: サービス管理タブから起動")
    if remaining_tasks:
        print("")
        print("残りの確認:")
        for task in remaining_tasks:
            print(f"  - {task}")
    print("============================================================")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PersonalRAG setup wizard")
    parser.add_argument("mode", nargs="?", help="local / remote-server / remote-client / dev / prod")
    parser.add_argument("shared_root_arg", nargs="?", help="remote mode shared root shortcut")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    parser.add_argument("--shared-root", default="")
    parser.add_argument("--remote-host", default="")
    parser.add_argument("--webui-port", type=int, default=3000)
    parser.add_argument("--huggingface-token", default="")
    parser.add_argument("--openwebui-api-key", default="")
    parser.add_argument("--knowledge-id", default="")
    parser.add_argument(
        "--proxy-url",
        "--proxy",
        dest="proxy_url",
        default="",
        help="pip など外部アクセス用コマンドで使うプロキシURL",
    )
    parser.add_argument("--set-ollama-keep-alive", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument("--apply-only", default="")
    args = parser.parse_args(argv)
    if args.shared_root_arg and not args.shared_root:
        args.shared_root = args.shared_root_arg
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.apply_only:
        payload = json.loads(Path(args.apply_only).read_text(encoding="utf-8"))
        tasks = apply_configuration(PROJECT_ROOT, SetupAnswers(**payload))
        print(json.dumps(tasks, ensure_ascii=False))
        return 0

    answers = collect_answers(args)
    if args.skip_install:
        remaining_tasks = apply_configuration(PROJECT_ROOT, answers)
    else:
        if args.skip_models:
            original_pull = pull_ollama_models
            globals()["pull_ollama_models"] = lambda _profile, _proxy_url="": None
            try:
                install_dependencies(PROJECT_ROOT, answers)
            finally:
                globals()["pull_ollama_models"] = original_pull
        else:
            install_dependencies(PROJECT_ROOT, answers)
        remaining_tasks = invoke_apply_with_venv(PROJECT_ROOT, answers)
    print_summary(answers, remaining_tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
