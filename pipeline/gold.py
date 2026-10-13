"""Gold stage: the star schema a report or BI tool reads.

Only current, clean rows cross this boundary — ``is_current and not
is_quarantined`` is applied to every source table. Dimensions are truncated and
reloaded; the fact table is rebuilt at the grain of one billed transaction line
with a foreign key out to each dimension.
"""

from __future__ import annotations

import logging

import pandas as pd

from .config import PROVIDER_KEY_PREFIX

log = logging.getLogger(__name__)


def current_clean(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows gold is allowed to see."""
    mask = ~frame["is_quarantined"]
    if "is_current" in frame:
        mask &= frame["is_current"]
    return frame.loc[mask]


def build_dim_patient(silver: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Patient dimension.

    Deliberately projected without SSN, phone, or address: no report in this
    project needs them, and a dimension nobody queries for a column is the wrong
    place to carry it.
    """
    patients = current_clean(silver["patients"])
    return pd.DataFrame(
        {
            "patient_key": patients["Patient_Key"],
            "src_patientid": patients["SRC_PatientID"],
            "firstname": patients["FirstName"],
            "lastname": patients["LastName"],
            "gender": patients["Gender"],
            "dob": patients["DOB"],
            "datasource": patients["datasource"],
        }
    ).reset_index(drop=True)


def build_dim_department(silver: dict[str, pd.DataFrame]) -> pd.DataFrame:
    departments = current_clean(silver["departments"])
    return (
        departments[["Dept_Id", "SRC_Dept_Id", "Name", "datasource"]]
        .drop_duplicates()
        .reset_index(drop=True)
    )


def build_dim_provider(silver: dict[str, pd.DataFrame]) -> pd.DataFrame:
    providers = current_clean(silver["providers"])
    return (
        providers[
            ["Provider_Key", "FirstName", "LastName", "Specialization", "DeptID", "NPI", "datasource"]
        ]
        .drop_duplicates(subset="Provider_Key")
        .reset_index(drop=True)
    )


def build_dim_cpt_code(silver: dict[str, pd.DataFrame]) -> pd.DataFrame:
    codes = current_clean(silver["cptcodes"])
    return (
        codes[["CPT_Code", "Category", "Description", "Code_Status"]]
        .drop_duplicates(subset="CPT_Code")
        .reset_index(drop=True)
    )


def build_fact_transactions(silver: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Transaction fact table, one row per billed line.

    The provider key is rebuilt with the same ``H1-``/``H2-`` prefix the provider
    extracts use, so the join can be attempted — whether it *succeeds* is what the
    quality checks measure.
    """
    transactions = current_clean(silver["transactions"])
    prefix = transactions["datasource"].map(PROVIDER_KEY_PREFIX)
    if prefix.isna().any():
        unknown = sorted(transactions.loc[prefix.isna(), "datasource"].unique())
        raise ValueError(f"no provider key prefix configured for datasource {unknown}")

    return pd.DataFrame(
        {
            "TransactionID": transactions["TransactionID"],
            "SRC_TransactionID": transactions["SRC_TransactionID"],
            "EncounterID": transactions["EncounterID"],
            "FK_PatientID": transactions["PatientID"].astype(str) + "-" + transactions["datasource"],
            "FK_ProviderID": prefix + transactions["ProviderID"].astype(str),
            "FK_DeptID": transactions["DeptID"].astype(str) + "-" + transactions["datasource"],
            "ICDCode": transactions["ICDCode"],
            "CPT_Code": transactions["ProcedureCode"],
            "VisitType": transactions["VisitType"],
            "ServiceDate": transactions["ServiceDate"],
            "PaidDate": transactions["PaidDate"],
            "Charge_Amt": transactions["Amount"],
            "Paid_Amt": transactions["PaidAmount"],
            "AmountType": transactions["AmountType"],
            "ClaimID": transactions["ClaimID"],
            "datasource": transactions["datasource"],
        }
    ).reset_index(drop=True)


BUILDERS = {
    "dim_patient": build_dim_patient,
    "dim_department": build_dim_department,
    "dim_provider": build_dim_provider,
    "dim_cpt_code": build_dim_cpt_code,
    "fact_transactions": build_fact_transactions,
}


def build(silver: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    gold: dict[str, pd.DataFrame] = {}
    for table, builder in BUILDERS.items():
        gold[table] = builder(silver)
        log.debug("gold.%s → %d rows", table, len(gold[table]))
    return gold
