"""
Genealogy-specific evaluation rules using khonliang's BaseEvaluator.

Extends the generic evaluator with domain rules that check LLM
responses against the family tree data.
"""

import re
from typing import Any, Dict, List, Optional

from khonliang.roles.evaluator import BaseEvaluator, EvalIssue, EvalRule, SpeculationRule, UncertaintyRule

from genealogy_agent.gedcom_parser import GedcomTree


class DateCheckRule(EvalRule):
    """Checks if date claims in response contradict tree data."""

    name = "date_check"

    def __init__(self, tree: GedcomTree):
        self.tree = tree

    def check(self, response, query="", metadata=None):
        """Find date claims and verify against tree."""
        issues = []

        date_claims = re.findall(
            r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b[^.]*?"
            r"(?:born|died|b\.|d\.)\s+(?:in\s+)?(\d{4})",
            response,
        )

        for name, year_str in date_claims:
            year = int(year_str)
            person = self.tree.find_person(name)
            if person is None:
                continue

            tree_birth = self._extract_year(person.birth_date)
            tree_death = self._extract_year(person.death_date)

            name_pos = response.find(name)
            context_window = response[
                max(0, name_pos - 30): name_pos + len(name) + 60
            ].lower()

            if tree_birth and abs(tree_birth - year) > 5:
                if "born" in context_window or "b." in context_window:
                    issues.append(EvalIssue(
                        rule=self.name,
                        issue_type="date_mismatch",
                        detail=(
                            f"Response says {name} born {year}, "
                            f"tree says {tree_birth}"
                        ),
                        severity="high",
                    ))

            if tree_death and abs(tree_death - year) > 5:
                if "died" in context_window or "d." in context_window:
                    issues.append(EvalIssue(
                        rule=self.name,
                        issue_type="date_mismatch",
                        detail=(
                            f"Response says {name} died {year}, "
                            f"tree says {tree_death}"
                        ),
                        severity="high",
                    ))

        return issues

    @staticmethod
    def _extract_year(date_str):
        if not date_str:
            return None
        match = re.search(r"\d{4}", date_str)
        return int(match.group()) if match else None


class RelationshipCheckRule(EvalRule):
    """Checks if relationship claims match the tree."""

    name = "relationship_check"

    def __init__(self, tree: GedcomTree):
        self.tree = tree

    def check(self, response, query="", metadata=None):
        """Find parent claims and verify against tree."""
        issues = []

        parent_claims = re.findall(
            r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b'?s?\s+"
            r"(?:father|mother|parent)\s+(?:was|is)\s+"
            r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
            response,
        )

        for child_name, parent_name in parent_claims:
            child = self.tree.find_person(child_name)
            parent = self.tree.find_person(parent_name)

            if child and parent:
                actual_parents = self.tree.get_parents(child.xref)
                parent_xrefs = {p.xref for p in actual_parents}
                if parent.xref not in parent_xrefs and actual_parents:
                    issues.append(EvalIssue(
                        rule=self.name,
                        issue_type="wrong_relationship",
                        detail=(
                            f"Response says {parent_name} is parent of "
                            f"{child_name}, tree shows: "
                            f"{', '.join(p.full_name for p in actual_parents)}"
                        ),
                        severity="high",
                    ))

        return issues


def create_genealogy_evaluator(tree: GedcomTree) -> BaseEvaluator:
    """
    Create an evaluator with genealogy-specific rules.

    Uses khonliang's BaseEvaluator with:
    - DateCheckRule: verifies dates against tree
    - RelationshipCheckRule: verifies parent claims
    - SpeculationRule: detects excessive speculation
    - UncertaintyRule: detects hedging language
    """
    return BaseEvaluator(
        rules=[
            DateCheckRule(tree),
            RelationshipCheckRule(tree),
            SpeculationRule(max_phrases=3),
            UncertaintyRule(),
        ],
    )
