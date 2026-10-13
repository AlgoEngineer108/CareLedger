"""Regression tests for the pipeline.

These assert the figures the pipeline produces from the committed datasets. They
are deliberately exact: the source files do not change, so if a number here moves,
a transformation rule changed and the change needs explaining.

Run with pytest, or directly::

    python3 -m pytest tests/
    python3 tests/test_pipeline.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import run  # noqa: E402
from pipeline.quality import summarise  # noqa: E402
from pipeline.silver import is_blank  # noqa: E402

import pandas as pd  # noqa: E402

RESULT = run(write=False)


# ------------------------------------------------------------------- row counts

def test_row_counts() -> None:
    """Each table loads the expected number of rows."""
    assert len(RESULT.gold["fact_transactions"]) == 20_000
    assert len(RESULT.gold["dim_patient"]) == 9_985
    assert len(RESULT.gold["dim_provider"]) == 55
    assert len(RESULT.gold["dim_department"]) == 40
    assert len(RESULT.gold["dim_cpt_code"]) == 1_161
    assert len(RESULT.silver["claims"]) == 20_000


def test_quarantine() -> None:
    """Exactly the 15 hospital B patients with a NULL first name are held back."""
    assert RESULT.quarantined == 15
    quarantined = RESULT.silver["patients"].loc[RESULT.silver["patients"]["is_quarantined"]]
    assert set(quarantined["datasource"]) == {"hosb"}
    assert is_blank(quarantined["FirstName"]).all()
    # and they are the difference between the silver and gold patient counts
    assert len(RESULT.silver["patients"]) - len(RESULT.gold["dim_patient"]) == 15


def test_no_transactions_or_claims_quarantined() -> None:
    """The transaction and claim null checks pass on every row."""
    assert RESULT.silver["transactions"]["is_quarantined"].sum() == 0
    assert RESULT.silver["claims"]["is_quarantined"].sum() == 0


# ---------------------------------------------------------------------- money

def test_headline_money() -> None:
    """Charged, collected, and the rate between them."""
    head = RESULT.metrics["headline"]
    assert head["charged"] == 10_490_422.05
    assert head["collected"] == 8_196_514.10
    assert head["collection_rate_pct"] == 78.13
    # Differenced at full precision and rounded once, which is a cent away from
    # subtracting the two already-rounded figures above. Rounding last is correct.
    assert head["uncollected"] == 2_293_907.96


def test_monthly_totals_sum_to_the_headline() -> None:
    """Every fact row lands in exactly one service month."""
    monthly = RESULT.metrics["monthly"]
    assert len(monthly) == 11
    assert monthly[0]["month"] == "2024-01"
    assert monthly[-1]["month"] == "2024-11"
    assert monthly[-1]["partial"] is True
    assert round(sum(row["charged"] for row in monthly), 2) == RESULT.metrics["headline"]["charged"]
    assert sum(row["transactions"] for row in monthly) == 20_000


def test_claims() -> None:
    head = RESULT.metrics["headline"]
    assert head["claims"] == 20_000
    assert head["claim_transaction_keys"] == 12_692
    assert head["denied_or_rejected"] == 7_979
    assert head["denial_rate_pct"] == 39.90
    outcomes = RESULT.metrics["claim_outcomes"]
    assert sum(outcomes.values()) == 20_000


# ------------------------------------------------------------------------ keys

def test_surrogate_keys_separate_the_hospitals() -> None:
    """Both hospitals issue the same patient IDs; the key keeps them distinct."""
    patients = RESULT.gold["dim_patient"]
    assert patients["patient_key"].is_unique
    ids_a = set(patients.loc[patients["datasource"] == "hosa", "src_patientid"])
    ids_b = set(patients.loc[patients["datasource"] == "hosb", "src_patientid"])
    assert len(ids_a & ids_b) == 4_985, "the ID ranges overlap — this is why the suffix exists"
    assert patients["patient_key"].str.endswith(("-hosa", "-hosb")).all()


def test_fact_keys_are_built_from_the_source() -> None:
    fact = RESULT.gold["fact_transactions"]
    assert fact["TransactionID"].is_unique
    assert fact["FK_ProviderID"].str.startswith(("H1-", "H2-")).all()
    hosa = fact.loc[fact["datasource"] == "hosa"]
    assert hosa["FK_PatientID"].str.endswith("-hosa").all()
    assert hosa["FK_ProviderID"].str.startswith("H1-").all()


# --------------------------------------------------------------------- quality

def test_quality_findings() -> None:
    """The known defects in this dataset are all reported."""
    summary = summarise(RESULT.findings)
    assert summary["critical"] == 2
    assert summary["serious"] == 1
    titles = " ".join(f.title for f in RESULT.findings)
    assert "dim_provider" in titles
    assert "CPT catalogue" in titles
    assert "PaidDate" in titles


def test_referential_integrity_numbers() -> None:
    """Match rates the dashboard reports on each arm of the star."""
    by_title = {f.title: f for f in RESULT.findings}
    provider = by_title["Provider keys in the fact table are not in dim_provider"]
    assert provider.numbers["unmatched"] == 18_970
    cpt = by_title["Procedure codes do not match the CPT catalogue"]
    assert cpt.numbers["unmatched"] == 19_762
    dates = by_title["PaidDate is not ordered against ServiceDate"]
    assert dates.numbers["paid_before_service"] == 9_959
    assert dates.numbers["min_lag_days"] == -310
    assert dates.numbers["max_lag_days"] == 309


def test_department_join_is_clean() -> None:
    """Every fact row resolves to a department, unlike provider and CPT."""
    fact = RESULT.gold["fact_transactions"]
    known = set(RESULT.gold["dim_department"]["Dept_Id"])
    assert (~fact["FK_DeptID"].isin(known)).sum() == 0
    assert len(RESULT.metrics["by_department"]) == 20


# --------------------------------------------------------------------- typing

def test_columns_are_typed() -> None:
    """Money is numeric and dates are dates by the time gold is built."""
    fact = RESULT.gold["fact_transactions"]
    assert pd.api.types.is_numeric_dtype(fact["Charge_Amt"])
    assert pd.api.types.is_numeric_dtype(fact["Paid_Amt"])
    assert pd.api.types.is_datetime64_any_dtype(fact["ServiceDate"])
    assert fact["Charge_Amt"].notna().all()
    assert (fact["Charge_Amt"] > 0).all()


def test_age_bands_cover_every_patient() -> None:
    bands = RESULT.metrics["age_bands"]
    assert sum(band["patients"] for band in bands) == len(RESULT.gold["dim_patient"])
    assert [band["band"] for band in bands] == ["0-17", "18-34", "35-49", "50-64", "65-79", "80+"]


def main() -> int:
    """Run every test in this module without pytest installed."""
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as error:
            failed += 1
            print(f"FAIL {test.__name__}: {error or 'assertion failed'}")
        else:
            print(f"pass {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
