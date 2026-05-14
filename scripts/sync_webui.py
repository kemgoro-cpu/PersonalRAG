"""sync_webui.py
Step 5: data/notes/*.md を Open WebUI の Knowledge に自動アップロードするスクリプト。

使い方:
    # 未同期の .md を全件 sync（初回バルクアップロードや漏れ回収に使う）
    python scripts/sync_webui.py

    # 単一ファイルだけ sync
    python scripts/sync_webui.py data/notes/some_meeting.md

    # Knowledge の一覧と ID を表示（knowledge_id 設定時に使う）
    python scripts/sync_webui.py --list-knowledges

    # 新しい Knowledge を作成して ID を表示
    python scripts/sync_webui.py --create-knowledge "PersonalRAG"

    # インデックスを無視して全件再 sync
    python scripts/sync_webui.py --reupload-all

仕組み:
    1. POST /api/v1/files/  でファイルをアップロード → file_id 取得
    2. GET  /api/v1/files/{file_id}/process/status  で処理完了を polling
    3. POST /api/v1/knowledge/{knowledge_id}/file/add  で Knowledge に紐付け
    重複防止のため data/.webui_synced.json に file_id と sha256 を記録する。
    同名ファイルでも sha256 が変わっていれば再アップロードする。

終了コード:
    0: 成功
    1: 設定エラー（API キー未設定 / knowledge_id 空 / 引数エラー）
    2: 接続エラー（WebUI 停止中など）
    3: タイムアウト（polling が時間内に完了しなかった）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

# scripts/ の親がプロジェクトルートなので、そこから config_loader を使う
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import PROJECT_ROOT, load_env, load_settings, resolve_path


# 重複防止インデックスのパス（data/.webui_synced.json）
INDEX_PATH: Path = PROJECT_ROOT / "data" / ".webui_synced.json"


# ---------------------------------------------------------------------------
# ユーティリティ: ログ出力
# ---------------------------------------------------------------------------

def log_info(msg: str) -> None:
    """情報ログを出力する。pipeline.py 経由で実行されると標準出力はロガーに吸収される。"""
    print(f"[sync] {msg}", flush=True)


def log_warn(msg: str) -> None:
    """警告ログを出力する。"""
    print(f"[warn] {msg}", flush=True)


def log_error(msg: str) -> None:
    """エラーログを出力する（stderr に出すことで pipeline.py のログと区別できる）。"""
    print(f"[error] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# ユーティリティ: ハッシュ計算
# ---------------------------------------------------------------------------

def calc_sha256(path: Path) -> str:
    """ファイルの SHA-256 ハッシュを計算して16進数文字列で返す。

    同名ファイルの内容変更を検出するために使う。

    Args:
        path: ハッシュを計算するファイルのパス。

    Returns:
        SHA-256 の16進数文字列（例: "abc123..."）。
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        # 大きいファイルでもメモリを使い過ぎないよう 64KB ずつ読む
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# インデックス管理
# ---------------------------------------------------------------------------

def load_index(path: Path) -> dict[str, Any]:
    """data/.webui_synced.json を読み込む。ファイルが無ければ空 dict を返す。

    Args:
        path: インデックスファイルのパス。

    Returns:
        {filename: {file_id, synced_at, sha256}} の辞書。
    """
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_warn(f"インデックス読み込み失敗（空 dict で継続）: {e}")
        return {}


def save_index(path: Path, index: dict[str, Any]) -> None:
    """data/.webui_synced.json を上書き保存する。

    atomic write（一時ファイル → rename）でファイル破損を防ぐ。

    Args:
        path: インデックスファイルのパス。
        index: 保存するインデックス辞書。
    """
    # data/ ディレクトリが無ければ作成
    path.parent.mkdir(parents=True, exist_ok=True)
    # 一時ファイルに書いてからリネームすることで、
    # 書き込み途中に異常終了してもデータが壊れない
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# API クライアント
# ---------------------------------------------------------------------------

