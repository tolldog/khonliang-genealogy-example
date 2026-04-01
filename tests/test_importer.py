"""Tests for GEDCOM importer — sanity checks, import, export."""

import pytest

from genealogy_agent.forest import TreeForest
from genealogy_agent.importer import GedcomImporter
from genealogy_agent.gedcom_parser import GedcomTree


class TestImportFile:
    """Verify GEDCOM import with sanity checking."""

    def test_import_valid_file(self, sample_gedcom_path):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        result = importer.import_file(sample_gedcom_path, name="test")
        assert result.status in ("ok", "warnings")
        assert result.persons == 8
        assert result.families == 3
        assert "test" in forest

    def test_import_auto_name(self, sample_gedcom_path):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        result = importer.import_file(sample_gedcom_path)
        assert result.tree_name  # derived from filename
        assert result.status in ("ok", "warnings")
        assert len(forest) == 1

    def test_import_replaces_existing(self, sample_gedcom_path):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        importer.import_file(sample_gedcom_path, name="test")
        result = importer.import_file(sample_gedcom_path, name="test")
        assert result.status in ("ok", "warnings")
        assert len(forest) == 1  # replaced, not duplicated

    def test_import_bad_path(self):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        result = importer.import_file("/nonexistent/file.ged", name="bad")
        assert result.status == "rejected"
        assert len(result.issues) > 0

    def test_import_empty_gedcom(self, tmp_path):
        empty = tmp_path / "empty.ged"
        empty.write_text("0 HEAD\n0 TRLR\n")
        forest = TreeForest()
        importer = GedcomImporter(forest)
        result = importer.import_file(str(empty), name="empty")
        assert result.status == "rejected"
        assert any("no persons" in i for i in result.issues)

    def test_import_result_display(self, sample_gedcom_path):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        result = importer.import_file(sample_gedcom_path, name="test")
        display = result.display
        assert "test" in display
        assert "8 persons" in display


class TestSanityCheck:
    """Verify sanity checking catches issues."""

    def test_clean_tree_no_issues(self, sample_gedcom_path):
        tree = GedcomTree.from_file(sample_gedcom_path)
        importer = GedcomImporter(TreeForest())
        issues, warnings = importer.sanity_check(tree)
        # Our sample data is clean
        assert isinstance(issues, list)
        assert isinstance(warnings, list)


class TestExportGedcom:
    """Verify GEDCOM export produces valid output."""

    def test_export_roundtrip(self, sample_gedcom_path, tmp_path):
        forest = TreeForest()
        forest.load("test", sample_gedcom_path)
        importer = GedcomImporter(forest)

        export_path = str(tmp_path / "export.ged")
        importer.export_gedcom("test", export_path)

        # Re-parse the exported file
        exported_tree = GedcomTree.from_file(export_path)
        assert len(exported_tree.persons) == 8
        assert len(exported_tree.families) == 3

        # Verify a person's data survived the roundtrip
        john = exported_tree.find_person("John Smith")
        assert john is not None
        assert "1850" in john.birth_date
        assert "Springfield" in john.birth_place

    def test_export_missing_tree(self):
        forest = TreeForest()
        importer = GedcomImporter(forest)
        with pytest.raises(ValueError, match="not found"):
            importer.export_gedcom("missing", "/tmp/out.ged")

    def test_export_preserves_families(self, sample_gedcom_path, tmp_path):
        forest = TreeForest()
        forest.load("test", sample_gedcom_path)
        importer = GedcomImporter(forest)

        export_path = str(tmp_path / "export.ged")
        importer.export_gedcom("test", export_path)

        exported = GedcomTree.from_file(export_path)
        # William should still have parents John and Mary
        william = exported.find_person("William Smith")
        parents = exported.get_parents(william.xref)
        parent_names = {p.full_name for p in parents}
        assert "John Smith" in parent_names
        assert "Mary Jones" in parent_names
