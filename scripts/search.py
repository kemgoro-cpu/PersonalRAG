"""search.py
Step 3（動作確認用）: ChromaDB に対して自然言語クエリで類似検索を行う CLI スクリプト。

使い方:
    python scripts/search.py "先週の会議で決まった納期は？"
    python scripts/search.py "ECUのテストについて" --top-k 3

設計メモ:
    Open WebUI の Knowledge 機能で検索できるようになるのが本番運用だが、
    自前 ChromaDB が正しく動いているかをCLIで素早く検証できるようにしておく。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import chromadb
import ollama

from config_loader import load_settings, resolve_path


def embed_query(query: str, embedding_cfg: dict[str, Any]) -> list[float]:
    """検索クエリを埋め込みベクター化する。"""
    client = ollama.Client(host=embedding_cfg["host"])
    response = client.embeddings(model=embedding_cfg["model"], prompt=query)
    return list(response["embedding"])


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(
        description="ChromaDB から類似検索",
        epilog=(
            "使用例:\n"
            "  python scripts/search.py \"先週の会議で決まった納期は？\"\n"
            "  python scripts/search.py \"ECUのテストについて\" --top-k 3\n"
            "  python scripts/search.py \"会議メモ\" --min-similarity 0.6\n"
            "\n"
            "※ データがまだない場合は先に `python scripts/ingest_db.py --all` を実行してください。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # query は任意引数（nargs="?"）にして、未指定時はヘルプを表示して終了する
    parser.add_argument("query", type=str, nargs="?", default=None, help="検索したい質問・キーワード")
    parser.add_argument(
        "--top-k", type=int, default=5, help="上位何件を返すか（デフォルト 5）"
    )
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="類似度の足切りしきい値（0.0〜1.0）。未指定時は設定ファイルの min_similarity を使用。0.0 で全件表示。",
    )
    args = parser.parse_args()

    # query が未指定の場合はヘルプを表示して正常終了する
    if args.query is None:
        parser.print_help()
        return 0

    settings = load_settings()
    chromadb_cfg = settings["chromadb"]
    embedding_cfg = settings["embedding"]
    chromadb_dir = resolve_path(settings["paths"]["chromadb_dir"])

    client = chromadb.PersistentClient(path=str(chromadb_dir))
    try:
        collection = client.get_collection(name=chromadb_cfg["collection_name"])
    except Exception as e:
        print(
            f"[error] コレクションが見つかりません: {e}\n"
            f"        まだ検索用のデータベースが作られていません。\n"
            f"        先に音声/テキストを処理してから、`python scripts/ingest_db.py --all` を実行してください。\n"
            f"        （詳しくは README の『使い方 C』を参照してください）",
            file=sys.stderr,
        )
        return 1

    print(f"[query] '{args.query}' を検索中...")
    query_vec = embed_query(args.query, embedding_cfg)

    results = collection.query(
        query_embeddings=[query_vec],
        n_results=args.top_k,
    )

    # results は各キーに「リストのリスト」が入る（複数クエリ対応のため）
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    if not docs:
        print("[result] ヒットなし")
        return 0

    # --min-similarity が未指定のときは設定ファイルの値を使う（なければ 0.0 = 全件表示）
    threshold: float = (
        args.min_similarity
        if args.min_similarity is not None
        else settings["chromadb"].get("min_similarity", 0.0)
    )

    # 類似度がしきい値以上のものだけ表示対象に絞り込む
    filtered = []
    for doc, meta, dist in zip(docs, metas, distances):
        similarity = 1.0 - dist if dist is not None else 0.0
        if similarity >= threshold:
            filtered.append((doc, meta, similarity))

    # しきい値フィルタで全件除外された場合は分かりやすいメッセージを表示
    if not filtered:
        print(
            f"[result] 関連の高い結果がありませんでした（しきい値 {threshold:.2f} 未満を除外）"
        )
        return 0

    print(f"\n=== 上位 {len(filtered)} 件（類似度 {threshold:.2f} 以上） ===\n")
    for rank, (doc, meta, similarity) in enumerate(filtered, start=1):
        source = meta.get("source_file", "?") if meta else "?"
        date = meta.get("date", "?") if meta else "?"
        print(f"--- [{rank}] 類似度 {similarity:.3f}  ({source} / {date}) ---")
        # 長すぎるチャンクは先頭 500 文字に省略表示
        snippet = doc if len(doc) <= 500 else doc[:500] + "..."
        print(snippet)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
