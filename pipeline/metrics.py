"""Gold-layer aggregations — the figures a revenue cycle team actually reads.

Every metric is computed from the gold tables only, never from the raw files, so
anything reported here has already passed the silver quality gate.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

#: Patient age bands, in the order they should be presented.
AGE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("0-17", 0, 17),
    ("18-34", 18, 34),
    ("35-49", 35, 49),
    ("50-64", 50, 64),
    ("65-79", 65, 79),
    ("80+", 80, 200),
)

#: Claim statuses grouped by what they mean for the revenue cycle.
CLAIM_OUTCOMES: dict[str, tuple[str, ...]] = {
    "settled": ("Paid", "Approved"),
    "in_flight": ("Pending",),
    "needs_rework": ("Denied", "Rejected"),
}


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def _money(value: float) -> float:
    return round(float(value), 2)


def _charge_rollup(fact: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    """Charged, collected, and collection rate grouped by one column."""
    grouped = (
        fact.groupby(column, dropna=False)
        .agg(charged=("Charge_Amt", "sum"), collected=("Paid_Amt", "sum"), transactions=("Charge_Amt", "size"))
        .reset_index()
        .sort_values("charged", ascending=False)
    )
    return [
        {
            column: row[column],
            "charged": _money(row["charged"]),
            "collected": _money(row["collected"]),
            "transactions": int(row["transactions"]),
            "collection_rate_pct": _rate(row["collected"], row["charged"]),
        }
        for _, row in grouped.iterrows()
    ]


def patient_age_bands(dim_patient: pd.DataFrame, as_of: dt.date | None = None) -> list[dict[str, Any]]:
    """Count patients per age band, from date of birth."""
    as_of = as_of or dt.date.today()
    dob = pd.to_datetime(dim_patient["dob"], errors="coerce").dropna()
    # whole years elapsed, not calendar-year difference
    age = (pd.Timestamp(as_of) - dob).dt.days // 365.2425
    counts = []
    for label, low, high in AGE_BANDS:
        counts.append({"band": label, "patients": int(((age >= low) & (age <= high)).sum())})
    return counts


def monthly_revenue(fact: pd.DataFrame) -> list[dict[str, Any]]:
    """Charged against collected, by service month."""
    frame = fact.dropna(subset=["ServiceDate"]).copy()
    frame["month"] = frame["ServiceDate"].dt.strftime("%Y-%m")
    grouped = (
        frame.groupby("month")
        .agg(charged=("Charge_Amt", "sum"), collected=("Paid_Amt", "sum"), transactions=("Charge_Amt", "size"))
        .reset_index()
        .sort_values("month")
    )
    rows = [
        {
            "month": row["month"],
            "charged": _money(row["charged"]),
            "collected": _money(row["collected"]),
            "uncollected": _money(row["charged"] - row["collected"]),
            "transactions": int(row["transactions"]),
            "collection_rate_pct": _rate(row["collected"], row["charged"]),
            "partial": False,
        }
        for _, row in grouped.iterrows()
    ]
    if rows:
        # the last month in the window is cut off mid-month by the extract date
        rows[-1]["partial"] = True
    return rows


def compute(gold: dict[str, pd.DataFrame], silver: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Build the full metrics document the dashboard and the CLI report read."""
    fact = gold["fact_transactions"]
    claims = silver["claims"].loc[~silver["claims"]["is_quarantined"]]

    charged = float(fact["Charge_Amt"].sum())
    collected = float(fact["Paid_Amt"].sum())

    status_counts = claims["ClaimStatus"].value_counts()
    outcomes = {
        name: int(status_counts.reindex(list(statuses)).fillna(0).sum())
        for name, statuses in CLAIM_OUTCOMES.items()
    }

    payors = (
        claims.groupby("PayorType")
        .agg(claims=("ClaimAmount", "size"), amount=("ClaimAmount", "sum"))
        .reset_index()
        .sort_values("claims", ascending=False)
    )

    metrics: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "headline": {
            "charged": _money(charged),
            "collected": _money(collected),
            "uncollected": _money(charged - collected),
            "collection_rate_pct": _rate(collected, charged),
            "transactions": int(len(fact)),
            "encounters": int(fact["EncounterID"].nunique()),
            "patients_billed": int(fact["FK_PatientID"].nunique()),
            "patients_in_dim": int(len(gold["dim_patient"])),
            "claims": int(len(claims)),
            "claim_transaction_keys": int(
                (claims["TransactionID"].astype(str) + "-" + claims["datasource"]).nunique()
            ),
            "denied_or_rejected": outcomes["needs_rework"],
            "denial_rate_pct": _rate(outcomes["needs_rework"], len(claims)),
            "average_charge": _money(charged / len(fact)) if len(fact) else None,
        },
        "claim_outcomes": outcomes,
        "claim_status": [
            {"status": status, "claims": int(count), "share_pct": _rate(count, len(claims))}
            for status, count in status_counts.items()
        ],
        "payor_mix": [
            {
                "payor_type": row["PayorType"],
                "claims": int(row["claims"]),
                "amount": _money(row["amount"]),
                "share_pct": _rate(row["claims"], len(claims)),
            }
            for _, row in payors.iterrows()
        ],
        "monthly": monthly_revenue(fact),
        "by_visit_type": _charge_rollup(fact, "VisitType"),
        "by_amount_type": _charge_rollup(fact, "AmountType"),
        "by_datasource": _charge_rollup(fact, "datasource"),
        "age_bands": patient_age_bands(gold["dim_patient"]),
        "row_counts": {
            **{f"gold.{name}": int(len(frame)) for name, frame in gold.items()},
            **{f"silver.{name}": int(len(frame)) for name, frame in silver.items()},
        },
    }

    # charges by department, resolved through the dimension
    departments = gold["dim_department"][["Dept_Id", "Name"]]
    joined = fact.merge(departments, left_on="FK_DeptID", right_on="Dept_Id", how="left")
    metrics["by_department"] = [
        {
            "department": row["Name"],
            "charged": row["charged"],
            "collected": row["collected"],
            "transactions": row["transactions"],
            "collection_rate_pct": row["collection_rate_pct"],
        }
        for row in _charge_rollup(joined.dropna(subset=["Name"]), "Name")
    ]
    return metrics
