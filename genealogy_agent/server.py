"""
Genealogy chat server — WebSocket + web UI + research pool.

Usage:
    python -m genealogy_agent.server
    python -m genealogy_agent.server --config config.yaml
    python -m genealogy_agent.server --port 9000
"""

import argparse
import asyncio
import logging
import os
from typing import Any, Dict

import re

from khonliang import ModelPool
from khonliang.client import OllamaClient
from khonliang.integrations.websocket_chat import ChatServer
from khonliang.knowledge import KnowledgeStore, Librarian
from khonliang.personalities import extract_mention, build_prompt, format_response
from khonliang.research import ResearchPool, ResearchTrigger
from khonliang.routing import ComplexityStrategy, ModelRouter
from khonliang.training import FeedbackStore, HeuristicPool

from genealogy_agent.chat_handler import ResearchChatHandler
from genealogy_agent.config import load_config
from genealogy_agent.consensus import create_consensus_team, create_debate_orchestrator
from genealogy_agent.report_server import get_detector, publish_report, start_report_server
from genealogy_agent.gedcom_parser import GedcomTree
from genealogy_agent.intent import IntentClassifier
from genealogy_agent.personalities import create_genealogy_registry
from genealogy_agent.self_eval import create_genealogy_evaluator
from genealogy_agent.researchers import TreeResearcher, WebSearchResearcher
from genealogy_agent.roles import (
    FactCheckerRole,
    NarratorRole,
    ResearcherRole,
    _session_context_var,
)
from genealogy_agent.router import GenealogyRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


