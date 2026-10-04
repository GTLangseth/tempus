from tempus import catalog
from tempus.catalog import CatalogEntry


def test_add_and_remove_round_trip(tmp_path):
    path = tmp_path / "catalog.toml"
    path.write_text('# my series\n\n[[series]]\nsource = "fred"\nid = "GDP" # keep me\n')

    assert catalog.add(path, "fred", "UNRATE")
    assert not catalog.add(path, "fred", "UNRATE")
    assert catalog.load(path) == [CatalogEntry("fred", "GDP"), CatalogEntry("fred", "UNRATE")]
    assert "# keep me" in path.read_text()  # comments survive edits

    assert catalog.remove(path, "fred", "GDP")
    assert not catalog.remove(path, "fred", "GDP")
    assert catalog.load(path) == [CatalogEntry("fred", "UNRATE")]


def test_add_creates_missing_file(tmp_path):
    path = tmp_path / "catalog.toml"
    assert catalog.add(path, "fred", "GDP")
    assert catalog.load(path) == [CatalogEntry("fred", "GDP")]
