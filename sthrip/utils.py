"""Shared utility functions."""


def escape_ilike(value: str) -> str:
    """Escape SQL ILIKE wildcard characters in user input."""
    return (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