class GenealogyChat(ChatServer):
    """
    Extended chat server with session context, research, and self-evaluation.

    - Session context passed to roles for multi-turn coherence
    - ! commands intercepted by ResearchChatHandler
    - Intent classification via LLM
    - Self-evaluation against tree data
    """

    def __init__(
        self, research_handler=None, evaluator=None,
        intent_classifier=None, feedback_store=None,
        heuristic_pool=None, personality_registry=None,
        consensus_team=None, debate_orchestrator=None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.research_handler = research_handler
        self.evaluator = evaluator
        self.intent_classifier = intent_classifier
        self.feedback_store = feedback_store
        self.heuristic_pool = heuristic_pool
        self.personality_registry = personality_registry
        self.consensus_team = consensus_team
        self.debate_orchestrator = debate_orchestrator
        # Per-session contexts (session_id -> SessionContext)
        self._session_contexts: Dict[str, Any] = {}

    def _get_session_context(self, session):
        """Get or create a SessionContext for this chat session."""
        from khonliang.roles.session import SessionContext

        sid = session.session_id
        if sid not in self._session_contexts:
            self._session_contexts[sid] = SessionContext(session_id=sid)
        return self._session_contexts[sid]

    async def _handle_client(self, websocket):
        """Override to clean up session context on disconnect."""
        try:
            await super()._handle_client(websocket)
        finally:
            # Parent removed the session from _sessions already.
            # Find which session_id was added during super() and clean up.
            for sid in list(self._session_contexts):
                if sid not in self._sessions:
                    self._session_contexts.pop(sid, None)
                    logger.debug(f"Cleaned up session context: {sid}")

    async def _handle_chat(self, msg, session):
        """Override: /rate + @mention + ! commands + session context + intent + self-evaluation + consensus."""
        content = msg.get("content", "").strip()

        # /rate command — rate the last interaction
        if content.startswith("/rate"):
            return self._handle_rate(content, session)

        # @mention — personality routing
        if content.startswith("@") and self.personality_registry:
            personality_resp = await self._handle_personality(content, msg, session)
            if personality_resp:
                return personality_resp

        # ! commands go to research handler directly
        if self.research_handler and self.research_handler.is_command(content):
            logger.info(f"Command: {content[:60]}")
            resp = await self.research_handler.handle(content)
            session.add_exchange(
                content,
                resp.get("content", ""),
                resp.get("role", "system"),
            )
            return resp

        # Intent classification for natural language
        if self.intent_classifier:
            pipeline = await self.intent_classifier.classify(content)
            if pipeline.primary and pipeline.primary.confidence > 0.5:
                intent = pipeline.primary
                logger.info(
                    f"Intent: {intent.skill} "
                    f"(confidence={intent.confidence:.0%}, "
                    f"compound={pipeline.is_compound}, "
                    f"extracted={intent.extracted})"
                )
                # Add intent info to the message metadata
                msg["_intent"] = intent.skill
                msg["_extracted"] = intent.extracted
                msg["_pipeline"] = pipeline

        # Inject session context for multi-turn coherence (async-safe).
        # Use ContextVar.set/reset so the value is scoped to this request only.
        session_ctx = self._get_session_context(session)
        token = _session_context_var.set(session_ctx.build_context(max_turns=5))
        try:
            # Normal chat routing
            resp = await super()._handle_chat(msg, session)
        finally:
            _session_context_var.reset(token)

        # Post-process: session context, eval, consensus, feedback logging
        return await self._post_process_response(resp, content, msg, session)

    async def _post_process_response(self, resp, content, msg, session):
        """Shared post-processing: session context, eval, consensus, heuristics, feedback."""
        if resp.get("type") != "response":
            return resp

        session_ctx = self._get_session_context(session)

        # Update session context with this exchange
        session_ctx.add_exchange(
            content,
            resp.get("content", "")[:500],
            resp.get("role", ""),
        )

        # Self-evaluate LLM responses
        evaluation = None
        if self.evaluator:
            response_text = resp.get("content", "")
            role = resp.get("role", "")
            resp_metadata = resp.get("metadata", {})

            evaluation = self.evaluator.evaluate(
                response_text,
                query=content,
                role=role,
                metadata=resp_metadata,
            )

            # Consensus voting on high-severity issues
            high_issues = [
                i for i in evaluation.issues if getattr(i, "severity", "") == "high"
            ]
            if high_issues and self.consensus_team:
                resp = await self._run_consensus(
                    resp, content, evaluation, high_issues
                )
            elif evaluation.caveat:
                # Simple caveat when no consensus needed
                resp["content"] = response_text + "\n\n" + evaluation.caveat

            resp.setdefault("metadata", {})
            resp["metadata"]["eval_confidence"] = evaluation.confidence
            resp["metadata"]["eval_issues"] = len(evaluation.issues)

            if not evaluation.passed:
                logger.warning(
                    f"Self-eval flagged: {len(evaluation.issues)} issues, "
                    f"confidence={evaluation.confidence:.0%}"
                )

            # Record outcome for heuristic learning
            if self.heuristic_pool:
                try:
                    self.heuristic_pool.record_outcome(
                        action=f"respond_as_{role}",
                        result="success" if evaluation.passed else "failure",
                        context={
                            "query_type": msg.get("_intent", "unknown"),
                            "role": role,
                        },
                        details={
                            "confidence": evaluation.confidence,
                            "issues": len(evaluation.issues),
                        },
                    )
                except Exception:
                    logger.debug("Failed to record heuristic outcome", exc_info=True)

            # Queue background research on uncertainty/date mismatches
            if self.research_handler:
                self._queue_research_from_eval(evaluation, content)

        # Log interaction to feedback store
        if self.feedback_store:
            try:
                iid = self.feedback_store.log_interaction(
                    message=content,
                    role=resp.get("role", ""),
                    route_reason=resp.get("reason", ""),
                    response=resp.get("content", ""),
                    generation_ms=resp.get("metadata", {}).get("generation_time_ms"),
                    session_id=session.session_id,
                    metadata=resp.get("metadata"),
                )
                session._last_interaction_id = iid
            except Exception:
                logger.debug("Failed to log interaction", exc_info=True)

        return resp

    def _handle_rate(self, content, session):
        """Handle /rate 1-5 [optional feedback text]."""
        parts = content.split(None, 2)
        if len(parts) < 2:
            return {
                "type": "response",
                "content": "Usage: /rate 1-5 [optional feedback]",
                "role": "system",
            }
        try:
            rating = int(parts[1])
        except ValueError:
            return {
                "type": "response",
                "content": "Rating must be a number 1-5.",
                "role": "system",
            }
        if not 1 <= rating <= 5:
            return {
                "type": "response",
                "content": "Rating must be between 1 and 5.",
                "role": "system",
            }

        feedback_text = parts[2] if len(parts) > 2 else None
        last_iid = getattr(session, "_last_interaction_id", None)

        if not self.feedback_store:
            return {
                "type": "response",
                "content": "Feedback store not configured.",
                "role": "system",
            }
        if not last_iid:
            return {
                "type": "response",
                "content": "No recent interaction to rate.",
                "role": "system",
            }

        self.feedback_store.add_feedback(
            interaction_id=last_iid, rating=rating, feedback=feedback_text
        )
        stars = "\u2605" * rating + "\u2606" * (5 - rating)
        return {
            "type": "response",
            "content": f"Rated {stars} ({rating}/5). Thank you!",
            "role": "system",
        }

    async def _handle_personality(self, content, msg, session):
        """Route @mention messages to a personality-specific prompt."""
        # Try khonliang's extract_mention first (resolves built-in personas),
        # then fall back to raw @word lookup against our registry (custom personas).
        personality_id = extract_mention(content)
        if not personality_id:
            match = re.match(r"@(\w+)", content)
            if match:
                personality_id = match.group(1).lower()

        if not personality_id:
            return None

        config = self.personality_registry.get(personality_id)
        if not config:
            return None

        # Strip @mention from content
        clean_content = re.sub(r"@\w+\s*", "", content).strip()
        if not clean_content:
            return {
                "type": "response",
                "content": f"**{config.name}**: What would you like me to look at?",
                "role": "researcher",
            }

        # Use researcher role for generation with personality system prompt
        role = self.roles.get("researcher")
        if not role:
            return None

        ctx = role.build_context(clean_content)
        prompt = build_prompt(personality_id, clean_content, context=ctx)

        response_text, elapsed_ms = await role._timed_generate(
            prompt=prompt, system=config.system_prompt
        )

        formatted = format_response(personality_id, response_text)
        resp = {
            "type": "response",
            "content": formatted,
            "role": "researcher",
            "reason": f"personality:{personality_id}",
            "session_id": session.session_id,
            "metadata": {
                "personality": personality_id,
                "generation_time_ms": elapsed_ms,
            },
        }

        # Run shared post-processing (eval, feedback, etc.)
        return await self._post_process_response(resp, clean_content, msg, session)

    async def _run_consensus(self, resp, content, evaluation, high_issues):
        """Run consensus voting and optional debate on high-severity eval issues."""
        response_text = resp.get("content", "")
        try:
            consensus_result = await self.consensus_team.evaluate(
                subject=content,
                context={
                    "original_response": response_text,
                    "query": content,
                    "eval_issues": [i.detail for i in high_issues],
                },
                use_cache=False,
            )

            # Run debate if there's disagreement
            if self.debate_orchestrator and consensus_result.votes:
                disagreement = self.debate_orchestrator.detect_disagreement(
                    consensus_result.votes
                )
                if disagreement:
                    updated_votes = await self.debate_orchestrator.run_debate(
                        votes=consensus_result.votes,
                        subject=content,
                        context={"query": content, "original_response": response_text},
                    )
                    consensus_result = self.consensus_team.consensus_engine.calculate_consensus(
                        updated_votes
                    )
                    resp.setdefault("metadata", {})["debate_occurred"] = True

            # Build response based on consensus outcome
            resp.setdefault("metadata", {})
            resp["metadata"]["consensus_action"] = consensus_result.action
            resp["metadata"]["consensus_confidence"] = consensus_result.confidence

            if consensus_result.action == "reject":
                # Gather corrections from vote reasoning
                corrections = [
                    v.reasoning for v in consensus_result.votes
                    if v.action == "reject"
                ]
                caveat = (
                    "\n\n---\n"
                    "**Consensus review** — this response was flagged for potential issues:\n"
                    + "\n".join(f"- {c}" for c in corrections)
                )
                resp["content"] = response_text + caveat
            elif evaluation.caveat:
                resp["content"] = response_text + "\n\n" + evaluation.caveat

            logger.info(
                f"Consensus: {consensus_result.action} "
                f"(confidence={consensus_result.confidence:.0%}, "
                f"votes={len(consensus_result.votes)})"
            )
        except Exception:
            logger.warning("Consensus voting failed, falling back to caveat", exc_info=True)
            if evaluation.caveat:
                resp["content"] = response_text + "\n\n" + evaluation.caveat

        return resp

    def _queue_research_from_eval(
        self, evaluation, query
    ):
        """Queue research tasks based on evaluation findings."""
        for issue in evaluation.issues:
            if issue.issue_type == "uncertainty":
                # Agent said "I don't have info" — research the query
                try:
                    from khonliang.research.models import ResearchTask
                    self.research_handler.pool.submit(ResearchTask(
                        task_type="web_search",
                        query=query,
                        scope="genealogy",
                        source="self_eval_uncertainty",
                        priority=-2,
                    ))
                    logger.info(f"Auto-research queued from uncertainty: {query[:40]}")
                except Exception:
                    logger.debug("Failed to queue uncertainty research", exc_info=True)

            elif issue.issue_type == "date_mismatch":
                # Date mismatch — research the person to verify
                try:
                    from khonliang.research.models import ResearchTask
                    self.research_handler.pool.submit(ResearchTask(
                        task_type="person_lookup",
                        query=f"{query} genealogy verify",
                        scope="genealogy",
                        source="self_eval_date_mismatch",
                        priority=-2,
                    ))
                    logger.info(f"Auto-research queued for date mismatch: {query[:40]}")
                except Exception:
                    logger.debug("Failed to queue date-mismatch research", exc_info=True)


def build_server(config: Dict[str, Any]):
    """Build the full genealogy chat server with research pool."""

    tree = GedcomTree.from_file(config["app"]["gedcom"])

    # Knowledge
    knowledge_db = config["app"]["knowledge_db"]
    store = KnowledgeStore(knowledge_db)
    librarian = Librarian(store)

    # Training: feedback store + heuristic pool
    feedback_store = None
    heuristic_pool = None
    training_cfg = config.get("training", {})
    if training_cfg.get("feedback_enabled", True):
        feedback_store = FeedbackStore(db_path=knowledge_db)
    if training_cfg.get("heuristics_enabled", True):
        heuristic_pool = HeuristicPool(db_path=knowledge_db)

    librarian.set_axiom(
        "identity",
        "You are a genealogy research assistant with access to a parsed "
        "family tree and a knowledge base of research notes.",
    )
    librarian.set_axiom(
        "cite_sources",
        "Always distinguish between facts from the family tree data and "
        "your own interpretation. Cite which records support your statements.",
    )
    librarian.set_axiom(
        "no_fabrication",
        "Never fabricate names, dates, places, or relationships. If the "
        "data does not contain the answer, say so clearly.",
    )

    # Research pool
    # Credentials read from os.environ — either export them in your shell,
    # use `export $(cat .env | xargs)`, or load via python-dotenv before starting.
    research_pool = ResearchPool()
    research_pool.register(WebSearchResearcher(
        tree=tree,
        geni_api_key=os.environ.get("GENI_API_KEY", ""),
        geni_api_secret=os.environ.get("GENI_API_SECRET", ""),
        geni_app_id=os.environ.get("GENI_APP_ID", ""),
    ))
    research_pool.register(TreeResearcher(tree=tree))
    research_pool.set_librarian(librarian)

    # Triggers
    trigger = ResearchTrigger(research_pool)
    trigger.add_prefix("!lookup", "person_lookup")
    trigger.add_prefix("!search", "web_search")
    trigger.add_prefix("!find", "web_search")
    trigger.add_prefix("!history", "historical_context")
    trigger.add_prefix("!ancestors", "tree_ancestors")
    trigger.add_prefix("!migration", "tree_migration")
    trigger.add_prefix("!tree", "tree_lookup")

    # Models — keep researcher hot, unload narrator/fact_checker after use
    pool = ModelPool(
        config["ollama"]["models"],
        base_url=config["ollama"]["url"],
        keep_alive={
            "researcher": "30m",     # fast model, stays loaded
            "fact_checker": "5m",    # medium, moderate keep
            "narrator": "5m",       # used less frequently
        },
    )

    # Model router — pick model size based on query complexity.
    # Uses the fast researcher model as the classifier.
    # Simple lookups stay on the 3b, complex queries escalate.
    models_config = config["ollama"]["models"]
    role_models = {}
    for role_name, model in models_config.items():
        # Build tier list: configured model + any larger alternatives
        role_models[role_name] = [model]

    classifier_client = OllamaClient(
        model=models_config.get("researcher", "llama3.2:3b"),
        base_url=config["ollama"]["url"],
    )
    model_router = ModelRouter(
        role_models=role_models,
        strategy=ComplexityStrategy(
            classifier_client=classifier_client,
            classifier_model=models_config.get("researcher", "llama3.2:3b"),
        ),
    )

    roles = {
        "researcher": ResearcherRole(
            pool, tree=tree, model_router=model_router,
            heuristic_pool=heuristic_pool,
        ),
        "fact_checker": FactCheckerRole(
            pool, tree=tree, model_router=model_router,
            heuristic_pool=heuristic_pool,
        ),
        "narrator": NarratorRole(
            pool, tree=tree, knowledge_store=store,
            model_router=model_router, heuristic_pool=heuristic_pool,
        ),
    }

    router = GenealogyRouter()

    # Personalities
    personality_registry = None
    if config.get("personalities", {}).get("enabled", True):
        personality_registry = create_genealogy_registry()

    # Consensus voting + debate
    consensus_team = None
    debate_orchestrator = None
    if config.get("consensus", {}).get("enabled", True):
        consensus_team = create_consensus_team(roles, tree, config)
        debate_orchestrator = create_debate_orchestrator(roles, tree, config)

    # Wire up the chat message handler with trigger checking
    def on_message(session_id, msg, response, role):
        logger.info(f"[{session_id}] {role}: {msg[:60]}")

        # Check if the response indicates missing info
        implicit_tasks = trigger.check_response(
            response=response,
            original_query=msg,
            scope=config.get("app", {}).get("default_scope", "global"),
        )
        if implicit_tasks:
            logger.info(
                f"Implicit research queued: {len(implicit_tasks)} tasks"
            )

    research_handler = ResearchChatHandler(
        research_pool, trigger, librarian=librarian, tree=tree
    )

    # Self-evaluation using khonliang BaseEvaluator + genealogy rules
    evaluator = create_genealogy_evaluator(tree)

    # Intent classifier (uses fast model for natural language understanding)
    intent_client = OllamaClient(
        model="llama3.2:3b", base_url=config["ollama"]["url"]
    )
    intent_classifier = IntentClassifier(
        ollama_client=intent_client, model="llama3.2:3b"
    )

    server = GenealogyChat(
        roles=roles,
        router=router,
        librarian=librarian,
        on_message=on_message,
        research_handler=research_handler,
        evaluator=evaluator,
        intent_classifier=intent_classifier,
        feedback_store=feedback_store,
        heuristic_pool=heuristic_pool,
        personality_registry=personality_registry,
        consensus_team=consensus_team,
        debate_orchestrator=debate_orchestrator,
    )

    return server, research_pool, trigger


async def run_server(config: Dict[str, Any]):
    """Run the WebSocket + web UI + research pool."""
    from genealogy_agent.web_server import start_web_server

    server, research_pool, trigger = build_server(config)

    host = config["server"]["host"]
    ws_port = config["server"]["ws_port"]
    web_port = config["server"]["web_port"]

    # Start research pool workers
    research_pool.start(workers=2)

    start_web_server(host=host, port=web_port, config=config)

    # Start report server (background thread)
    start_report_server(config)

    logger.info(f"WebSocket: ws://{host}:{ws_port}")
    logger.info(f"Web UI: http://{host}:{web_port}")
    logger.info("CLI: python -m genealogy_agent.chat_client")
    logger.info(
        f"Research pool: {len(research_pool.list_researchers())} researchers, "
        f"triggers: !lookup, !search, !find, !history, !ancestors, !migration, !tree"
    )
    await server.start(host=host, port=ws_port)


def main():
    parser = argparse.ArgumentParser(description="Genealogy chat server")
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument(
        "--port", type=int, default=None, help="WebSocket port override"
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.port:
        config["server"]["ws_port"] = args.port
        config["server"]["web_port"] = args.port + 1

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()
