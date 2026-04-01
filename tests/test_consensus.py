"""Tests for consensus voting — voting agents, team creation, debate setup."""


import pytest

from khonliang.consensus import AgentVote, ConsensusEngine

from genealogy_agent.consensus import (
    GenealogyVotingAgent,
    create_consensus_team,
    create_debate_orchestrator,
    create_voting_agents,
)


class FakeRole:
    """Minimal role stub for testing voting agents."""

    def __init__(self, role_name, response_text="This looks correct. I approve."):
        self.role = role_name
        self._response = response_text

    async def handle(self, message, session_id=None, context=None):
        return {"response": self._response}

    async def _timed_generate(self, prompt, system=""):
        return self._response, 100


class TestGenealogyVotingAgent:
    """Verify voting agent wrapper behavior."""

    def test_agent_id(self):
        role = FakeRole("researcher")
        agent = GenealogyVotingAgent(role, "researcher", tree=None)
        assert agent.agent_id == "researcher"

    @pytest.mark.asyncio
    async def test_analyze_approve(self):
        role = FakeRole("researcher", "This is correct and I approve it.")
        agent = GenealogyVotingAgent(role, "researcher", tree=None)
        vote = await agent.analyze("test query", {"original_response": "hi", "query": "q", "eval_issues": []})
        assert isinstance(vote, AgentVote)
        assert vote.agent_id == "researcher"
        assert vote.action == "approve"
        assert vote.confidence > 0

    @pytest.mark.asyncio
    async def test_analyze_reject(self):
        role = FakeRole("fact_checker", "This is incorrect and contains errors.")
        agent = GenealogyVotingAgent(role, "fact_checker", tree=None)
        vote = await agent.analyze("test", {"original_response": "wrong", "query": "q", "eval_issues": ["bad date"]})
        assert vote.action == "reject"

    @pytest.mark.asyncio
    async def test_analyze_defer(self):
        role = FakeRole("narrator", "I'm not sure about this one.")
        agent = GenealogyVotingAgent(role, "narrator", tree=None)
        vote = await agent.analyze("test", {"original_response": "maybe", "query": "q", "eval_issues": []})
        assert vote.action == "defer"
        assert vote.confidence == 0.5


class TestConsensusEngine:
    """Verify consensus engine vote aggregation."""

    def test_unanimous_approve(self):
        engine = ConsensusEngine()
        votes = [
            AgentVote(agent_id="a", action="approve", confidence=0.9, reasoning="ok"),
            AgentVote(agent_id="b", action="approve", confidence=0.8, reasoning="ok"),
        ]
        result = engine.calculate_consensus(votes)
        assert result.action == "approve"

    def test_majority_reject(self):
        engine = ConsensusEngine()
        votes = [
            AgentVote(agent_id="a", action="reject", confidence=0.9, reasoning="bad"),
            AgentVote(agent_id="b", action="reject", confidence=0.8, reasoning="bad"),
            AgentVote(agent_id="c", action="approve", confidence=0.5, reasoning="ok"),
        ]
        result = engine.calculate_consensus(votes)
        assert result.action == "reject"

    def test_weighted_votes(self):
        engine = ConsensusEngine(
            agent_weights={"expert": 0.8, "novice": 0.2}
        )
        votes = [
            AgentVote(agent_id="expert", action="reject", confidence=0.9, reasoning="bad"),
            AgentVote(agent_id="novice", action="approve", confidence=0.9, reasoning="ok"),
        ]
        result = engine.calculate_consensus(votes)
        assert result.action == "reject"  # expert outweighs novice


class TestTeamCreation:
    """Verify factory functions create valid objects."""

    def test_create_voting_agents(self):
        roles = {
            "researcher": FakeRole("researcher"),
            "fact_checker": FakeRole("fact_checker"),
        }
        agents = create_voting_agents(roles, tree=None)
        assert len(agents) == 2
        ids = {a.agent_id for a in agents}
        assert "researcher" in ids
        assert "fact_checker" in ids

    def test_create_consensus_team(self):
        roles = {
            "researcher": FakeRole("researcher"),
            "fact_checker": FakeRole("fact_checker"),
            "narrator": FakeRole("narrator"),
        }
        config = {"consensus": {"timeout": 10}}
        team = create_consensus_team(roles, tree=None, config=config)
        assert team is not None

    def test_create_debate_orchestrator(self):
        roles = {
            "researcher": FakeRole("researcher"),
            "fact_checker": FakeRole("fact_checker"),
        }
        config = {
            "consensus": {
                "debate_enabled": True,
                "debate_rounds": 2,
                "disagreement_threshold": 0.6,
            }
        }
        orchestrator = create_debate_orchestrator(roles, tree=None, config=config)
        assert orchestrator is not None
