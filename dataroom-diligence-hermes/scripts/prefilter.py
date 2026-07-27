#!/usr/bin/env python3
"""Reduce chunks.jsonl to candidates.jsonl by regex trigger match.

High-recall and unashamedly noisy: the goal is to drop ~90% of chunks while
keeping essentially every true positive. Precision is the model's job.

This script does NOT classify. `clause_hints` records which trigger vocabulary
fired, which is a suggestion to the model and never a conclusion. If you find
yourself writing clause classification logic here, it belongs in the prompt.

The trigger vocabulary lives in references/clause-taxonomy.md, inside fenced
```regex blocks under each clause-type heading. That file is the single source
of truth; this script only parses it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402
    CLAUSE_TYPES,
    Candidate,
    Chunk,
    die,
    eprint,
    read_jsonl,
    stable_id,
    write_jsonl,
)

DEFAULT_TAXONOMY = (
    Path(__file__).resolve().parent.parent / "references" / "clause-taxonomy.md"
)
DEFAULT_MAX_TRIGGERS = 12

_HEADING_RE = re.compile(r"^#{2,4}\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\s*$")
_FENCE_RE = re.compile(r"^```\s*([A-Za-z0-9_-]*)\s*$")


# --------------------------------------------------------------------------
# Trigger vocabulary
# --------------------------------------------------------------------------


def load_triggers(path: Path) -> Dict[str, List[str]]:
    """Parse ```regex blocks out of the taxonomy markdown, keyed by clause type."""
    if not path.exists():
        die(
            f"clause taxonomy not found: {path}\n"
            "pass --taxonomy with the path to references/clause-taxonomy.md"
        )
    vocabulary: Dict[str, List[str]] = {}
    current_clause: Optional[str] = None
    in_regex_block = False

    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fence = _FENCE_RE.match(raw)
        if fence:
            if in_regex_block:
                in_regex_block = False
            elif fence.group(1).lower() == "regex":
                if current_clause is None:
                    die(f"{path}:{lineno}: regex block before any clause heading")
                in_regex_block = True
            continue

        if in_regex_block:
            pattern = raw.strip()
            if not pattern or pattern.startswith("#"):
                continue
            vocabulary.setdefault(current_clause, []).append(pattern)
            continue

        heading = _HEADING_RE.match(raw)
        if heading and heading.group(1) in CLAUSE_TYPES:
            current_clause = heading.group(1)

    if in_regex_block:
        die(f"{path}: unterminated ```regex block")
    if not vocabulary:
        die(f"{path}: no ```regex blocks found under any clause-type heading")

    missing = [clause for clause in CLAUSE_TYPES if clause not in vocabulary]
    if missing:
        eprint(
            f"[prefilter] warning: no triggers defined for {', '.join(missing)} "
            f"in {path}"
        )
    return vocabulary


def compile_triggers(
    vocabulary: Dict[str, List[str]], clauses: Sequence[str]
) -> Dict[str, List[Pattern[str]]]:
    compiled: Dict[str, List[Pattern[str]]] = {}
    for clause in clauses:
        patterns = vocabulary.get(clause, [])
        built: List[Pattern[str]] = []
        for pattern in patterns:
            try:
                built.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                die(
                    f"invalid trigger regex for {clause}: {pattern!r}: {exc}\n"
                    "fix the pattern in references/clause-taxonomy.md"
                )
        if built:
            compiled[clause] = built
    if not compiled:
        die("no usable trigger patterns for the requested clause types")
    return compiled


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def match_chunk(
    text: str, compiled: Dict[str, List[Pattern[str]]], max_triggers: int
) -> Tuple[List[str], List[str]]:
    """Return (triggers, clause_hints) for one chunk. Order is deterministic:
    clause hints follow CLAUSE_TYPES order, triggers follow match order."""
    hints: List[str] = []
    triggers: List[str] = []
    seen_triggers = set()
    for clause in CLAUSE_TYPES:
        patterns = compiled.get(clause)
        if not patterns:
            continue
        hit = False
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            hit = True
            snippet = " ".join(match.group(0).split()).lower()[:60]
            if snippet and snippet not in seen_triggers:
                seen_triggers.add(snippet)
                triggers.append(snippet)
        if hit:
            hints.append(clause)
    return triggers[:max_triggers], hints


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select candidate windows from chunks.jsonl using the regex trigger "
            "vocabulary in references/clause-taxonomy.md. High recall by design."
        )
    )
    parser.add_argument("--input", required=True, help="chunks.jsonl from ingest.py")
    parser.add_argument("--output", required=True, help="candidates.jsonl to write")
    parser.add_argument(
        "--clauses",
        default="",
        help=(
            "comma-separated subset of clause types to screen for "
            "(default: all eight)"
        ),
    )
    parser.add_argument(
        "--taxonomy",
        default=str(DEFAULT_TAXONOMY),
        help="path to clause-taxonomy.md (default: ../references/clause-taxonomy.md)",
    )
    parser.add_argument(
        "--max-triggers",
        type=int,
        default=DEFAULT_MAX_TRIGGERS,
        help=(
            "cap on trigger strings recorded per candidate, to keep prompts "
            f"short (default {DEFAULT_MAX_TRIGGERS})"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if args.clauses.strip():
        requested = [c.strip() for c in args.clauses.split(",") if c.strip()]
        unknown = [c for c in requested if c not in CLAUSE_TYPES]
        if unknown:
            die(
                f"unknown clause type(s): {', '.join(unknown)}\n"
                f"valid values: {', '.join(CLAUSE_TYPES)}"
            )
    else:
        requested = list(CLAUSE_TYPES)

    vocabulary = load_triggers(Path(args.taxonomy).expanduser())
    compiled = compile_triggers(vocabulary, requested)
    eprint(
        f"[prefilter] screening for {len(compiled)} clause type(s) with "
        f"{sum(len(v) for v in compiled.values())} patterns from {args.taxonomy}"
    )

    candidates: List[Candidate] = []
    total = 0
    per_clause: Dict[str, int] = {clause: 0 for clause in compiled}

    for payload in read_jsonl(Path(args.input)):
        total += 1
        try:
            chunk = Chunk.from_dict(payload)
        except ValueError as exc:
            die(f"{args.input}: {exc}")
        triggers, hints = match_chunk(chunk.text, compiled, args.max_triggers)
        if not hints:
            continue
        for clause in hints:
            per_clause[clause] = per_clause.get(clause, 0) + 1
        candidates.append(
            Candidate(
                candidate_id=stable_id("cand", chunk.chunk_id),
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                doc_title=chunk.doc_title,
                page=chunk.page,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                text=chunk.text,
                triggers=triggers,
                clause_hints=hints,
            )
        )

    if total == 0:
        die(f"{args.input} contained no chunks")

    written = write_jsonl(Path(args.output), candidates)
    kept = (written / total) * 100 if total else 0.0
    eprint(
        f"[prefilter] {total} chunks -> {written} candidates "
        f"({kept:.1f}% kept, {100 - kept:.1f}% dropped)"
    )
    for clause in CLAUSE_TYPES:
        if clause in per_clause:
            eprint(f"[prefilter]   {clause}: {per_clause[clause]} candidates")
    if written == 0:
        eprint(
            "[prefilter] warning: no candidates. Check that ingest.py produced "
            "real text (scanned PDFs yield none) before blaming the vocabulary."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
