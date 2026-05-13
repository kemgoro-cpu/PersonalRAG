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
    parser = argparse.ArgumentParser(description="ChromaDB から類似検索")
    parser.add_argument("query", type=str, help="検索したい質問・キーワード")
    parser.add_argument(
        "--top-k", type=int, default=5, help="上位何件を返すか（デフォルト 5）"
    )
    args = parser.parse_args()

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
            f"        先に `python scripts/ingest_db.py --all` で投入してください。",
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

    print(f"\n=== 上位 {len(docs)} 件 ===\n")
    for rank, (doc, meta, dist) in enumerate(zip(docs, metas, distances), start=1):
        source = meta.get("source_file", "?") if meta else "?"
        date = meta.get("date", "?") if meta else "?"
        # コサイン距離 → 類似度概算（1 - distance）
        similarity = 1.0 - dist if dist is not None else 0.0
        print(f"--- [{rank}] 類似度 {similarity:.3f}  ({source} / {date}) ---")
        # 長すぎるチャンクは先頭 500 文字に省略表示
        snippet = doc if len(doc) <= 500 else doc[:500] + "..."
        print(snippet)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
