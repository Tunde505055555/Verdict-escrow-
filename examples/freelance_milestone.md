# Example: freelance milestone escrow

A minimal end-to-end walkthrough against a deployed `VerdictEscrow`. Every
call below is a normal contract call — from the Studio UI, `genlayer-py`, or
any wallet.

Roles: **payer** (client), **provider** (freelancer), **arbiter** (a named
address of last resort — may be a multisig or a DAO executor).

---

## 1. Open the agreement

```python
agreement_id = contract.open_agreement(
    provider="0xProvider...",
    arbiter="0xArbiter...",
    title="Landing page rebuild",
    allowed_domains=["github.com", "raw.githubusercontent.com", "vercel.app"],
)
```

`allowed_domains` is the evidence allowlist. It is enforced **deterministically
before any node touches the network**, so a malicious evidence URL can never
steer a validator to an attacker-controlled host.

## 2. Fund a milestone

Criteria are an *ordered list*. Order matters: validators express disagreement
as the set of criterion **indices** they consider unmet, so the list is the
shared vocabulary of the dispute. Write them as checkable statements.

```python
contract.add_milestone(
    agreement_id=agreement_id,
    amount=..., # wei, sent as call value
    criteria=[
        "A public repository exists containing the site source.",
        "The deployed site returns HTTP 200 at the URL given as evidence.",
        "Lighthouse performance score on mobile is at least 85.",
        "All copy from the supplied content doc appears on the page.",
    ],
    deadline_hours=336,   # 14 days
)
```

Bad criteria produce bad rulings — this contract makes judgment *converge*,
it does not make it *correct*. "Looks professional" is not a criterion;
"returns HTTP 200" is.

## 3. Submit evidence

```python
contract.submit_evidence(
    agreement_id=agreement_id,
    index=0,
    claim="Site is live and all four criteria are met.",
    evidence_urls=[
        "https://github.com/acme/landing",
        "https://acme-landing.vercel.app",
        "https://raw.githubusercontent.com/acme/landing/main/lighthouse.json",
    ],
)
```

The milestone moves to `UNDER_REVIEW` and the review clock starts.

## 4. Adjudicate

```python
contract.adjudicate(agreement_id=agreement_id, index=0)
```

Each validator independently fetches the allowlisted evidence, rules against
the criteria, and the round-1 equivalence rule accepts the leader's ruling only
if the verdict matches exactly, confidence is within ±20, and the unmet-criteria
sets overlap at Jaccard ≥ 0.50.

* `APPROVE` → funds credited to the provider (withdraw via `withdraw()`).
* `REJECT` → milestone returns for new evidence, or refunds once attempts run out.
* `INCONCLUSIVE` → evidence was insufficient; nothing moves.

## 5. Appeal (optional)

```python
contract.appeal(agreement_id=agreement_id, index=0)  # send the bond as value
```

The appellant posts a bond (10% of the milestone). Round 2 tightens tolerance
to ±12 / Jaccard ≥ 0.75 and adds a validator-local LLM check that the leader's
*reasoning* is materially the same as the validator's own. Round 3 tightens
again to ±6 / Jaccard 1.00. After the last round the milestone lands in
`DEADLOCKED` and the arbiter settles it.

## 6. If adjudication never converges

```python
contract.resolve_stalled_review(agreement_id=agreement_id, index=0)
```

Callable by payer, provider or arbiter once the milestone has sat in
`UNDER_REVIEW` for 48 hours. On round 1 it returns the milestone for fresh
evidence (or refunds the payer if attempts are exhausted) and returns bonds;
on an appeal round it moves to `DEADLOCKED` with bonds still escrowed. No
successful ruling is required — funds can never be stranded by an unreachable
evidence host or a model that will not converge.

## 7. Withdraw

```python
contract.withdraw()
```

Pull-payment: every payout, refund and bond return is credited to a ledger and
claimed by the recipient. Nothing is pushed during adjudication.
