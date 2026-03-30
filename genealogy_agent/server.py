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

from khonliang import ModelPool
from khonliang.integrations.websocket_chat import ChatServer
from khonliang.knowledge import KnowledgeStore, Librarian
from khonliang.research import ResearchPool, ResearchTrigger

from genealogy_agent.chat_handler import ResearchChatHandler
from genealogy_agent.config import load_config
from genealogy_agent.gedcom_parser import GedcomTree
from genealogy_agent.intent import IntentClassifier
from genealogy_agent.self_eval import create_genealogy_evaluator
from genealogy_agent.researchers import TreeResearcher, WebSearchResearcher
from genealogy_agent.roles import FactCheckerRole, NarratorRole, ResearcherRole
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
        intent_classifier=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.research_handler = research_handler
        self.evaluator = evaluator
        self.intent_classifier = intent_classifier
        # Per-session contexts (session_id -> SessionContext)
        self._session_contexts: Dict[str, Any] = {}

    def _get_session_context(self, session):
        """Get or create a SessionContext for this chat session."""
        from khonliang.roles.session import SessionContext

        sid = session.session_id
        if sid not in self._session_contexts:
            self._session_contexts[sid] = SessionContext(session_id=sid)
        return self._session_contexts[sid]

    async def _handle_chat(self, msg, session):
        """Override: ! commands + session context + intent + self-evaluation."""
        content = msg.get("content", "").strip()

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

        # Inject session context for multi-turn coherence
        session_ctx = self._get_session_context(session)
        import genealogy_agent.roles as _roles_mod
        _roles_mod._active_session_context = session_ctx.build_context(max_turns=5)

        # Normal chat routing
        resp = await super()._handle_chat(msg, session)

        # Update session context with this exchange
        if resp.get("type") == "response":
            session_ctx.add_exchange(
                content,
                resp.get("content", "")[:500],
                resp.get("role", ""),
            )

        # Self-evaluate LLM responses
        if self.evaluator and resp.get("type") == "response":
            response_text = resp.get("content", "")
            role = resp.get("role", "")

            resp_metadata = resp.get("metadata", {})
            evaluation = self.evaluator.evaluate(
                response_text,
                query=content,
                role=role,
                metadata=resp_metadata,
            )

            # Append caveat if issues found
            if evaluation.caveat:
                resp["content"] = response_text + "\n\n" + evaluation.caveat

            # Adjust knowledge confidence based on evaluation
            resp.setdefault("metadata", {})
            resp["metadata"]["eval_confidence"] = evaluation.confidence
            resp["metadata"]["eval_issues"] = len(evaluation.issues)

            if not evaluation.passed:
                logger.warning(
                    f"Self-eval flagged: {len(evaluation.issues)} issues, "
                    f"confidence={evaluation.confidence:.0%}"
                )

            # Feed evaluation back to research — if the agent was uncertain
            # or couldn't find info, queue background research
            if self.research_handler:
                self._queue_research_from_eval(
                    evaluation, content
                )

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
    store = KnowledgeStore(config["app"]["knowledge_db"])
    librarian = Librarian(store)

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

    roles = {
        "researcher": ResearcherRole(pool, tree=tree),
        "fact_checker": FactCheckerRole(pool, tree=tree),
        "narrator": NarratorRole(pool, tree=tree, knowledge_store=store),
    }

    router = GenealogyRouter()

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
    from khonliang.client import OllamaClient

    classifier_client = OllamaClient(
        model="llama3.2:3b", base_url=config["ollama"]["url"]
    )
    intent_classifier = IntentClassifier(
        ollama_client=classifier_client, model="llama3.2:3b"
    )

    server = GenealogyChat(
        roles=roles,
        router=router,
        librarian=librarian,
        on_message=on_message,
        research_handler=research_handler,
        evaluator=evaluator,
        intent_classifier=intent_classifier,
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
