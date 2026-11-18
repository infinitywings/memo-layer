"""An in-memory audit log."""

ENTRIES = []


def record(name):
    ENTRIES.append(name)
