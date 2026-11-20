"""Local permission reclamation coordinator.

The package exposes the storage/model layer (``solution.core``) and a small
command line front end (``python -m solution``).  See ``README.md`` for the
input format, error codes and semantics.
"""

from .core import Coordinator, ReclaimError
from .clock import parse_instant, format_instant

__all__ = ["Coordinator", "ReclaimError", "parse_instant", "format_instant"]
__version__ = "1.0.0"
