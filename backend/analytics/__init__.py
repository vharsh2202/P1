"""P1 local analytics engine.

The package is deliberately split by concern:

``config``      runtime knobs
``profile``     schema introspection, semantic column roles, question->column binding
``ingest``      file -> DuckDB table
``store``       dataset registry and persistence
``analyses``    schema-agnostic analysis templates (SQL + chart + prose)
``router``      local embedding router (question -> analysis)
``formatting``  markdown/table/value rendering
``insights``    automatic charts derived from a freshly ingested dataset
"""

__all__ = [
    "config",
    "profile",
    "ingest",
    "store",
    "analyses",
    "router",
    "formatting",
    "insights",
]
