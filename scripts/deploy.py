#!/usr/bin/env python3
"""
Deploy VerdictEscrow to a GenLayer Studio / testnet endpoint.

Usage
-----
    pip install -r requirements.txt
    export GENLAYER_RPC=http://localhost:4000/api      # Studio default
    export GENLAYER_PRIVATE_KEY=0x...                  # omit on Studio to use a burner
    python scripts/deploy.py

Notes
-----
* The constructor takes **no arguments**. Protocol defaults are compiled in
  (appeal bond 10%, max 3 rounds, 72h appeal window, 48h review timeout).
  This is deliberate: GenLayer Studio encodes omitted integer args as `0`,
  which would trip the constructor's range checks.
* Everything domain-specific (acceptance criteria, evidence allowlist,
  arbiter) is set per agreement via `open_agreement`, so a single deployment
  serves many use cases.
"""

from __future__ import annotations

import os
import pathlib
import sys

CONTRACT = pathlib.Path(__file__).resolve().parents[1] / "contracts" / "verdict_escrow.py"


def main() -> int:
    try:
        from genlayer_py import create_account, create_client  # type: ignore
        from genlayer_py.chains import localnet, studionet  # type: ignore
    except ImportError:
        print(
            "genlayer-py is not installed.\n"
            "  pip install -r requirements.txt\n"
            "Or paste contracts/verdict_escrow.py straight into the Studio editor "
            "and deploy with no constructor arguments.",
            file=sys.stderr,
        )
        return 1

    code = CONTRACT.read_bytes()
    rpc = os.environ.get("GENLAYER_RPC")
    chain = studionet if (rpc and "localhost" not in rpc) else localnet

    key = os.environ.get("GENLAYER_PRIVATE_KEY")
    account = create_account(key) if key else create_account()
    client = create_client(chain=chain, account=account, endpoint=rpc) if rpc else create_client(
        chain=chain, account=account
    )

    tx_hash = client.deploy_contract(code=code, args=[])
    receipt = client.wait_for_transaction_receipt(transaction_hash=tx_hash, status="FINALIZED")
    address = receipt["data"]["contract_address"]

    print(f"deployer : {account.address}")
    print(f"tx       : {tx_hash}")
    print(f"contract : {address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
