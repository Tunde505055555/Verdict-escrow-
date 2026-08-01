# VerdictEscrow — how it was written, and how to reuse it

## The problem it solves

A deterministic chain cannot answer *"did the counterparty actually deliver what
the agreement said?"* — that question requires reading real-world evidence and
making a judgment. VerdictEscrow answers it on GenLayer with validator
consensus rather than a trusted backend or an oracle committee.

Funds are escrowed per milestone. The provider submits a claim plus evidence
URLs. Validators independently fetch that evidence, rule against the *written
acceptance criteria*, and money moves only when they agree on the decision.

## How the contract was written

The whole design follows one rule: **never ask independent nodes to agree on
prose.** Everything else falls out of that.

**1. The judgment is reduced to comparable fields.** Each ruling is normalized
to `verdict` (a 3-valued enum), `confidence` (0–100), `unmet_criteria`
(indices into an ordered criteria list) and `rationale` (prose). Only the first
three are compared. Because criteria are an ordered list, "do two validators
disagree?" becomes a set operation on small integers instead of a vibe check.
`_normalize_ruling` additionally rejects self-contradictory output — `APPROVE`
may not list unmet criteria, `REJECT` must cite at least one — so malformed
model output never reaches storage.

**2. Validator strictness is tiered by appeal round.** Escalation is part of
the consensus rule, not an off-chain process:

| Round | Confidence tolerance | Unmet-set Jaccard | Validator LLM check |
| --- | --- | --- | --- |
| 1 | ±20 | ≥ 0.50 | no |
| 2 | ±12 | ≥ 0.75 | yes |
| 3 | ±6 | = 1.00 | yes |

Round 1 is cheap and tolerant because most milestones are uncontested. Each
appeal narrows the band, and from round 2 the validator also asks its own model
whether the leader's reasoning and its own rest on the same decisive facts.
Contested value therefore requires progressively stronger agreement before it
moves.

**3. The LLM check stays validator-local.** It runs inside the validator half of
`gl.vm.run_nondet_unsafe`, so its result is a local boolean deciding only
whether *this* validator accepts. It never has to converge across nodes, so an
unstable model response degrades into a retry instead of a chain-wide
non-convergence.

**4. Evidence is treated as hostile input.** `_is_allowed_url` runs as ordinary
deterministic contract code *before* any node touches the network: https-only,
no userinfo, no explicit port, exact-or-subdomain match so `notgithub.com` fails
against `github.com`. Fetched bodies are truncated and fenced in
`<<<EVIDENCE id=… >>>` markers, instructions come *after* the data, and the
model is told marker contents are data. A dead link becomes `FETCH_FAILED`, a
fact that steers the ruling toward `INSUFFICIENT_EVIDENCE` rather than crashing.

**5. Time without a clock.** Wall-clock reads go through the equivalence
principle like any other external read: fetch UTC, floor to the hour,
`strict_eq`. Every honest node in the same hour returns the same integer. Hour
granularity is deliberate — every deadline in the contract is measured in hours.

**6. It cannot hang and it cannot go insolvent.** Rounds are bounded by
`max_rounds`; past that the losing party posts one escalation bond and the
arbiter named at agreement creation decides — and the arbiter has power in *no
other state*. `INSUFFICIENT_EVIDENCE` is a real third outcome where nobody loses
and bonds are returned. Bonds are tracked **per party**, because the losing side
can change between rounds. Payouts are pull-based (`credits` + `withdraw`), so a
hostile recipient cannot block or re-enter settlement.

> **Invariant:** at all times `agreement.escrowed` equals the sum of unsettled
> milestone amounts plus all posted bonds.

## Why other builders should care

The reusable part is three functions and one convention — escrow, milestones and
bonds are scaffolding you can delete.

1. **Make your decision comparable.** Reduce whatever your contract judges to a
   small enum plus a set of integers.
2. **Copy `_normalize_ruling`.** Change the enum names and the bound; leave the
   coherence checks alone.
3. **Copy the tiered acceptance rule.** `TIERS = {1: (20, 0.50, False), 2: (12,
   0.75, True), 3: (6, 1.00, True)}`. The round counter can be anything that
   escalates: appeal count, value at risk, or a caller-paid strictness flag.
4. **Keep the LLM check validator-local.**
5. **Reuse `_is_allowed_url` verbatim** if you fetch anything, paired with the
   fencing convention.

Typical swap points, in rough order of likelihood: the criteria enum, the tier
table, the prompt body, the settlement effects of each verdict, and the time
bucket size in `_now_hour()`.

Because the domain lives entirely in agreement parameters (criteria, evidence
allowlist, arbiter), one deployment already serves freelance milestones,
bug-bounty payouts, grant tranches, SLA credits, RWA delivery confirmation and
DAO-funded deliverables — without a code change.

## What's in this bundle

| Path | What it is |
| --- | --- |
| `contracts/verdict_escrow.py` | the Intelligent Contract — paste into studio.genlayer.com and deploy with **no constructor arguments** |
| `tests/direct/test_verdict_escrow.py` | direct-mode test suite: pure consensus logic + contract behaviour with mocked web/LLM |
| `README.md` | full reference: state machine, API, consensus patterns, test matrix, limitations |
| `DESIGN.md` | this document |

```bash
pip install genlayer-test genvm-linter
genvm-lint check contracts/verdict_escrow.py
pytest tests/direct/ -v
```

MIT licensed.
