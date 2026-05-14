"""search_lib.py
ChromaDB に対するセマンティック検索ロジックを、GUI などから import して
使えるライブラリ関数として提供するモジュール。

scripts/search.py は CLI 専用構造（argparse + main()）のため、
GUI から直接 import して呼び出すことができない。
このファイルは search.py の検索ロジックを関数化したもので、
note_viewer.py などから利用する。

設計メモ:
    - このモジュール自体は chromadb / ollama を import しない。
      呼び出し元が「遅延 import する」設計にするため、
      search_semantic() を呼んだ時点で初めて import する。
    - search.py の CLI としての動作は変更しない（後方互換を維持）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def search_semantic(
    query: str,
    top_k: int = 10,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """ChromaDB に対してセマンティック検索を行い、結果リストを返す。

    ChromaDB / ollama の import はこの関数内で行う（遅延 import）。
    note_viewer.py が起動した時点では import されないため、
    テキスト検索のみで使う場合は ChromaDB の起動遅延（3〜5 秒）が発生しない。

    Args:
        query: 検索したい質問・キーワード（自然言語）。
        top_k: 上位何件を返すか（デフォルト 10）。
        settings: load_settings() で読み込んだ設定辞書。
                  None の場合はこの関数内で load_settings() を呼ぶ。

    Returns:
        検索結果のリスト。各要素は以下のキーを持つ辞書:
            - "source_file" (str): ノートのファイル名（拡張子付き）
            - "note_path"   (Path | None): ノートファイルの絶対パス（特定できた場合）
            - "similarity"  (float): 類似度（0.0〜1.0、高いほど類似）
            - "snippet"     (str): チャンクの先頭 300 文字
            - "date"        (str): ノートの date メタデータ（不明時は空文字）
        ヒットなしの場合は空リストを返す。
        ChromaDB や Ollama に接続できない場合も例外を伝播させず空リストを返す。
    """
    # --- 遅延 import: この関数が呼ばれて初めて重いライブラリを読み込む ---
    try:
        import chromadb
        import ollama
    except ImportError as exc:
        raise ImportError(
            f"セマンティック検索に必要なライブラリが見つかりません: {exc}\n"
            "`pip install chromadb ollama` を実行してください。"
        ) from exc

    from config_loader import load_settings, resolve_path

    if settings is None:
        settings = load_settings()

    chromadb_cfg: dict[str, Any] = settings["chromadb"]
    embedding_cfg: dict[str, Any] = settings["embedding"]
    chromadb_dir: Path = resolve_path(settings["paths"]["chromadb_dir"])
    notes_dir: Path = resolve_path(settings["paths"]["notes_dir"])

    # ChromaDB クライアントの初期化
    client = chromadb.PersistentClient(path=str(chromadb_dir))

    try:
        collection = client.get_collection(name=chromadb_cfg["collection_name"])
    except Exception:
        # コレクションが存在しない（まだ ingest_db.py を実行していない等）
        return []

    # クエリのベクトル化（Ollama の埋め込みモデルを使用）
    try:
        ollama_client = ollama.Client(host=embedding_cfg["host"])
        response = ollama_client.embeddings(
            model=embedding_cfg["model"], prompt=query
        )
        query_vec: list[float] = list(response["embedding"])
    except Exception:
        # Ollama が起動していない等のエラー
        return []

    # ChromaDB へのクエリ
    try:
        results = collection.query(
            query_embeddings=[query_vec],
            n_results=top_k,
        )
    except Exception:
        return []

    # results は各キーに「リストのリスト」が入る（複数クエリ対応のため）
    docs: list[str] = results.get("documents", [[]])[0]
    metas: list[dict[str, Any]] = results.get("metadatas", [[]])[0]
    distances: list[float] = results.get("distances", [[]])[0]

    if not docs:
        return []

    # 結果を整形して返す
    output: list[dict[str, Any]] = []
    for doc, meta, dist in zip(docs, metas, distances):
        source_file: str = meta.get("source_file", "") if meta else ""
        date: str = meta.get("date", "") if meta else ""

        # コサイン距離 → 類似度（1 - distance）
        similarity: float = 1.0 - dist if dist is not None else 0.0

        # ノートファイルの絶対パスを特定する（path traversal 対策付き）
        # ingest_db.py は source_file にファイル名（拡張子付き）を入れる
        # ノートは .md 拡張子なので source_file をそのまま notes_dir と結合する
        note_path: Path | None = None
        if source_file:
            # --- 二重防御 (1) basename だけを使う ---
            # source_file が "../../evil" 等のパストラバーサル攻撃文字列でも、
            # .name で「ファイル名部分のみ」を取り出せばディレクトリ部分を無効化できる。
            # data/notes はフラット構造なので basename だけで問題ない。
            safe_name = Path(source_file).name
            candidate = notes_dir / safe_name

            # --- 二重防御 (2) resolve() 後に notes_dir の配下かチェック ---
            # シンボリックリンクで notes_dir 外に逃げようとするケースを防ぐ。
            # resolve(strict=True) は実体パスに解決し、存在しないなら FileNotFoundError を出す。
            # relative_to() は配下でなければ ValueError を出す。
            try:
                resolved = candidate.resolve(strict=True)
                notes_root = notes_dir.resolve(strict=True)
                resolved.relative_to(notes_root)  # ValueError → notes_dir 外
                note_path = resolved
            except (FileNotFoundError, ValueError):
                # 存在しないか notes_dir 配下でない → スキップ
                note_path = None

            # note_path が見つからない場合、拡張子が .txt になっている古いメタを .md で試す
            if note_path is None:
                stem = Path(safe_name).stem
                candidate_md = notes_dir / f"{stem}.md"
                try:
                    resolved_md = candidate_md.resolve(strict=True)
                    notes_root_md = notes_dir.resolve(strict=True)
                    resolved_md.relative_to(notes_root_md)
                    note_path = resolved_md
                except (FileNotFoundError, ValueError):
                    note_path = None

        # スニペット（先頭 300 文字）
        snippet: str = doc[:300] + "..." if len(doc) > 300 else doc

        output.append(
            {
                "source_file": source_file,
                "note_path": note_path,
                "similarity": similarity,
                "snippet": snippet,
                "date": date,
            }
        )

    return output
