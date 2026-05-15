"""test_config_loader.py
config_loader.py の update_settings_path 関数の単体テスト。

テスト内容:
    1. 正常系: 存在するキーを正しく更新できる
    2. 正常系: 書き戻し後に再読み込みしても値が正しい
    3. 正常系: ネストが深いキーも更新できる
    4. 異常系: ファイルが存在しない場合に FileNotFoundError が送出される
    5. 異常系: 存在しないキー階層を指定した場合に ValueError が送出される

実行:
    python scripts/test_config_loader.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

# scripts/ ディレクトリを sys.path に追加して本番モジュールを import できるようにする
sys.path.insert(0, str(Path(__file__).parent))

# テスト対象の関数を import する
from config_loader import update_settings_path

# --- テスト用の最小限 settings.yaml テンプレート ---
SAMPLE_SETTINGS: dict = {
    "paths": {
        "recordings_dir": "data/recordings",
        "input_dir": "data/input",
    },
    "recording": {
        "sample_rate": 16000,
        "channels": 1,
    },
    "deep": {
        "nested": {
            "key": "original_value",
        }
    },
}


def _make_temp_settings(tmp_dir: Path) -> tuple[Path, Path]:
    """テスト用の一時 settings.yaml と config/ ディレクトリを作成する。

    update_settings_path は PROJECT_ROOT/config/settings.yaml を対象にするため、
    PROJECT_ROOT をモンキーパッチする必要がある。ここでは config_loader モジュールの
    PROJECT_ROOT を tmp_dir に書き換えて使う。

    Args:
        tmp_dir: 一時ディレクトリのパス

    Returns:
        (config_dir, settings_path) のタプル
    """
    import config_loader
    config_loader.PROJECT_ROOT = tmp_dir  # type: ignore[assignment]

    config_dir = tmp_dir / "config"
    config_dir.mkdir(exist_ok=True)
    settings_path = config_dir / "settings.yaml"
    with settings_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(SAMPLE_SETTINGS, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return config_dir, settings_path


def test_update_existing_key() -> None:
    """テスト 1: 正常系 - 存在するキーを更新できる。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _, settings_path = _make_temp_settings(tmp_dir)

        # recordings_dir を変更する
        update_settings_path(["paths", "recordings_dir"], "Z:/PersonalRAG/input")

        # 書き戻したファイルを読み込んで検証する
        with settings_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["paths"]["recordings_dir"] == "Z:/PersonalRAG/input", (
            f"期待値: 'Z:/PersonalRAG/input', 実際: {data['paths']['recordings_dir']}"
        )
        # 他のキーが壊れていないことも確認する
        assert data["paths"]["input_dir"] == "data/input"
        assert data["recording"]["sample_rate"] == 16000
    print("テスト 1 PASS: 存在するキーを更新できる")


def test_reload_after_update() -> None:
    """テスト 2: 正常系 - 書き戻し後に再読み込みしても値が正しい。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _, settings_path = _make_temp_settings(tmp_dir)

        new_value = "\\\\nas-server\\share\\PersonalRAG\\input"
        update_settings_path(["paths", "recordings_dir"], new_value)

        # yaml.safe_load で再読み込みして値を確認する
        with settings_path.open("r", encoding="utf-8") as f:
            reloaded = yaml.safe_load(f)
        assert reloaded["paths"]["recordings_dir"] == new_value, (
            f"期待値: {new_value!r}, 実際: {reloaded['paths']['recordings_dir']!r}"
        )
    print("テスト 2 PASS: 書き戻し後に再読み込みしても値が正しい")


def test_deep_nested_key() -> None:
    """テスト 3: 正常系 - 深いネストのキーも更新できる。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _, settings_path = _make_temp_settings(tmp_dir)

        update_settings_path(["deep", "nested", "key"], "updated_value")

        with settings_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["deep"]["nested"]["key"] == "updated_value", (
            f"期待値: 'updated_value', 実際: {data['deep']['nested']['key']}"
        )
    print("テスト 3 PASS: 深いネストのキーも更新できる")


def test_file_not_found() -> None:
    """テスト 4: 異常系 - ファイルが存在しない場合は FileNotFoundError が送出される。"""
    import config_loader

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        config_loader.PROJECT_ROOT = Path(tmp_dir)  # type: ignore[assignment]
        # config/ ディレクトリを作らない（settings.yaml が存在しない状態にする）

        raised = False
        try:
            update_settings_path(["paths", "recordings_dir"], "/some/path")
        except FileNotFoundError:
            raised = True

        assert raised, "FileNotFoundError が送出されるべきだった"
    print("テスト 4 PASS: ファイル不在時に FileNotFoundError が送出される")


def test_invalid_key_path() -> None:
    """テスト 5: 異常系 - 存在しないキー階層を指定した場合は ValueError が送出される。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _make_temp_settings(tmp_dir)

        raised = False
        try:
            # "nonexistent_section" は settings.yaml に存在しないキー
            update_settings_path(["nonexistent_section", "recordings_dir"], "/path")
        except ValueError:
            raised = True

        assert raised, "ValueError が送出されるべきだった"
    print("テスト 5 PASS: 存在しないキー階層で ValueError が送出される")


def test_final_key_missing_raises_error() -> None:
    """テスト 6: 異常系 - 最終キーが存在しない場合は ValueError が送出される（allow_create デフォルト）。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _make_temp_settings(tmp_dir)

        raised = False
        try:
            # "typo_key" は settings.yaml の paths セクションに存在しないキー
            update_settings_path(["paths", "typo_key"], "/new/path")
        except ValueError:
            raised = True

        assert raised, "ValueError が送出されるべきだった"
    print("テスト 6 PASS: 最終キー未存在で ValueError が送出される")


def test_final_key_missing_allow_create() -> None:
    """テスト 7: 正常系 - allow_create=True なら最終キーが存在しなくても新規作成できる。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _, settings_path = _make_temp_settings(tmp_dir)

        # allow_create=True で存在しないキーに値を書き込む
        update_settings_path(["paths", "new_key"], "/added/path", allow_create=True)

        with settings_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["paths"]["new_key"] == "/added/path", (
            f"期待値: '/added/path', 実際: {data['paths'].get('new_key')}"
        )
        # 既存キーが壊れていないことも確認する
        assert data["paths"]["recordings_dir"] == "data/recordings"
    print("テスト 7 PASS: allow_create=True で最終キー未存在でも新規作成できる")


def run_all_tests() -> None:
    """全テストを実行する。1 つでも失敗したら AssertionError で停止する。"""
    print("=== config_loader.update_settings_path 単体テスト ===")
    test_update_existing_key()
    test_reload_after_update()
    test_deep_nested_key()
    test_file_not_found()
    test_invalid_key_path()
    test_final_key_missing_raises_error()
    test_final_key_missing_allow_create()
    print("\n全テスト PASS")


if __name__ == "__main__":
    run_all_tests()
