# dataroom-diligence

A CLI pipeline that reviews a folder of commercial contracts for M&A
due-diligence issues and emits a ranked issues list where every citation is
verified against source text.

`SKILL.md` is for an agent running the pipeline. This file is for a human running
it from a shell.

Nothing here imports an agent framework. The scripts are plain `argparse` CLIs
that read and write flat files; only `sweep.py` touches the network.

## Install

Three packages plus the standard library. Pinned, because this is authored on one
architecture and deployed to aarch64.

```bash
# Host, with network:
pip install -r requirements.txt

# Target machine or sandbox, no egress:
pip install --no-index --find-links <wheelhouse> -r requirements.txt
```

Build the wheelhouse ahead of time (see the comments in `requirements.txt` for
the `pip download` invocation, including transitive dependencies). Nothing is
ever installed at runtime; a missing import is a staging problem, and every
script says so when it hits one.

## Run

```bash
mkdir -p out

python scripts/ingest.py    --input ./contracts --output out/chunks.jsonl \
                            --fulltext-dir out/fulltext
python scripts/prefilter.py --input out/chunks.jsonl --output out/candidates.jsonl
python scripts/sweep.py     --candidates out/candidates.jsonl \
                            --output out/findings.jsonl \
                            --endpoint http://localhost:8000/v1 \
                            --errors out/sweep-errors.jsonl
python scripts/validate.py  --findings out/findings.jsonl \
                            --fulltext-dir out/fulltext \
                            --output out/validated.jsonl \
                            --report out/validation-report.json
python scripts/score.py     --input out/validated.jsonl --deal-structure stock \
                            --output out/scored.jsonl
python scripts/export.py    --input out/scored.jsonl --xlsx out/issues.xlsx \
                            --memo out/redflag-memo.md \
                            --validation-report out/validation-report.json
```

Every script: `--help` for flags, exit 0 on success, non-zero with a stderr
message otherwise. All intermediates are JSONL, one object per line, so `jq`
works on everything.

## Stages

| script | in | out | does |
|---|---|---|---|
| `ingest.py` | PDF directory | `chunks.jsonl`, `fulltext/` | PyMuPDF text extraction, ~1500-char paragraph chunks with 200-char overlap, exact full-document offsets |
| `prefilter.py` | `chunks.jsonl` | `candidates.jsonl` | case-insensitive regex screen; high recall, ~90% of chunks dropped |
| `sweep.py` | `candidates.jsonl` | `findings.jsonl` | one model call per candidate against an OpenAI-compatible endpoint |
| `validate.py` | `findings.jsonl` + `fulltext/` | `validated.jsonl`, `*.failures.jsonl`, report | verifies every quote against source text at the stated page and offsets |
| `score.py` | `validated.jsonl` | `scored.jsonl` | severity by clause type and deal structure; pure function, no model |
| `export.py` | `scored.jsonl` | `issues.xlsx`, `redflag-memo.md` | deliverables |
| `bench.py` | CUAD + `scored.jsonl` | `metrics.json` | precision/recall/F1 vs CUAD annotations at IoU ≥ 0.5 |

Only `schemas.py` is imported by other scripts. It holds the dataclasses, the
eight clause types, the `terms` key sets, and the JSONL/text IO helpers.

## Offsets

Every downstream guarantee rests on this:

- A document's full text is the concatenation of its page texts in order, each
  terminated by exactly one `\n`.
- A chunk's `text` is exactly `full_text[char_start:char_end]`.
- `fulltext/<doc_id>.txt` is that same full text, written with newline
  translation disabled.
- `fulltext/<doc_id>.pages.json` records each page's `[start, end)` span, so the
  cited page number is verifiable too, not just the characters.
- Chunk overlap is expressed in whole paragraph units and each chunk's start
  strictly increases, so boundaries never land mid-paragraph and chunk ids are
  unique.

## The core invariant

> No finding is reported unless its `quote` appears verbatim in the source
> document at the stated page and character offsets.

Enforced in `validate.py` by exact string comparison against the ingested full
text, after whitespace collapse and nothing else. No fuzzy matching, no case
folding. Failures are dropped and counted, never repaired. `validate.py` exits
non-zero when the drop rate exceeds `--max-drop-rate` (default 0.2), and
`export.py` refuses input containing an unvalidated finding.

