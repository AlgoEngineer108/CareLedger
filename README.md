# CareLedger

End-to-end medallion pipeline that turns two hospitals' raw billing feeds into a clean, queryable star schema for revenue cycle reporting — and runs entirely in the browser, with no cloud account or cluster needed.

Open the dashboard and the whole pipeline executes in front of you: 71,000 source rows read from `datasets/`, conformed, quality-checked, and loaded into a star schema, with every figure on the page counted from that run.

```sh
./dashboard/run.sh
# then open http://localhost:8787/dashboard/
```

## What it does

| Stage | Job |
|---|---|
| **Landing** | Reads 12 delimited files exactly as delivered — the audit trail, never edited. |
| **Bronze** | Types every column and stamps each row with its `datasource`. |
| **Silver** | Maps both hospitals onto common column names, applies quality predicates, quarantines failures, versions rows with `is_current`. |
| **Gold** | Loads a star schema — four dimensions and one fact — from current, clean rows only. |

The two hospitals do **not** share a schema. Hospital B delivers `ID`, `F_Name`, `L_Name`, `M_Name`, `Updated_Date` where hospital A delivers `PatientID`, `FirstName`, `LastName`, `MiddleName`, `ModifiedDate`. The silver layer reconciles them into one common data model before anything downstream sees them.

Both hospitals also issue the **same patient ID range** — 4,985 IDs collide across the two feeds. Surrogate keys append the data source (`HOSP1-000402-hosb`), which is the only thing keeping two different people from being merged into one.

## The star schema

```
          dim_patient                     dim_provider
          9,985 rows                        55 rows
                \                           /
                 \                         /
                  gold.fact_transactions
                      20,000 rows
                  one billed line each
                 /                         \
                /                           \
        dim_department                   dim_cpt_code
          40 rows                         1,161 rows
```

`fact_transactions` holds the measures — `Charge_Amt`, `Paid_Amt`, service and paid dates — at the grain of one billed transaction line, with four foreign keys out to the dimensions that describe them.

## What the run reports

| Figure | Value |
|---|---|
| Charged amount | $10,490,422.05 |
| Collected amount | $8,196,514.10 |
| Collection rate | 78.1% |
| Transactions | 20,000 |
| Claims processed | 20,000 over 12,692 transaction keys |
| Denied or rejected | 39.9% |
| Rows quarantined | 15 of 51,256 |

## Data quality is a first-class view

The dashboard has a tab for what this data **cannot** support, because shipping a confident-looking metric over broken data is worse than shipping no metric:

- **Provider keys don't resolve.** The transaction feed references 1,000 provider keys; the provider extracts define 55. 18,970 of 20,000 fact rows (94.9%) have no matching dimension row — so there is deliberately no revenue-by-provider chart.
- **Procedure codes don't match the CPT catalogue.** 19,762 of 20,000 fact rows (98.8%) carry a code absent from `dim_cpt_code`.
- **`PaidDate` is not ordered against `ServiceDate`.** 9,959 rows are paid *before* the service date, spread from −310 to +309 days. Days-to-payment and AR aging — normally the headline of an RCM dashboard — cannot be computed, so they are absent rather than wrong.
- **Claim amounts don't reconcile to charges.** $50.66M claimed against $10.49M charged, 4.8× apart, because the feeds are generated independently. Claim-side and charge-side metrics stay in separate cards.
- **Quarantine orphans dependent facts.** 15 patients fail the `lower(firstname) = 'null'` check and are held out of `dim_patient`; their 36 transactions still load, now pointing at a key that no longer exists. The fix is an unknown-member row or quarantining the facts in the same run.

Each finding carries the count behind it and what fixing it would take.

## The dashboard

Five views, all computed client-side from the CSVs:

- **Revenue overview** — collection rate, charged against collected by service month, claim outcomes grouped as settled / in flight / needs rework, charges by department, payer mix, patient age bands.
- **Pipeline run** — per-stage row counts and timings for the run you are looking at, the architecture end to end, and the config table that drives ingestion.
- **Star schema** — the model with a live join match rate on every arm, table grains, and real row previews.
- **Data quality** — the findings above, ordered by severity.
- **How to read this** — the revenue cycle path a dollar takes, 22 defined terms, and a provenance table showing how each headline figure was computed.

## Config-driven ingestion

`datasets/load_config.csv` is the control table: one row per source table, with `loadtype` choosing full against incremental, `watermark` naming the column an incremental load compares, and `is_active` switching a table off. Adding an ingestion is a row, not a code change — a Lookup activity reads the table and a ForEach loops it, one Copy activity per row.

## Repository layout

```
dashboard/     the pipeline and dashboard (single HTML file, no build step)
datasets/      source files the pipeline reads
  EMR/         hospital-a and hospital-b billing extracts
  claims/      claim feeds, one per hospital
  cptcodes/    CPT procedure code reference catalogue
  load_config.csv   ingestion control table
```

## Notes

- `datasets/` is **synthetic** sample data — generated hospital billing records, no real patients. The dashboard projects `dim_patient` without SSN, phone or address regardless.
- The page must be served from the repository root, since it reads `../datasets/` with relative paths. Opening `index.html` as a `file://` URL will not work — browsers block `fetch` on local files.
- No dependencies, no build, no cloud resources. One HTML file and the CSVs.
