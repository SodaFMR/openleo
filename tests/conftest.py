"""Shared pytest configuration."""

# Frozen v1.0.0 fixtures and their generator are data, not tests: the generator only runs
# against the v1.0.0 tag and must never be collected or imported by the 2.0 suite.
collect_ignore = ["fixtures"]
