"""catalog.toml: the declarative list of series `tempus sync` keeps up to date."""

from dataclasses import dataclass
from pathlib import Path

import tomlkit


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    id: str


def load(path: Path) -> list[CatalogEntry]:
    if not path.exists():
        return []
    doc = tomlkit.parse(path.read_text())
    return [CatalogEntry(source=s["source"], id=s["id"]) for s in doc.get("series", [])]


def add(path: Path, source: str, series_id: str) -> bool:
    """Append an entry; returns False if it was already present."""
    if CatalogEntry(source, series_id) in load(path):
        return False
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    if "series" not in doc:
        doc["series"] = tomlkit.aot()
    doc["series"].append({"source": source, "id": series_id})
    path.write_text(tomlkit.dumps(doc))
    return True


def remove(path: Path, source: str, series_id: str) -> bool:
    """Remove an entry; returns False if it wasn't present."""
    if not path.exists():
        return False
    doc = tomlkit.parse(path.read_text())
    entries = doc.get("series", [])
    for i, s in enumerate(entries):
        if s["source"] == source and s["id"] == series_id:
            del entries[i]
            path.write_text(tomlkit.dumps(doc))
            return True
    return False