def get_api_client(api_key: str) -> requests.Session:
    """OPENWEBUI_API_KEY を Authorization ヘッダーに設定した requests.Session を返す。

    Session オブジェクトにヘッダーを持たせることで、
    各リクエストで毎回 headers= を書かなくて済む。

    Args:
        api_key: Open WebUI で生成した API キー（sk-xxx 形式）。

    Returns:
        認証済みの requests.Session。
    """
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}"})
    return session


def list_knowledges(client: requests.Session, base_url: str) -> list[dict[str, Any]]:
    """GET /api/v1/knowledge/ で Knowledge 一覧を取得する。

    Args:
        client: get_api_client() で作った Session。
        base_url: WebUI の URL（例: "http://localhost:3000"）。

    Returns:
        Knowledge 情報の辞書のリスト。
        各要素のキー例: {"id": "...", "name": "..."}
        ※ 実 API レスポンスで要確認: キー名が異なる場合はここを修正する。
    """
    url = f"{base_url}/api/v1/knowledge/"
    response = client.get(url, timeout=10)
    response.raise_for_status()
    # レスポンスがリストで返ってくる場合とオブジェクト（{"data": [...]}）の場合がある
    # 実 API レスポンスで要確認
    data = response.json()
    if isinstance(data, list):
        return data
    # {"data": [...]} 形式だった場合
    return data.get("data", [])


def create_knowledge(client: requests.Session, base_url: str, name: str) -> str:
    """POST /api/v1/knowledge/create で新規 Knowledge を作成して ID を返す。

    Args:
        client: get_api_client() で作った Session。
        base_url: WebUI の URL。
        name: 作成する Knowledge の名前。

    Returns:
        作成された Knowledge の ID 文字列。
        ※ レスポンス JSON のキー名は実 API で要確認（"id" を仮定）。
    """
    url = f"{base_url}/api/v1/knowledge/create"
    payload = {
        "name": name,
        "description": f"{name} - PersonalRAG 自動同期",
    }
    response = client.post(url, json=payload, timeout=10)
    response.raise_for_status()
    result = response.json()
    # ※ 実 API レスポンスで要確認: "id" キーが存在することを前提としている
    knowledge_id = result.get("id", "")
    if not knowledge_id:
        raise ValueError(
            f"Knowledge 作成レスポンスに 'id' が見つかりません。実レスポンス: {result}"
        )
    return knowledge_id


def upload_file(
    client: requests.Session, base_url: str, md_path: Path
) -> str:
    """POST /api/v1/files/ でファイルをアップロードして file_id を返す。

    multipart/form-data で .md ファイルを送信する。

    Args:
        client: get_api_client() で作った Session。
        base_url: WebUI の URL。
        md_path: アップロードする .md ファイルのパス。

    Returns:
        アップロードされたファイルの ID 文字列。
        ※ レスポンス JSON のキー名は実 API で要確認（"id" を仮定）。
    """
    url = f"{base_url}/api/v1/files/"
    with md_path.open("rb") as f:
        # multipart/form-data で送る。ファイルの MIME タイプは text/markdown を指定
        files = {"file": (md_path.name, f, "text/markdown")}
        response = client.post(url, files=files, timeout=30)
    response.raise_for_status()
    result = response.json()
    # ※ 実 API レスポンスで要確認: "id" キーが存在することを前提としている
    file_id = result.get("id", "")
    if not file_id:
        raise ValueError(
            f"ファイルアップロードレスポンスに 'id' が見つかりません。実レスポンス: {result}"
        )
    return file_id


