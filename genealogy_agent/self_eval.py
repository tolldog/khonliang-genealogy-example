"""
Agent self-evaluation — validates responses before sending to user.

After a specialist role generates a response, the evaluator checks it
against the tree data for:
- Names that don't exist in the tree
- Dates that contradict tree data
- Relationships that don't match
- Obvious hallucinations (people/places not in the data)

The evaluation can:
- Pass the response through unchanged (good)
- Append a caveat ("Note: could not verify...")
- Flag specific claims as unverified
- Skip evaluation for certain roles (e.g. research)
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

    Example:
        evaluator = ResponseEvaluator(tree)
        result = evaluator.evaluate(response_text, original_query)
        if result["issues"]:
            response_text += "\\n\\n" + result["caveat"]
    """

    def __init__(self, tree: GedcomTree):
        self.tree = tree

    def evaluate(
        self,
        response: str,
        query: str = "",
        role: str = "",
    ) -> Dict[str, Any]:
        """
        Evaluate a response for accuracy.

        Returns:
            {
                "passed": bool,
                "confidence": float (0-1),
                "issues": [{"type": "...", "detail": "..."}],
                "caveat": str or None,
            }
        """
        # Skip evaluation for certain roles where checking is not useful
        if role == "research":
            return {
                "passed": True,
                "confidence": 1.0,
                "issues": [],
                "caveat": None,
            }

        issues = []

        # Check for names mentioned that aren't in the tree
        name_issues = self._check_names(response)
        issues.extend(name_issues)

        # Check for date claims that contradict tree
        date_issues = self._check_dates(response)
        issues.extend(date_issues)

        # Check for relationship claims
        rel_issues = self._check_relationships(response)
        issues.extend(rel_issues)

        # Check for hedging language that suggests uncertainty
        hedge_issues = self._check_hedging(response, query=query)
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

        # Build caveat
        caveat = None
        if high_issues:
            details = "; ".join(i["detail"] for i in high_issues[:3])
            caveat = f"⚠ Verification issues: {details}"
        elif med_issues:
            details = "; ".join(i["detail"] for i in med_issues[:2])
            caveat = f"Note: {details}"

        return {
            "passed": len(high_issues) == 0,
            "confidence": confidence,
            "issues": issues,
            "caveat": caveat,
        }

    def _check_names(self, response: str) -> List[Dict]:
        """Check if names mentioned in response exist in tree."""
        issues = []

        # Extract potential names (capitalized word pairs)
        name_pattern = re.findall(
            r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", response
        )

        # Filter out common non-name phrases
        skip = {
            "United States", "North America", "New York", "New Jersey",
            "New England", "St Mary", "St Marys", "Civil War",
            "World War", "Family Tree", "Great Britain",
        }

        for name in set(name_pattern):
            if name in skip or len(name.split()) > 3:
                continue

            # Check if this looks like a person name and is in the tree
            person = self.tree.find_person(name)
            if person is None:
                # Could be a real person not in our tree — low severity
                # Only flag if the response states this as family
                if any(
                    kw in response.lower()
                    for kw in ["parent", "child", "married", "spouse",
                               "father", "mother", "son", "daughter",
                               "sibling", "brother", "sister"]
                ):
                    issues.append({
                        "type": "unknown_name",
                        "name": name,
                        "detail": f"'{name}' not found in tree data",
                        "severity": "medium",
                    })

        return issues

    def _check_dates(self, response: str) -> List[Dict]:
        """Check if dates in response contradict tree data."""
        issues = []

        # Find date claims in format "born YYYY" or "in YYYY"
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

            # Check birth date
            tree_birth = self._extract_year(person.birth_date)
            tree_death = self._extract_year(person.death_date)

            context = response[
                max(0, response.find(name) - 20):
                response.find(name) + len(name) + 50
            ].lower()

            if tree_birth and abs(tree_birth - year) > 5:
                # Response says different birth year than tree
                if "born" in context or "b." in context:
                    issues.append({
                        "type": "date_mismatch",
                        "detail": (
                            f"Response says {name} born {year}, "
                            f"tree says {tree_birth}"
                        ),
                        "severity": "high",
                    })

            if tree_death and abs(tree_death - year) > 5:
                # Response says different death year than tree
                if "died" in context or "d." in context:
                    issues.append({
                        "type": "date_mismatch",
                        "detail": (
                            f"Response says {name} died {year}, "
                            f"tree says {tree_death}"
                        ),
                        "severity": "high",
                    })

        return issues

    def _check_relationships(self, response: str) -> List[Dict]:
        """Check relationship claims against tree."""
        issues = []

        # Pattern: "X's father/mother was Y" or "Y was the father/mother of X"
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
                    detail += f" (query: {query[:80]})"
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
