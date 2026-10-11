"""Versioned configuration: taxonomy, path templates and placement policy."""

from enum import StrEnum


class ConfigurationKind(StrEnum):
    """The configuration documents a person edits through the control plane."""

    TAXONOMY = "taxonomy"
    TEMPLATES = "templates"
    POLICY = "policy"
