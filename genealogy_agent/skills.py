"""
Skill declarations for genealogy agents.

Each agent registers skills with:
- Name and description
- Example utterances (for intent matching)
- Handler name (which method processes this)
- Output hint (what the result looks like)

The intent classifier uses these to route user messages.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Skill:
    """A capability that an agent can perform."""

    name: str
    description: str
    examples: List[str]
    agent: str  # which agent handles this
    handler: str = ""  # method name on the agent
    output_type: str = "text"  # text, json, list, summary
    needs_postprocess: bool = False  # hand off to another agent after?
    postprocess_agent: str = ""  # which agent post-processes
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---- Genealogy skill definitions ----

QUERY_SKILL = Skill(
    name="query",
    description="Find people in the family tree matching criteria (sex, place, dates, surname)",
    examples=[
        "find all men born in Ohio",
        "who was born before 1800",
        "list females surname Thomas",
        "show me people born in maryland",
        "males from indiana after 1900",
        "people with no parents",
        "women born between 1850 and 1900",
        "find Toll family members born in kentucky",
    ],
    agent="researcher",
    handler="query_tree",
    output_type="list",
    needs_postprocess=True,
    postprocess_agent="narrator",
)

LOOKUP_SKILL = Skill(
    name="lookup",
    description="Look up details about a specific person",
    examples=[
        "who was Roger Tolle",
        "tell me about Timothy Toll",
        "what do we know about William Toll",
        "information on Sarah Tolle",
        "details for Rebecca Taylor",
    ],
    agent="researcher",
    handler="lookup_person",
    output_type="text",
)

NARRATIVE_SKILL = Skill(
    name="narrative",
    description="Tell a story or narrative about family history",
    examples=[
        "tell me the story of the Tolle family",
        "describe the migration from Maryland",
        "what was life like for Roger Tolle",
        "write about the Toll family history",
        "tell me about all the Tims",
    ],
    agent="narrator",
    handler="narrate",
    output_type="text",
)

VERIFY_SKILL = Skill(
    name="verify",
    description="Check or validate genealogy data for accuracy",
    examples=[
        "is Roger Tolle's birth date correct",
        "check the dates for the Toll family",
        "validate Timothy Toll's parents",
        "are there any errors in the tree",
        "fact check this information",
    ],
    agent="fact_checker",
    handler="verify",
    output_type="text",
)

RESEARCH_SKILL = Skill(
    name="research",
    description="Search the web for more information about a person or topic",
    examples=[
        "research Roger Tolle",
        "look up William Toll online",
        "search for information about the Tolle family in Maryland",
        "find records for Rebecca Taylor",
    ],
    agent="researcher",
    handler="web_research",
    output_type="text",
)

GAPS_SKILL = Skill(
    name="gaps",
    description="Analyze the tree for missing data, dead ends, and anomalies",
    examples=[
        "what's missing in the tree",
        "find gaps in Timothy Toll's ancestry",
        "where are the dead ends",
        "what data is incomplete",
        "find date errors",
    ],
    agent="analyst",
    handler="analyze_gaps",
    output_type="text",
)

# All skills
ALL_SKILLS = [
    QUERY_SKILL,
    LOOKUP_SKILL,
    NARRATIVE_SKILL,
    VERIFY_SKILL,
    RESEARCH_SKILL,
    GAPS_SKILL,
]


def build_skill_prompt() -> str:
    """Build a system prompt describing available skills for the intent classifier."""
    lines = [
        "You are an intent classifier for a genealogy research service.",
        "Given a user message, classify it into one of these skills:\n",
    ]

    for skill in ALL_SKILLS:
        lines.append(f"Skill: {skill.name}")
        lines.append(f"  Description: {skill.description}")
        lines.append(f"  Examples: {', '.join(skill.examples[:3])}")
        lines.append("")

    lines.append(
        "Respond with ONLY a JSON object: "
        '{"skill": "<name>", "confidence": 0.0-1.0, '
        '"extracted": {"name": "...", "place": "...", "year": "...", "sex": "..."}}'
    )
    lines.append(
        'The "extracted" field should contain any entities you can '
        "identify from the message (person names, places, dates, sex)."
    )

    return "\n".join(lines)
