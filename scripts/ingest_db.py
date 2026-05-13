"""ingest_db.py
Step 3: data/notes/*.md を読み込み、ChromaDB に埋め込みベクターとして登録するスクリプト。

使い方:
    # 単一ファイル投入
    python scripts/ingest_db.py data/notes/meeting_001_2026-05-13_1030.md

    # data/notes/ 配下の .md を全件投入
    python scripts/ingest_db.py --all

設計メモ:
    - 1 ファイルを「見出し or 文字数」でチャンク分割
    - チャンクごとに nomic-embed-text で埋め込みを生成
    - ChromaDB（永続化ディレクトリ: data/chromadb/）に upsert
    - ドキュメント ID は "<filename>__chunk<index>" 形式（同名ファイル再投入時は上書きされる）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import chromadb
import ollama

from config_loader import load_settings, resolve_path


def strip_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Markdown 先頭の YAML フロントマターを分離する。

    Args:
        text: Markdown 全文。

    Returns:
        (フロントマターを簡易パースした辞書, 本文)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta_block = parts[1].strip()
    body = parts[2].lstrip("\n")
    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """テキストを文字数ベースでチャンク分割する。

    まず Markdown 見出し（## など）で大きく分け、それでも長い場合は
    max_chars 単位でスライドウィンドウ的に分割する。

    Args:
        text: 入力テキスト。
        max_chars: 1 チャンクあたりの最大文字数。
        overlap: 前チャンクと重ねる文字数（文脈ロス防止）。

    Returns:
        チャンクのリスト（空チャンクは除外）。
    """
    # 見出しで分割（## 以上、ただし先頭の `---` フロントマターは事前に剥がしてある前提）
    sections = re.split(r"\n(?=#{1,6}\s)", text)
    chunks: list[str] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= max_chars:
            chunks.append(section)
        else:
            # スライドウィンドウで分割
            start = 0
            step = max(1, max_chars - overlap)
            while start < len(section):
                chunks.append(section[start : start + max_chars])
                start += step
    return chunks


def embed_text(text: str, embedding_cfg: dict[str, Any]) -> list[float]:
    """Ollama で埋め込みベクターを生成する。

    Args:
        text: 入力テキスト。
        embedding_cfg: settings.yaml の embedding セクション。

    Returns:
        埋め込みベクター（float のリスト）。
    """
    client = ollama.Client(host=embedding_cfg["host"])
    response = client.embeddings(model=embedding_cfg["model"], prompt=text)
    return list(response["embedding"])


def get_or_create_collection(
    chromadb_cfg: dict[str, Any], persist_dir: Path
) -> chromadb.Collection:
    """ChromaDB のコレクションを取得（無ければ作成）する。"""
    client = chromadb.PersistentClient(path=str(persist_dir))
    return client.get_or_create_collection(
        name=chromadb_cfg["collection_name"],
        metadata={"hnsw:space": "cosine"},
    )


def ingest_file(
    md_path: Path,
    collection: chromadb.Collection,
    chromadb_cfg: dict[str, Any],
    embedding_cfg: dict[str, Any],
) -> int:
    """単一の Markdown ファイルを ChromaDB に投入する。

    Args:
        md_path: 投入する .md のパス。
        collection: ChromaDB コレクション。
        chromadb_cfg: settings.yaml の chromadb セクション。
        embedding_cfg: settings.yaml の embedding セクション。

    Returns:
        登録したチャンク数。
    """
    text = md_path.read_text(encoding="utf-8")
    meta, body = strip_frontmatter(text)

    chunks = chunk_text(
        body,
        max_chars=chromadb_cfg["chunk_chars"],
        overlap=chromadb_cfg["chunk_overlap_chars"],
    )
    if not chunks:
        print(f"[warn] チャンクが生成されませんでした: {md_path.name}")
        return 0

    ids: list[str] = []
    documents: list[str] = []
    embeddings: list[list[float]] = []
    metadatas: list[dict[str, Any]] = []

    for i, chunk in enumerate(chunks):
        chunk_id = f"{md_path.stem}__chunk{i:03d}"
        embedding = embed_text(chunk, embedding_cfg)
        ids.append(chunk_id)
        documents.append(chunk)
        embeddings.append(embedding)
        metadatas.append(
            {
                "source_file": md_path.name,
                "source_path": str(md_path),
                "chunk_index": i,
                # フロントマターのメタを継承（None は ChromaDB が嫌うので空文字に）
                "date": meta.get("date", ""),
                "keywords": meta.get("keywords", ""),
            }
        )

    # upsert: 同 ID が存在すれば置き換え、なければ追加
    collection.upsert(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"[ingest] {md_path.name}: {len(chunks)} チャンク登録")
    return len(chunks)


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(description="Markdown ノートを ChromaDB に投入")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("note", nargs="?", type=str, help="単一ファイル投入")
    group.add_argument(
        "--all", action="store_true", help="data/notes/ 配下の .md を全件投入"
    )
    args = parser.parse_args()

    settings = load_settings()
    chromadb_cfg = settings["chromadb"]
    embedding_cfg = settings["embedding"]
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    chromadb_dir = resolve_path(settings["paths"]["chromadb_dir"])
    chromadb_dir.mkdir(parents=True, exist_ok=True)

    collection = get_or_create_collection(chromadb_cfg, chromadb_dir)
    print(f"[chromadb] collection='{collection.name}' を使用")

    # 投入対象を決定
    targets: list[Path]
    if args.all:
        targets = sorted(notes_dir.glob("*.md"))
        if not targets:
            print(f"[error] {notes_dir} に .md ファイルがありません。", file=sys.stderr)
            return 1
    else:
        targets = [Path(args.note).resolve()]
        if not targets[0].exists():
            print(f"[error] ファイルが見つかりません: {targets[0]}", file=sys.stderr)
            return 1

    total_chunks = 0
    for md_path in targets:
        try:
            total_chunks += ingest_file(
                md_path, collection, chromadb_cfg, embedding_cfg
            )
        except Exception as e:
            # 1 ファイル失敗で全体停止させない
            print(f"[error] {md_path.name} の投入に失敗: {e}", file=sys.stderr)

    print(
        f"\n[done] 合計 {total_chunks} チャンクを登録しました。"
        f"  コレクション総件数: {collection.count()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
