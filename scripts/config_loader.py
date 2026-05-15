"""config_loader.py
PersonalRAG の設定ファイル（config/settings.yaml）と環境変数（.env）を
読み込むための共通ヘルパーモジュール。

すべてのスクリプトでこのモジュールを import して使うことで、
設定値の参照を一箇所に集約し、修正が楽になる。
"""

from __future__ import annotations

import os
import shutil
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


def update_settings_path(
    key_path: list[str], value: str, allow_create: bool = False
) -> None:
    """config/settings.yaml の指定キーを更新して書き戻す。

    コメントは YAML に保存されないため、書き戻し後にコメントは失われる。
    書き戻し前に settings.yaml.bak を作成する（既存の .bak は上書き）。
    書き戻し失敗時は .bak から自動復元する。

    使用例:
        update_settings_path(["paths", "recordings_dir"], "Z:\\\\PersonalRAG\\\\input")

    Args:
        key_path:     更新するキーのパス。例: ["paths", "recordings_dir"]
        value:        新しい値（文字列）。
        allow_create: True にすると最終キーが存在しない場合でも新規作成する。
                      デフォルト False（存在しないキーへの typo 混入を防ぐため）。

    Raises:
        FileNotFoundError: settings.yaml が存在しない場合。
        ValueError: key_path が空、指定のキー階層が存在しない（dict でない）場合、
                    または allow_create=False かつ最終キーが存在しない場合。
        OSError: ファイル書き込みに失敗した場合（.bak から復元済み）。
    """
    if not key_path:
        raise ValueError("key_path は 1 要素以上必要です")

    settings_path = PROJECT_ROOT / "config" / "settings.yaml"
    bak_path = settings_path.with_suffix(".yaml.bak")

    if not settings_path.exists():
        raise FileNotFoundError(
            f"設定ファイルが見つかりません: {settings_path}"
        )

    # --- 1. バックアップ作成 ---
    shutil.copy2(settings_path, bak_path)

    # --- 2. 現在の設定を読み込む ---
    with settings_path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    # --- 3. 指定キーを更新 ---
    # key_path = ["paths", "recordings_dir"] なら data["paths"]["recordings_dir"] を更新する
    node = data
    for key in key_path[:-1]:
        # 途中のキーが存在しない、または dict でない場合はエラー
        if key not in node or not isinstance(node[key], dict):
            raise ValueError(
                f"settings.yaml にキー '{key}' が存在しないか、dict ではありません "
                f"（key_path={key_path}）"
            )
        node = node[key]

    final_key = key_path[-1]
    # allow_create=False（デフォルト）のとき、最終キーが存在しなければ ValueError
    # → typo や誤ったキー名での silent な新規作成を防ぐ
    if final_key not in node and not allow_create:
        raise ValueError(
            f"settings.yaml に最終キー '{final_key}' が存在しません "
            f"（key_path={key_path}）。"
            " 新規キーを追加したい場合は allow_create=True を指定してください。"
        )
    node[final_key] = value

    # --- 4. 書き戻す ---
    # allow_unicode=True: 日本語パスをエスケープせずそのまま書く
    # sort_keys=False:    元のキー順を保持する
    # default_flow_style=False: ブロックスタイルで読みやすく書く
    new_content = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )

    try:
        with settings_path.open("w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as exc:
        # 書き込み失敗 → バックアップから復元してエラーを再送出
        try:
            shutil.copy2(bak_path, settings_path)
        except Exception:
            pass  # 復元失敗時は元の OSError だけを報告する
        raise OSError(
            f"settings.yaml の書き込みに失敗しました: {exc}\n"
            f"バックアップから復元しました: {bak_path}"
        ) from exc


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
    """settings.yaml に書かれたパスをプロジェクトルート基準の絶対パスに変換する。

    絶対パス（UNC `\\\\server\\share\\...` やドライブ文字付き `Z:\\...` 等）が
    渡された場合は、プロジェクトルートを付け足さずそのまま返す。
    これにより NAS や共有ドライブを `settings.yaml` に直接書ける
    （リモートPC運用時に input フォルダを社内 NAS に置きたいケース）。

    Args:
        relative_path: 例 "data/input" や "\\\\nas-server\\share\\PersonalRAG\\input"

    Returns:
        絶対パスの Path オブジェクト。
    """
    p = Path(relative_path)
    # UNC パス・ドライブ文字付きパスは Windows でも is_absolute() が True を返す
    if p.is_absolute():
        return p
    return PROJECT_ROOT / relative_path