def wait_until_processed(
    client: requests.Session,
    base_url: str,
    file_id: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> bool:
    """GET /api/v1/files/{file_id}/process/status を polling し、処理完了を待つ。

    Open WebUI はファイルアップロード後に非同期で埋め込み処理を行う。
    これを確認せずに Knowledge に追加しようとするとエラーになるため、
    完了まで繰り返し確認する。

    Args:
        client: get_api_client() で作った Session。
        base_url: WebUI の URL。
        file_id: polling 対象のファイル ID。
        timeout_seconds: 最大待機秒数。超えると False を返す。
        interval_seconds: polling の間隔（秒）。

    Returns:
        処理が完了した（または status エンドポイントが 404 の場合も完了扱い）なら True、
        タイムアウトしたら False。

    Note:
        status エンドポイントが存在しない WebUI バージョンでは 404 が返ることがある。
        その場合は「処理なし = 完了」とみなして True を返す。
        ※ status レスポンスのキー名は実 API で要確認（"status" キーと "completed" 値を仮定）。
    """
    url = f"{base_url}/api/v1/files/{file_id}/process/status"
    elapsed = 0
    while elapsed < timeout_seconds:
        try:
            response = client.get(url, timeout=10)
            # status エンドポイントが存在しない WebUI バージョン対策
            if response.status_code == 404:
                log_warn(
                    f"処理ステータス確認エンドポイントが存在しません（404）。"
                    f"処理完了とみなして続行します（file_id={file_id}）"
                )
                return True
            response.raise_for_status()
            result = response.json()
            # ※ 実 API レスポンスで要確認:
            #   "status": "completed" が完了を示すと仮定している
            status = result.get("status", "")
            if status in ("completed", "done", "processed"):
                log_info(f"処理完了確認 (file_id={file_id}, status={status})")
                return True
            if status in ("failed", "error"):
                log_error(
                    f"ファイル処理がエラーになりました (file_id={file_id}, status={status})"
                )
                return False
            # まだ処理中 → 少し待って再 polling
            log_info(
                f"処理中... (file_id={file_id}, status={status!r}, "
                f"経過={elapsed}s / 最大={timeout_seconds}s)"
            )
        except requests.exceptions.RequestException as e:
            log_warn(f"polling 中に通信エラー: {e}")
            # 通信エラーはリトライしない方針なので、そのままループを抜ける
            return False

        time.sleep(interval_seconds)
        elapsed += interval_seconds

    # ループを抜けた = タイムアウト
    log_warn(
        f"処理完了 polling がタイムアウトしました "
        f"（file_id={file_id}, timeout={timeout_seconds}s）"
    )
    return False


def add_to_knowledge(
    client: requests.Session, base_url: str, knowledge_id: str, file_id: str
) -> None:
    """POST /api/v1/knowledge/{knowledge_id}/file/add でファイルを Knowledge に紐付ける。

    Args:
        client: get_api_client() で作った Session。
        base_url: WebUI の URL。
        knowledge_id: 紐付け先の Knowledge ID。
        file_id: 紐付けるファイルの ID。
    """
    url = f"{base_url}/api/v1/knowledge/{knowledge_id}/file/add"
    payload = {"file_id": file_id}
    response = client.post(url, json=payload, timeout=10)
    response.raise_for_status()


# ---------------------------------------------------------------------------
# 同期メイン処理
# ---------------------------------------------------------------------------

def _make_index_key(md_path: Path, notes_dir: Path) -> str:
    """インデックス辞書のキー文字列を生成する。

    notes_dir からの相対パスを POSIX 形式（スラッシュ区切り）で返す。
    これにより、別サブディレクトリの同名ファイル（例: notes/2026-05/meeting.md と
    notes/2026-06/meeting.md）が同一キーに衝突する問題を防ぐ。

    ※ インデックスファイル（data/.webui_synced.json）は新形式（相対パス）のみ対応。
       旧形式（ベース名のみ）からの自動変換は行わない。

    Args:
        md_path: インデックスキーを作りたいファイルのパス。
        notes_dir: notes ディレクトリのパス（相対パスの起点）。

    Returns:
        POSIX 形式の相対パス文字列（例: "2026-05/meeting.md"）。
        notes_dir 配下に無い特殊ケースではベース名をフォールバックとして返す。
    """
    try:
        # resolve() で Windows/Linux 両方の絶対パスに統一してから relative_to を計算
        rel = md_path.resolve().relative_to(notes_dir.resolve())
        # as_posix() で OS によらずスラッシュ区切りの文字列にする
        return rel.as_posix()
    except ValueError:
        # notes_dir 配下に無い場合（通常は発生しないが念のためフォールバック）
        return md_path.name


def sync_single_file(
    md_path: Path,
    settings: dict[str, Any],
    index: dict[str, Any],
    force: bool = False,
    notes_dir: Path | None = None,
) -> bool:
    """単一 .md ファイルを upload → poll → add の 3 ステップで Knowledge に同期する。

    Args:
        md_path: 同期する .md ファイルのパス。
        settings: load_settings() で読み込んだ設定辞書。
        index: load_index() で読み込んだ同期済みインデックス。（この関数内で更新する）
        force: True なら sha256 一致でもスキップせず再アップロードする。
        notes_dir: インデックスキーの起点となる notes ディレクトリ。
                   None の場合はベース名をフォールバックとして使う。

    Returns:
        同期成功したら True、スキップまたは失敗なら False。
        （スキップの場合も「問題なし」なので呼び出し側では True 相当として扱ってよい）
    """
    webui_cfg = settings.get("openwebui", {})
    base_url: str = webui_cfg.get("base_url", "http://localhost:3000").rstrip("/")
    knowledge_id: str = webui_cfg.get("knowledge_id", "")
    poll_timeout: int = int(webui_cfg.get("poll_timeout_seconds", 60))
    poll_interval: int = int(webui_cfg.get("poll_interval_seconds", 2))

    # knowledge_id が未設定ならスキップ（エラーではなく設定不備として扱う）
    if not knowledge_id:
        log_error(
            "settings.yaml の openwebui.knowledge_id が未設定です。\n"
            "  取得方法: python scripts/sync_webui.py --list-knowledges\n"
            "  作成方法: python scripts/sync_webui.py --create-knowledge \"PersonalRAG\""
        )
        return False

    # API キー取得（.env から読む）
    load_env()
    api_key = os.environ.get("OPENWEBUI_API_KEY", "")
    if not api_key:
        log_error(
            "OPENWEBUI_API_KEY が未設定です。.env に追記してください（.env.example 参照）"
        )
        return False

    # sha256 計算 → 既に同期済みかチェック
    try:
        current_sha256 = calc_sha256(md_path)
    except OSError as e:
        log_error(f"ファイル読み込み失敗: {md_path} → {e}")
        return False

    # インデックスキーは notes_dir からの相対パス（POSIX形式）を使う。
    # notes_dir が None の場合（単一ファイル sync など）はベース名をフォールバックとする。
    if notes_dir is not None:
        index_key = _make_index_key(md_path, notes_dir)
    else:
        index_key = md_path.name

    # 表示用には相対パス or ベース名を使う（ログが読みやすくなる）
    filename = index_key
    entry = index.get(index_key, {})
    if not force and entry.get("sha256") == current_sha256:
        log_info(f"スキップ（既に同期済み・内容変更なし）: {filename}")
        return True  # スキップも「成功」扱い

    client = get_api_client(api_key)
    log_info(f"同期開始: {filename}")

    # TODO: 同名ファイル再アップロード時、WebUI 側の旧 file_id を削除してから再 upload すると
    #       Knowledge 内で重複ファイルが残らなくて済む。
    #       現時点では DELETE API のパスが不明なため未実装。
    #       重複が気になる場合は WebUI 側で手動削除してください。
    #       削除 API が判明次第: DELETE /api/v1/files/{old_file_id} あたりを試すこと。
    old_file_id = entry.get("file_id", "")
    if old_file_id:
        log_warn(
            f"旧 file_id={old_file_id} の削除は未実装です。"
            f"WebUI 側で手動削除を検討してください。"
        )

    # Step 1: ファイルアップロード
    try:
        file_id = upload_file(client, base_url, md_path)
        log_info(f"アップロード完了: {filename} → file_id={file_id}")
    except requests.exceptions.ConnectionError as e:
        log_error(f"WebUI への接続に失敗しました（起動しているか確認）: {e}")
        return False
    except requests.exceptions.HTTPError as e:
        log_error(f"アップロード HTTP エラー: {e}")
        return False
    except (requests.exceptions.RequestException, ValueError) as e:
        log_error(f"アップロード失敗: {e}")
        return False

    # Step 2: 処理完了を polling で待つ
    if not wait_until_processed(client, base_url, file_id, poll_timeout, poll_interval):
        # polling 失敗でもインデックスは更新しない（次回リトライできるように）
        log_error(f"処理完了待ちに失敗。Knowledge への追加をスキップ: {filename}")
        return False

    # Step 3: Knowledge に追加
    try:
        add_to_knowledge(client, base_url, knowledge_id, file_id)
        log_info(f"Knowledge への追加完了: {filename} → knowledge_id={knowledge_id}")
    except requests.exceptions.HTTPError as e:
        log_error(f"Knowledge 追加 HTTP エラー: {e}")
        return False
    except (requests.exceptions.RequestException, ValueError) as e:
        log_error(f"Knowledge 追加失敗: {e}")
        return False

    # 成功 → インデックス更新（time.strftime を使い標準ライブラリのみで記録）
    # キーは notes_dir からの相対パス（POSIX形式）= index_key を使う
    index[index_key] = {
        "file_id": file_id,
        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sha256": current_sha256,
    }
    log_info(f"同期完了: {filename}")
    return True


def sync_all_pending(
    notes_dir: Path, settings: dict[str, Any], force: bool = False
) -> tuple[int, int]:
    """notes_dir 配下の .md を全件走査し、未同期 or 変更されたものを sync する。

    Args:
        notes_dir: data/notes/ などの .md が入ったディレクトリ。
        settings: load_settings() で読み込んだ設定辞書。
        force: True なら既に同期済みのものも再アップロードする（--reupload-all 用）。

    Returns:
        (成功件数, 失敗件数) のタプル。
        成功にはスキップ（既同期・内容変更なし）は含まない。
        失敗件数が 1 以上のとき、呼び出し側は終了コード 1 を返すべき。
    """
    index = load_index(INDEX_PATH)
    # rglob でサブディレクトリも再帰的に探索する（フラット構成でも動く）
    md_files = sorted(notes_dir.rglob("*.md"))

    if not md_files:
        log_info(f"{notes_dir} に .md ファイルがありません。")
        return 0, 0

    log_info(f"{len(md_files)} 件の .md ファイルを確認中...")

    success_count = 0
    failed_count = 0

    for md_path in md_files:
        # インデックスキーは notes_dir からの相対パス（POSIX形式）を使う。
        # ベース名だけだとサブディレクトリの同名ファイルが衝突するため。
        index_key = _make_index_key(md_path, notes_dir)
        entry = index.get(index_key, {})
        already_synced = (
            not force
            and entry.get("sha256") == (calc_sha256(md_path) if md_path.exists() else "")
        )

        if already_synced:
            log_info(f"スキップ（既に同期済み）: {index_key}")
            continue

        result = sync_single_file(md_path, settings, index, force=force, notes_dir=notes_dir)
        if result:
            success_count += 1
        else:
            # sync_single_file 内でエラーログ出力済み。ここでは失敗件数だけ増やす
            failed_count += 1

        # ファイルごとにインデックスを保存する（途中失敗でも完了分を保持するため）
        save_index(INDEX_PATH, index)

    return success_count, failed_count


# ---------------------------------------------------------------------------
# CLI コマンド処理
# ---------------------------------------------------------------------------

def cmd_list_knowledges(settings: dict[str, Any]) -> int:
    """--list-knowledges: Knowledge 一覧と ID を表示する。

    Returns:
        終了コード（0: 成功, 1: エラー, 2: 接続失敗）。
    """
    load_env()
    api_key = os.environ.get("OPENWEBUI_API_KEY", "")
    if not api_key:
        log_error("OPENWEBUI_API_KEY が未設定です。.env に追記してください（.env.example 参照）")
        return 1

    webui_cfg = settings.get("openwebui", {})
    base_url = webui_cfg.get("base_url", "http://localhost:3000").rstrip("/")
    client = get_api_client(api_key)

    try:
        knowledges = list_knowledges(client, base_url)
    except requests.exceptions.ConnectionError as e:
        log_error(f"WebUI への接続に失敗しました（起動しているか確認）: {e}")
        return 2
    except requests.exceptions.RequestException as e:
        log_error(f"Knowledge 一覧取得エラー: {e}")
        return 1

    if not knowledges:
        print("Knowledge が 1 件もありません。")
        print("作成するには: python scripts/sync_webui.py --create-knowledge \"PersonalRAG\"")
        return 0

    print(f"Knowledge 一覧（{len(knowledges)} 件）:")
    print("-" * 50)
    for k in knowledges:
        # ※ 実 API レスポンスで要確認: "id" と "name" キーを仮定
        kid = k.get("id", "（id なし）")
        kname = k.get("name", "（name なし）")
        print(f"  ID  : {kid}")
        print(f"  名前: {kname}")
        print("-" * 50)
    print("→ 使いたい Knowledge の ID を settings.yaml の openwebui.knowledge_id に貼ってください。")
    return 0


def cmd_create_knowledge(settings: dict[str, Any], name: str) -> int:
    """--create-knowledge NAME: 新しい Knowledge を作成して ID を表示する。

    Returns:
        終了コード（0: 成功, 1: エラー, 2: 接続失敗）。
    """
    load_env()
    api_key = os.environ.get("OPENWEBUI_API_KEY", "")
    if not api_key:
        log_error("OPENWEBUI_API_KEY が未設定です。.env に追記してください（.env.example 参照）")
        return 1

    webui_cfg = settings.get("openwebui", {})
    base_url = webui_cfg.get("base_url", "http://localhost:3000").rstrip("/")
    client = get_api_client(api_key)

    try:
        knowledge_id = create_knowledge(client, base_url, name)
    except requests.exceptions.ConnectionError as e:
        log_error(f"WebUI への接続に失敗しました（起動しているか確認）: {e}")
        return 2
    except (requests.exceptions.RequestException, ValueError) as e:
        log_error(f"Knowledge 作成エラー: {e}")
        return 1

    print(f"Knowledge を作成しました。")
    print(f"  名前: {name}")
    print(f"  ID  : {knowledge_id}")
    print()
    print(f"→ 以下を config/settings.yaml の openwebui.knowledge_id に貼ってください:")
    print(f"  knowledge_id: \"{knowledge_id}\"")
    return 0


def cmd_sync_file(md_path_str: str, settings: dict[str, Any]) -> int:
    """単一ファイルを sync する。

    Returns:
        終了コード（0: 成功, 1: 設定エラー, 2: 接続エラー, 3: タイムアウト）。
    """
    md_path = Path(md_path_str).resolve()
    if not md_path.exists():
        log_error(f"ファイルが見つかりません: {md_path}")
        return 1

    webui_cfg = settings.get("openwebui", {})
    if not webui_cfg.get("knowledge_id", ""):
        log_error(
            "settings.yaml の openwebui.knowledge_id が未設定です。\n"
            "  取得: python scripts/sync_webui.py --list-knowledges\n"
            "  作成: python scripts/sync_webui.py --create-knowledge \"PersonalRAG\""
        )
        return 1

    load_env()
    if not os.environ.get("OPENWEBUI_API_KEY", ""):
        log_error("OPENWEBUI_API_KEY が未設定です。.env に追記してください（.env.example 参照）")
        return 1

    # notes_dir を渡してインデックスキーを相対パス形式に統一する。
    # こうすることで、単一ファイル sync と全件 sync でインデックスキーが一致する。
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    index = load_index(INDEX_PATH)
    success = sync_single_file(md_path, settings, index, notes_dir=notes_dir)
    save_index(INDEX_PATH, index)

    if success:
        return 0
    # 失敗の種類を細かく返したいが sync_single_file 内でログ出力済みのため 1 で統一
    return 1


def cmd_sync_all(settings: dict[str, Any], force: bool = False) -> int:
    """全件 sync（または force 再アップロード）を実行する。

    Returns:
        終了コード（0: 完全成功, 1: 一部失敗または設定エラー）。
    """
    webui_cfg = settings.get("openwebui", {})
    if not webui_cfg.get("knowledge_id", ""):
        log_error(
            "settings.yaml の openwebui.knowledge_id が未設定です。\n"
            "  取得: python scripts/sync_webui.py --list-knowledges\n"
            "  作成: python scripts/sync_webui.py --create-knowledge \"PersonalRAG\""
        )
        return 1

    load_env()
    if not os.environ.get("OPENWEBUI_API_KEY", ""):
        log_error("OPENWEBUI_API_KEY が未設定です。.env に追記してください（.env.example 参照）")
        return 1

    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    if not notes_dir.exists():
        log_error(f"notes ディレクトリが存在しません: {notes_dir}")
        return 1

    label = "強制再アップロード" if force else "未同期分の同期"
    log_info(f"{label} を開始: {notes_dir}")

    success_count, failed_count = sync_all_pending(notes_dir, settings, force=force)

    # sync_all_pending は (成功件数, 失敗件数) を返す。
    # 1 件でも失敗があれば終了コード 1 を返してシェルに失敗を通知する。
    # （成功件数 0 でも失敗件数 0 = 対象ファイルなし は正常扱い）
    log_info(f"完了: 成功={success_count} 件, 失敗={failed_count} 件")
    return 1 if failed_count > 0 else 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI 引数を解析して各コマンドを実行する。"""
    parser = argparse.ArgumentParser(
        description="Open WebUI Knowledge に .md ファイルを自動同期するツール"
    )

    # mutually_exclusive_group を使って「どれか 1 つ（または引数なし）」にする
    # 引数なしは「全件 sync」として動作する
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "file",
        nargs="?",
        metavar="FILE",
        help="単一の .md ファイルを sync（省略時は notes/ 全件 sync）",
    )
    group.add_argument(
        "--list-knowledges",
        action="store_true",
        help="Knowledge 一覧と ID を表示する（初期セットアップ時に使う）",
    )
    group.add_argument(
        "--create-knowledge",
        metavar="NAME",
        help="新しい Knowledge を作成して ID を表示する",
    )
    group.add_argument(
        "--reupload-all",
        action="store_true",
        help="インデックスを無視して notes/ 全件を強制再アップロードする",
    )

    args = parser.parse_args()

    # 設定ファイル読み込み
    try:
        settings = load_settings()
    except FileNotFoundError as e:
        log_error(str(e))
        return 1

    # --- 設定値バリデーション（起動時に早期エラーを出す）---
    # poll_interval_seconds / poll_timeout_seconds が 0 以下だと
    # polling ループの elapsed が進まず無限ループになるため、ここで弾く。
    webui_cfg = settings.get("openwebui", {})
    poll_timeout_val = webui_cfg.get("poll_timeout_seconds", 60)
    poll_interval_val = webui_cfg.get("poll_interval_seconds", 2)
    if poll_timeout_val <= 0:
        log_error(
            "settings.yaml の openwebui.poll_timeout_seconds は正の数を指定してください"
            f"（現在の値: {poll_timeout_val}）"
        )
        return 1
    if poll_interval_val <= 0:
        log_error(
            "settings.yaml の openwebui.poll_interval_seconds は正の数を指定してください"
            f"（現在の値: {poll_interval_val}）"
        )
        return 1

    # openwebui セクションが有効かチェック
    if not webui_cfg.get("enabled", False):
        log_warn(
            "settings.yaml の openwebui.enabled が false です。"
            "同期をスキップします。"
        )
        return 0

    # コマンド分岐
    if args.list_knowledges:
        return cmd_list_knowledges(settings)
    elif args.create_knowledge:
        return cmd_create_knowledge(settings, args.create_knowledge)
    elif args.reupload_all:
        return cmd_sync_all(settings, force=True)
    elif args.file:
        return cmd_sync_file(args.file, settings)
    else:
        # 引数なし → 全件 sync（最も頻繁に使う）
        return cmd_sync_all(settings, force=False)


if __name__ == "__main__":
    sys.exit(main())
