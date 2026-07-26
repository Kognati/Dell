#!/usr/bin/env python3
"""Assign severity by clause type and deal structure, then rank.

A pure function of its input: no model call, no network, no filesystem beyond
reading one JSONL and writing another. Rules are documented in
references/severity-rules.md; this file is authoritative if they diverge.

Deal structure is the point. An asset deal must transfer contracts, so
anti-assignment language bites and change-of-control language often does not. A
stock deal keeps the entity and its contracts, so the reverse holds.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from schemas import (  # noqa: E402
    CLAUSE_TYPES,
    DEAL_STRUCTURES,
    SEVERITY_RANK,
    Finding,
    die,
    eprint,
    read_jsonl,
    write_jsonl,
)

BLOCKING = "blocking"
CONSENT = "consent_required"
NOTE = "note"

DEFAULT_MIN_CONFIDENCE = 0.5

# Base severity per (clause_type, deal_structure).
BASE_SEVERITY: Dict[Tuple[str, str], str] = {}
for _clause in CLAUSE_TYPES:
    for _structure in DEAL_STRUCTURES:
        BASE_SEVERITY[(_clause, _structure)] = NOTE
BASE_SEVERITY[("change_of_control", "stock")] = CONSENT
BASE_SEVERITY[("anti_assignment", "asset")] = CONSENT

# A consent right the counterparty can withhold for free is not a real consent
# path; it is a veto.
FREE_REFUSAL_RE = re.compile(r"sole|absolute|discretion|any\s+reason", re.IGNORECASE)


def _is_true(value: Any) -> bool:
    """Tolerate the model emitting "true"/"yes" instead of a JSON boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y")
    return False


def _stated(terms: Dict[str, Any], key: str) -> bool:
    value = terms.get(key)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return True
    return True


def severity_for(
    clause_type: str, terms: Dict[str, Any], deal_structure: str
) -> Tuple[str, str]:
    """Return (severity, reason). Reason is a short rule label for the memo."""
    severity = BASE_SEVERITY.get((clause_type, deal_structure), NOTE)
    reason = f"base:{clause_type}/{deal_structure}"

    # --- escalate to blocking ---------------------------------------------
    if clause_type == "change_of_control" and deal_structure == "stock":
        if _is_true(terms.get("termination_right")):
            return BLOCKING, "counterparty may terminate on a change of control"
    if clause_type == "anti_assignment" and deal_structure == "asset":
        if _is_true(terms.get("assignment_barred")) and not _stated(
            terms, "exceptions"
        ):
            return BLOCKING, "assignment barred outright with no exceptions"
        standard = terms.get("consent_standard")
        if isinstance(standard, str) and FREE_REFUSAL_RE.search(standard):
            return BLOCKING, "consent may be withheld at the counterparty's discretion"
    if clause_type == "indemnity_cap" and _is_true(terms.get("uncapped")):
        return BLOCKING, "liability expressly uncapped"

    # --- escalate to consent_required -------------------------------------
    if clause_type == "change_of_control" and deal_structure == "asset":
        if _is_true(terms.get("consent_required")):
            return CONSENT, "deemed-assignment consent may reach an asset transfer"

    # --- de-escalate to note ----------------------------------------------
    if clause_type == "change_of_control" and deal_structure == "stock":
        if not (
            _is_true(terms.get("consent_required"))
            or _is_true(terms.get("termination_right"))
            or _stated(terms, "notice_days")
        ):
            return NOTE, "trigger defined but no consequence stated in the span"
    if clause_type == "anti_assignment" and deal_structure == "asset":
        if not (
            _is_true(terms.get("assignment_barred"))
            or _is_true(terms.get("consent_required"))
        ):
            return NOTE, "successors-and-assigns language, no transfer restriction"

    return severity, reason


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assign severity to validated findings for a given deal structure "
            "and sort them into review order."
        )
    )
    parser.add_argument(
        "--input", required=True, help="validated.jsonl from validate.py"
    )
    parser.add_argument(
        "--deal-structure",
        required=True,
        choices=list(DEAL_STRUCTURES),
        help="stock: entity survives with its contracts; asset: contracts transfer",
    )
    parser.add_argument("--output", required=True, help="scored.jsonl to write")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help=(
            "findings below this are marked low_confidence and sorted last "
            f"within their severity (default {DEFAULT_MIN_CONFIDENCE}); "
            "severity itself is never demoted"
        ),
    )
    parser.add_argument(
        "--require-validated",
        action="store_true",
        default=True,
        help="refuse findings with validated != true (default: on)",
    )
    parser.add_argument(
        "--allow-unvalidated",
        dest="require_validated",
        action="store_false",
        help=(
            "score findings that skipped validation -- diagnostic only, output "
            "must not be reported"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    scored: List[Dict[str, Any]] = []
    skipped_unvalidated = 0
    unknown_clause = 0
    counts: Dict[str, int] = {name: 0 for name in SEVERITY_RANK}

    for payload in read_jsonl(Path(args.input)):
        try:
            finding = Finding.from_dict(payload)
        except ValueError as exc:
            die(f"{args.input}: {exc}")

        if args.require_validated and not finding.validated:
            skipped_unvalidated += 1
            continue
        if finding.clause_type not in CLAUSE_TYPES:
            unknown_clause += 1
            continue

        severity, reason = severity_for(
            finding.clause_type, finding.terms, args.deal_structure
        )
        finding.severity = severity
        counts[severity] = counts.get(severity, 0) + 1

        record = finding.to_dict()
        record["severity_reason"] = reason
        record["severity_rank"] = SEVERITY_RANK[severity]
        record["deal_structure"] = args.deal_structure
        record["low_confidence"] = (
            finding.confidence is not None
            and finding.confidence < args.min_confidence
        )
        scored.append(record)

    if not scored:
        if skipped_unvalidated:
            die(
                f"all {skipped_unvalidated} findings were unvalidated; run "
                "validate.py first (nothing may be reported unvalidated)",
                code=2,
            )
        die(f"{args.input} produced no scorable findings", code=2)

    scored.sort(
        key=lambda r: (
            r["severity_rank"],
            r["low_confidence"],
            r["doc_id"],
            r["page"],
            r["char_start"],
        )
    )

    written = write_jsonl(Path(args.output), scored)
    eprint(
        f"[score] {written} findings scored for a {args.deal_structure} deal -> "
        f"{args.output}"
    )
    eprint(
        "[score]   "
        + ", ".join(f"{name}={counts.get(name, 0)}" for name in SEVERITY_RANK)
    )
    if skipped_unvalidated:
        eprint(f"[score] skipped {skipped_unvalidated} unvalidated finding(s)")
    if unknown_clause:
        eprint(f"[score] skipped {unknown_clause} finding(s) with an unknown clause_type")
    return 0


if __name__ == "__main__":
    sys.exit(main())
