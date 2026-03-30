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
from typing import Any, Dict

from khonliang import ModelPool
from khonliang.integrations.websocket_chat import ChatServer
from khonliang.knowledge import KnowledgeStore, Librarian
from khonliang.research import ResearchPool, ResearchTrigger

from genealogy_agent.chat_handler import ResearchChatHandler
from genealogy_agent.config import load_config
from genealogy_agent.gedcom_parser import GedcomTree
from genealogy_agent.intent import IntentClassifier
from genealogy_agent.self_eval import ResponseEvaluator
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
    Extended chat server with research commands and self-evaluation.

    - ! commands intercepted by ResearchChatHandler
    - LLM responses evaluated against tree data before returning
    - Evaluation issues trigger automatic research to fill gaps
    """

    def __init__(
        self, research_handler=None, evaluator=None,
        intent_classifier=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.research_handler = research_handler
        self.evaluator = evaluator
        self.intent_classifier = intent_classifier

    async def _handle_chat(self, msg, session):
        """Override: ! commands + intent classification + self-evaluation."""
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

        # Normal chat routing
        resp = await super()._handle_chat(msg, session)

        # Self-evaluate LLM responses
        if self.evaluator and resp.get("type") == "response":
            response_text = resp.get("content", "")
            role = resp.get("role", "")

            evaluation = self.evaluator.evaluate(
                response_text, query=content, role=role
            )

            # Append caveat if issues found
            if evaluation["caveat"]:
                resp["content"] = response_text + "\n\n" + evaluation["caveat"]

            # Adjust knowledge confidence based on evaluation
            resp.setdefault("metadata", {})
            resp["metadata"]["eval_confidence"] = evaluation["confidence"]
            resp["metadata"]["eval_issues"] = len(evaluation["issues"])

            if not evaluation["passed"]:
                logger.warning(
                    f"Self-eval flagged: {len(evaluation['issues'])} issues, "
                    f"confidence={evaluation['confidence']:.0%}"
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
        for issue in evaluation["issues"]:
            if issue["type"] == "uncertainty":
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

            elif issue["type"] == "unknown_name":
                # Name mentioned but not in tree — look it up
                name = issue.get("name", "")
                if name:
                    try:
                        from khonliang.research.models import ResearchTask
                        self.research_handler.pool.submit(ResearchTask(
                            task_type="person_lookup",
                            query=f'"{name}" genealogy',
                            scope="genealogy",
                            source="self_eval_unknown_name",
                            priority=-2,
                        ))
                        logger.info(f"Auto-research queued for unknown name: {name}")
                    except Exception:
                        logger.debug("Failed to queue unknown-name research", exc_info=True)


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
    import os

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

    # Models
    pool = ModelPool(
        config["ollama"]["models"],
        base_url=config["ollama"]["url"],
    )

    roles = {
        "researcher": ResearcherRole(pool, tree=tree),
        "fact_checker": FactCheckerRole(pool, tree=tree),
        "narrator": NarratorRole(pool, tree=tree),
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

    # Self-evaluation
    evaluator = ResponseEvaluator(tree)

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
