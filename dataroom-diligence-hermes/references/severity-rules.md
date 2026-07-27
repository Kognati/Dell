# Severity rules

Load this file when scoring, or when explaining to a reader why a finding landed
where it did. `scripts/score.py` implements these rules and is authoritative if
the two ever disagree; scoring is a pure function of `clause_type`, `terms`, and
`--deal-structure`, with no model call.

## The three levels

| severity | meaning |
|---|---|
| `blocking` | the deal cannot close until this is resolved |
| `consent_required` | counterparty consent is needed, which adds timeline risk |
| `note` | worth flagging, not gating |

`consent_required` is reserved for clauses that literally require a third party
to say yes before the transaction can complete. A clause that is merely
commercially unattractive is a `note`; a clause that hands the counterparty a
right the acquirer cannot live with is `blocking`.

## Why deal structure reorders everything

The same contract yields a different issues list depending on how the target is
acquired:

- **Stock deal** — the target entity survives and keeps its contracts. Nothing is
  assigned, so plain anti-assignment language is usually inert. But the target's
  ownership changes, which is exactly what a change-of-control clause is written
  to catch.
- **Asset deal** — contracts must be transferred to the buyer. That transfer is
  an assignment, so anti-assignment clauses bite. The target's ownership may
  never change, so many change-of-control triggers never fire.

Hence: asset deals trip `anti_assignment` where stock deals do not; stock deals
trip `change_of_control` where asset deals may not.

## Base severity

| `clause_type` | stock | asset |
|---|---|---|
| `change_of_control` | `consent_required` | `note` |
| `anti_assignment` | `note` | `consent_required` |
| `mfn` | `note` | `note` |
| `indemnity_cap` | `note` | `note` |
| `auto_renewal` | `note` | `note` |
| `termination_convenience` | `note` | `note` |
| `exclusivity` | `note` | `note` |
| `governing_law` | `note` | `note` |

## Adjustments

Applied in order; the first matching rule in each group wins. Every adjustment
reads only `terms` keys, so a sparse `terms` object simply means no adjustment
fires.

**Escalate to `blocking`**

| condition | rationale |
|---|---|
| `change_of_control` + stock + `termination_right` is true | the counterparty can walk on closing; the revenue does not survive the deal |
| `anti_assignment` + asset + `assignment_barred` is true and `exceptions` is empty | the contract cannot be moved to the buyer at all |
| `anti_assignment` + asset + `consent_standard` contains `sole`, `absolute`, `discretion`, or `any reason` | consent exists on paper but the counterparty can refuse for free |
| `indemnity_cap` + `uncapped` is true (either structure) | unbounded exposure travels with the contract |

**Escalate to `consent_required`**

| condition | rationale |
|---|---|
| `change_of_control` + asset + `consent_required` is true | deemed-assignment language can catch an asset transfer too |

**De-escalate to `note`**

| condition | rationale |
|---|---|
| `change_of_control` + stock, and none of `consent_required`, `termination_right`, `notice_days` stated | the span defines a trigger but attaches no consequence |
| `anti_assignment` + asset, and neither `assignment_barred` nor `consent_required` stated | boilerplate successors-and-assigns language, not a restriction |

## Confidence

`score.py` never changes severity based on confidence. A finding below
`--min-confidence` (default 0.5) is marked `low_confidence: true` and sorts after
its confident peers within the same severity band. Demoting an uncertain
`blocking` finding would hide the exact item a reviewer most needs to look at.

## Judgment the rules cannot make

Flag these by hand; they are deliberately absent from `score.py`:

- **Materiality.** A blocking clause in a $2k SaaS renewal is not a blocking
  issue in the deal. Severity here is per-clause, not per-dollar.
- **Which side is the target.** `termination_convenience.party` records the
  contract's own label ("Customer", "either party"). Whether that party is the
  target or the counterparty is not derivable from the span.
- **"By operation of law" transfers.** Anti-assignment language that expressly
  covers transfers by operation of law or by merger can reach a stock deal.
  `score.py` will have scored it `note`; escalate it yourself and say why.
- **Aggregation.** Ten `note`-level auto-renewals across one vendor family can
  matter more than a single `consent_required`.
