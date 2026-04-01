"""Tests for TreeForest — multi-tree management, qualified xrefs, search."""


from genealogy_agent.forest import TreeForest, load_forest_from_config


class TestTreeForestLoading:
    """Verify tree loading and lifecycle."""

    def test_load_tree(self, sample_gedcom_path):
        forest = TreeForest()
        tree = forest.load("test", sample_gedcom_path)
        assert tree is not None
        assert len(tree.persons) == 8
        assert "test" in forest

    def test_first_loaded_is_default(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("first", sample_gedcom_path)
        forest.load("second", sample_gedcom_path)
        assert forest.default_name == "first"
        assert forest.default_tree is not None

    def test_tree_names(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("alpha", sample_gedcom_path)
        forest.load("beta", sample_gedcom_path)
        assert set(forest.tree_names) == {"alpha", "beta"}

    def test_len(self, sample_gedcom_path):
        forest = TreeForest()
        assert len(forest) == 0
        forest.load("test", sample_gedcom_path)
        assert len(forest) == 1

    def test_unload(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("test", sample_gedcom_path)
        forest.unload("test")
        assert "test" not in forest
        assert len(forest) == 0

    def test_unload_default_promotes_next(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("first", sample_gedcom_path)
        forest.load("second", sample_gedcom_path)
        forest.unload("first")
        assert forest.default_name == "second"

    def test_set_default(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        forest.default_name = "b"
        assert forest.default_name == "b"

    def test_get_tree(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("test", sample_gedcom_path)
        assert forest.get_tree("test") is not None
        assert forest.get_tree("missing") is None

    def test_empty_forest(self):
        forest = TreeForest()
        assert forest.default_tree is None
        assert forest.default_name is None
        assert forest.tree_names == []


class TestQualifiedXrefs:
    """Verify xref namespacing and resolution."""

    def test_resolve_qualified_xref(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        tree_name, xref = forest.resolve_xref("toll:@I1@")
        assert tree_name == "toll"
        assert xref == "@I1@"

    def test_resolve_unqualified_uses_default(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        tree_name, xref = forest.resolve_xref("@I1@")
        assert tree_name == "toll"
        assert xref == "@I1@"

    def test_get_person_qualified(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        qp = forest.get_person("toll:@I1@")
        assert qp is not None
        assert qp.tree_name == "toll"
        assert qp.person.full_name == "John Smith"
        assert qp.qualified_xref == "toll:@I1@"

    def test_get_person_not_found(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        assert forest.get_person("toll:@I999@") is None
        assert forest.get_person("missing:@I1@") is None

    def test_qualified_person_display(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        qp = forest.get_person("toll:@I1@")
        assert "[toll]" in qp.display
        assert "John Smith" in qp.display

    def test_qualified_person_to_dict(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        qp = forest.get_person("toll:@I1@")
        d = qp.to_dict()
        assert d["tree_name"] == "toll"
        assert d["qualified_xref"] == "toll:@I1@"
        assert d["name"] == "John Smith"


class TestForestSearch:
    """Verify cross-tree search."""

    def test_find_person_in_default(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        qp = forest.find_person("John Smith")
        assert qp is not None
        assert qp.tree_name == "toll"

    def test_find_person_scoped(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        qp = forest.find_person("John Smith", tree_name="toll")
        assert qp is not None
        assert forest.find_person("John Smith", tree_name="missing") is None

    def test_search_all(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("tree_a", sample_gedcom_path)
        forest.load("tree_b", sample_gedcom_path)
        results = forest.search_all("Smith")
        # Both trees have the same Smiths
        assert len(results) >= 8  # 4 Smiths x 2 trees
        tree_names = {r.tree_name for r in results}
        assert "tree_a" in tree_names
        assert "tree_b" in tree_names

    def test_search_all_no_results(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        assert forest.search_all("zzzzz") == []


class TestForestSummary:
    """Verify summary and info methods."""

    def test_get_summary(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        summary = forest.get_summary()
        assert "toll" in summary
        assert "8 persons" in summary
        assert "3 families" in summary
        assert "(default)" in summary

    def test_get_summary_empty(self):
        forest = TreeForest()
        assert "No trees" in forest.get_summary()

    def test_get_tree_info(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        info = forest.get_tree_info("toll")
        assert info["name"] == "toll"
        assert info["persons"] == 8
        assert info["families"] == 3
        assert info["is_default"] is True

    def test_list_trees(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        trees = forest.list_trees()
        assert len(trees) == 2


class TestLoadForestFromConfig:
    """Verify config-driven forest loading."""

    def test_single_gedcom_compat(self, sample_gedcom_path):
        config = {"app": {"gedcom": sample_gedcom_path, "gedcoms": {}}}
        forest = load_forest_from_config(config)
        assert len(forest) == 1
        assert forest.default_tree is not None

    def test_multiple_gedcoms(self, sample_gedcom_path):
        config = {
            "app": {
                "gedcom": "",
                "gedcoms": {
                    "tree_a": sample_gedcom_path,
                    "tree_b": sample_gedcom_path,
                },
            }
        }
        forest = load_forest_from_config(config)
        assert len(forest) == 2
        assert "tree_a" in forest
        assert "tree_b" in forest

    def test_empty_config(self):
        config = {"app": {"gedcom": "", "gedcoms": {}}}
        forest = load_forest_from_config(config)
        assert len(forest) == 0