Two mechanical details worth knowing:

- The model returns a quote, not offsets. `sweep.py` locates that quote inside
  the candidate window — exact match first, then whitespace-tolerant — and
  records the *source* substring plus its full-document offsets. A quote that
  cannot be located is dropped as hallucinated.
- `validate.py` therefore re-checks against a different artifact (the full-text
  sidecar) than `sweep.py` used (the candidate window). That is what catches
  offset arithmetic errors, which are the failure mode that silently produces
  confident wrong citations.

## Endpoint configuration

`sweep.py` only. Flags override environment variables, which override defaults.

| flag | env | default |
|---|---|---|
| `--endpoint` | `DILIGENCE_ENDPOINT` | `https://inference.local/v1` |
| `--model` | `DILIGENCE_MODEL` | `local-model` |
| — | `DILIGENCE_API_KEY` | `dummy` |

The default targets the sandbox route, where traffic to `inference.local` is
intercepted by the OpenShell proxy and forwarded to the host-side server. That
interception is HTTPS-only, so `http://inference.local` hangs until timeout
rather than erroring — `sweep.py` refuses that combination up front. For a
host-side run pass `--endpoint http://localhost:8000/v1`.

A missing API key is never an error: the proxy injects the real credential at
egress, so the placeholder bearer token is correct behaviour. Proxy environment
variables are always honoured; there is no flag to bypass them and adding one
would break the sandbox.

## Troubleshooting

| symptom | cause |
|---|---|
| `sweep.py` hangs, no output | endpoint unreachable. `http://` on `inference.local`, a `localhost` endpoint inside the sandbox, or a bypassed proxy. The startup line names the endpoint in use. |
| `ImportError` on `fitz` / `httpx` / `openpyxl` | wheelhouse missing or incomplete. Nothing installs at runtime by design. |
| `ingest.py`: "no extractable text" | scanned PDF. Out of scope — there is no OCR. |
| zero candidates | check `ingest.py` produced real text before suspecting the trigger vocabulary |
| zero findings, many `sweep-errors.jsonl` entries | model drifting off schema. Lower `--workers`, try `--json-mode` if the server supports guided decoding. |
| high validation drop rate | `--fulltext-dir` does not match the ingest run, or an offset bug. Inspect `validated.failures.jsonl`: `quote_mismatch` and `page_mismatch` point at offsets, `quote_not_in_source` in `sweep-errors.jsonl` points at the model. |
| `score.py`: "all findings were unvalidated" | `validate.py` was skipped |

## Tuning

- `prefilter.py` triggers live in `references/clause-taxonomy.md`, in fenced
  ```regex blocks per clause type. That file is the single source of truth; the
  script only parses it. Adding a noisy pattern costs model calls, removing one
  silently loses findings.
- `sweep.py` prompts are module-level constants (`SYSTEM_PROMPT`, `USER_PROMPT`,
  `REPAIR_PROMPT`) so they can be swapped without touching logic.
- `score.py` severity rules are documented in `references/severity-rules.md`.
  Code is authoritative if the two diverge.
- `--clauses change_of_control,anti_assignment` on `prefilter.py` narrows a run.

## Benchmarking

```bash
python scripts/bench.py --cuad /path/to/CUAD_v1 --findings out/scored.jsonl \
    --output out/metrics.json --fulltext-dir out/fulltext
```

CUAD ships its own text extraction, so its offsets are not comparable to ours.
`bench.py` works in CUAD's coordinate space instead: gold spans are already
there, and each finding's verified quote is located inside the CUAD context by
whitespace-tolerant search. A match needs the same document, the same mapped
clause category, and IoU ≥ 0.5. Quotes that cannot be located count as false
positives and gold that cannot be located counts as a miss, so the reported
numbers are a lower bound. Pass `--fulltext-dir` so documents that produced zero
findings still count against recall.

## Not in scope

No OCR or non-PDF formats. No amendment chaining or supersession logic. No
retrieval or vector database — a regex prefilter is sufficient at this scale. No
tests, fixtures, or mock modes. No persistence beyond flat files. Severity is
per clause, not weighted by contract value; the output is extraction for a lawyer
to review, not advice.
