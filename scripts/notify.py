"""notify.py
Windows トースト通知（winotify）の薄いラッパーモジュール。

pipeline.py / record_gui.py など複数スクリプトから共通で使う通知関数を提供する。
- Windows 以外のOS では何もしない（プラットフォームチェック内蔵）
- winotify が未インストールでも静かに no-op（pip uninstall されてもクラッシュしない）
- 通知失敗による例外は全部握り潰す（通知失敗で本処理を止めない）

使い方:
    from notify import notify
    notify("PersonalRAG", "✓ 要約完了: sample.wav", "info")
    notify("PersonalRAG", "✗ transcribe 失敗: broken.wav\nエラー内容", "error")
"""

from __future__ import annotations

import sys

# winotify は Windows 専用ライブラリ。未インストール時も起動できるよう try/except で読む
try:
    from winotify import Notification as _WinotifyNotification
    _WINOTIFY_AVAILABLE = True
except ImportError:
    _WinotifyNotification = None  # type: ignore[assignment]
    _WINOTIFY_AVAILABLE = False

# PersonalRAG の通知で使う app_id（Windows の通知センターに表示される名前）
_APP_ID = "PersonalRAG"

# level → duration の変換テーブル
# winotify の icon 引数はファイルパスを期待するため使用しない（record_gui.py と同じ方針）。
# 代わりに重要度に応じて表示時間だけ変える:
#   error / warning → "long"（約25秒）: 見逃しを防ぐ
#   info → "short"（約5秒）: 通常の完了通知は短め
_LEVEL_TO_DURATION: dict[str, str] = {
    "info": "short",
    "warning": "long",
    "error": "long",
}


def notify(title: str, message: str, level: str = "info") -> None:
    """Windows トースト通知を表示する。

    Windows 以外のOS、または winotify が未インストールの場合は何もしない。
    通知の失敗（PowerShell 呼び出しエラー等）も全部握り潰すため、
    本処理が通知失敗によって停止することはない。

    Args:
        title: トーストのタイトル行（例: "PersonalRAG"）。
        message: トーストの本文（例: "✓ 要約完了: sample.wav"）。
        level: 重要度。"info" / "warning" / "error" のいずれか。
               duration（表示時間）の切り替えに使う。
               それ以外の値が渡された場合は "info"（short）として扱う。
    """
    # Windows 以外では何もしない（macOS/Linux での実行を無害にする）
    if sys.platform != "win32":
        return

    # winotify が使えなければ静かに no-op
    if not _WINOTIFY_AVAILABLE or _WinotifyNotification is None:
        return

    try:
        # level に応じた表示時間を決定（未知の level は "short" にフォールバック）
        duration = _LEVEL_TO_DURATION.get(level, "short")

        # 通知オブジェクトを作成して表示（record_gui.py と同じ呼び出し形式）
        # icon 引数は winotify がファイルパスを期待するため渡さず、デフォルトを使う
        toast = _WinotifyNotification(
            app_id=_APP_ID,
            title=title,
            msg=message,
            duration=duration,
        )
        toast.show()
    except Exception:
        # 通知失敗は全て無視（ディスクフル・PowerShell エラー等で本処理を止めない）
        pass


if __name__ == "__main__":
    # 単体テスト用: python scripts/notify.py を実行すると info トーストが出る（Windows のみ）
    print("[notify] テスト通知を送信します...")
    notify("PersonalRAG テスト", "notify.py の動作確認通知です。", "info")
    print("[notify] 完了（Windows 以外 / winotify 未インストールの場合は何も出ません）")
