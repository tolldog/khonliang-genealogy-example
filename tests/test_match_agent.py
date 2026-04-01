"""Tests for MatchAgent — LLM-backed match evaluation and consensus integration."""

import pytest

from khonliang.consensus import AgentVote

from genealogy_agent.forest import TreeForest
from genealogy_agent.match_agent import (
    MatchAgentRole,
    MatchAssessment,
    MatchVotingAgent,
)


class FakeModelPool:
    """Minimal model pool stub."""
    pass


class TestMatchAssessmentParsing:
    """Verify structured output parsing."""

    def setup_method(self):
        self.forest = TreeForest()
        self.role = MatchAgentRole(FakeModelPool(), self.forest)

    def test_parse_full_assessment(self):
        response = """
VERDICT: match
CONFIDENCE: 0.92
EVIDENCE:
- Same name: John Smith
- Birth year matches: 1850
- Both born in Springfield, Illinois
CONFLICTS:
- none
RECOMMENDATION: link
REASONING: Strong match based on name, date, and place alignment.
"""
        result = self.role._parse_assessment(response)
        assert result.verdict == "match"
        assert result.confidence == 0.92
        assert len(result.evidence) >= 2
        assert result.recommendation == "link"
        assert "Strong match" in result.reasoning

    def test_parse_no_match(self):
        response = """
VERDICT: no_match
CONFIDENCE: 0.85
EVIDENCE:
- Same surname
CONFLICTS:
- Different birth years: 1850 vs 1900
- Different states: Illinois vs Ohio
RECOMMENDATION: skip
REASONING: Too many conflicts for a match.
"""
        result = self.role._parse_assessment(response)
        assert result.verdict == "no_match"
        assert result.confidence == 0.85
        assert len(result.conflicts) >= 2
        assert result.recommendation == "skip"

    def test_parse_possible_match(self):
        response = """
VERDICT: possible_match
CONFIDENCE: 0.6
EVIDENCE:
- Same surname and similar given name
CONFLICTS:
- Birth year differs by 3 years
RECOMMENDATION: review
REASONING: Needs more evidence.
"""
        result = self.role._parse_assessment(response)
        assert result.verdict == "possible_match"
        assert result.recommendation == "review"

    def test_parse_malformed_response(self):
        response = "I think these might be the same person but I'm not sure."
        result = self.role._parse_assessment(response)
        # Should fall back to defaults
        assert result.verdict == "possible_match"
        assert result.confidence == 0.5
        assert result.recommendation == "review"

    def test_assessment_to_dict(self):
        assessment = MatchAssessment(
            confidence=0.9,
            verdict="match",
            evidence=["same name"],
            conflicts=[],
            recommendation="link",
            reasoning="clear match",
        )
        d = assessment.to_dict()
        assert d["confidence"] == 0.9
        assert d["verdict"] == "match"


class TestComparisonPrompt:
    """Verify prompt building for side-by-side comparison."""

    def test_build_prompt_includes_both_persons(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        role = MatchAgentRole(FakeModelPool(), forest)

        person_a = forest.get_person("toll:@I1@")
        person_b = forest.get_person("toll:@I3@")
        prompt = role._build_comparison_prompt(person_a, person_b)

        assert "PERSON A" in prompt
        assert "PERSON B" in prompt
        assert "John Smith" in prompt
        assert "William Smith" in prompt
        assert "toll" in prompt

    def test_build_prompt_includes_family(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        role = MatchAgentRole(FakeModelPool(), forest)

        person_a = forest.get_person("toll:@I1@")
        person_b = forest.get_person("toll:@I3@")
        prompt = role._build_comparison_prompt(person_a, person_b)

        # Person A (John Smith) has spouse Mary Jones
        assert "Mary Jones" in prompt

    def test_build_prompt_with_context(self, sample_gedcom_path):
        forest = TreeForest()
        forest.load("toll", sample_gedcom_path)
        role = MatchAgentRole(FakeModelPool(), forest)

        person_a = forest.get_person("toll:@I1@")
        person_b = forest.get_person("toll:@I3@")
        prompt = role._build_comparison_prompt(
            person_a, person_b, context="Census records show same household"
        )
        assert "Census records" in prompt


class TestMatchVotingAgent:
    """Verify consensus protocol integration."""

    def test_agent_id(self):
        role = MatchAgentRole(FakeModelPool(), TreeForest())
        agent = MatchVotingAgent(role)
        assert agent.agent_id == "match_agent"

    @pytest.mark.asyncio
    async def test_analyze_approve(self):
        class FakeRole:
            role = "match_agent"
            async def handle(self, msg, session_id=None, context=None):
                return {"response": "These are clearly the same person. Match confirmed."}
        agent = MatchVotingAgent(FakeRole())
        vote = await agent.analyze("compare these records")
        assert isinstance(vote, AgentVote)
        assert vote.action == "approve"

    @pytest.mark.asyncio
    async def test_analyze_reject(self):
        class FakeRole:
            role = "match_agent"
            async def handle(self, msg, session_id=None, context=None):
                return {"response": "No match. These are different people."}
        agent = MatchVotingAgent(FakeRole())
        vote = await agent.analyze("compare these records")
        assert vote.action == "reject"
