"""Tests for CrossMatcher — heuristic person matching across trees."""


from genealogy_agent.cross_matcher import CrossMatcher
from genealogy_agent.forest import TreeForest
from genealogy_agent.gedcom_parser import Person


class TestCrossMatcherScoring:
    """Verify individual scoring dimensions."""

    def setup_method(self):
        self.matcher = CrossMatcher(TreeForest())

    def test_name_score_exact(self):
        a = Person(xref="@I1@", given_name="John", surname="Smith")
        b = Person(xref="@I2@", given_name="John", surname="Smith")
        assert self.matcher._name_score(a, b) == 1.0

    def test_name_score_surname_only(self):
        a = Person(xref="@I1@", given_name="John", surname="Smith")
        b = Person(xref="@I2@", given_name="James", surname="Smith")
        score = self.matcher._name_score(a, b)
        assert 0.5 < score < 0.8  # surname match but different given

    def test_name_score_initial_match(self):
        a = Person(xref="@I1@", given_name="John", surname="Smith")
        b = Person(xref="@I2@", given_name="Jacob", surname="Smith")
        score = self.matcher._name_score(a, b)
        assert score >= 0.7  # same initial J

    def test_name_score_abbreviation(self):
        a = Person(xref="@I1@", given_name="William", surname="Smith")
        b = Person(xref="@I2@", given_name="Wm", surname="Smith")
        score = self.matcher._name_score(a, b)
        assert score >= 0.7  # same initial, surname match

    def test_name_score_no_surname(self):
        a = Person(xref="@I1@", given_name="John", surname="")
        b = Person(xref="@I2@", given_name="John", surname="Smith")
        assert self.matcher._name_score(a, b) == 0.0

    def test_date_score_exact(self):
        a = Person(xref="@I1@", birth_date="1850")
        b = Person(xref="@I2@", birth_date="1850")
        assert self.matcher._date_score(a, b) == 1.0

    def test_date_score_close(self):
        a = Person(xref="@I1@", birth_date="1850")
        b = Person(xref="@I2@", birth_date="1852")
        assert self.matcher._date_score(a, b) == 0.9

    def test_date_score_far_apart(self):
        a = Person(xref="@I1@", birth_date="1850")
        b = Person(xref="@I2@", birth_date="1870")
        assert self.matcher._date_score(a, b) == 0.0

    def test_date_score_no_dates(self):
        a = Person(xref="@I1@")
        b = Person(xref="@I2@")
        assert self.matcher._date_score(a, b) == 0.5  # neutral

    def test_place_score_exact(self):
        a = Person(xref="@I1@", birth_place="Springfield, Illinois")
        b = Person(xref="@I2@", birth_place="Springfield, Illinois")
        assert self.matcher._place_score(a, b) == 1.0

    def test_place_score_partial(self):
        a = Person(xref="@I1@", birth_place="Springfield, Illinois")
        b = Person(xref="@I2@", birth_place="Chicago, Illinois")
        score = self.matcher._place_score(a, b)
        assert 0.0 < score < 1.0  # "Illinois" overlap

    def test_place_score_no_data(self):
        a = Person(xref="@I1@")
        b = Person(xref="@I2@")
        assert self.matcher._place_score(a, b) == 0.3  # neutral

    def test_conflict_sex_mismatch(self):
        a = Person(xref="@I1@", sex="M")
        b = Person(xref="@I2@", sex="F")
        conflicts = self.matcher._detect_conflicts(a, b)
        assert any("sex mismatch" in c for c in conflicts)

    def test_conflict_large_date_gap(self):
        a = Person(xref="@I1@", birth_date="1850")
        b = Person(xref="@I2@", birth_date="1900")
        conflicts = self.matcher._detect_conflicts(a, b)
        assert any("birth year gap" in c for c in conflicts)

    def test_no_conflicts(self):
        a = Person(xref="@I1@", sex="M", birth_date="1850")
        b = Person(xref="@I2@", sex="M", birth_date="1852")
        assert self.matcher._detect_conflicts(a, b) == []


class TestCrossMatcherScan:
    """Verify full scan across trees."""

    def test_scan_finds_same_persons(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        candidates = matcher.scan("a", "b", min_score=0.5)
        assert len(candidates) > 0
        # Same GEDCOM loaded twice — should find high-confidence matches
        assert candidates[0].score >= 0.8

    def test_scan_sorted_by_score(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        candidates = matcher.scan("a", "b", min_score=0.3)
        for i in range(len(candidates) - 1):
            assert candidates[i].score >= candidates[i + 1].score

    def test_scan_respects_min_score(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        candidates = matcher.scan("a", "b", min_score=0.99)
        for c in candidates:
            assert c.score >= 0.99

    def test_scan_missing_tree(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        matcher = CrossMatcher(forest)
        assert matcher.scan("a", "missing") == []

    def test_compare_specific_persons(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        result = matcher.compare("a:@I1@", "b:@I1@")
        assert result is not None
        assert result.score >= 0.8  # same person in both trees

    def test_match_candidate_display(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        result = matcher.compare("a:@I1@", "b:@I1@")
        assert "<->" in result.display
        assert "score=" in result.display

    def test_match_candidate_to_dict(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("a", sample_gedcom_path)
        forest.load("b", sample_gedcom_path)
        matcher = CrossMatcher(forest)

        result = matcher.compare("a:@I1@", "b:@I1@")
        d = result.to_dict()
        assert "score" in d
        assert "person_a" in d
        assert "person_b" in d
