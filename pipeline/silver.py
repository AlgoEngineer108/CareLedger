"""Silver stage: conform, type, check, and version.

Four things happen here, and the order matters:

1. **Conform** — hospital B's column names are mapped onto the common data model,
   so both feeds become one frame with one schema.
2. **Key** — a surrogate key is built by appending the data source to the source
   ID. Both hospitals issue the same patient ID range, so this is the only thing
   preventing two different people from collapsing into one row.
3. **Check** — the quality predicates run and set ``is_quarantined``. Failing rows
   are flagged, never dropped, so a bad row stays auditable.
4. **Version** — ``is_current`` marks the live version of each row. On a first load
   every row is current; an incremental load would close the previous version by
   setting it ``False`` (SCD Type 2) rather than overwriting it.
"""

from __future__ import annotations

import logging

import pandas as pd

from .config import (
    DATE_COLUMNS,
    HOSPITAL_A,
    HOSPITAL_B,
    NUMERIC_COLUMNS,
    PATIENT_COLUMN_MAP,
)

log = logging.getLogger(__name__)


# ----------------------------------------------------------------- column helpers

def is_blank(series: pd.Series) -> pd.Series:
    """True where a value is missing, empty, whitespace, or the text ``"null"``.

    The last case is not hypothetical: hospital B delivers first names as the
    literal four-character string ``NULL``, which is a value as far as the file
    format is concerned and a missing name as far as the business is concerned.
    """
    text = series.fillna("").astype(str).str.strip()
    return (text == "") | (text.str.lower() == "null")


def to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def to_date(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", format="mixed")


def _type_columns(frame: pd.DataFrame, table: str) -> pd.DataFrame:
    for column in NUMERIC_COLUMNS.get(table, ()):
        if column in frame:
            frame[column] = to_number(frame[column])
    for column in DATE_COLUMNS.get(table, ()):
        if column in frame:
            frame[column] = to_date(frame[column])
    return frame


def _surrogate_key(frame: pd.DataFrame, source_column: str) -> pd.Series:
    """``<source id>-<datasource>`` — the key that keeps the hospitals apart."""
    return frame[source_column].astype(str) + "-" + frame["datasource"].astype(str)


def _finalise(frame: pd.DataFrame, quarantine: pd.Series) -> pd.DataFrame:
    frame["is_quarantined"] = quarantine
    frame["is_current"] = True
    return frame


# ------------------------------------------------------------------------- tables

def build_patients(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Reconcile both patient feeds onto the common model, then check them."""
    frames = []
    for datasource in (HOSPITAL_A, HOSPITAL_B):
        raw = bronze[f"patients_{datasource}"]
        column_map = PATIENT_COLUMN_MAP[datasource]
        missing = set(column_map) - set(raw.columns)
        if missing:
            raise KeyError(
                f"patients.{datasource} is missing expected columns {sorted(missing)}; "
                "the source schema changed and PATIENT_COLUMN_MAP needs updating"
            )
        frame = raw[list(column_map)].rename(columns=column_map).copy()
        frame["datasource"] = datasource
        frames.append(frame)

    patients = pd.concat(frames, ignore_index=True)
    patients.insert(0, "Patient_Key", _surrogate_key(patients, "SRC_PatientID"))
    patients = _type_columns(patients, "patients")

    quarantine = (
        is_blank(patients["SRC_PatientID"])
        | patients["DOB"].isna()
        | is_blank(patients["FirstName"])
    )
    return _finalise(patients, quarantine)


def build_transactions(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frame = bronze["transactions"].copy()
    frame.insert(0, "SRC_TransactionID", frame["TransactionID"])
    frame["TransactionID"] = _surrogate_key(frame, "SRC_TransactionID")
    frame = _type_columns(frame, "transactions")

    quarantine = (
        is_blank(frame["EncounterID"])
        | is_blank(frame["PatientID"])
        | is_blank(frame["SRC_TransactionID"])
        | frame["VisitDate"].isna()
    )
    return _finalise(frame, quarantine)


def build_claims(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frame = bronze["claims"].copy()
    frame.insert(0, "SRC_ClaimID", frame["ClaimID"])
    frame["ClaimID"] = _surrogate_key(frame, "SRC_ClaimID")
    frame = _type_columns(frame, "claims")

    quarantine = (
        is_blank(frame["SRC_ClaimID"])
        | is_blank(frame["TransactionID"])
        | is_blank(frame["PatientID"])
        | frame["ServiceDate"].isna()
    )
    return _finalise(frame, quarantine)


def build_departments(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frame = bronze["departments"].copy()
    frame = frame.rename(columns={"DeptID": "SRC_Dept_Id"})
    frame.insert(0, "Dept_Id", _surrogate_key(frame, "SRC_Dept_Id"))
    quarantine = is_blank(frame["SRC_Dept_Id"]) | is_blank(frame["Name"])
    return _finalise(frame, quarantine)


def build_providers(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Provider keys arrive source-prefixed already, so no key is built here."""
    frame = bronze["providers"].copy()
    frame = frame.rename(columns={"ProviderID": "SRC_ProviderID"})
    frame.insert(0, "Provider_Key", frame["SRC_ProviderID"])
    quarantine = is_blank(frame["SRC_ProviderID"]) | is_blank(frame["NPI"])
    return _finalise(frame, quarantine)


def build_encounters(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frame = bronze["encounters"].copy()
    frame.insert(0, "SRC_EncounterID", frame["EncounterID"])
    frame["EncounterID"] = _surrogate_key(frame, "SRC_EncounterID")
    frame["EncounterDate"] = to_date(frame["EncounterDate"])
    quarantine = (
        is_blank(frame["SRC_EncounterID"])
        | is_blank(frame["PatientID"])
        | frame["EncounterDate"].isna()
    )
    return _finalise(frame, quarantine)


def build_cptcodes(bronze: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The CPT catalogue, whose descriptions contain commas and quoted text."""
    frame = bronze["cptcodes"].rename(
        columns={
            "CPT Codes": "CPT_Code",
            "Procedure Code Category": "Category",
            "Procedure Code Descriptions": "Description",
            "Code Status": "Code_Status",
        }
    ).copy()
    for column in ("CPT_Code", "Description", "Code_Status"):
        frame[column] = frame[column].fillna("").astype(str).str.strip()
    quarantine = is_blank(frame["CPT_Code"])
    return _finalise(frame, quarantine)


BUILDERS = {
    "patients": build_patients,
    "transactions": build_transactions,
    "claims": build_claims,
    "departments": build_departments,
    "providers": build_providers,
    "encounters": build_encounters,
    "cptcodes": build_cptcodes,
}


def build(bronze: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Run every silver builder and log what each one quarantined."""
    silver: dict[str, pd.DataFrame] = {}
    for table, builder in BUILDERS.items():
        frame = builder(bronze)
        silver[table] = frame
        held = int(frame["is_quarantined"].sum())
        log.debug("silver.%s → %d rows, %d quarantined", table, len(frame), held)
    return silver
