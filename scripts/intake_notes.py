"""
intake_notes.py — Notes intake adapter for L0 universal source layer.

Accepts freeform text (from stdin, string, or file) and creates a source
record with paragraph-level segments in sources.db.

Usage:
  from intake_notes import ingest_note
  result = ingest_note("Some observations about X...", project="personal-memory")

CLI:
  python3 intake_notes.py "inline text here" [--project <name>] [--title <title>]
  echo "piped text" | python3 intake_notes.py - [--project <name>]
  python3 intake_notes.py --file notes.txt [--project <name>]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from source_store import create_source, create_segments_batch


MIN_SEGMENT_CHARS = 20


def _split_paragraphs(text: str) -> list[dict]:
    """Split text into paragraph segments."""
    blocks = re.split(r'\n{2,}', text)
    segments = []
    pos = 0

    for block in blocks:
        block_stripped = block.strip()
        if len(block_stripped) < MIN_SEGMENT_CHARS:
            pos = text.find(block, pos) + len(block)
            continue

        char_start = text.find(block, pos)
        char_end = char_start + len(block)

        segments.append({
            "segment_type": "paragraph",
            "ordinal": len(segments),
            "content": block_stripped,
            "char_start": char_start,
            "char_end": char_end,
        })
        pos = char_end

    return segments


def ingest_note(
    text: str,
    project: str = None,
    title: str = None,
    source_time: str = None,
    file_path: str = None,
) -> dict:
    """Ingest a freeform note into sources.db.

    Returns dict with source_id, segment_count, char_count.
    """
    if not text or not text.strip():
        raise ValueError("Note text cannot be empty")

    # Default title from first line
    if not title:
        first_line = text.strip().split("\n")[0][:60]
        title = first_line.rstrip(".")

    source_id = create_source(
        source_type="note",
        title=title,
        raw_content=text,
        file_path=file_path,
        project=project,
        source_time=source_time,
        capture_quality="complete",
        metadata={"char_count": len(text)},
    )

    segments = _split_paragraphs(text)
    seg_ids = []
    if segments:
        seg_ids = create_segments_batch(source_id, segments)

    return {
        "source_id": source_id,
        "segment_count": len(seg_ids),
        "char_count": len(text),
        "title": title,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest a note into the vault")
    parser.add_argument("text", nargs="?", help="Note text (use '-' for stdin)")
    parser.add_argument("--file", help="Read note from file")
    parser.add_argument("--project", help="Project scope")
    parser.add_argument("--title", help="Note title (default: first line)")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text == "-" or (args.text is None and not sys.stdin.isatty()):
        text = sys.stdin.read()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide text, --file, or pipe stdin")

    result = ingest_note(text, project=args.project, title=args.title,
                         file_path=args.file)
    print(f"Ingested: {result['title']}")
    print(f"  Source ID:  {result['source_id']}")
    print(f"  Segments:   {result['segment_count']}")
    print(f"  Chars:      {result['char_count']:,}")
