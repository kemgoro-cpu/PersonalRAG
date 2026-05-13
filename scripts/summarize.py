"""summarize.py
Step 2: 文字起こしテキスト（.txt）を Ollama 経由で Gemma に投げ、
要約・ToDo・キーワード等を抽出して Markdown ファイル（.md）に保存するスクリプト。

使い方:
    python scripts/summarize.py <文字起こしテキストのパス>

例:
    python scripts/summarize.py data/transcripts/meeting_001_2026-05-13_1030.txt

LLM 出力:
    config/prompts/summarize.txt に従って LLM は JSON を返す想定。
    JSON パース失敗時は本文をそのまま埋め込む（落とさない）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama

from config_loader import PROJECT_ROOT, load_settings, resolve_path


def load_prompt_template() -> str:
    """要約用プロンプトテンプレートを読み込む。"""
    path = PROJECT_ROOT / "config" / "prompts" / "summarize.txt"
    return path.read_text(encoding="utf-8")


def call_ollama(
    prompt: str, llm_cfg: dict[str, Any]
) -> str:
    """Ollama API を叩いて応答テキストを取得する。

    Args:
        prompt: LLM に渡すプロンプト全体。
        llm_cfg: settings.yaml の llm セクション。

    Returns:
        LLM の応答テキスト（生の文字列）。
    """
    client = ollama.Client(host=llm_cfg["host"])
    print(f"[ollama] モデル={llm_cfg['model']} に問い合わせ中（応答待ち）...")
    response = client.generate(
        model=llm_cfg["model"],
        prompt=prompt,
        options={
            "temperature": llm_cfg.get("temperature", 0.3),
            "num_ctx": llm_cfg.get("num_ctx", 8192),
        },
        keep_alive=llm_cfg.get("keep_alive", 0),
    )
    return response.get("response", "")


def extract_json(text: str) -> dict[str, Any] | None:
    """LLM 応答から JSON 部分を抜き出してパースする。

    LLM が前後に余計な文を付けることがあるため、最初の `{` から最後の `}` までを
    抜き出して試行する。

    Args:
        text: LLM の応答文字列。

    Returns:
        パースに成功した辞書、失敗時は None。
    """
    # コードフェンスを取り除く
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.replace("```", "")

    # 最初の { から最後の } までを抽出
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        print(f"[warn] JSON パース失敗: {e}", file=sys.stderr)
        return None


def render_markdown(
    parsed: dict[str, Any] | None,
    raw_response: str,
    source_transcript: Path,
) -> str:
    """LLM 応答を Markdown 形式に整形する。

    JSON パースに成功した場合は構造化された Markdown を生成。
    失敗した場合は生応答をそのまま本文に貼り付ける（情報を失わない）。

    Args:
        parsed: パース済み辞書。None なら failure 時。
        raw_response: LLM の生応答。
        source_transcript: 元の文字起こしファイルパス。

    Returns:
        Markdown 文字列。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if parsed is None:
        # フォールバック: 生応答をそのまま貼り付け
        return (
            f"---\n"
            f"source: {source_transcript.name}\n"
            f"date: {today}\n"
            f"generated_at: {now}\n"
            f"parse_status: failed\n"
            f"---\n\n"
            f"# 要約（JSON パース失敗、生応答）\n\n"
            f"```\n{raw_response.strip()}\n```\n"
        )

    summary = parsed.get("summary", "")
    topics = parsed.get("topics", []) or []
    decisions = parsed.get("decisions", []) or []
    todos = parsed.get("todos", []) or []
    questions = parsed.get("questions", []) or []
    keywords = parsed.get("keywords", []) or []

    # フロントマター（Open WebUI / Obsidian / 全文検索用にメタ情報を保持）
    frontmatter = [
        "---",
        f"source: {source_transcript.name}",
        f"date: {today}",
        f"generated_at: {now}",
        f"keywords: [{', '.join(keywords)}]",
        "---",
        "",
    ]

    body: list[str] = []
    body.append(f"# 要約 ({today})\n")
    body.append(f"**ソース**: `{source_transcript.name}`\n")

    body.append("## サマリー\n")
    body.append(summary if summary else "_（要約が空です）_")
    body.append("")

    if topics:
        body.append("## 主要トピック\n")
        for t in topics:
            body.append(f"- {t}")
        body.append("")

    if decisions:
        body.append("## 決定事項\n")
        for d in decisions:
            body.append(f"- {d}")
        body.append("")

    if todos:
        body.append("## ToDo\n")
        body.append("| 内容 | 担当 | 期限 |")
        body.append("|---|---|---|")
        for t in todos:
            task = t.get("task", "")
            assignee = t.get("assignee", "") or "—"
            due = t.get("due", "") or "—"
            body.append(f"| {task} | {assignee} | {due} |")
        body.append("")

    if questions:
        body.append("## 未解決の論点\n")
        for q in questions:
            body.append(f"- {q}")
        body.append("")

    if keywords:
        body.append("## キーワード\n")
        body.append(" / ".join(f"`{k}`" for k in keywords))
        body.append("")

    return "\n".join(frontmatter + body) + "\n"


def build_output_path(transcript_path: Path, notes_dir: Path) -> Path:
    """出力 Markdown のパスを決定する。

    例: meeting_001_2026-05-13_1030.txt → meeting_001_2026-05-13_1030.md
    """
    return notes_dir / f"{transcript_path.stem}.md"


def main() -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(
        description="文字起こしテキストを要約して Markdown 保存する"
    )
    parser.add_argument(
        "transcript", type=str, help="入力テキスト（文字起こし結果）のパス"
    )
    args = parser.parse_args()

    transcript_path = Path(args.transcript).resolve()
    if not transcript_path.exists():
        print(f"[error] ファイルが見つかりません: {transcript_path}", file=sys.stderr)
        return 1

    settings = load_settings()
    llm_cfg = settings["llm"]
    notes_dir = resolve_path(settings["paths"]["notes_dir"])
    notes_dir.mkdir(parents=True, exist_ok=True)

    transcript_text = transcript_path.read_text(encoding="utf-8")
    if not transcript_text.strip():
        print("[error] 文字起こしテキストが空です。", file=sys.stderr)
        return 2

    # プロンプト組み立て
    template = load_prompt_template()
    prompt = template.replace("{TRANSCRIPT}", transcript_text)

    # LLM 呼び出し
    try:
        raw_response = call_ollama(prompt, llm_cfg)
    except Exception as e:
        print(
            f"[error] Ollama 呼び出しに失敗: {e}\n"
            f"        Ollama サーバが起動しているか、モデル '{llm_cfg['model']}' が pull 済みか確認してください。",
            file=sys.stderr,
        )
        return 3

    if not raw_response.strip():
        print("[error] LLM から空応答が返りました。", file=sys.stderr)
        return 4

    # JSON 抽出 → Markdown 化
    parsed = extract_json(raw_response)
    markdown = render_markdown(parsed, raw_response, transcript_path)

    output_path = build_output_path(transcript_path, notes_dir)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"[output] 保存しました: {output_path}")
    if parsed is None:
        print(
            "[warn] JSON パースに失敗したため、生応答を保存しました。\n"
            "       プロンプトの調整やモデル変更を検討してください。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
