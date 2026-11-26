"""Export recorded delegation chains for the dashboard's replay mode.

    uv run python scripts/export_demo_chains.py .ci-triage/e2e-plan.db 1 6

Reads finished runs, with their delegations, from a service database and
writes `dashboard/demo/chains.json`: each run as `GET /runs/{id}` returns it,
and its verdict with every citation checked again against the capture, as
`GET /api/runs/{id}/verdict` returns it. The replay dashboard reads that file
in place of the service, so the public demo shows a real chain and never runs
one live.

The demo is public, so only runs on a public repository listed in `PUBLIC` are
exported, and only a chain that has ended: an open delegation would replay as
if it were still waiting on something.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ci_triage.api import _verdict_payload
from ci_triage.models import Verdict
from ci_triage.store import OPEN_DELEGATION, Store
from ci_triage.tools import TriageContext

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dashboard" / "demo" / "chains.json"

#: Repositories whose runs may go in the public demo.
PUBLIC = {"yosrkharrat/ci-triage-demo"}
#: Fields left out of the export.
PRIVATE = {"installation", "delivery"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", type=Path, help="the service database the chains ran on")
    ap.add_argument("runs", type=int, nargs="+", help="row ids of the failed runs that started a chain")
    ap.add_argument("--model", default="", help="the model the triage ran on, shown with its verdict")
    args = ap.parse_args()

    # A copy: opening a store migrates it, and the recorded database stays as it ran.
    tmp = Path(tempfile.mkdtemp()) / args.db.name
    shutil.copy2(args.db, tmp)
    store = Store(tmp)
    runs: dict[str, dict] = {}
    verdicts: dict[str, dict] = {}
    for rid in args.runs:
        record = store.get(rid)
        if record is None:
            sys.exit(f"run {rid}: not in {args.db}")
        if record.repo not in PUBLIC:
            sys.exit(f"run {rid}: {record.repo} is not a public demo repository")
        delegations = store.delegations(origin_run=rid)
        if not delegations:
            sys.exit(f"run {rid}: started no delegation")
        if any(d.status in OPEN_DELEGATION for d in delegations):
            sys.exit(f"run {rid}: its delegation has not ended")
        # The App installation and webhook delivery ids say nothing the demo shows.
        out = {k: v for k, v in record.to_json().items() if k not in PRIVATE}
        out["delegations"] = [
            {k: v for k, v in d.to_json().items() if k not in PRIVATE} for d in delegations
        ]
        runs[str(rid)] = out
        if record.verdict is not None and record.fixture is not None:
            ctx = TriageContext(ROOT / record.fixture)
            verdicts[str(rid)] = {
                "model": args.model,
                **_verdict_payload(ctx, Verdict.model_validate(record.verdict)),
            }

    snapshot = {
        "captured_at": datetime.now(UTC).isoformat(),
        "source": args.db.name,
        "runs": runs,
        "verdicts": verdicts,
    }
    OUT.write_text(json.dumps(snapshot, indent=1, default=str) + "\n")
    shutil.rmtree(tmp.parent)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(runs)} chain(s), {len(verdicts)} checked verdict(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
