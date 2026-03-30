"""
Agent self-evaluation — validates responses before sending to user.

After a specialist role generates a response, the evaluator checks it
against the tree data for:
- Dates that contradict tree data
- Relationship claims that don't match
- Excessive speculation
- Skip evaluation for certain roles (e.g. research)

The evaluator is context-aware: it understands response structure and
avoids false positives from section headings, labels, and non-name phrases.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from genealogy_agent.gedcom_parser import GedcomTree

logger = logging.getLogger(__name__)


class ResponseEvaluator:
    """
    Evaluates agent responses against tree data.

    Lightweight, non-LLM — uses string matching and tree lookups
    to catch obvious errors without an extra inference call.
    """

    def __init__(self, tree: GedcomTree):
        self.tree = tree

    def evaluate(
        self,
        response: str,
        query: str = "",
        role: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate a response for accuracy.

        Args:
            response: The LLM response text
            query: The original user query
            role: Which role generated the response
            metadata: Response metadata (may include referenced_persons
                      with known facts from the tree)

        Returns:
            {
                "passed": bool,
                "confidence": float (0-1),
                "issues": [{"type": "...", "detail": "...", "severity": "..."}],
                "caveat": str or None,
            }
        """
        # Skip evaluation for research/system roles
        if role in ("research", "system", "analyst", "librarian"):
            return {"passed": True, "confidence": 0.95, "issues": [], "caveat": None}

        meta = metadata or {}
        referenced_persons = meta.get("referenced_persons", [])

        # Build a name → facts lookup from metadata ground truth
        persons_lookup: Dict[str, Dict[str, Any]] = {
            p["name"].lower(): p
            for p in referenced_persons
            if p.get("name")
        }

        issues = []

        # Check for date claims that contradict tree
        date_issues = self._check_dates(response, persons_lookup)
        issues.extend(date_issues)

        # Check for relationship claims
        rel_issues = self._check_relationships(response)
        issues.extend(rel_issues)

        # Check for excessive speculation
        hedge_issues = self._check_hedging(response, query)
        issues.extend(hedge_issues)

        # Score
        high_issues = [i for i in issues if i["severity"] == "high"]
        med_issues = [i for i in issues if i["severity"] == "medium"]

        if high_issues:
            confidence = 0.3
        elif med_issues:
            confidence = 0.6
        elif issues:
            confidence = 0.8
        else:
            confidence = 0.95

        # Build caveat — only for real data issues, not structural noise
        caveat = None
        if high_issues:
            details = "; ".join(i["detail"] for i in high_issues[:3])
            caveat = f"Verification issues: {details}"
        elif med_issues:
            details = "; ".join(i["detail"] for i in med_issues[:2])
            caveat = f"Note: {details}"

        return {
            "passed": len(high_issues) == 0,
            "confidence": confidence,
            "issues": issues,
            "caveat": caveat,
        }

    def _check_dates(
        self,
        response: str,
        persons_lookup: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """Check if date claims in response contradict tree data.

        Args:
            response: LLM response text to check.
            persons_lookup: Optional name→facts dict built from role metadata
                            (ground truth from tree).  When a name is found
                            here the metadata birth_year is used directly;
                            a tree search is still performed for death year.
        """
        issues = []
        if persons_lookup is None:
            persons_lookup = {}

        # Find "Name born/died YEAR" patterns with multi-word names
        date_claims = re.findall(
            r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b[^.]*?"
            r"(?:born|died|b\.|d\.)\s+(?:in\s+)?(\d{4})",
            response,
        )

        for name, year_str in date_claims:
            year = int(year_str)

            # Prefer metadata ground truth; fall back to tree search
            if name.lower() in persons_lookup:
                p_meta = persons_lookup[name.lower()]
                tree_birth: Optional[int] = p_meta.get("birth_year")
                person = self.tree.find_person(name)
                tree_death: Optional[int] = (
                    self._extract_year(person.death_date) if person else None
                )
            else:
                person = self.tree.find_person(name)
                if person is None:
                    continue
                tree_birth = self._extract_year(person.birth_date)
                tree_death = self._extract_year(person.death_date)

            # Check context around the name to determine if birth or death
            name_pos = response.find(name)
            context_window = response[
                max(0, name_pos - 30): name_pos + len(name) + 60
            ].lower()

            if tree_birth and abs(tree_birth - year) > 5:
                if "born" in context_window or "b." in context_window:
                    issues.append({
                        "type": "date_mismatch",
                        "detail": (
                            f"Response says {name} born {year}, "
                            f"tree says {tree_birth}"
                        ),
                        "severity": "high",
                        "name": name,
                    })

            if tree_death and abs(tree_death - year) > 5:
                if "died" in context_window or "d." in context_window:
                    issues.append({
                        "type": "date_mismatch",
                        "detail": (
                            f"Response says {name} died {year}, "
                            f"tree says {tree_death}"
                        ),
                        "severity": "high",
                        "name": name,
                    })

        return issues

    def _check_relationships(self, response: str) -> List[Dict]:
        """Check relationship claims against tree."""
        issues = []

        # Pattern: "X's father/mother was Y"
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
                    issues.append({
                        "type": "wrong_relationship",
                        "detail": (
                            f"Response says {parent_name} is parent of "
                            f"{child_name}, tree shows: "
                            f"{', '.join(p.full_name for p in actual_parents)}"
                        ),
                        "severity": "high",
                        "name": child_name,
                    })

        return issues

    def _check_hedging(self, response: str, query: str = "") -> List[Dict]:
        """Detect hedging language that suggests the LLM is uncertain."""
        issues = []
        resp_lower = response.lower()

        heavy_hedge = [
            "i'm not sure",
            "i don't have",
            "no information",
            "no data",
            "cannot find",
            "could not find",
            "not in the",
            "no records",
        ]

        for phrase in heavy_hedge:
            if phrase in resp_lower:
                detail = f"Agent expressed uncertainty: '{phrase}'"
                if query:
                    detail += f" (query: {query[:40]})"
                issues.append({
                    "type": "uncertainty",
                    "detail": detail,
                    "severity": "low",
                })
                break

        speculation = [
            "it is possible",
            "it's possible",
            "speculation",
            "may have",
            "might have",
            "could have been",
            "likely",
            "perhaps",
            "probably",
        ]

        spec_count = sum(1 for p in speculation if p in resp_lower)
        if spec_count >= 3:
            issues.append({
                "type": "excessive_speculation",
                "detail": f"Response contains {spec_count} speculative phrases",
                "severity": "medium",
            })

        return issues

    @staticmethod
    def _extract_year(date_str: str) -> Optional[int]:
        if not date_str:
            return None
        match = re.search(r"\d{4}", date_str)
        return int(match.group()) if match else None
