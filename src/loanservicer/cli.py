"""Command line: simulate (with a strict invariant gate for CI) and serve."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .money import fmt
from .simulate import SimConfig, Simulation


def render_markdown(sim: Simulation, s: dict) -> str:
    lines = [
        f"## Servicing simulation: {s['loans']:,} loans, {s['months']} months"
        f" ({s['first_month']} to {s['last_month']})",
        "",
        f"- Invariants: {'all hold' if s['all_invariants_hold'] else str(s['invariant_failures']) + ' failures'}"
        f" ({s['loan_checks']:,} loan-month checks)",
        f"- Remittance tie-out: {'every pool, every month, to the cent' if s['remittance_ties_out'] else 'FAILED'}",
        f"- Active loans at end: {s['active_loans_end']:,}, UPB {fmt(s['upb_end'])}",
        "",
        "| Month | Active | Current | 30 | 60 | 90 | 120+ | Remitted | Ties out |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for mo in sim.months:
        b = mo["buckets"]
        remitted = sum(r["remittance"] for r in mo["remittance"].values())
        lines.append(
            f"| {mo['month']} | {mo['active_loans']:,} | {b['current']:,} | {b['30']} | {b['60']} | {b['90']} | "
            f"{b['120+']} | {fmt(remitted)} | {'yes' if mo['ties_out'] else 'NO'} |"
        )
    lines += ["", "Average monthly roll rates:", "", "| Transition | Rate |", "|---|---|"]
    for k, v in s["roll_rates_avg_bp"].items():
        lines.append(f"| {k.replace('->', ' to ')} | {v / 100:.2f}% |")
    return "\n".join(lines) + "\n"


def cmd_simulate(args) -> int:
    sim = Simulation(SimConfig(loans=args.loans, months=args.months, seed=args.seed))
    s = sim.run()
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"summary": s, "months": sim.months}, f, indent=1, default=str)
    md = render_markdown(sim, s)
    print(md)
    c = s["counts"]
    print(
        f"payments {c.get('payments', 0):,}  NSF reversals {c.get('nsf_reversals', 0)}"
        f"  late fees {c.get('late_fees_assessed', 0):,}"
        f"  payoffs {c.get('payoffs', 0)}  recasts {c.get('recasts', 0)}  modifications {c.get('modifications', 0)}"
        f"  ARM resets {c.get('arm_resets', 0)}  escrow analyses {c.get('escrow_analyses', 0):,}"
        f"  ({c.get('escrow_shortages', 0):,} shortages)  {s['elapsed_seconds']}s"
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY") if args.step_summary else None
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(md)
    if args.strict and not (s["all_invariants_hold"] and s["remittance_ties_out"]):
        for mo in sim.months:
            for fail in mo["invariants"]["failures"][:10]:
                print(f"FAIL {mo['month']}: {fail}", file=sys.stderr)
        print("invariant or tie-out failure", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "loanservicer.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", args.port)),
        access_log=False,
    )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="loanservicer")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate", help="run the servicing simulation and report")
    s.add_argument("--loans", type=int, default=5000)
    s.add_argument("--months", type=int, default=24)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--json", help="write months and summary to this file")
    s.add_argument("--strict", action="store_true", help="exit 1 unless every invariant holds and remittance ties out")
    s.add_argument("--step-summary", action="store_true", help="append the markdown report to $GITHUB_STEP_SUMMARY")
    s.set_defaults(func=cmd_simulate)
    v = sub.add_parser("serve", help="run the API and web UI")
    v.add_argument("--port", type=int, default=8880)
    v.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
