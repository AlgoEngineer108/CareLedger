"""Command line entry point: ``python -m pipeline``.

Prints the stage log, the headline figures, and the data quality findings, then
writes every layer under ``output/``.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import quality
from .runner import RunResult, run

BAR = "─" * 78

SEVERITY_MARK = {"critical": "!!", "serious": " !", "warning": " ~", "passing": " ✓"}


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def print_stage_log(result: RunResult) -> None:
    print(f"\n{BAR}\nPIPELINE RUN\n{BAR}")
    print(f"{'stage':<9}{'rows out':>11}{'elapsed':>10}   detail")
    for stage in result.stages:
        print(f"{stage.name:<9}{stage.rows_out:>11,}{stage.millis:>9.0f}ms   {stage.detail}")
    print(f"{'total':<9}{'':>11}{result.seconds * 1000:>9.0f}ms")


def print_tables(result: RunResult) -> None:
    print(f"\n{BAR}\nTABLES BUILT\n{BAR}")
    for layer, tables in (("silver", result.silver), ("gold", result.gold)):
        for name, frame in tables.items():
            held = (
                f"  ({int(frame['is_quarantined'].sum())} quarantined)"
                if "is_quarantined" in frame and frame["is_quarantined"].any()
                else ""
            )
            print(f"  {layer}.{name:<22}{len(frame):>8,} rows{held}")


def print_headline(result: RunResult) -> None:
    head = result.metrics["headline"]
    print(f"\n{BAR}\nGOLD LAYER\n{BAR}")
    print(f"  Charged amount        {_money(head['charged']):>18}")
    print(f"  Collected amount      {_money(head['collected']):>18}")
    print(f"  Uncollected           {_money(head['uncollected']):>18}")
    print(f"  Collection rate       {head['collection_rate_pct']:>17.2f}%")
    print(f"  Transactions          {head['transactions']:>18,}")
    print(f"  Encounters            {head['encounters']:>18,}")
    print(f"  Patients in dimension {head['patients_in_dim']:>18,}")
    print(f"  Claims processed      {head['claims']:>18,}")
    print(f"  Denied or rejected    {head['denial_rate_pct']:>17.2f}%")

    print("\n  charged against collected, by service month")
    widest = max(row["charged"] for row in result.metrics["monthly"])
    for row in result.metrics["monthly"]:
        filled = int(row["charged"] / widest * 28)
        collected = int(row["collected"] / widest * 28)
        bar = "█" * collected + "▒" * (filled - collected)
        flag = " (partial)" if row["partial"] else ""
        print(f"    {row['month']}  {bar:<28} {_money(row['charged']):>14}  {row['collection_rate_pct']:>5.1f}%{flag}")


def print_quality(result: RunResult) -> None:
    summary = quality.summarise(result.findings)
    print(f"\n{BAR}\nDATA QUALITY\n{BAR}")
    print(
        f"  {summary['critical']} critical · {summary['serious']} serious · "
        f"{summary['warning']} warning · {summary['passing']} passing"
    )
    for finding in result.findings:
        print(f"\n  {SEVERITY_MARK[finding.severity]} [{finding.severity.upper()}] {finding.title}")
        print(f"     {finding.metric}")
        if finding.remedy:
            print(f"     → {finding.remedy}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Run the CareLedger medallion pipeline over datasets/ and report the gold layer.",
    )
    parser.add_argument(
        "--format",
        choices=("csv", "parquet"),
        default="csv",
        help="output format for each layer (parquet needs pyarrow; default: csv)",
    )
    parser.add_argument("--no-write", action="store_true", help="run in memory, write nothing to disk")
    parser.add_argument("--quiet", action="store_true", help="suppress the console report")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every table as it is built")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        result = run(write=not args.no_write, output_format=args.format)
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.quiet:
        print_stage_log(result)
        print_tables(result)
        print_headline(result)
        print_quality(result)
        if not args.no_write:
            print(f"\n{BAR}")
            print("  wrote output/{bronze,silver,gold}/ · metrics.json · data_quality.json · stage_log.json")
        print()

    # A failing quality check is a finding to report, not a reason to fail the run.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
