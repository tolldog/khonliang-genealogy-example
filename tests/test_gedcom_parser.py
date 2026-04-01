"""Tests for GEDCOM parser — persons, families, relationships, search."""



class TestGedcomParsing:
    """Verify GEDCOM file is parsed correctly."""

    def test_parse_persons(self, tree):
        assert len(tree.persons) == 8

    def test_parse_families(self, tree):
        assert len(tree.families) == 3

    def test_person_fields(self, tree):
        john = tree.persons["@I1@"]
        assert john.given_name == "John"
        assert john.surname == "Smith"
        assert john.sex == "M"
        assert "1850" in john.birth_date
        assert "Springfield" in john.birth_place
        assert "1920" in john.death_date
        assert "Chicago" in john.death_place
        assert john.occupation == "Farmer"

    def test_person_full_name(self, tree):
        assert tree.persons["@I1@"].full_name == "John Smith"
        assert tree.persons["@I2@"].full_name == "Mary Jones"

    def test_person_display(self, tree):
        john = tree.persons["@I1@"]
        display = john.display
        assert "John Smith" in display
        assert "1850" in display
        assert "Springfield" in display

    def test_person_to_dict(self, tree):
        d = tree.persons["@I1@"].to_dict()
        assert d["name"] == "John Smith"
        assert d["sex"] == "M"
        assert d["xref"] == "@I1@"

    def test_family_links(self, tree):
        fam = tree.families["@F1@"]
        assert fam.husband == "@I1@"
        assert fam.wife == "@I2@"
        assert "@I3@" in fam.children
        assert "1876" in fam.marriage_date
        assert "Springfield" in fam.marriage_place


class TestGedcomSearch:
    """Verify person search and lookup."""

    def test_find_person_exact(self, tree):
        person = tree.find_person("John Smith")
        assert person is not None
        assert person.xref == "@I1@"

    def test_find_person_partial(self, tree):
        person = tree.find_person("John")
        assert person is not None
        assert person.surname == "Smith"

    def test_find_person_case_insensitive(self, tree):
        person = tree.find_person("john smith")
        assert person is not None

    def test_find_person_not_found(self, tree):
        assert tree.find_person("Nobody Here") is None

    def test_search_persons_by_name(self, tree):
        results = tree.search_persons("Smith")
        names = [p.full_name for p in results]
        assert "John Smith" in names
        assert "William Smith" in names
        assert "James Smith" in names
        assert "Elizabeth Smith" in names

    def test_search_persons_by_place(self, tree):
        results = tree.search_persons("Denver")
        assert len(results) >= 2
        names = [p.full_name for p in results]
        assert "William Smith" in names

    def test_search_persons_no_results(self, tree):
        assert tree.search_persons("zzzzz") == []


class TestGedcomRelationships:
    """Verify family relationship queries."""

    def test_get_parents(self, tree):
        # William's parents are John and Mary
        parents = tree.get_parents("@I3@")
        parent_names = {p.full_name for p in parents}
        assert "John Smith" in parent_names
        assert "Mary Jones" in parent_names

    def test_get_children(self, tree):
        # John and Mary's child is William
        children = tree.get_children("@I1@")
        child_names = [c.full_name for c in children]
        assert "William Smith" in child_names

    def test_get_spouses(self, tree):
        # John's spouse is Mary
        spouses = tree.get_spouses("@I1@")
        assert len(spouses) == 1
        assert spouses[0].full_name == "Mary Jones"

    def test_get_siblings(self, tree):
        # James and Elizabeth are siblings
        siblings = tree.get_siblings("@I5@")
        sibling_names = [s.full_name for s in siblings]
        assert "Elizabeth Smith" in sibling_names

    def test_get_parents_no_person(self, tree):
        assert tree.get_parents("@MISSING@") == []

    def test_get_ancestors(self, tree):
        # James's ancestors: William, Sarah, John, Mary, Robert, Anna
        ancestors = tree.get_ancestors("@I5@", generations=3)
        ancestor_names = {a.full_name for a in ancestors}
        assert "William Smith" in ancestor_names
        assert "John Smith" in ancestor_names
        assert "Mary Jones" in ancestor_names

    def test_get_descendants(self, tree):
        # John's descendants: William, James, Elizabeth
        descendants = tree.get_descendants("@I1@", generations=3)
        desc_names = {d.full_name for d in descendants}
        assert "William Smith" in desc_names
        assert "James Smith" in desc_names

    def test_build_context(self, tree):
        ctx = tree.build_context("@I1@", depth=1)
        assert "John Smith" in ctx
        assert "1850" in ctx

    def test_get_summary(self, tree):
        summary = tree.get_summary()
        assert "8" in summary  # 8 persons
        assert "3" in summary  # 3 families
