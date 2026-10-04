"""catalog.toml: the declarative list of series `tempus sync` keeps up to date."""

from dataclasses import dataclass
from pathlib import Path

import tomlkit


@dataclass(frozen=True)
class CatalogEntry:
    source: str
    id: str
    concept: str | None = None  # manual placement in the concept tree (a pin)


def load(path: Path) -> list[CatalogEntry]:
    if not path.exists():
        return []
    doc = tomlkit.parse(path.read_text())
    return [
        CatalogEntry(source=s["source"], id=s["id"], concept=s.get("concept"))
        for s in doc.get("series", [])
    ]


def pins(entries: list[CatalogEntry]) -> dict[tuple[str, str], str]:
    return {(e.source, e.id): e.concept for e in entries if e.concept}


def add(path: Path, source: str, series_id: str, title: str | None = None) -> bool:
    """Append an entry (with the title as a comment); returns False if already present."""
    if any((e.source, e.id) == (source, series_id) for e in load(path)):
        return False
    # Append as text so existing comments and layout stay exactly as written.
    existing = path.read_text().rstrip("\n") + "\n\n" if path.exists() else ""
    comment = f"  # {' '.join(title.split())}" if title else ""
    path.write_text(f'{existing}[[series]]\nsource = "{source}"\nid = "{series_id}"{comment}\n')
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
