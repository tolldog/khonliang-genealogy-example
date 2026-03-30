"""
Genealogy query parser — extends khonliang's generic QueryParser.

Demonstrates how to specialize the generic parser for a specific domain
by providing:
1. A domain-specific schema (genealogy fields)
2. Domain-specific examples (genealogy queries)
3. A regex fallback for when the LLM is unavailable

Example:
    parser = GenealogyQueryParser(ollama_client)
    params = await parser.parse("find me all the men from Ohio born before 1920")
    # {"sex": "M", "place": "ohio", "place_type": "birth", "year_before": 1920, "action": "query"}
"""

import re
from typing import Any, Dict

from khonliang.parsing.query_parser import QueryParser

# Genealogy-specific schema
GENEALOGY_SCHEMA = {
    "sex": {
        "type": "string",
        "enum": ["M", "F"],
        "description": "M for male/man/men, F for female/woman/women",
        "sample_values": ["M", "F"],
    },
    "name": {
        "type": "string",
        "description": "Person name to look up or filter by",
        "sample_values": ["Roger Tolle", "Timothy Toll", "Rebecca Taylor"],
    },
    "surname": {
        "type": "string",
        "description": "Family name / last name to filter by",
        "sample_values": ["toll", "tolle", "thomas", "hughes", "hoy"],
    },
    "place": {
        "type": "string",
        "description": "Location name (state, county, city, country)",
        "sample_values": ["ohio", "maryland", "virginia", "indiana", "kentucky"],
    },
    "place_type": {
        "type": "string",
        "enum": ["birth", "death", "lived", "any"],
        "description": "What the place refers to: born in, died in, lived in",
    },
    "year_before": {
        "type": "integer",
        "description": "Born before this year",
        "sample_values": [1920, 1800, 1700, 1650],
    },
    "year_after": {
        "type": "integer",
        "description": "Born after this year",
        "sample_values": [1900, 1850, 1800],
    },
    "year_between": {
        "type": "array",
        "description": "Born between [start_year, end_year]",
        "sample_values": [[1700, 1800], [1850, 1900]],
    },
    "has_parents": {
        "type": "boolean",
        "description": "Filter for people with (true) or without (false) parents in the tree",
    },
    "has_death_date": {
        "type": "boolean",
        "description": "Filter for people with (true) or without (false) death dates",
    },
    "action": {
        "type": "string",
        "enum": ["query", "lookup", "narrative", "verify", "research", "gaps"],
        "description": "What to do: query (filter tree), lookup (find person), narrative (tell story), verify (check facts), research (web search), gaps (find missing data)",
    },
}

# Examples for the LLM
GENEALOGY_EXAMPLES = [
    (
        "find all men born in Ohio before 1920",
        '{"sex": "M", "place": "ohio", "place_type": "birth", "year_before": 1920, "action": "query"}',
    ),
    (
        "women from the Thomas family",
        '{"sex": "F", "surname": "thomas", "action": "query"}',
    ),
    (
        "who were Roger Tolle's parents",
        '{"name": "Roger Tolle", "action": "lookup"}',
    ),
    (
        "people who lived in Maryland between 1700 and 1800",
        '{"place": "maryland", "place_type": "lived", "year_between": [1700, 1800], "action": "query"}',
    ),
    (
        "dead end ancestors with no parents",
        '{"has_parents": false, "action": "gaps"}',
    ),
    (
        "tell me about Timothy Toll",
        '{"name": "Timothy Toll", "action": "narrative"}',
    ),
    (
        "what's the story of the Toll family migration",
        '{"surname": "toll", "action": "narrative"}',
    ),
    (
        "check if Roger Tolle was really born in 1642",
        '{"name": "Roger Tolle", "year_before": 1642, "action": "verify"}',
    ),
    (
        "research the Hoy family in Ohio",
        '{"surname": "hoy", "place": "ohio", "action": "research"}',
    ),
]


def _genealogy_regex_fallback(message: str) -> Dict[str, Any]:
    """Regex fallback for genealogy queries."""
    msg_lower = message.lower()
    params: Dict[str, Any] = {}

    # Sex
    if any(w in msg_lower.split() for w in ["female", "females", "women", "woman"]):
        params["sex"] = "F"
    elif any(w in msg_lower.split() for w in ["male", "males", "men", "man"]):
        params["sex"] = "M"

    # Place
    place_match = re.search(
        r"(?:born in|died in|lived in|from|in)\s+([a-z][a-z\s]+?)(?:\s+(?:before|after|between|no\s|surname|last|born|died)|$)",
        msg_lower,
    )
    if place_match:
        params["place"] = place_match.group(1).strip()
        if "born" in msg_lower:
            params["place_type"] = "birth"
        elif "died in" in msg_lower:
            params["place_type"] = "death"
        elif "lived in" in msg_lower:
            params["place_type"] = "lived"
        else:
            params["place_type"] = "any"

    # Year
    between = re.search(r"between\s+(\d{4})\s+and\s+(\d{4})", msg_lower)
    before = re.search(r"before\s+(\d{4})", msg_lower)
    after = re.search(r"after\s+(\d{4})", msg_lower)

    if between:
        params["year_between"] = [int(between.group(1)), int(between.group(2))]
    elif before:
        params["year_before"] = int(before.group(1))
    elif after:
        params["year_after"] = int(after.group(1))

    # Surname
    surname_match = re.search(r"(?:surname|last name|family)\s+(\w+)", msg_lower)
    if surname_match:
        params["surname"] = surname_match.group(1)

    # Name
    name_match = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", message)
    skip = {"United States", "New York", "St Mary", "Civil War"}
    names = [n for n in name_match if n not in skip]
    if names:
        params["name"] = names[0]

    # Missing data
    if "no parents" in msg_lower:
        params["has_parents"] = False
    if "no death" in msg_lower:
        params["has_death_date"] = False

    # Action
    if "story" in msg_lower or "tell me" in msg_lower or "narrative" in msg_lower:
        params["action"] = "narrative"
    elif "check" in msg_lower or "verify" in msg_lower or "correct" in msg_lower:
        params["action"] = "verify"
    elif "research" in msg_lower or "search" in msg_lower or "look up" in msg_lower:
        params["action"] = "research"
    elif "gap" in msg_lower or "dead end" in msg_lower or "missing" in msg_lower:
        params["action"] = "gaps"
    elif any(
        key in params
        for key in (
            "sex",
            "place",
            "year_before",
            "year_after",
            "year_between",
            "surname",
            "has_parents",
            "has_death_date",
        )
    ):
        params["action"] = "query"
    elif params.get("name"):
        params["action"] = "lookup"
    else:
        params["action"] = "lookup"

    return params


class GenealogyQueryParser(QueryParser):
    """
    Genealogy-specific query parser.

    Extends khonliang's generic QueryParser with genealogy schema,
    examples, and regex fallback. This is the example of how to
    specialize the generic parser for a specific domain.

    Example:
        parser = GenealogyQueryParser(ollama_client)
        params = await parser.parse("find men from Ohio before 1920")
        # {"sex": "M", "place": "ohio", "place_type": "birth",
        #  "year_before": 1920, "action": "query"}
    """

    def __init__(self, client=None, model: str = "llama3.2:3b"):
        super().__init__(
            client=client,
            model=model,
            schema=GENEALOGY_SCHEMA,
            domain="genealogy research",
            examples=GENEALOGY_EXAMPLES,
            fallback=_genealogy_regex_fallback,
        )
