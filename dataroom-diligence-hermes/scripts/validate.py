#!/usr/bin/env python3
"""Enforce the core invariant, in code, never in a prompt.

  No finding may be reported unless its `quote` appears verbatim in the source
  document at the stated page and character offsets.

Method: load the document full text written by ingest.py, slice
[char_start:char_end], and compare to `quote` after whitespace collapse only.
No fuzzy matching, no case folding, no punctuation stripping, no realignment.

Failures are dropped and counted. A quote that cannot be verified is never
repaired -- it is not a citation. The page number is the single exception and
is deliberately outside that guarantee; see check_finding.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402
    MAX_QUOTE_CHARS,
    Finding,
    collapse_ws,
    die,
    eprint,
    page_for_offset,
    read_fulltext,
    read_jsonl,
    read_pagemap,
    write_json,
    write_jsonl,
)

DEFAULT_MAX_DROP_RATE = 0.2


def check_finding(
    finding: Finding,
    full_text: Optional[str],
    page_spans: Optional[List[List[int]]],
) -> Tuple[bool, str]:
    """Return (validated, reason). `reason` is 'ok' when validated.

    The quote comparison is the gate and is never repaired. The page number is
    not part of it: it is derived data that this function can recompute from
    verified offsets, so a disagreement is corrected rather than fatal.
    """
    if full_text is None:
        return False, "missing_fulltext"
    if not finding.quote.strip():
        return False, "empty_quote"
    if len(finding.quote) > MAX_QUOTE_CHARS:
        return False, "quote_too_long"
    if finding.char_start < 0 or finding.char_end <= finding.char_start:
        return False, "invalid_offsets"
    if finding.char_end > len(full_text):
        return False, "offsets_past_end_of_document"

    source = full_text[finding.char_start : finding.char_end]
    if collapse_ws(source) != collapse_ws(finding.quote):
        return False, "quote_mismatch"

    # sweep.py has no page map, so it stamps a finding with the *candidate
    # chunk's* start page. A quote inside a chunk that straddles a page break is
    # therefore attributed one page early -- through no fault of the model. By
    # this line the quote has verified at a known offset, which makes
    # page_for_offset authoritative: correct the page and count the repair.
    # Discarding a verified quote over stale derived metadata loses real
    # findings and inflates the drop rate past its own threshold.
    if page_spans:
        expected = page_for_offset(page_spans, finding.char_start)
        if expected != finding.page:
            cited = finding.page
            finding.page = expected
            return True, f"ok:page_repaired_{cited}_to_{expected}"
    return True, "ok"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify every finding's quote against source text at the stated "
            "offsets. Unverified findings are dropped."
        )
    )
    parser.add_argument(
        "--findings", required=True, help="findings.jsonl from sweep.py"
    )
    parser.add_argument(
        "--fulltext-dir", required=True, help="sidecar directory from ingest.py"
    )
    parser.add_argument("--output", required=True, help="validated.jsonl to write")
    parser.add_argument(
        "--report", default="", help="optional JSON path for validation statistics"
    )
    parser.add_argument(
        "--failures",
        default="",
        help=(
            "JSONL path for dropped findings "
            "(default: alongside --output as *.failures.jsonl)"
        ),
    )
    parser.add_argument(
        "--max-drop-rate",
        type=float,
        default=DEFAULT_MAX_DROP_RATE,
        help=(
            "exit non-zero if the drop rate exceeds this "
            f"(default {DEFAULT_MAX_DROP_RATE}; pass 1.0 to never fail)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    fulltext_dir = Path(args.fulltext_dir).expanduser()
    if not fulltext_dir.is_dir():
        die(
            f"--fulltext-dir not found: {fulltext_dir}\n"
            "validation requires the sidecars written by ingest.py"
        )
    output_path = Path(args.output)
    failures_path = (
        Path(args.failures)
        if args.failures
        else output_path.with_suffix(".failures.jsonl")
    )

    text_cache: Dict[str, Optional[str]] = {}
    page_cache: Dict[str, Optional[List[List[int]]]] = {}

    validated: List[Finding] = []
    failed: List[Dict[str, Any]] = []
    by_reason: Dict[str, int] = {}
    page_repairs = 0
    by_doc: Dict[str, Dict[str, int]] = {}
    total = 0
    missing_pagemaps = set()

    for payload in read_jsonl(Path(args.findings)):
        total += 1
        try:
            finding = Finding.from_dict(payload)
        except ValueError as exc:
            failed.append({"reason": f"unreadable_finding:{exc}", "finding": payload})
            by_reason["unreadable_finding"] = by_reason.get("unreadable_finding", 0) + 1
            continue

        doc_id = finding.doc_id
        if doc_id not in text_cache:
            try:
                text_cache[doc_id] = read_fulltext(fulltext_dir, doc_id)
                page_cache[doc_id] = read_pagemap(fulltext_dir, doc_id)
            except ValueError:
                # safe_doc_id rejected it -- treat as missing rather than
                # touching a path built from untrusted input.
                text_cache[doc_id] = None
                page_cache[doc_id] = None
            if page_cache.get(doc_id) is None and text_cache.get(doc_id) is not None:
                missing_pagemaps.add(doc_id)

        ok, reason = check_finding(finding, text_cache[doc_id], page_cache[doc_id])
        bucket = by_doc.setdefault(doc_id, {"validated": 0, "dropped": 0})
        if ok:
            finding.validated = True
            validated.append(finding)
            bucket["validated"] += 1
            if reason.startswith("ok:page_repaired"):
                page_repairs += 1
        else:
            key = reason.split(":")[0]
            by_reason[key] = by_reason.get(key, 0) + 1
            bucket["dropped"] += 1
            record = finding.to_dict()
            record["validated"] = False
            record["validation_error"] = reason
            failed.append(record)

    if total == 0:
        die(f"{args.findings} contained no findings")

    written = write_jsonl(output_path, validated)
    write_jsonl(failures_path, failed)

    drop_rate = len(failed) / total
    report = {
        "total": total,
        "validated": written,
        "dropped": len(failed),
        "drop_rate": round(drop_rate, 4),
        "max_drop_rate": args.max_drop_rate,
        "threshold_exceeded": drop_rate > args.max_drop_rate,
        "by_reason": by_reason,
        "page_repairs": page_repairs,
        "by_doc": by_doc,
        "findings_input": str(args.findings),
        "fulltext_dir": str(fulltext_dir),
        "failures_file": str(failures_path),
    }
    if args.report:
        write_json(Path(args.report), report)

    eprint(
        f"[validate] {written}/{total} findings verified "
        f"({drop_rate * 100:.1f}% dropped) -> {output_path}"
    )
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        eprint(f"[validate]   dropped {count}: {reason}")
    if page_repairs:
        eprint(
            f"[validate] repaired {page_repairs} page number(s): the quote "
            "verified, but sweep.py had stamped the chunk's start page"
        )
    if failed:
        eprint(f"[validate] dropped findings written to {failures_path}")
    if missing_pagemaps:
        eprint(
            "[validate] warning: no .pages.json for "
            f"{len(missing_pagemaps)} document(s); page numbers were not checked"
        )
    if "missing_fulltext" in by_reason:
        eprint(
            "[validate] missing_fulltext means the doc_id in findings.jsonl has "
            "no sidecar: --fulltext-dir does not match the ingest run."
        )

    if drop_rate > args.max_drop_rate:
        eprint(
            f"error: validation drop rate {drop_rate * 100:.1f}% exceeds the "
            f"{args.max_drop_rate * 100:.1f}% threshold. "
            f"{output_path} was still written, but do not report these findings "
            "until the cause is understood (usually an offset bug in ingest.py "
            "or a mismatched --fulltext-dir)."
        )
        return 2
    if written == 0:
        die("no findings survived validation; nothing to score", code=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
