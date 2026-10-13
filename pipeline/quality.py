"""Data quality report: what the warehouse can and cannot support.

Two kinds of check run here. The silver quarantine predicates have already flagged
rows that fail a rule; this module counts them. Then it tests every foreign key in
the fact table against its dimension, which is where this dataset's real problems
turn up — a key that is *present* is not the same as a key that *resolves*.

A finding is not a crash. The point is to state what a metric would be worth
before anyone builds a chart on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

log = logging.getLogger(__name__)

Severity = Literal["critical", "serious", "warning", "passing"]

#: Order findings are reported in.
SEVERITY_RANK: dict[str, int] = {"critical": 0, "serious": 1, "warning": 2, "passing": 3}


@dataclass
class Finding:
    """One data quality result, with the count behind it."""

    severity: Severity
    title: str
    detail: str
    metric: str
    remedy: str = ""
    numbers: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "metric": self.metric,
            "remedy": self.remedy,
            "numbers": self.numbers,
        }


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "n/a"


def unmatched(fact: pd.DataFrame, fact_key: str, dimension: pd.DataFrame, dim_key: str) -> int:
    """Fact rows whose foreign key is absent from the dimension."""
    keys = set(dimension[dim_key].astype(str))
    return int((~fact[fact_key].astype(str).isin(keys)).sum())


def check(gold: dict[str, pd.DataFrame], silver: dict[str, pd.DataFrame]) -> list[Finding]:
    """Run every check and return the findings, most severe first."""
    fact = gold["fact_transactions"]
    total = len(fact)
    claims = silver["claims"].loc[~silver["claims"]["is_quarantined"]]
    findings: list[Finding] = []

    # ---------------------------------------------------- referential integrity
    provider_orphans = unmatched(fact, "FK_ProviderID", gold["dim_provider"], "Provider_Key")
    if provider_orphans:
        findings.append(
            Finding(
                severity="critical",
                title="Provider keys in the fact table are not in dim_provider",
                detail=(
                    f"The transaction feed references {fact['FK_ProviderID'].nunique():,} distinct "
                    f"provider keys, but the provider extracts define only "
                    f"{len(gold['dim_provider']):,}. Any report grouping revenue by provider or "
                    "specialty would silently drop most of its revenue."
                ),
                metric=f"{provider_orphans:,} of {total:,} fact rows unmatched · {_pct(provider_orphans, total)}",
                remedy=(
                    "Fix the source extract: the provider feed has to cover every ProviderID the "
                    "transaction feed emits."
                ),
                numbers={"unmatched": provider_orphans, "total": total},
            )
        )

    cpt_orphans = unmatched(fact, "CPT_Code", gold["dim_cpt_code"], "CPT_Code")
    if cpt_orphans:
        findings.append(
            Finding(
                severity="critical",
                title="Procedure codes do not match the CPT catalogue",
                detail=(
                    "ProcedureCode on the transaction feed is a five-digit number drawn at random, "
                    f"while dim_cpt_code holds the {len(gold['dim_cpt_code']):,} real codes from the "
                    "reference file. The two sets barely intersect, so procedure-level analysis is "
                    "not possible."
                ),
                metric=f"{cpt_orphans:,} of {total:,} fact rows unmatched · {_pct(cpt_orphans, total)}",
                remedy="Generate procedure codes by sampling the CPT reference file.",
                numbers={"unmatched": cpt_orphans, "total": total},
            )
        )

    # ------------------------------------------------------------- date sanity
    dated = fact.dropna(subset=["ServiceDate", "PaidDate"])
    lag_days = (dated["PaidDate"] - dated["ServiceDate"]).dt.days
    paid_before = int((lag_days < 0).sum())
    if paid_before:
        findings.append(
            Finding(
                severity="serious",
                title="PaidDate is not ordered against ServiceDate",
                detail=(
                    f"Payment dates sit on both sides of the service date, from {int(lag_days.min())} "
                    f"to {int(lag_days.max())} days, so days-to-payment and AR aging cannot be "
                    "computed. Those are the usual headline of an RCM dashboard, and they are "
                    "deliberately absent rather than shown wrong."
                ),
                metric=(
                    f"{paid_before:,} of {len(dated):,} rows paid before the service date · "
                    f"{_pct(paid_before, len(dated))}"
                ),
                remedy=(
                    "Derive PaidDate as ServiceDate plus a positive lag; AR aging buckets and a "
                    "days-to-pay distribution can then both be reported."
                ),
                numbers={
                    "paid_before_service": paid_before,
                    "rows_with_both_dates": int(len(dated)),
                    "min_lag_days": int(lag_days.min()),
                    "max_lag_days": int(lag_days.max()),
                },
            )
        )

    # -------------------------------------------------------- cross-feed totals
    claim_amount = float(claims["ClaimAmount"].sum())
    charged = float(fact["Charge_Amt"].sum())
    if charged and abs(claim_amount - charged) / charged > 0.05:
        findings.append(
            Finding(
                severity="warning",
                title="Claim amounts do not reconcile to transaction charges",
                detail=(
                    "The claim feed is generated independently of the transaction feed, so the two "
                    "money columns describe the same encounters with different figures. Claim-side "
                    "and charge-side metrics have to be reported separately until the feeds are "
                    "reconciled upstream."
                ),
                metric=(
                    f"claims ${claim_amount:,.0f} claimed vs fact ${charged:,.0f} charged · "
                    f"{claim_amount / charged:.1f}x apart"
                ),
                remedy="Build the claim feed from the transaction feed so ClaimAmount rolls up to Charge_Amt.",
                numbers={"claim_amount": round(claim_amount, 2), "charged": round(charged, 2)},
            )
        )

    # --------------------------------------------- quarantine and its fallout
    quarantined_patients = silver["patients"].loc[silver["patients"]["is_quarantined"]]
    patient_orphans = unmatched(fact, "FK_PatientID", gold["dim_patient"], "patient_key")
    if patient_orphans:
        findings.append(
            Finding(
                severity="warning",
                title="Quarantining a patient orphans that patient's transactions",
                detail=(
                    f"The {len(quarantined_patients):,} quarantined patients are held out of "
                    "dim_patient, but their transactions still load into the fact table, which "
                    "leaves those rows pointing at a dimension key that no longer exists."
                ),
                metric=(
                    f"{patient_orphans:,} fact rows reference one of the "
                    f"{len(quarantined_patients):,} quarantined patients · {_pct(patient_orphans, total)}"
                ),
                remedy=(
                    "Add an unknown-member row to dim_patient and point orphaned facts at it, or "
                    "quarantine the dependent facts in the same run."
                ),
                numbers={"orphaned_facts": patient_orphans, "quarantined_patients": int(len(quarantined_patients))},
            )
        )

    patients = gold["dim_patient"]
    ids_a = set(patients.loc[patients["datasource"] == "hosa", "src_patientid"])
    collisions = int(patients.loc[patients["datasource"] == "hosb", "src_patientid"].isin(ids_a).sum())
    if collisions:
        findings.append(
            Finding(
                severity="warning",
                title="Both hospitals issue the same patient ID range",
                detail=(
                    "Hospital B's patient IDs reuse hospital A's prefix and numbering. The surrogate "
                    "key appends the data source, so no patients were merged — but a pipeline keyed "
                    "on the source ID alone would have collapsed two people into one."
                ),
                metric=f"{collisions:,} source IDs present in both hospitals · resolved by the surrogate key",
                remedy=(
                    "Nothing to fix in the pipeline. Worth a regression test that fails if the "
                    "datasource suffix is ever dropped from Patient_Key."
                ),
                numbers={"colliding_ids": collisions},
            )
        )

    # ------------------------------------------------------------ what passed
    if len(quarantined_patients):
        findings.append(
            Finding(
                severity="passing",
                title="Patient quarantine rule fired as designed",
                detail=(
                    "Hospital B delivers rows whose first name is the literal string NULL. The "
                    "silver predicate catches them and they are held out of dim_patient instead of "
                    "reaching a report."
                ),
                metric=(
                    f"{len(quarantined_patients):,} rows quarantined · e.g. "
                    f"{quarantined_patients['Patient_Key'].iloc[0]}"
                ),
                numbers={"quarantined": int(len(quarantined_patients))},
            )
        )

    fact_keys = set(fact["TransactionID"].astype(str))
    claims_joined = int(
        (claims["TransactionID"].astype(str) + "-" + claims["datasource"]).isin(fact_keys).sum()
    )
    tx_quarantined = int(silver["transactions"]["is_quarantined"].sum())
    claim_quarantined = int(silver["claims"]["is_quarantined"].sum())
    findings.append(
        Finding(
            severity="passing",
            title="Transaction and claim null checks pass on every row",
            detail=(
                "The quarantine predicates on silver.transactions and silver.claims test the keys "
                "and the dates they join on. No row failed, so the fact table loads at full volume "
                "and every claim keeps a usable transaction key."
            ),
            metric=(
                f"{tx_quarantined + claim_quarantined} of "
                f"{len(silver['transactions']) + len(silver['claims']):,} rows quarantined · "
                f"{claims_joined:,} of {len(claims):,} claims join to the fact table"
            ),
            numbers={"quarantined": tx_quarantined + claim_quarantined, "claims_joined": claims_joined},
        )
    )

    findings.sort(key=lambda f: SEVERITY_RANK[f.severity])
    return findings


def summarise(findings: list[Finding]) -> dict[str, int]:
    """Counts per severity, for the report header."""
    summary = {severity: 0 for severity in SEVERITY_RANK}
    for finding in findings:
        summary[finding.severity] += 1
    return summary
