"""Paths, source registry, and the column maps that reconcile the two hospitals.

Everything the pipeline needs to know about *where* data comes from and *what it is
called* lives here, so the layer modules only have to express transformations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# --------------------------------------------------------------------------- paths

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASETS = REPO_ROOT / "datasets"
OUTPUT = REPO_ROOT / "output"

LAYERS = ("bronze", "silver", "gold")


def layer_dir(layer: str) -> Path:
    """Output directory for a medallion layer, created on demand."""
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; expected one of {LAYERS}")
    path = OUTPUT / layer
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------- source files

#: Data source codes. Every surrogate key in silver and gold carries one of these,
#: which is what keeps two hospitals that issue identical IDs from being merged.
HOSPITAL_A = "hosa"
HOSPITAL_B = "hosb"


@dataclass(frozen=True)
class SourceFile:
    """One file the landing stage reads.

    Attributes:
        table: Logical table the file belongs to. Several files can share one.
        path: Location on disk, relative to ``datasets/``.
        datasource: Hospital code stamped onto every row, or ``None`` for
            reference data that belongs to no single hospital.
        origin: Human-readable description of where the file came from upstream.
    """

    table: str
    path: str
    datasource: str | None
    origin: str

    @property
    def full_path(self) -> Path:
        return DATASETS / self.path

    @property
    def key(self) -> str:
        """Registry key: ``transactions.hosa``, or ``cptcodes`` for shared files."""
        return f"{self.table}.{self.datasource}" if self.datasource else self.table


SOURCES: tuple[SourceFile, ...] = (
    # Hospital A — EMR extract out of Azure SQL
    SourceFile("transactions", "EMR/hospital-a/transactions.csv", HOSPITAL_A, "Azure SQL · hospital-a"),
    SourceFile("patients", "EMR/hospital-a/patients.csv", HOSPITAL_A, "Azure SQL · hospital-a"),
    SourceFile("departments", "EMR/hospital-a/departments.csv", HOSPITAL_A, "Azure SQL · hospital-a"),
    SourceFile("providers", "EMR/hospital-a/providers.csv", HOSPITAL_A, "Azure SQL · hospital-a"),
    SourceFile("encounters", "EMR/hospital-a/encounters.csv", HOSPITAL_A, "Azure SQL · hospital-a"),
    # Hospital B — same tables, different column names
    SourceFile("transactions", "EMR/hospital-b/transactions.csv", HOSPITAL_B, "Azure SQL · hospital-b"),
    SourceFile("patients", "EMR/hospital-b/patients.csv", HOSPITAL_B, "Azure SQL · hospital-b"),
    SourceFile("departments", "EMR/hospital-b/departments.csv", HOSPITAL_B, "Azure SQL · hospital-b"),
    SourceFile("providers", "EMR/hospital-b/providers.csv", HOSPITAL_B, "Azure SQL · hospital-b"),
    SourceFile("encounters", "EMR/hospital-b/encounters.csv", HOSPITAL_B, "Azure SQL · hospital-b"),
    # Claim feeds arrive as flat files, one per hospital
    SourceFile("claims", "claims/hospital1_claim_data.csv", HOSPITAL_A, "Flat file drop · claims"),
    SourceFile("claims", "claims/hospital2_claim_data.csv", HOSPITAL_B, "Flat file drop · claims"),
    # Reference data, owned by neither hospital
    SourceFile("cptcodes", "cptcodes/cptcodes.csv", None, "Reference file · CPT catalogue"),
)

#: Ingestion control table. ADF reads this with a Lookup activity and drives one
#: Copy activity per row inside a ForEach, so adding a table is a row, not a code
#: change. The local runner loads every source as a full refresh instead of
#: honouring ``is_active``, and reports the table for reference.
LOAD_CONFIG = DATASETS / "load_config.csv"


# --------------------------------------------------------------- the common model

#: Hospital B ships the patient table with its own column names. The silver layer
#: renames both feeds onto this common data model before anything joins them.
PATIENT_COLUMN_MAP: Mapping[str, Mapping[str, str]] = {
    HOSPITAL_A: {
        "PatientID": "SRC_PatientID",
        "FirstName": "FirstName",
        "LastName": "LastName",
        "MiddleName": "MiddleName",
        "Gender": "Gender",
        "DOB": "DOB",
        "ModifiedDate": "SRC_ModifiedDate",
    },
    HOSPITAL_B: {
        "ID": "SRC_PatientID",
        "F_Name": "FirstName",
        "L_Name": "LastName",
        "M_Name": "MiddleName",
        "Gender": "Gender",
        "DOB": "DOB",
        "Updated_Date": "SRC_ModifiedDate",
    },
}

#: Provider keys arrive already source-prefixed in the extracts (``H1-``, ``H2-``),
#: so the fact table has to rebuild the same prefix to join back to them.
PROVIDER_KEY_PREFIX: Mapping[str, str] = {HOSPITAL_A: "H1-", HOSPITAL_B: "H2-"}

#: Money and date columns, typed once on the way into silver.
NUMERIC_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "transactions": ("Amount", "PaidAmount"),
    "claims": ("ClaimAmount", "PaidAmount", "Deductible", "Coinsurance", "Copay"),
}
DATE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "transactions": ("VisitDate", "ServiceDate", "PaidDate"),
    "claims": ("ServiceDate", "ClaimDate"),
    "patients": ("DOB",),
}
