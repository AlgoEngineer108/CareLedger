"""CareLedger: a medallion pipeline over hospital revenue cycle data.

Stages run landing → bronze → silver → gold. Import :func:`run` to execute the
whole pipeline, or the layer modules to use one stage on its own.

    >>> from pipeline import run
    >>> result = run(write=False)
    >>> result.metrics["headline"]["collection_rate_pct"]
    78.13
"""

from .runner import RunResult, StageResult, run, write_outputs

__all__ = ["run", "write_outputs", "RunResult", "StageResult"]
__version__ = "1.0.0"
