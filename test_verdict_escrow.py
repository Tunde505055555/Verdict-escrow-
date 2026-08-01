"""Direct-mode tests for VerdictEscrow.

Run with:   pytest genlayer/tests/direct/ -v

These tests cover four things a reviewer should care about:

  1. The pure consensus helpers (`_normalize_ruling`, `_jaccard`,
     `_domain_allowed`, `_extract_json`) — these are the parts that decide
     whether two validators agree, so they are tested in isolation without a
     VM.
  2. The happy path: fund -> submit -> adjudicate -> accept -> withdraw.
  3. Access control and the escrow accounting invariant.
  4. The appeal path, including bond slashing and the deadlock/arbiter exit.

Tiers 1-3 of the validator rule are exercised directly against
`_normalize_ruling` + `_jaccard`, which is where the acceptance logic lives;
Tier 4 needs a live template and is covered by the integration suite.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

CONTRACT = str(Path(__file__).resolve().parents[2] / "contracts" / "verdict_escrow.py")


# ---------------------------------------------------------------------------
# Pure-logic tests (no VM required)
# ---------------------------------------------------------------------------


def _ensure_genvm_sdk():
    """Put the GenVM `py-genlayer` standard library on `sys.path`.

    The pip package named `genlayer` is the *client* library, not the GenVM
    SDK the contract imports. `genvm-lint download` caches the real SDK, so
    reuse it here and stub the wasi host module that only exists inside the VM.
    """
    try:
        import genlayer as _gl

        if hasattr(_gl, "storage") or hasattr(_gl, "u8"):
            return True
    except Exception:
        pass

    cache = Path.home() / ".cache" / "genvm-linter" / "extracted"
    std = sorted(cache.glob("*/py-lib-genlayer-std/*"))
    proto = sorted(cache.glob("*/py-lib-protobuf/*/src"))
    if not std:
        return False

    for p in [str(std[-1])] + [str(p) for p in proto[-1:]]:
        if p not in sys.path:
            sys.path.insert(0, p)

    if "_genlayer_wasi" not in sys.modules:
        import types

        stub = types.ModuleType("_genlayer_wasi")
        stub.FAKE_VM = True
        stub.__getattr__ = lambda name: (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"wasi stub: {name}"))
        )
        sys.modules["_genlayer_wasi"] = stub

    sys.modules.pop("genlayer", None)
    return True


def _load_pure_module():
    """Import the contract module for its pure helpers.

    `from genlayer import *` is only available inside GenVM, so when the SDK
    is absent we skip rather than pretend to pass.
    """
    if not _ensure_genvm_sdk():
        pytest.skip("GenVM SDK not available (run `genvm-lint download`)")
    spec = importlib.util.spec_from_file_location("verdict_escrow_pure", CONTRACT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verdict_escrow_pure"] = module
    spec.loader.exec_module(module)
    return module



@pytest.fixture(scope="module")
def ve():
    return _load_pure_module()


class TestDomainAllowlist:
    def test_exact_and_subdomain_match(self, ve):
        allowed = ["github.com"]
        assert ve._domain_allowed("https://github.com/org/repo/pull/1", allowed)
        assert ve._domain_allowed("https://api.github.com/repos/x", allowed)

    def test_rejects_lookalike_suffix(self, ve):
        # The classic bypass: notgithub.com must not match github.com.
        assert not ve._domain_allowed("https://notgithub.com/x", ["github.com"])
        assert not ve._domain_allowed("https://github.com.evil.io/x", ["github.com"])

    def test_rejects_non_https_and_userinfo(self, ve):
        assert not ve._domain_allowed("http://github.com/x", ["github.com"])
        assert not ve._domain_allowed("https://github.com@evil.io/x", ["github.com"])
        assert not ve._domain_allowed("https://github.com:8443/x", ["github.com"])


class TestRulingNormalization:
    def test_canonicalizes_verdict_and_clamps_confidence(self, ve):
        out = ve._normalize_ruling(
            {
                "verdict": " approve ",
                "confidence": 140,
                "unmet_criteria": [],
                "rationale": "All three criteria are shown directly by evidence 0.",
            },
            3,
        )
        assert out["verdict"] == "APPROVE"
        assert out["confidence"] == 100

    def test_dedupes_sorts_and_bounds_unmet_indices(self, ve):
        out = ve._normalize_ruling(
            {
                "verdict": "REJECT",
                "confidence": 70,
                "unmet_criteria": [2, "1", 1, 99, -4],
                "rationale": "Evidence 0 shows criteria 1 and 2 were not delivered.",
            },
            3,
        )
        # 99 and -4 are out of range and dropped; the rest are deduped+sorted.
        assert out["unmet_criteria"] == [1, 2]

    def test_rejects_incoherent_approve(self, ve):
        with pytest.raises(ValueError):
            ve._normalize_ruling(
                {
                    "verdict": "APPROVE",
                    "confidence": 90,
                    "unmet_criteria": [0],
                    "rationale": "Approving even though criterion 0 is unmet.",
                },
                2,
            )

    def test_rejects_unsupported_reject(self, ve):
        with pytest.raises(ValueError):
            ve._normalize_ruling(
                {
                    "verdict": "REJECT",
                    "confidence": 90,
                    "unmet_criteria": [],
                    "rationale": "Rejecting without citing any unmet criterion.",
                },
                2,
            )

    def test_rejects_unknown_verdict(self, ve):
        with pytest.raises(ValueError):
            ve._normalize_ruling(
                {"verdict": "MAYBE", "confidence": 50, "rationale": "x" * 40}, 2
            )


class TestJsonExtraction:
    def test_unwraps_fenced_json(self, ve):
        raw = '```json\n{"verdict": "APPROVE", "confidence": 91}\n```'
        assert ve._extract_json(raw)["confidence"] == 91

    def test_ignores_surrounding_prose(self, ve):
        raw = 'Here is my ruling:\n{"verdict": "REJECT"}\nHope that helps.'
        assert ve._extract_json(raw)["verdict"] == "REJECT"

    def test_raises_without_object(self, ve):
        with pytest.raises(ValueError):
            ve._extract_json("I could not decide.")


class TestValidatorAcceptanceRule:
    """Tiers 1-3 of the validator rule, expressed against the same helpers the
    contract uses. Each case is a (leader, validator) pair."""

    def _agrees(self, ve, leader, validator, round_no):
        tol = ve.CONFIDENCE_TOLERANCE.get(round_no, ve.DEFAULT_TOLERANCE)
        jmin = ve.UNMET_JACCARD_MIN.get(round_no, ve.DEFAULT_JACCARD)
        if leader["verdict"] != validator["verdict"]:
            return False
        if abs(leader["confidence"] - validator["confidence"]) > tol:
            return False
        return (
            ve._jaccard(set(leader["unmet_criteria"]), set(validator["unmet_criteria"]))
            >= jmin
        )

    def test_verdict_mismatch_never_passes(self, ve):
        leader = {"verdict": "APPROVE", "confidence": 90, "unmet_criteria": []}
        validator = {"verdict": "REJECT", "confidence": 90, "unmet_criteria": [0]}
        assert not self._agrees(ve, leader, validator, 1)

    def test_round_one_tolerates_score_drift(self, ve):
        leader = {"verdict": "APPROVE", "confidence": 80, "unmet_criteria": []}
        validator = {"verdict": "APPROVE", "confidence": 62, "unmet_criteria": []}
        assert self._agrees(ve, leader, validator, 1)

    def test_appeal_round_rejects_same_drift(self, ve):
        leader = {"verdict": "APPROVE", "confidence": 80, "unmet_criteria": []}
        validator = {"verdict": "APPROVE", "confidence": 62, "unmet_criteria": []}
        assert not self._agrees(ve, leader, validator, 2)
        assert not self._agrees(ve, leader, validator, 3)

    def test_partial_reason_overlap_passes_round_one_only(self, ve):
        # Same verdict, overlapping but not identical reasons: {0,1} vs {1}.
        leader = {"verdict": "REJECT", "confidence": 70, "unmet_criteria": [0, 1]}
        validator = {"verdict": "REJECT", "confidence": 70, "unmet_criteria": [1]}
        assert self._agrees(ve, leader, validator, 1)  # jaccard 0.5 >= 0.5
        assert not self._agrees(ve, leader, validator, 2)  # needs 0.75

    def test_disjoint_reasons_never_pass(self, ve):
        leader = {"verdict": "REJECT", "confidence": 70, "unmet_criteria": [0]}
        validator = {"verdict": "REJECT", "confidence": 70, "unmet_criteria": [2]}
        assert not self._agrees(ve, leader, validator, 1)

    def test_final_round_demands_identical_reasons(self, ve):
        leader = {"verdict": "REJECT", "confidence": 70, "unmet_criteria": [0, 1]}
        same = {"verdict": "REJECT", "confidence": 68, "unmet_criteria": [1, 0]}
        near = {"verdict": "REJECT", "confidence": 68, "unmet_criteria": [0, 1, 2]}
        assert self._agrees(ve, leader, same, 3)
        assert not self._agrees(ve, leader, near, 3)


# ---------------------------------------------------------------------------
# Direct-mode VM tests
# ---------------------------------------------------------------------------

HOUR = 3600
NOW_HOUR = 480_000
GEN = 10**18


def _time_body(hour: int) -> str:
    return json.dumps({"unixtime": hour * HOUR + 30})


def _mock_time(vm, hour: int = NOW_HOUR):
    vm.mock_web(r".*worldtimeapi\.org.*", {"status": 200, "body": _time_body(hour)})


def _mock_evidence(vm, body: str = "Deliverable v1.2 shipped. Docs published."):
    vm.mock_web(r".*evidence\.example\.com.*", {"status": 200, "body": body})


def _mock_ruling(vm, verdict: str, confidence: int, unmet: list, rationale: str):
    vm.mock_llm(
        r".*adjudicating a funded delivery milestone.*",
        json.dumps(
            {
                "verdict": verdict,
                "confidence": confidence,
                "unmet_criteria": unmet,
                "rationale": rationale,
            }
        ),
    )


CRITERIA = [
    "A public changelog entry for version 1.2 exists.",
    "The API reference documents every new endpoint.",
]


@pytest.fixture
def escrow(direct_vm, direct_deploy, direct_alice):
    """Funded, active agreement with one 10 GEN milestone.

    alice = payer, bob = provider, charlie = arbiter.
    """
    contract = direct_deploy(CONTRACT)
    return contract


def _setup(vm, contract, payer, provider, arbiter, amount=10 * GEN):
    vm.sender = payer
    aid = contract.create_agreement(
        str(provider), str(arbiter), "Docs revamp", ["evidence.example.com"]
    )
    contract.add_milestone(aid, "v1.2 docs", CRITERIA, amount, NOW_HOUR + 48)
    vm.value = amount
    contract.fund_and_activate(aid)
    vm.value = 0
    return aid


def test_happy_path_release_and_withdraw(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Published the v1.2 changelog and the full API reference.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    assert escrow.get_milestone(aid, 0)["state"] == 2  # UNDER_REVIEW

    _mock_ruling(
        direct_vm, "APPROVE", 92, [], "Evidence 0 shows the changelog and endpoints."
    )
    escrow.adjudicate(aid, 0)

    ruling = escrow.get_rulings(aid, 0)[0]
    assert ruling["verdict"] == "APPROVE"
    assert ruling["round_no"] == 1

    # Payer waives the appeal window instead of waiting 72h.
    direct_vm.sender = direct_alice
    escrow.accept_ruling(aid, 0)

    assert escrow.get_milestone(aid, 0)["state"] == 4  # RELEASED
    assert escrow.credit_of(str(direct_bob)) == str(10 * GEN)
    assert escrow.get_agreement(aid)["escrowed"] == "0"
    assert escrow.get_agreement(aid)["state"] == 2  # CLOSED

    direct_vm.sender = direct_bob
    escrow.withdraw()
    assert escrow.credit_of(str(direct_bob)) == "0"


def test_reject_refunds_payer(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm, "Placeholder page. Nothing published yet.")
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Everything is done, see the link for the published documentation.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    _mock_ruling(
        direct_vm, "REJECT", 88, [0, 1], "Evidence 0 is a placeholder; nothing shipped."
    )
    escrow.adjudicate(aid, 0)

    direct_vm.sender = direct_bob  # provider is the losing party here
    escrow.accept_ruling(aid, 0)

    assert escrow.get_milestone(aid, 0)["state"] == 5  # REFUNDED
    assert escrow.credit_of(str(direct_alice)) == str(10 * GEN)


def test_evidence_domain_allowlist_is_enforced(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("evidence domain not allowlisted"):
        escrow.submit_evidence(
            aid,
            0,
            "Delivered, proof is hosted on my own server for convenience.",
            ["https://attacker.example.net/proof"],
        )


def test_only_provider_may_submit(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("only the provider"):
        escrow.submit_evidence(
            aid,
            0,
            "I am the payer trying to self-approve this milestone right now.",
            ["https://evidence.example.com/x"],
        )


def test_funding_must_match_exactly(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    direct_vm.sender = direct_alice
    aid = escrow.create_agreement(
        str(direct_bob), str(direct_charlie), "Docs revamp", ["evidence.example.com"]
    )
    escrow.add_milestone(aid, "v1.2 docs", CRITERIA, 10 * GEN, NOW_HOUR + 48)
    direct_vm.value = 9 * GEN
    with direct_vm.expect_revert("send exactly"):
        escrow.fund_and_activate(aid)
    direct_vm.value = 0


def test_appeal_flips_verdict_and_returns_bond(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Changelog and API reference are both live at the linked page.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    _mock_ruling(direct_vm, "REJECT", 61, [1], "Evidence 0 omits the new endpoints.")
    escrow.adjudicate(aid, 0)

    # Provider lost round 1 and appeals with a 10% bond.
    bond = 10 * GEN // 10
    direct_vm.sender = direct_bob
    direct_vm.value = bond
    escrow.appeal(
        aid, 0, "The endpoint reference is on the linked page under 'Reference'."
    )
    direct_vm.value = 0

    m = escrow.get_milestone(aid, 0)
    assert m["round_no"] == 2
    assert m["state"] == 2  # back UNDER_REVIEW
    assert m["provider_bond"] == str(bond)

    # Round 2 rules the other way; the appellant wins and gets the bond back.
    direct_vm.clear_mocks()
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    _mock_ruling(
        direct_vm, "APPROVE", 90, [], "Evidence 0 documents every new endpoint."
    )
    escrow.adjudicate(aid, 0)

    direct_vm.sender = direct_alice
    escrow.accept_ruling(aid, 0)

    assert escrow.credit_of(str(direct_bob)) == str(10 * GEN + bond)
    assert escrow.get_agreement(aid)["escrowed"] == "0"
    assert len(escrow.get_rulings(aid, 0)) == 2


def test_failed_appeal_slashes_bond_to_opponent(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Changelog and API reference are both live at the linked page.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    _mock_ruling(direct_vm, "REJECT", 80, [1], "Evidence 0 omits the new endpoints.")
    escrow.adjudicate(aid, 0)

    bond = 10 * GEN // 10
    direct_vm.sender = direct_bob
    direct_vm.value = bond
    escrow.appeal(aid, 0, "I believe the reference section covers every endpoint.")
    direct_vm.value = 0

    escrow.adjudicate(aid, 0)  # same REJECT mock -> ruling stands

    direct_vm.sender = direct_bob
    escrow.accept_ruling(aid, 0)

    # Payer receives the milestone amount plus the forfeited bond.
    assert escrow.credit_of(str(direct_alice)) == str(10 * GEN + bond)
    assert escrow.credit_of(str(direct_bob)) == "0"


def test_only_losing_party_may_appeal(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)
    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Changelog and API reference are both live at the linked page.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    _mock_ruling(direct_vm, "APPROVE", 95, [], "Evidence 0 satisfies both criteria.")
    escrow.adjudicate(aid, 0)

    direct_vm.sender = direct_bob  # the winner
    direct_vm.value = 10 * GEN // 10
    with direct_vm.expect_revert("only the losing party"):
        escrow.appeal(aid, 0, "I won but I would like an even better ruling please.")
    direct_vm.value = 0


def test_insufficient_evidence_returns_to_provider(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm, "404 not found")
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "The documentation is published, link included below for review.",
        ["https://evidence.example.com/missing"],
    )
    _mock_ruling(
        direct_vm,
        "INSUFFICIENT_EVIDENCE",
        30,
        [0, 1],
        "Evidence 0 returned a not-found page; cannot verify either criterion.",
    )
    escrow.adjudicate(aid, 0)

    direct_vm.sender = direct_alice
    escrow.accept_ruling(aid, 0)

    m = escrow.get_milestone(aid, 0)
    assert m["state"] == 1  # back to AWAITING_EVIDENCE
    assert m["submissions"] == 1  # one attempt consumed
    assert escrow.get_agreement(aid)["escrowed"] == str(10 * GEN)


def test_payer_reclaims_overdue_milestone(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.clear_mocks()
    _mock_time(direct_vm, NOW_HOUR + 100)  # past the 48h deadline

    direct_vm.sender = direct_alice
    escrow.cancel_overdue(aid, 0)
    assert escrow.get_milestone(aid, 0)["state"] == 5  # REFUNDED
    assert escrow.credit_of(str(direct_alice)) == str(10 * GEN)


def test_arbiter_settles_deadlock(
    direct_vm, escrow, direct_alice, direct_bob, direct_charlie
):
    _mock_time(direct_vm)
    _mock_evidence(direct_vm)
    aid = _setup(direct_vm, escrow, direct_alice, direct_bob, direct_charlie)

    direct_vm.sender = direct_bob
    escrow.submit_evidence(
        aid,
        0,
        "Changelog and API reference are both live at the linked page.",
        ["https://evidence.example.com/changelog/1.2"],
    )
    _mock_ruling(direct_vm, "REJECT", 80, [1], "Evidence 0 omits the new endpoints.")

    bond = 10 * GEN // 10
    # Round 1 ruling, then appeals up to max_rounds (3).
    escrow.adjudicate(aid, 0)
    for _ in range(2):
        direct_vm.sender = direct_bob
        direct_vm.value = bond
        escrow.appeal(aid, 0, "The reference section does in fact list the endpoints.")
        direct_vm.value = 0
        escrow.adjudicate(aid, 0)

    assert escrow.get_milestone(aid, 0)["round_no"] == 3

    # A fourth appeal is refused; escalation is the only remaining path.
    direct_vm.sender = direct_bob
    direct_vm.value = bond
    with direct_vm.expect_revert("appeal rounds exhausted"):
        escrow.appeal(aid, 0, "One more round should finally settle this dispute.")
    escrow.escalate_to_arbiter(
        aid, 0, "Final escalation: the endpoints are documented in full."
    )
    direct_vm.value = 0
    assert escrow.get_milestone(aid, 0)["state"] == 6  # DEADLOCKED

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("only the appointed arbiter"):
        escrow.arbiter_ruling(aid, 0, True, "I would like to approve my own payment.")

    direct_vm.sender = direct_charlie
    escrow.arbiter_ruling(
        aid, 0, True, "Reviewed both pages manually; the endpoints are documented."
    )
    assert escrow.get_milestone(aid, 0)["state"] == 4  # RELEASED
    # Provider posted 3 bonds and won: milestone plus every bond returns to them.
    assert escrow.credit_of(str(direct_bob)) == str(10 * GEN + 3 * bond)
    assert escrow.get_agreement(aid)["escrowed"] == "0"
