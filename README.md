# Dell

Contract due-diligence tooling for M&A data rooms, built as an
[Agent Skill](https://code.claude.com/docs/en/skills) and designed to run
entirely on local hardware — no contract text leaves the machine.

Point it at a folder of PDF contracts and it produces a ranked issues list where
**every citation is verified verbatim against the source document**.

## The invariant

The reason this exists rather than "ask a model about my contracts":

> No finding may be reported unless its `quote` appears verbatim in the source
> document at the stated offsets.

`scripts/validate.py` enforces that in code, not in a prompt. It slices the
source text at the recorded offsets and compares to the quote after whitespace
collapse only — no fuzzy matching, no case folding, no realignment. Findings that
fail are dropped and counted, never repaired. A run whose drop rate exceeds 20%
exits non-zero and refuses to certify.

That split is the whole design:

- **Scripts do mechanical work** — parse PDFs, match regexes, compare strings.
  A script never decides whether a clause is a change-of-control provision.
- **The model does judgment** — whether a span is operative, what a cap resolves
  to, whether a trigger is a real finding.
- **`validate.py` is the gate**, and nothing routes around it.

## Layout

| Path | What |
|---|---|
| `dataroom-diligence/` | the original skill — generic environment, single question |
| `dataroom-diligence-hermes/` | variant for the Hermes agent in a NemoClaw sandbox, with five task modes |
| `PRD-dataroom-diligence-skill (2).md` | the product requirements it was built from |

## What it looks for

Eight clause families, defined in `references/clause-taxonomy.md`:

`change_of_control` · `anti_assignment` · `mfn` · `indemnity_cap` ·
`auto_renewal` · `termination_convenience` · `exclusivity` · `governing_law`

Severity is assigned by rule from `references/severity-rules.md`, and **inverts
with deal structure**: asset deals transfer contracts so anti-assignment
language bites, stock deals keep the entity so change-of-control language bites.
Pass `--deal-structure stock|asset` — it reorders the entire issues list.

## Pipeline

Six stages, each writing a file the next one reads. Keep every intermediate.

```
ingest.py     PDFs        -> chunks.jsonl + fulltext/ sidecars
prefilter.py  chunks      -> candidates.jsonl     (regex, high recall, ~90% dropped)
sweep.py      candidates  -> findings.jsonl       (one model call per candidate)
validate.py   findings    -> validated.jsonl      (THE GATE)
score.py      validated   -> scored.jsonl         (severity by deal structure)
export.py     scored      -> issues.xlsx + redflag-memo.md
```

`bench.py` evaluates against CUAD and is not part of a review.

## Five task modes (hermes variant)

The same verified evidence answers five different questions a lawyer actually
asks. Phase 1 runs the pipeline once; Phase 2 reads `validated.jsonl` five
different ways, so each additional answer costs seconds rather than another
inference pass.

| Mode | Question | Why a lawyer cares |
|---|---|---|
| `diligence` | What's the risk? | deal-breaking clauses — the default |
| `obligations` | What must we do, by when? | post-close integration calendar |
| `dates` | Every deadline, notice window, expiry | compliance calendar |
| `parties` | Who's on the other side, what's the value | counterparty rollup |
| `anomaly` | How does this differ from the others? | finds the one weird contract |

Playbooks are in `dataroom-diligence-hermes/references/mode-playbooks.md`. Each
states what evidence it uses and, importantly, **what it cannot see** — four of
the five answer a narrower question than their name suggests.

`anomaly` needs at least five documents to have a distribution to compare against.

## Quick start

```bash
pip install -r dataroom-diligence/requirements.txt

cd "$(mktemp -d)"
SKILL=/path/to/dataroom-diligence

python $SKILL/scripts/ingest.py     --input /path/to/contracts --output out/chunks.jsonl --fulltext-dir out/fulltext
python $SKILL/scripts/prefilter.py  --input out/chunks.jsonl --output out/candidates.jsonl
python $SKILL/scripts/sweep.py      --candidates out/candidates.jsonl --output out/findings.jsonl \
                                    --endpoint http://localhost:8000/v1 --model <model> \
                                    --errors out/sweep-errors.jsonl --workers 1 --max-tokens 3000
python $SKILL/scripts/validate.py   --findings out/findings.jsonl --fulltext-dir out/fulltext \
                                    --output out/validated.jsonl --report out/validation-report.json
python $SKILL/scripts/score.py      --input out/validated.jsonl --deal-structure stock --output out/scored.jsonl
python $SKILL/scripts/export.py     --input out/scored.jsonl --xlsx out/issues.xlsx --memo out/redflag-memo.md \
                                    --validation-report out/validation-report.json
```

Every script supports `--help`. Run `sweep.py` with `--limit 20` first on a large
data room — at one worker each candidate is a serial model call of roughly 25
seconds, so a few hundred candidates is an hour or more.

## Deployment notes

Learned the hard way; none are guessable from the error text.

- **`--max-tokens 3000`** — with a *reasoning* model the whole budget goes into
  `reasoning_content` first, so a small budget yields empty `content` and a
  misleading `stage: parse` error with `raw: ""`. Raise the budget; don't touch
  the parser.
- **A wall of `502`/`ECONNRESET` does not prove a concurrency problem.** A
  backend that is down or crashing produces identical errors. Always send one
  `curl` to `/v1/models` before changing any flag — if that also fails, wait,
  because no worker count will help. This bit us hard: a `--workers 1`
  requirement was documented on the strength of such a wall, when the real cause
  was the vLLM engine crashing (`EngineDeadError` from
  `torch.AcceleratorError: CUDA error: an illegal instruction was encountered`).
  Serializing made the crash rarer, not absent.
- **Serve with `--enforce-eager` on GB10 / DGX Spark.** With this image's
  FlashInfer + FP4 kernel stack, CUDA graphs produced illegal-instruction faults
  both at capture time and during requests. Eager mode costs ~25% throughput and
  is stable. Measured after the switch: 32.5 s/candidate at `--workers 1` versus
  **12.3 s/candidate at `--workers 4`**, with no engine restarts — so concurrency
  is fine once the server is stable.
- **`sweep.py` writes its output only on completion.** A crash mid-run yields
  nothing, and an absent `findings.jsonl` *together with* an absent
  `sweep-errors.jsonl` is that signature rather than a zero-findings result. On a
  large corpus, sweep in `--limit` batches.
- **`stage: schema` / `quote_not_in_source` errors are not failures.** They are
  `sweep.py` rejecting a quote the model did not reproduce verbatim — the
  citation gate working one stage upstream of `validate.py`.
- **`terms` is a hint, not a lookup key.** `CLAUSE_TERMS` in `schemas.py` is
  documented and injected into the prompt, but nothing enforces it — one measured
  run produced 4 populated term values across 13 findings, under invented names.
  Select findings by `clause_type` and derive facts by reading the verified
  `quote`.
- **Page numbers are derived, not claimed.** `sweep.py` has no page map, so it
  stamps the candidate chunk's start page; a quote inside a chunk that straddles a
  page break lands one page early. The hermes variant's `validate.py` recomputes
  and repairs it (reported as `page_repairs`) rather than discarding a verified
  citation.

## Verifying it works

[CUAD](https://www.atticusprojectai.org/cuad) is the natural test set: its
`master_clauses.csv` annotations map almost 1:1 onto the eight clause families
(`Change Of Control`, `Anti-Assignment`, `Most Favored Nation`, `Cap On
Liability` / `Uncapped Liability`, `Renewal Term` / `Notice Period To Terminate
Renewal`, `Termination For Convenience`, `Exclusivity`, `Governing Law`), so
recall can be measured rather than eyeballed.

Pick contracts from a single genre — `Distributor` agreements exercise six of the
eight families — so `anomaly` has a real distribution to compare against.

## Known issues

- `dataroom-diligence/scripts/validate.py` still drops findings on page
  mismatch, which produced a measured 38.5% drop rate (entirely
  `page_mismatch`) and therefore fails its own 20% threshold. Fixed in
  `dataroom-diligence-hermes/` only; the fix is not hermes-specific and should
  be brought upstream.
- The hermes variant duplicates the whole `scripts/` tree to change one file.
  Every future script fix has to be made twice. The cleaner structure is one
  `scripts/` tree with two `SKILL.md` front-ends.
- No OCR. Scanned PDFs yield no text and are silently useless.

## Security

**Contract text is untrusted input.** It is attacker-influenceable and must
never be executed, interpolated into a shell command, or used to build a file
path. Where the workflow reads raw contract text — only the `parties` mode does —
it goes file-to-file through `grep -F -f`, never onto a command line.
