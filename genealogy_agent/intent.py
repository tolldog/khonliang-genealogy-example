"""
Intent classifier — determines what the user wants and builds a pipeline.

Uses the fast 3b model to classify user messages against registered skills.
Detects compound intents (query + narrative, lookup + verify, etc.) and
chains them into a processing pipeline.

Examples:
    "find all men born in Ohio" → [QUERY]
    "tell me about Roger Tolle" → [LOOKUP]
    "find men from Ohio and tell me about their migration" → [QUERY, NARRATIVE]
    "check if Roger Tolle's dates are correct and research his parents" → [VERIFY, RESEARCH]
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from genealogy_agent.skills import ALL_SKILLS, Skill, build_skill_prompt

logger = logging.getLogger(__name__)


@dataclass
class Intent:
    """A classified intent with extracted entities."""

    skill: str
    confidence: float
    extracted: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Pipeline:
    """An ordered list of intents to process."""

    intents: List[Intent]
    raw_message: str
    is_compound: bool = False

    @property
    def primary(self) -> Optional[Intent]:
        return self.intents[0] if self.intents else None


class IntentClassifier:
    """
    Classifies user messages into skills and builds processing pipelines.

    Uses the fast classifier model (llama3.2:3b) for intent detection.
    Falls back to keyword matching if LLM is unavailable.

    Example:
        classifier = IntentClassifier(ollama_client)
        pipeline = await classifier.classify("find men from Ohio and describe their migration")
        # pipeline.intents = [Intent("query", ...), Intent("narrative", ...)]
        # pipeline.is_compound = True
    """

    # Compound intent connectors
    CONNECTORS = [
        " and then ", " then ", " and ", " also ", ", then ",
        ". also ", ". then ",
    ]

    def __init__(
        self,
        ollama_client=None,
        model: str = "llama3.2:3b",
        skills: Optional[List[Skill]] = None,
    ):
        self.client = ollama_client
        self.model = model
        self.skills = {s.name: s for s in (skills or ALL_SKILLS)}
        self._system_prompt = build_skill_prompt()

    async def classify(self, message: str) -> Pipeline:
        """
        Classify a message into a pipeline of intents.

        Detects compound intents by:
        1. Checking for connector words ("and then", "also")
        2. If found, classifies each part separately
        3. If not compound, classifies the whole message
        """
        # Check for compound intent
        parts = self._split_compound(message)

        if len(parts) > 1:
            intents = []
            for part in parts:
                intent = await self._classify_single(part)
                if intent:
                    intents.append(intent)
            return Pipeline(
                intents=intents,
                raw_message=message,
                is_compound=True,
            )

        # Single intent
        intent = await self._classify_single(message)
        return Pipeline(
            intents=[intent] if intent else [],
            raw_message=message,
            is_compound=False,
        )

    async def _classify_single(self, message: str) -> Optional[Intent]:
        """Classify a single (non-compound) message."""
        # Try LLM classification first
        if self.client:
            try:
                return await self._classify_llm(message)
            except Exception as e:
                logger.debug(f"LLM classification failed: {e}")

        # Fallback to keyword matching
        return self._classify_keywords(message)

    async def _classify_llm(self, message: str) -> Optional[Intent]:
        """Use the fast model to classify intent."""
        prompt = f"User message: \"{message}\"\n\nClassify this message."

        response = await self.client.generate(
            prompt=prompt,
            system=self._system_prompt,
            model=self.model,
            temperature=0.1,
            max_tokens=200,
        )

        return self._parse_llm_response(response)

    def _parse_llm_response(self, response: str) -> Optional[Intent]:
        """Parse the LLM's JSON classification."""
        import re

        text = response.strip()

        # Extract JSON from response
        json_match = re.search(r"\{[^{}]+\}", text)
        if not json_match:
            return None

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return None

        skill_name = data.get("skill", "").lower()
        if skill_name not in self.skills:
            return None

        return Intent(
            skill=skill_name,
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
            extracted=data.get("extracted", {}),
        )

    def _classify_keywords(self, message: str) -> Optional[Intent]:
        """Fallback keyword-based classification."""
        msg_lower = message.lower()

        # Score each skill by keyword overlap with examples
        best_skill = None
        best_score = 0

        for skill in self.skills.values():
            score = 0
            for example in skill.examples:
                # Count matching words
                example_words = set(example.lower().split())
                message_words = set(msg_lower.split())
                overlap = len(example_words & message_words)
                score = max(score, overlap)

            if score > best_score:
                best_score = score
                best_skill = skill

        if best_skill and best_score >= 2:
            return Intent(
                skill=best_skill.name,
                confidence=min(1.0, best_score / 5.0),
                extracted=self._extract_entities_simple(msg_lower),
            )

        # Default to lookup if a name is detected, else narrative
        if any(c.isupper() for c in message[1:]):
            return Intent(skill="lookup", confidence=0.3, extracted={})
        return Intent(skill="narrative", confidence=0.3, extracted={})

    def _extract_entities_simple(self, message: str) -> Dict[str, Any]:
        """Simple entity extraction without LLM."""
        import re

        entities: Dict[str, Any] = {}

        # Sex
        if any(w in message.split() for w in ["male", "males", "men", "man"]):
            entities["sex"] = "M"
        elif any(w in message.split() for w in ["female", "females", "women", "woman"]):
            entities["sex"] = "F"

        # Year
        years = re.findall(r"\b\d{4}\b", message)
        if years:
            entities["year"] = years[0]
        if "before" in message and years:
            entities["year_before"] = years[0]
        if "after" in message and years:
            entities["year_after"] = years[-1]

        # Place (after "in", "from", "born in")
        place_match = re.search(
            r"(?:born in|from|in)\s+([a-z][a-z\s]+?)(?:\s+(?:before|after|between|and)|$)",
            message,
        )
        if place_match:
            entities["place"] = place_match.group(1).strip()

        return entities

    def _split_compound(self, message: str) -> List[str]:
        """Split a compound message into parts."""
        msg_lower = message.lower()

        for connector in self.CONNECTORS:
            if connector in msg_lower:
                idx = msg_lower.index(connector)
                part1 = message[:idx].strip()
                part2 = message[idx + len(connector):].strip()
                if part1 and part2:
                    return [part1, part2]

        return [message]

    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self.skills.get(name)
