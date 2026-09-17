"""Handoff of qualified prospects to a downstream outreach agent."""

from .handoff import (
    SCHEMA_VERSION,
    STANDING_CONSTRAINTS,
    OutreachBrief,
    build_brief,
    export_csv,
    export_json,
    personalization_hooks,
    readme_for_agent,
    suggested_angle,
)

__all__ = [
    "OutreachBrief",
    "build_brief",
    "export_json",
    "export_csv",
    "readme_for_agent",
    "personalization_hooks",
    "suggested_angle",
    "STANDING_CONSTRAINTS",
    "SCHEMA_VERSION",
]
