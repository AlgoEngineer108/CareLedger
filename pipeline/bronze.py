"""Bronze stage: union each logical table across hospitals and stamp the source.

The grain does not change — one bronze row per landing row. The only additions are
the ``datasource`` column, which every downstream surrogate key depends on, and a
consistent column order per table.
"""

from __future__ import annotations

import logging

import pandas as pd

from .landing import LandedFile

log = logging.getLogger(__name__)


def build(landed: dict[str, LandedFile]) -> dict[str, pd.DataFrame]:
    """Group landed files into one bronze frame per logical table.

    Hospital A and B are kept apart for ``patients`` only, because their column
    names differ and reconciling them is the silver layer's job. Every other table
    shares a schema and can be unioned here.
    """
    groups: dict[str, list[LandedFile]] = {}
    for landed_file in landed.values():
        table = landed_file.source.table
        key = (
            f"patients_{landed_file.source.datasource}"
            if table == "patients"
            else table
        )
        groups.setdefault(key, []).append(landed_file)

    bronze: dict[str, pd.DataFrame] = {}
    for key, files in groups.items():
        frames = []
        for landed_file in files:
            frame = landed_file.frame.copy()
            frame["datasource"] = landed_file.source.datasource
            frames.append(frame)
        bronze[key] = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        log.debug("bronze.%s → %d rows", key, len(bronze[key]))

    return bronze
