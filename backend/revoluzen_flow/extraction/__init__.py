"""Extraction runtime — declarative source profiles + a generic engine
that reads them.

The catalog module (`revoluzen_flow.connections.catalog`) handles *discovery*
— what objects exist on a connection. This module handles *runtime
extraction* — how to fetch them efficiently in the face of slow APIs,
rate limits, deeply nested JSON, and resumability requirements.

A new connector is a `SourceProfile` declaration. The engine reads
the profile and applies the right paginator / rate limiter / retry
policy / schema mapper / checkpoint store automatically.
"""

from revoluzen_flow.extraction.engine import ExtractionEngine, ExtractionResult
from revoluzen_flow.extraction.profile import (
    AuthProfile,
    CheckpointProfile,
    ConcurrencyProfile,
    EnrichmentProfile,
    PaginationProfile,
    RateLimitProfile,
    SchemaProfile,
    SourceProfile,
)
from revoluzen_flow.extraction.schema_mapper import (
    SchemaMapper,
    coerce_value,
    get_json_path,
)
from revoluzen_flow.extraction.session import build_session

__all__ = [
    "AuthProfile",
    "CheckpointProfile",
    "ConcurrencyProfile",
    "EnrichmentProfile",
    "ExtractionEngine",
    "ExtractionResult",
    "PaginationProfile",
    "RateLimitProfile",
    "SchemaProfile",
    "SourceProfile",
    "SchemaMapper",
    "build_session",
    "coerce_value",
    "get_json_path",
]
