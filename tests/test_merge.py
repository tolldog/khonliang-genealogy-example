"""Tests for merge engine — field merging, conflict detection, strategies."""

from genealogy_agent.forest import TreeForest
from genealogy_agent.merge import MergeEngine


class TestMergePreferTarget:
    """Verify prefer_target strategy — fill gaps only."""

    def test_fills_empty_fields(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)

        # Clear occupation in tree B's John to simulate missing data
        tree_b = forest.get_tree("b")
        john_b = tree_b.find_person("John Smith")
        john_b.occupation = ""

        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I1@", "b:@I1@", strategy="prefer_target")

        assert "occupation" in result.merged_fields
        assert john_b.occupation == "Farmer"  # filled from source

    def test_keeps_existing_values(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)

        # Modify tree A's birth place
        tree_a = forest.get_tree("a")
        john_a = tree_a.find_person("John Smith")
        john_a.birth_place = "Different Place"

        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I1@", "b:@I1@", strategy="prefer_target")

        # Target keeps its existing value
        tree_b = forest.get_tree("b")
        john_b = tree_b.find_person("John Smith")
        assert john_b.birth_place == "Springfield, Illinois"
        assert "birth_place" not in result.merged_fields


class TestMergePreferSource:
    """Verify prefer_source strategy — overwrite with source."""

    def test_overwrites_with_source(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)

        tree_a = forest.get_tree("a")
        john_a = tree_a.find_person("John Smith")
        john_a.occupation = "Merchant"

        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I1@", "b:@I1@", strategy="prefer_source")

        tree_b = forest.get_tree("b")
        john_b = tree_b.find_person("John Smith")
        assert john_b.occupation == "Merchant"
        assert "occupation" in result.merged_fields


class TestMergeAll:
    """Verify merge_all strategy — report conflicts."""

    def test_reports_conflicts(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)

        tree_a = forest.get_tree("a")
        john_a = tree_a.find_person("John Smith")
        john_a.occupation = "Merchant"

        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I1@", "b:@I1@", strategy="merge_all")

        assert len(result.conflicts) > 0
        assert any("occupation" in c for c in result.conflicts)


class TestMergeEdgeCases:
    """Verify edge cases."""

    def test_merge_missing_person(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I999@", "a:@I1@")
        assert "not found" in result.conflicts[0]

    def test_merge_result_display(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        engine = MergeEngine(forest)
        result = engine.merge_person("a:@I1@", "b:@I1@")
        assert "Merge" in result.display

    def test_merge_with_triple_store(self, sample_gedcom_path, knowledge_db_path):
        from khonliang.knowledge.triples import TripleStore

        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        triples = TripleStore(knowledge_db_path)

        engine = MergeEngine(forest, triple_store=triples)

        # Clear a field so something gets merged
        tree_b = forest.get_tree("b")
        john_b = tree_b.find_person("John Smith")
        john_b.occupation = ""

        engine.merge_person("a:@I1@", "b:@I1@")

        # Verify merge recorded in triple store
        results = triples.get(
            subject="a:@I1@", predicate="merged_into"
        )
        assert len(results) == 1
        assert results[0].object == "b:@I1@"
