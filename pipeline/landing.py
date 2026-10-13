"""Landing stage: read the source files exactly as delivered.

Nothing is cleaned, cast, or dropped here. Every column arrives as text so that a
malformed value survives to the silver layer, where a quality rule can see it and
quarantine the row, rather than being silently coerced on the way in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .config import LOAD_CONFIG, SOURCES, SourceFile

log = logging.getLogger(__name__)


@dataclass
class LandedFile:
    """A source file after reading, with the counts the stage log reports."""

    source: SourceFile
    frame: pd.DataFrame

    @property
    def rows(self) -> int:
        return len(self.frame)

    @property
    def columns(self) -> int:
        return len(self.frame.columns)

    @property
    def bytes_on_disk(self) -> int:
        return self.source.full_path.stat().st_size


def read_source(source: SourceFile) -> LandedFile:
    """Read one landing file as text.

    ``keep_default_na=False`` matters: hospital B delivers rows whose first name is
    the literal string ``NULL``, and the silver quality rule has to be able to see
    that string. If pandas turned it into a missing value here, the rule would
    still catch it but the stage log could no longer tell the two cases apart.
    """
    frame = pd.read_csv(
        source.full_path,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        skipinitialspace=False,
    )
    log.debug("read %s (%d rows, %d cols)", source.path, len(frame), len(frame.columns))
    return LandedFile(source=source, frame=frame)


def read_all() -> dict[str, LandedFile]:
    """Read every registered source file, keyed by ``table.datasource``."""
    landed: dict[str, LandedFile] = {}
    for source in SOURCES:
        if not source.full_path.exists():
            raise FileNotFoundError(
                f"landing file missing: {source.full_path}. "
                "Run from the repository root so datasets/ resolves."
            )
        landed[source.key] = read_source(source)
    return landed


def read_load_config() -> pd.DataFrame:
    """Read the ingestion control table ADF drives its ForEach from."""
    if not LOAD_CONFIG.exists():
        return pd.DataFrame()
    return pd.read_csv(LOAD_CONFIG, dtype=str, keep_default_na=False, na_values=[])
