"""config_loader.py
PersonalRAG の設定ファイル（config/settings.yaml）と環境変数（.env）を
読み込むための共通ヘルパーモジュール。

すべてのスクリプトでこのモジュールを import して使うことで、
設定値の参照を一箇所に集約し、修正が楽になる。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# プロジェクトのルートディレクトリ（scripts/ の親）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def load_settings() -> dict[str, Any]:
    """config/settings.yaml を読み込んで dict として返す。

    Returns:
        設定全体を表すネストされた辞書。
    """
    settings_path = PROJECT_ROOT / "config" / "settings.yaml"
    if not settings_path.exists():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {settings_path}\n"
            "config/settings.yaml が存在することを確認してください。"
        )
    with settings_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> None:
    """プロジェクトルートの .env を読み込んで環境変数に展開する。

    .env が無くてもエラーにはしない（HF トークンが不要なケースもあるため）。
    """
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def get_huggingface_token() -> str | None:
    """環境変数から Hugging Face トークンを取得する。

    Returns:
        トークン文字列。未設定の場合は None。
    """
    load_env()
    return os.environ.get("HUGGINGFACE_TOKEN")


def resolve_path(relative_path: str) -> Path:
    """settings.yaml に書かれた相対パスをプロジェクトルート基準の絶対パスに変換する。

    Args:
        relative_path: 例 "data/input"

    Returns:
        絶対パスの Path オブジェクト。
    """
    return PROJECT_ROOT / relative_path
