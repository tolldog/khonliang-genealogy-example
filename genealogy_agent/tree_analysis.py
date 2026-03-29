"""
Tree analysis — finds gaps, anomalies, and research opportunities.

Scans the GEDCOM tree to identify:
- Missing data (no birth date, no parents, no death date for old records)
- Dead-end lines (ancestors with no parents beyond a certain point)
- Incomplete families (spouse unknown, children count seems low)
- Date anomalies (impossible dates, suspicious gaps)
- Geographic gaps (sudden location changes with no explanation)
- Research opportunities (well-documented lines that could go deeper)

Can queue background research tasks for each finding.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from genealogy_agent.gedcom_parser import GedcomTree, Person

logger = logging.getLogger(__name__)


@dataclass
class Gap:
    """A gap or anomaly found in the tree."""

    gap_type: str  # missing_birth, dead_end, no_parents, anomaly, etc.
    person_xref: str
    person_name: str
    description: str
    severity: str = "info"  # info, low, medium, high
    research_query: str = ""  # suggested search to fill the gap
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.gap_type,
            "person": self.person_name,
            "description": self.description,
            "severity": self.severity,
            "research_query": self.research_query,
        }


class TreeAnalyzer:
    """
    Analyzes a GEDCOM tree for gaps and research opportunities.

    Example:
        analyzer = TreeAnalyzer(tree)
        gaps = analyzer.find_all_gaps()
        dead_ends = analyzer.find_dead_ends("Timothy Toll")
        print(analyzer.summary())
    """

    def __init__(self, tree: GedcomTree):
        self.tree = tree

    def find_all_gaps(self, max_results: int = 100) -> List[Gap]:
        """Run all gap analyses and return combined results."""
        gaps = []
        gaps.extend(self.find_missing_data())
        gaps.extend(self.find_dead_ends())
        gaps.extend(self.find_date_anomalies())
        gaps.extend(self.find_incomplete_families())

        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
        gaps.sort(key=lambda g: severity_order.get(g.severity, 3))

        return gaps[:max_results]

    def find_dead_ends(
        self, root_name: Optional[str] = None, min_year: int = 1700
    ) -> List[Gap]:
        """
        Find ancestors who have no parents recorded (dead-end lines).

        These are the most productive research targets — extending
        them adds the most to the tree.
        """
        gaps = []

        if root_name:
            person = self.tree.find_person(root_name)
            if not person:
                return gaps
            ancestors = self.tree.get_ancestors(person.xref, generations=20)
            candidates = ancestors
        else:
            candidates = list(self.tree.persons.values())

        for person in candidates:
            parents = self.tree.get_parents(person.xref)
            if parents:
                continue

            # No parents — is this person old enough to be a dead end?
            year = self._extract_year(person.birth_date)
            if year and year > min_year:
                # Recent person with no parents — might just be incomplete
                severity = "medium"
            elif year and year <= min_year:
                # Old ancestor with no parents — genuine dead end
                severity = "low"
            else:
                severity = "info"

            place = person.birth_place or person.death_place or ""
            search_name = self._search_name(person)
            query_parts = [f'"{search_name}"']
            if place:
                query_parts.append(place.split(",")[0].strip())
            if year:
                query_parts.append(str(year))

            gaps.append(Gap(
                gap_type="dead_end",
                person_xref=person.xref,
                person_name=person.full_name,
                description=(
                    f"No parents recorded for {person.full_name}"
                    f" ({person.birth_date or '?'})"
                    f"{f' in {place}' if place else ''}"
                ),
                severity=severity,
                research_query=" ".join(query_parts) + " genealogy parents",
            ))

        return gaps

    def find_missing_data(self) -> List[Gap]:
        """Find persons with significant missing data."""
        gaps = []

        for person in self.tree.persons.values():
            missing = []

            if not person.birth_date:
                missing.append("birth date")
            if not person.birth_place:
                missing.append("birth place")
            if not person.sex:
                missing.append("sex")

            # Check for death date on old records
            year = self._extract_year(person.birth_date)
            if year and year < 1940 and not person.death_date:
                missing.append("death date")

            if not person.given_name or not person.surname:
                missing.append("full name")

            if len(missing) >= 2:
                gaps.append(Gap(
                    gap_type="missing_data",
                    person_xref=person.xref,
                    person_name=person.full_name,
                    description=f"Missing: {', '.join(missing)}",
                    severity="low" if len(missing) <= 2 else "medium",
                    research_query=f'"{self._search_name(person)}" genealogy records',
                    metadata={"missing_fields": missing},
                ))

        return gaps

    def find_date_anomalies(self) -> List[Gap]:
        """Find impossible or suspicious dates."""
        gaps = []

        for person in self.tree.persons.values():
            birth_year = self._extract_year(person.birth_date)
            death_year = self._extract_year(person.death_date)

            # Death before birth
            if birth_year and death_year and death_year < birth_year:
                gaps.append(Gap(
                    gap_type="anomaly_death_before_birth",
                    person_xref=person.xref,
                    person_name=person.full_name,
                    description=(
                        f"Death ({death_year}) before birth ({birth_year})"
                    ),
                    severity="high",
                ))

            # Unreasonable age (> 110)
            if birth_year and death_year:
                age = death_year - birth_year
                if age > 110:
                    gaps.append(Gap(
                        gap_type="anomaly_age",
                        person_xref=person.xref,
                        person_name=person.full_name,
                        description=f"Lived {age} years ({birth_year}-{death_year})",
                        severity="medium",
                    ))

            # Child born before parent
            parents = self.tree.get_parents(person.xref)
            if birth_year:
                for parent in parents:
                    parent_birth = self._extract_year(parent.birth_date)
                    if parent_birth and birth_year <= parent_birth:
                        gaps.append(Gap(
                            gap_type="anomaly_child_before_parent",
                            person_xref=person.xref,
                            person_name=person.full_name,
                            description=(
                                f"Born {birth_year} but parent "
                                f"{parent.full_name} born {parent_birth}"
                            ),
                            severity="high",
                        ))
                    elif parent_birth and birth_year - parent_birth < 12:
                        gaps.append(Gap(
                            gap_type="anomaly_young_parent",
                            person_xref=person.xref,
                            person_name=person.full_name,
                            description=(
                                f"Parent {parent.full_name} was "
                                f"{birth_year - parent_birth} at birth"
                            ),
                            severity="medium",
                        ))

        return gaps

    def find_incomplete_families(self) -> List[Gap]:
        """Find families with missing spouse or suspiciously few children."""
        gaps = []

        for family in self.tree.families.values():
            if not family.husband and family.wife:
                wife = self.tree.persons.get(family.wife)
                if wife:
                    gaps.append(Gap(
                        gap_type="missing_spouse",
                        person_xref=family.wife,
                        person_name=wife.full_name,
                        description=f"Husband unknown for {wife.full_name}",
                        severity="low",
                        research_query=(
                            f'"{self._search_name(wife)}" husband marriage '
                            f"{wife.birth_place or ''}"
                        ),
                    ))

            if not family.wife and family.husband:
                husb = self.tree.persons.get(family.husband)
                if husb:
                    gaps.append(Gap(
                        gap_type="missing_spouse",
                        person_xref=family.husband,
                        person_name=husb.full_name,
                        description=f"Wife unknown for {husb.full_name}",
                        severity="low",
                        research_query=(
                            f'"{self._search_name(husb)}" wife marriage '
                            f"{husb.birth_place or ''}"
                        ),
                    ))

        return gaps

    def find_dead_ends_for(self, name: str) -> List[Gap]:
        """Find dead-end lines for a specific person's ancestry."""
        person = self.tree.find_person(name)
        if not person:
            return []

        ancestors = self.tree.get_ancestors(person.xref, generations=20)
        dead_ends = []

        for ancestor in ancestors:
            parents = self.tree.get_parents(ancestor.xref)
            if not parents:
                place = ancestor.birth_place or ancestor.death_place or ""
                year = self._extract_year(ancestor.birth_date)

                dead_ends.append(Gap(
                    gap_type="dead_end",
                    person_xref=ancestor.xref,
                    person_name=ancestor.full_name,
                    description=(
                        f"Dead end: {ancestor.display}"
                    ),
                    severity="medium",
                    research_query=(
                        f'"{self._search_name(ancestor)}" '
                        f"{place.split(',')[0].strip() if place else ''} "
                        f"{year or ''} genealogy parents"
                    ).strip(),
                ))

        return dead_ends

    def summary(self, root_name: Optional[str] = None) -> str:
        """Generate a human-readable gap analysis summary."""
        if root_name:
            dead_ends = self.find_dead_ends_for(root_name)
        else:
            dead_ends = self.find_dead_ends()

        anomalies = self.find_date_anomalies()
        missing = self.find_missing_data()
        incomplete = self.find_incomplete_families()

        lines = ["=== Tree Analysis ==="]
        lines.append(f"Total persons: {len(self.tree.persons)}")
        lines.append(f"Total families: {len(self.tree.families)}")

        lines.append(f"\nDead-end lines: {len(dead_ends)}")
        for g in dead_ends[:10]:
            lines.append(f"  - {g.description}")

        if anomalies:
            lines.append(f"\nDate anomalies: {len(anomalies)}")
            for g in anomalies[:10]:
                lines.append(f"  [{g.severity}] {g.description}")

        lines.append(f"\nMissing data: {len(missing)} persons")
        lines.append(f"Incomplete families: {len(incomplete)}")

        return "\n".join(lines)

    @staticmethod
    def _extract_year(date_str: str) -> Optional[int]:
        if not date_str:
            return None
        match = re.search(r"\d{4}", date_str)
        return int(match.group()) if match else None

    @staticmethod
    def _search_name(person: Person) -> str:
        """Build a search-friendly name: first + last, drop middle names."""
        given = person.given_name.split()[0] if person.given_name else ""
        surname = person.surname or ""
        name = f"{given} {surname}".strip()
        return name or person.full_name
