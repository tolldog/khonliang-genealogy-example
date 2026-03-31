"""
Chat handler — wraps the WebSocket chat server with research and ingestion.

Intercepts messages starting with ! prefixes:
- Research commands: !lookup, !search, !find, !history, !tree, !ancestors, !migration
- Ingestion commands: !ingest, !ingest-file, !ingest-dir
- Knowledge commands: !knowledge, !prune, !promote
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from khonliang.knowledge import Librarian
from khonliang.knowledge.store import Tier
from khonliang.research import ResearchPool, ResearchTrigger

from genealogy_agent.report_server import get_detector, publish_report

logger = logging.getLogger(__name__)


class ResearchChatHandler:
    """
    Middleware that adds research + ingestion commands to the chat.

    Intercepts messages starting with ! prefixes, queues research,
    ingests content, or manages knowledge. Non-! messages pass through.
    """

    # Commands that this handler owns
    COMMANDS = {
        # Research (delegated to trigger/pool)
        "!lookup", "!search", "!find", "!history",
        "!ancestors", "!migration", "!tree",
        # Ingestion
        "!ingest", "!ingest-file", "!ingest-dir",
        # Direct search
        "!google", "!fetch",
        # Tree analysis
        "!gaps", "!dead-ends", "!anomalies",
        # Batch research
        "!researchwho",
        # Reports
        "!report", "!session",
        # Knowledge management
        "!knowledge", "!prune", "!promote", "!demote",
        "!axiom",
    }

    def __init__(
        self,
        pool: ResearchPool,
        trigger: ResearchTrigger,
        librarian: Optional[Librarian] = None,
        tree: Optional[Any] = None,
        poll_interval: float = 0.5,
        poll_timeout: float = 30.0,
    ):
        self.pool = pool
        self.trigger = trigger
        self.librarian = librarian
        self.tree = tree
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def is_command(self, message: str) -> bool:
        """Check if a message is any ! command."""
        msg_lower = message.lower().strip()
        return any(msg_lower.startswith(cmd) for cmd in self.COMMANDS)

    async def handle(
        self,
        message: str,
        scope: str = "global",
        source: str = "user",
    ) -> Dict[str, Any]:
        """Route a ! command to the right handler."""
        msg_lower = message.lower().strip()

        # Research commands
        if self.trigger.strip_prefix(message) is not None:
            return await self._handle_research(message, scope, source)

        # Direct search / fetch
        if msg_lower.startswith("!google"):
            return self._handle_google(message)
        if msg_lower.startswith("!fetch"):
            return self._handle_fetch(message, scope)

        # Tree analysis
        if msg_lower.startswith("!gaps"):
            return self._handle_gaps(message)
        if msg_lower.startswith("!dead-ends"):
            return self._handle_dead_ends(message)
        if msg_lower.startswith("!anomalies"):
            return self._handle_anomalies()
        if msg_lower.startswith("!researchwho"):
            return await self._handle_researchwho(message)

        # Reports
        if msg_lower.startswith("!report"):
            return self._handle_report(message)
        if msg_lower.startswith("!session"):
            return self._handle_session_report()

        # Ingestion commands
        if msg_lower.startswith("!ingest-file"):
            return self._handle_ingest_file(message, scope)
        if msg_lower.startswith("!ingest-dir"):
            return self._handle_ingest_dir(message, scope)
        if msg_lower.startswith("!ingest"):
            return self._handle_ingest_text(message, scope)

        # Knowledge management
        if msg_lower.startswith("!knowledge"):
            return self._handle_knowledge_status()
        if msg_lower.startswith("!prune"):
            return self._handle_prune()
        if msg_lower.startswith("!promote"):
            return self._handle_promote(message)
        if msg_lower.startswith("!demote"):
            return self._handle_demote(message)
        if msg_lower.startswith("!axiom"):
            return self._handle_axiom(message)

        return {"type": "error", "content": f"Unknown command: {message[:30]}"}

    # ------------------------------------------------------------------
    # Research
    # ------------------------------------------------------------------

    async def _handle_research(
        self, message: str, scope: str, source: str
    ) -> Dict[str, Any]:
        """Queue research task and wait for result."""
        task_ids = self.trigger.check_message(message, scope=scope, source=source)

        if not task_ids:
            return {"type": "error", "content": "Could not parse research command."}

        results = []
        for task_id in task_ids:
            result = await self._wait_for_result(task_id)
            if result:
                results.append(result)

        if not results:
            return {
                "type": "response",
                "content": "Research queued but timed out. Results will appear in knowledge when complete.",
                "role": "research",
                "reason": "timeout",
            }

        content_parts = []
        all_sources: List[str] = []
        for r in results:
            content_parts.append(f"**{r.title}**\n{r.content}")
            all_sources.extend(r.sources)

        return {
            "type": "response",
            "content": "\n\n".join(content_parts),
            "role": "research",
            "reason": "research_complete",
            "metadata": {
                "task_ids": task_ids,
                "sources": all_sources[:10],
                "result_count": len(results),
            },
        }

    async def _wait_for_result(self, task_id: str) -> Optional[Any]:
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            result = self.pool.get_result(task_id)
            if result:
                return result
            await asyncio.sleep(self.poll_interval)
        return None

    # ------------------------------------------------------------------
    # Google search / Fetch
    # ------------------------------------------------------------------

    def _handle_google(self, message: str) -> Dict[str, Any]:
        """
        Google search. Format: !google query text
        """
        from genealogy_agent.web_search import GenealogySearcher

        query = message.split(None, 1)[1].strip() if " " in message else ""
        if not query:
            return {"type": "error", "content": "Usage: !google <query>"}

        searcher = GenealogySearcher()
        results = searcher.google_search(query, max_results=8)

        if not results:
            # Fallback to DDG
            results = searcher.search(query, max_results=8)

        if not results:
            return {
                "type": "response",
                "content": "No results found.",
                "role": "research",
                "reason": "google",
            }

        return {
            "type": "response",
            "content": searcher.build_context(results),
            "role": "research",
            "reason": "google",
            "metadata": {"sources": [r.url for r in results[:10]]},
        }

    def _handle_fetch(self, message: str, scope: str) -> Dict[str, Any]:
        """
        Fetch a URL, extract text, and optionally ingest into knowledge.
        Format: !fetch <url>
        Format: !fetch ingest <url>  (also saves to knowledge)
        """
        from genealogy_agent.web_search import GenealogySearcher

        text = message.split(None, 1)[1].strip() if " " in message else ""
        if not text:
            return {
                "type": "error",
                "content": "Usage: !fetch <url> or !fetch ingest <url>",
            }

        ingest = False
        if text.lower().startswith("ingest "):
            ingest = True
            text = text[7:].strip()

        url = text
        searcher = GenealogySearcher()
        content = searcher.fetch_page(url, max_chars=6000)

        if not content:
            return {"type": "error", "content": f"Failed to fetch {url}"}

        # Optionally ingest into knowledge
        if ingest and self.librarian:
            domain = url.split("/")[2] if url.count("/") >= 2 else url
            self.librarian.ingest_text(
                content=content,
                title=f"Fetched: {domain}",
                scope=scope,
                source=url,
            )
            return {
                "type": "response",
                "content": f"Fetched and ingested ({len(content)} chars):\n\n"
                + content[:2000],
                "role": "librarian",
                "reason": "fetch_ingest",
            }

        return {
            "type": "response",
            "content": content[:4000],
            "role": "research",
            "reason": "fetch",
            "metadata": {"url": url, "length": len(content)},
        }

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def _handle_ingest_text(self, message: str, scope: str) -> Dict[str, Any]:
        """
        Ingest raw text into knowledge.
        Format: !ingest [scope:name] title | content
        Example: !ingest scope:toll Census note | John Toll listed in 1850 census
        """
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        text = message.split(None, 1)[1] if " " in message else ""
        if not text:
            return {
                "type": "error",
                "content": "Usage: !ingest [scope:name] title | content",
            }

        # Parse optional scope override
        ingest_scope = scope
        if text.lower().startswith("scope:"):
            scope_part, text = text.split(None, 1)
            ingest_scope = scope_part.split(":", 1)[1]

        # Split title | content
        if "|" in text:
            title, content = text.split("|", 1)
            title = title.strip()
            content = content.strip()
        else:
            title = text[:60]
            content = text

        result = self.librarian.ingest_text(
            content=content, title=title, scope=ingest_scope, source="user_chat"
        )

        return {
            "type": "response",
            "content": f"Ingested: \"{title}\" into scope '{ingest_scope}' "
            f"({result.added} added, {result.skipped} skipped)",
            "role": "librarian",
            "reason": "ingestion",
        }

    def _handle_ingest_file(self, message: str, scope: str) -> Dict[str, Any]:
        """
        Ingest a file.
        Format: !ingest-file [scope:name] /path/to/file.txt
        """
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        text = message.split(None, 1)[1] if " " in message else ""
        if not text:
            return {
                "type": "error",
                "content": "Usage: !ingest-file [scope:name] /path/to/file",
            }

        ingest_scope = scope
        if text.lower().startswith("scope:"):
            scope_part, text = text.split(None, 1)
            ingest_scope = scope_part.split(":", 1)[1]

        path = text.strip()
        result = self.librarian.ingest_file(path, scope=ingest_scope)

        return {
            "type": "response",
            "content": f"Ingested file: {path} into scope '{ingest_scope}' "
            f"({result.added} added, {result.errors} errors)",
            "role": "librarian",
            "reason": "ingestion",
        }

    def _handle_ingest_dir(self, message: str, scope: str) -> Dict[str, Any]:
        """
        Ingest a directory.
        Format: !ingest-dir [scope:name] /path/to/directory
        """
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        text = message.split(None, 1)[1] if " " in message else ""
        if not text:
            return {
                "type": "error",
                "content": "Usage: !ingest-dir [scope:name] /path/to/dir",
            }

        ingest_scope = scope
        if text.lower().startswith("scope:"):
            scope_part, text = text.split(None, 1)
            ingest_scope = scope_part.split(":", 1)[1]

        directory = text.strip()
        result = self.librarian.ingest_directory(directory, scope=ingest_scope)

        return {
            "type": "response",
            "content": f"Ingested directory: {directory} into scope '{ingest_scope}' "
            f"({result.added} added, {result.errors} errors)",
            "role": "librarian",
            "reason": "ingestion",
        }

    # ------------------------------------------------------------------
    # Knowledge management
    # ------------------------------------------------------------------

    def _handle_knowledge_status(self) -> Dict[str, Any]:
        """Show knowledge store status."""
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        stats = self.librarian.get_status()
        by_tier = stats.get("by_tier", {})
        by_scope = stats.get("by_scope", {})

        lines = [
            f"Knowledge Store: {stats.get('total_entries', 0)} entries",
            f"  Axioms (Tier 1): {by_tier.get('axiom', 0)}",
            f"  Imported (Tier 2): {by_tier.get('imported', 0)}",
            f"  Derived (Tier 3): {by_tier.get('derived', 0)}",
        ]
        if by_scope:
            lines.append("  Scopes: " + ", ".join(
                f"{k} ({v})" for k, v in by_scope.items()
            ))

        pool_status = self.pool.get_status()
        lines.append(
            f"\nResearch Pool: {pool_status['completed']} completed, "
            f"{pool_status['queue_size']} queued, "
            f"{pool_status['failed']} failed"
        )

        return {
            "type": "response",
            "content": "\n".join(lines),
            "role": "librarian",
            "reason": "status",
        }

    def _handle_prune(self) -> Dict[str, Any]:
        """Prune low-quality Tier 3 entries."""
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        pruned = self.librarian.prune()
        promoted = self.librarian.auto_promote()

        return {
            "type": "response",
            "content": f"Maintenance complete: {pruned} pruned, {promoted} promoted.",
            "role": "librarian",
            "reason": "maintenance",
        }

    def _handle_promote(self, message: str) -> Dict[str, Any]:
        """Promote a Tier 3 entry to Tier 2. Format: !promote entry_id"""
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        entry_id = message.split(None, 1)[1].strip() if " " in message else ""
        if not entry_id:
            return {"type": "error", "content": "Usage: !promote <entry_id>"}

        if self.librarian.store.promote(entry_id):
            return {
                "type": "response",
                "content": f"Promoted {entry_id} from Tier 3 to Tier 2.",
                "role": "librarian",
                "reason": "promote",
            }
        return {"type": "error", "content": f"Could not promote {entry_id}."}

    def _handle_demote(self, message: str) -> Dict[str, Any]:
        """Demote a Tier 2 entry to Tier 3. Format: !demote entry_id"""
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        entry_id = message.split(None, 1)[1].strip() if " " in message else ""
        if not entry_id:
            return {"type": "error", "content": "Usage: !demote <entry_id>"}

        if self.librarian.store.demote(entry_id):
            return {
                "type": "response",
                "content": f"Demoted {entry_id} from Tier 2 to Tier 3.",
                "role": "librarian",
                "reason": "demote",
            }
        return {"type": "error", "content": f"Could not demote {entry_id}."}

    def _handle_axiom(self, message: str) -> Dict[str, Any]:
        """
        Add/view axioms.
        !axiom — list all axioms
        !axiom key | content — set an axiom
        """
        if not self.librarian:
            return {"type": "error", "content": "No librarian configured."}

        text = message.split(None, 1)[1].strip() if " " in message else ""

        if not text:
            # List axioms
            axioms = self.librarian.get_axioms()
            if not axioms:
                return {
                    "type": "response",
                    "content": "No axioms set.",
                    "role": "librarian",
                    "reason": "axioms",
                }
            lines = ["Axioms (Tier 1):"]
            for a in axioms:
                lines.append(f"  - {a.title}: {a.content}")
            return {
                "type": "response",
                "content": "\n".join(lines),
                "role": "librarian",
                "reason": "axioms",
            }

        # Set axiom: !axiom key | content
        if "|" in text:
            key, content = text.split("|", 1)
            self.librarian.set_axiom(key.strip(), content.strip())
            return {
                "type": "response",
                "content": f"Axiom set: {key.strip()}",
                "role": "librarian",
                "reason": "axiom_set",
            }

        return {
            "type": "error",
            "content": "Usage: !axiom key | content (or !axiom to list)",
        }

    # ------------------------------------------------------------------
    # Tree analysis
    # ------------------------------------------------------------------

    def _handle_gaps(self, message: str) -> Dict[str, Any]:
        """Analyze tree for gaps. !gaps or !gaps Timothy Toll"""
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        from genealogy_agent.tree_analysis import TreeAnalyzer

        analyzer = TreeAnalyzer(self.tree)
        name = message.split(None, 1)[1].strip() if " " in message else ""

        return {
            "type": "response",
            "content": analyzer.summary(root_name=name or None),
            "role": "analyst",
            "reason": "gap_analysis",
        }

    def _handle_dead_ends(self, message: str) -> Dict[str, Any]:
        """
        Find dead-end lines. !dead-ends Timothy Toll
        Optionally auto-queues research for top dead ends.
        !dead-ends Timothy Toll research — queues web lookups
        """
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        from genealogy_agent.tree_analysis import TreeAnalyzer

        text = message.split(None, 1)[1].strip() if " " in message else ""
        auto_research = "research" in text.lower()
        name = text.replace("research", "").strip()

        if not name:
            return {
                "type": "error",
                "content": "Usage: !dead-ends <name> [research]",
            }

        analyzer = TreeAnalyzer(self.tree)
        gaps = analyzer.find_dead_ends_for(name)

        if not gaps:
            return {
                "type": "response",
                "content": f"No dead ends found for {name}.",
                "role": "analyst",
                "reason": "dead_ends",
            }

        lines = [f"Dead-end lines for {name} ({len(gaps)}):"]
        queued = 0

        for g in gaps[:20]:
            lines.append(f"\n  {g.description}")
            if g.research_query:
                lines.append(f"    Query: {g.research_query}")

            # Auto-queue research for top dead ends
            if auto_research and g.research_query and queued < 5:
                try:
                    from khonliang.research.models import ResearchTask

                    self.pool.submit(ResearchTask(
                        task_type="person_lookup",
                        query=g.research_query,
                        scope="genealogy",
                        source="dead_end_analysis",
                        priority=-1,
                    ))
                    queued += 1
                    lines.append("    → Queued for research")
                except Exception:
                    pass

        if len(gaps) > 20:
            lines.append(f"\n  ... and {len(gaps) - 20} more")

        if queued:
            lines.append(f"\nQueued {queued} research tasks (background)")

        return {
            "type": "response",
            "content": "\n".join(lines),
            "role": "analyst",
            "reason": "dead_ends",
        }

    def _handle_anomalies(self) -> Dict[str, Any]:
        """Find date anomalies in the tree."""
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        from genealogy_agent.tree_analysis import TreeAnalyzer

        analyzer = TreeAnalyzer(self.tree)
        gaps = analyzer.find_date_anomalies()

        if not gaps:
            return {
                "type": "response",
                "content": "No date anomalies found.",
                "role": "analyst",
                "reason": "anomalies",
            }

        lines = [f"Date anomalies ({len(gaps)}):"]
        for g in gaps[:20]:
            lines.append(f"  [{g.severity}] {g.person_name}: {g.description}")

        if len(gaps) > 20:
            lines.append(f"\n  ... and {len(gaps) - 20} more")

        return {
            "type": "response",
            "content": "\n".join(lines),
            "role": "analyst",
            "reason": "anomalies",
        }

    # ------------------------------------------------------------------
    # Batch research
    # ------------------------------------------------------------------

    async def _handle_researchwho(self, message: str) -> Dict[str, Any]:
        """
        Find persons matching criteria, then queue web lookups for each.

        !researchwho males born in ohio before 1920
        !researchwho females surname Thomas no parents
        !researchwho born in maryland between 1700 and 1800
        """
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        parts = message.split(None, 1)
        criteria = parts[1].strip() if len(parts) > 1 else ""
        if not criteria:
            return {
                "type": "error",
                "content": (
                    "Usage: !researchwho <criteria>\n"
                    "Examples:\n"
                    "  !researchwho males born in ohio before 1920\n"
                    "  !researchwho females surname Thomas no parents\n"
                    "  !researchwho born in maryland between 1700 and 1800"
                ),
            }

        from genealogy_agent.tree_analysis import TreeAnalyzer

        analyzer = TreeAnalyzer(self.tree)
        matches = analyzer.query_persons(criteria)

        if not matches:
            return {
                "type": "response",
                "content": f"No persons match: {criteria}",
                "role": "analyst",
                "reason": "researchwho",
            }

        # Show matches and queue research
        lines = [f"Found {len(matches)} persons matching: {criteria}\n"]
        queued = 0
        max_research = 10  # don't queue more than 10

        for p in matches[:20]:
            line = f"  {p.display}"
            lines.append(line)

            # Queue web lookup for each (up to max)
            if queued < max_research:
                search_name = analyzer.search_name(p)
                year = None
                year_match = re.search(r"\d{4}", p.birth_date or "")
                if year_match:
                    year = year_match.group()

                # Use criteria place (what user searched for) as primary,
                # fall back to birth place if no criteria place
                criteria_place = ""
                place_match = re.search(
                    r"(?:born in|lived in|from|in)\s+([a-z][a-z\s]+?)(?:\s|$)",
                    criteria.lower(),
                )
                if place_match:
                    criteria_place = place_match.group(1).strip()

                place = criteria_place
                if not place and p.birth_place:
                    place = p.birth_place.split(",")[0].strip()

                query = f'"{search_name}"'
                if place:
                    query += f" {place}"
                if year:
                    query += f" {year}"
                query += " genealogy"

                try:
                    from khonliang.research.models import ResearchTask

                    self.pool.submit(ResearchTask(
                        task_type="person_lookup",
                        query=query,
                        scope="genealogy",
                        source="researchwho",
                        priority=-1,
                    ))
                    queued += 1
                    lines.append(f"    → Queued: {query}")
                except Exception:
                    logger.debug("Failed to queue research", exc_info=True)

        if len(matches) > 20:
            lines.append(f"\n  ... and {len(matches) - 20} more (showing first 20)")

        lines.append(f"\nQueued {queued} web lookups (background)")

        return {
            "type": "response",
            "content": "\n".join(lines),
            "role": "analyst",
            "reason": "researchwho",
            "metadata": {
                "matches": len(matches),
                "queued": queued,
                "criteria": criteria,
            },
        }

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _handle_report(self, message: str) -> Dict[str, Any]:
        """
        Generate a report.
        !report — knowledge report
        !report <name> — person report
        !report gaps — gap analysis
        !report gaps <name> — person-specific gap analysis
        """
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        from genealogy_agent.reports import ReportBuilder

        builder = ReportBuilder(
            self.tree,
            knowledge_store=self.librarian.store if self.librarian else None,
        )

        text = message.split(None, 1)[1].strip() if " " in message else ""

        if not text:
            content = builder.knowledge_report()
            url = publish_report(
                content, title="Knowledge Report", created_by="analyst",
                report_type="knowledge",
            )
            if url:
                content += f"\n\n[View full report]({url})"
            return {
                "type": "response",
                "content": content,
                "role": "analyst",
                "reason": "knowledge_report",
                "metadata": {"report_url": url},
            }

        if text.lower().startswith("gaps"):
            name = text[4:].strip() if len(text) > 4 else ""
            content = builder.gap_report(name or None)
            title = f"Gap Analysis — {name}" if name else "Gap Analysis"
            url = publish_report(
                content, title=title, created_by="analyst",
                report_type="gap_analysis",
                metadata={"person": name} if name else None,
            )
            if url:
                content += f"\n\n[View full report]({url})"
            return {
                "type": "response",
                "content": content,
                "role": "analyst",
                "reason": "gap_report",
                "metadata": {"report_url": url},
            }

        # Person report
        content = builder.person_report(text)
        url = publish_report(
            content, title=f"Report: {text}", created_by="analyst",
            report_type="person_report",
            metadata={"person": text},
        )
        if url:
            content += f"\n\n[View full report]({url})"
        return {
            "type": "response",
            "content": content,
            "role": "analyst",
            "reason": "person_report",
            "metadata": {"report_url": url},
        }

    def _handle_session_report(self) -> Dict[str, Any]:
        """Generate a session summary."""
        if not self.tree:
            return {"type": "error", "content": "No tree loaded."}

        from genealogy_agent.reports import ReportBuilder

        builder = ReportBuilder(
            self.tree,
            knowledge_store=self.librarian.store if self.librarian else None,
        )

        content = builder.session_report()
        url = publish_report(
            content, title="Session Summary", created_by="analyst",
            report_type="session_summary",
        )
        if url:
            content += f"\n\n[View full report]({url})"
        return {
            "type": "response",
            "content": content,
            "role": "analyst",
            "reason": "session_report",
            "metadata": {"report_url": url},
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            "pool": self.pool.get_status(),
            "researchers": self.pool.list_researchers(),
        }
