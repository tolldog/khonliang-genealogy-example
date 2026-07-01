"""
Genealogy MCP server — extends khonliang's MCP server with tree-specific tools.

Exposes GEDCOM tree queries, ancestor chains, migration timelines, gap analysis,
and web search to external LLMs like Claude Code.

Usage:
    python -m genealogy_agent.mcp_server
    python -m genealogy_agent.mcp_server --transport http --port 8080
"""

import argparse
import json
import logging
import re

from khonliang.gateway.blackboard import Blackboard
from khonliang.knowledge.store import KnowledgeStore
from khonliang.knowledge.triples import TripleStore
from khonliang.mcp import KhonliangMCPServer

from genealogy_agent.config import load_config
from genealogy_agent.gedcom_parser import GedcomTree

logger = logging.getLogger(__name__)


class GenealogyMCPServer(KhonliangMCPServer):
    """MCP server with genealogy-specific tree tools.

    Extends the generic khonliang MCP server with GEDCOM tree queries,
    ancestor/descendant chains, migration timelines, and gap analysis.
    Also exposes feedback, heuristic, and personality tools.
    """

    def __init__(
        self, tree: GedcomTree, feedback_store=None,
        heuristic_pool=None, personality_registry=None,
        forest=None, cross_matcher=None, match_agent=None,
        importer=None, **kwargs
    ):
        super().__init__(**kwargs)
        self.add_guide("genealogy_guide", "genealogy research workflow, evidence standards, and tree tools")
        self.tree = tree
        self.feedback_store = feedback_store
        self.heuristic_pool = heuristic_pool
        self.personality_registry = personality_registry
        self.forest = forest
        self.cross_matcher = cross_matcher
        self.match_agent = match_agent
        self.importer = importer

    def create_app(self):
        app = super().create_app()
        self._register_tree_tools(app)
        self._register_training_tools(app)
        self._register_forest_tools(app)
        self._register_genealogy_guide(app)
        return app

    def _register_genealogy_guide(self, app) -> None:
        """Register the genealogy_guide tool advertised via add_guide().

        add_guide() only lists the guide in the catalog; without this the
        catalog would point at a tool that does not exist.
        """
        @app.tool()
        def genealogy_guide(topic: str = "workflow") -> str:
            """Genealogy research workflow, evidence standards, and tool catalog.

            topic: workflow | evidence | tools | matching (default: workflow).
            """
            sections = self._genealogy_guide_sections()
            if topic in sections:
                return sections[topic]
            return (
                f"Unknown topic '{topic}'. Available: {', '.join(sections)}.\n\n"
                + sections["workflow"]
            )

    def _genealogy_guide_sections(self) -> "dict[str, str]":
        """Guide content, reflecting only the tools THIS server registered.

        Forest / matching / import-export / training tools are conditional on
        their components (self.forest, cross_matcher, importer, feedback_store,
        heuristic_pool, personality_registry) — the guide must not point at tools
        that aren't registered (the same broken-discovery bug this fix closes).
        """
        has_forest = bool(self.forest)
        has_match = bool(self.cross_matcher and self.forest)
        has_import = bool(self.importer and self.forest)
        training = []
        if self.feedback_store:
            training.append("feedback_stats")
        if self.heuristic_pool:
            training.append("heuristic_list")
        if self.personality_registry:
            training.append("personality_list")

        # --- workflow ---
        wf = [
            "# Genealogy research workflow\n",
            "1. Orient: `tree_summary` (size, families, date range)"
            + ("; `forest_list` (all loaded trees)" if has_forest else "") + ".",
            "2. Find people: `tree_search <name>` then `tree_person <name>` for full "
            "detail (parents/spouses/children/siblings).",
            "3. Trace lines: `tree_ancestors` / `tree_descendants` (set generations); "
            "`tree_migration` follows a line's places over time.",
            "4. Inspect: `tree_gaps` surfaces missing/uncertain relationships; "
            "`tree_context <name>` returns the raw LLM context for one person (what "
            "the agent sees) — it takes a person NAME, not a free-text query.",
        ]
        n = 5
        if has_match:
            wf.append(f"{n}. Resolve identities across trees: `match_scan <tree_a> "
                      "<tree_b>` proposes candidates; `match_confirm <xref_a> "
                      "<xref_b>` records a reviewed match as a same_as triple "
                      "(topic=matching).")
            n += 1
        if has_import:
            wf.append(f"{n}. Import/export: `import_gedcom <path>` / "
                      "`export_gedcom <tree_name>`.")
        wf.append("\nReserve strong claims for corroborated evidence — see topic=evidence.")

        # --- tools (only what's registered) ---
        tools = [
            "# Tool catalog (this server)\n",
            "TREE: tree_summary, tree_search, tree_person, tree_ancestors, "
            "tree_descendants, tree_migration, tree_context (raw context for one "
            "person by name), tree_gaps.",
        ]
        if has_forest:
            forest_line = "FOREST: forest_list, forest_search"
            if has_match:
                forest_line += ", match_scan, match_confirm"
            if has_import:
                forest_line += ", import_gedcom, export_gedcom"
            tools.append(forest_line + ".")
        if training:
            tools.append("TRAINING / introspection: " + ", ".join(training) + ".")
        tools.append("Plus base khonliang tools: catalog, knowledge_search, the "
                     "triple_* graph tools, coding_guide, response_modes.")

        sections = {
            "workflow": "\n".join(wf),
            "evidence": (
                "# Evidence standards\n\n"
                "- Prefer PRIMARY sources (created at the event: vital records, "
                "census, church registers) over derivative/compiled trees.\n"
                "- A conclusion needs CORROBORATION — two independent sources beat "
                "one. Flag single-source or tree-only claims as tentative.\n"
                "- Distinguish DIRECT evidence (a record states the fact) from "
                "INDIRECT (inferred across records); record which.\n"
                "- Resolve conflicts explicitly, don't silently pick one; `tree_gaps` "
                "marks where evidence is thin.\n"
                "- Cite a source for every asserted relationship/date/place. A "
                "cross-tree identity claim must rest on corroborating dates/places/"
                "relationships, not name similarity alone."
            ),
            "tools": "\n".join(tools),
        }
        if has_match:
            sections["matching"] = (
                "# Cross-tree entity resolution (matching)\n\n"
                "The same person can appear in several trees with variant names/"
                "dates. Workflow:\n"
                "1. `forest_list` / `forest_search` to see trees and candidates.\n"
                "2. `match_scan <tree_a> <tree_b>` proposes candidate matches with "
                "scores and the evidence behind each.\n"
                "3. Review: a match needs more than name similarity — corroborating "
                "dates/places/relationships (topic=evidence). The GRA (Generative "
                "Reviewer Adjudicator) pipeline adjudicates uncertain matches.\n"
                "4. `match_confirm <xref_a> <xref_b>` records a reviewed match; leave "
                "uncertain ones unmerged rather than assert a false identity."
            )
        return sections

    def _register_tree_tools(self, app) -> None:
        tree = self.tree

        @app.tool()
        def tree_summary() -> str:
            """Get a summary of the family tree (person count, families, date range)."""
            return tree.get_summary()

        @app.tool()
        def tree_search(query: str) -> str:
            """Search for persons in the family tree by name."""
            results = tree.search_persons(query)
            if not results:
                return f"No persons found matching: {query}"
            lines = [f"Found {len(results)} persons:"]
            for p in results:
                lines.append(f"  {p.display}")
            return "\n".join(lines)

        @app.tool()
        def tree_person(name: str) -> str:
            """Get detailed information about a person including family."""
            person = tree.find_person(name)
            if not person:
                return f"Person '{name}' not found in the tree."

            data = person.to_dict()
            data["parents"] = [p.to_dict() for p in tree.get_parents(person.xref)]
            data["spouses"] = [s.to_dict() for s in tree.get_spouses(person.xref)]
            data["children"] = [c.to_dict() for c in tree.get_children(person.xref)]
            data["siblings"] = [s.to_dict() for s in tree.get_siblings(person.xref)]
            return json.dumps(data, indent=2)

        @app.tool()
        def tree_ancestors(name: str, generations: int = 4) -> str:
            """Get the ancestor chain for a person."""
            person = tree.find_person(name)
            if not person:
                return f"Person '{name}' not found."

            ancestors = tree.get_ancestors(person.xref, generations=generations)
            if not ancestors:
                return f"No ancestors found for {person.full_name}."

            lines = [f"Ancestors of {person.full_name} ({len(ancestors)}):"]
            for a in ancestors:
                lines.append(f"  {a.display}")
            return "\n".join(lines)

        @app.tool()
        def tree_descendants(name: str, generations: int = 3) -> str:
            """Get descendants of a person."""
            person = tree.find_person(name)
            if not person:
                return f"Person '{name}' not found."

            descendants = tree.get_descendants(person.xref, generations=generations)
            if not descendants:
                return f"No descendants found for {person.full_name}."

            lines = [f"Descendants of {person.full_name} ({len(descendants)}):"]
            for d in descendants:
                lines.append(f"  {d.display}")
            return "\n".join(lines)

        @app.tool()
        def tree_migration(name: str, generations: int = 10) -> str:
            """Trace migration patterns through a person's ancestor line."""
            person = tree.find_person(name)
            if not person:
                return f"Person '{name}' not found."

            ancestors = tree.get_ancestors(person.xref, generations=generations)
            all_people = [person] + ancestors

            moves = []
            for p in all_people:
                year = None
                for date_str in [p.birth_date, p.death_date]:
                    if date_str:
                        m = re.search(r"\d{4}", date_str)
                        if m:
                            year = int(m.group())
                            break
                place = p.birth_place or p.death_place or ""
                if place:
                    moves.append({
                        "name": p.full_name,
                        "year": year,
                        "birth_place": p.birth_place,
                        "death_place": p.death_place,
                    })

            moves.sort(key=lambda x: x["year"] or 9999)

            lines = [f"Migration timeline for {person.full_name}'s line:\n"]
            for m in moves:
                yr = str(m["year"]) if m["year"] else "????"
                bp = m["birth_place"] or "?"
                dp = m["death_place"] or ""
                line = f"  {yr}  {m['name']}  b. {bp}"
                if dp and dp != bp:
                    line += f"  d. {dp}"
                lines.append(line)
            return "\n".join(lines)

        @app.tool()
        def tree_context(name: str) -> str:
            """Get the raw LLM context for a person (what the agent sees)."""
            person = tree.find_person(name)
            if not person:
                return f"Person '{name}' not found."
            return tree.build_context(person.xref, depth=2)

        @app.tool()
        def tree_gaps(name: str = "") -> str:
            """Analyze the tree for gaps and research opportunities."""
            from genealogy_agent.tree_analysis import TreeAnalyzer

            analyzer = TreeAnalyzer(tree)
            if name:
                return analyzer.summary(root_name=name)
            return analyzer.summary()

        @app.resource("tree://summary")
        def tree_summary_resource() -> str:
            """Family tree summary statistics."""
            return tree.get_summary()

    def _register_training_tools(self, app) -> None:
        feedback_store = self.feedback_store
        heuristic_pool = self.heuristic_pool
        personality_registry = self.personality_registry

        if feedback_store:
            @app.tool()
            def feedback_stats() -> str:
                """Get interaction and feedback statistics (total interactions, ratings breakdown)."""
                stats = feedback_store.get_stats()
                return json.dumps(stats, indent=2, default=str)

        if heuristic_pool:
            @app.tool()
            def heuristic_list() -> str:
                """List learned heuristic rules extracted from interaction outcomes."""
                rules = heuristic_pool.get_heuristics(min_confidence=0.0)
                if not rules:
                    return "No heuristics learned yet."
                lines = [f"Learned rules ({len(rules)}):"]
                for h in rules:
                    lines.append(
                        f"  [{h.confidence:.0%}] {h.rule} "
                        f"(samples={h.sample_count})"
                    )
                return "\n".join(lines)

        if personality_registry:
            @app.tool()
            def personality_list() -> str:
                """List available personalities for @mention routing."""
                personalities = personality_registry.list_enabled()
                if not personalities:
                    return "No personalities configured."
                lines = ["Available personalities:"]
                for p in personalities:
                    aliases = ", ".join(p.aliases) if p.aliases else "none"
                    lines.append(
                        f"  @{p.id} — {p.name} ({p.description}) "
                        f"[weight={p.voting_weight:.0%}, aliases={aliases}]"
                    )
                return "\n".join(lines)


    def _register_forest_tools(self, app) -> None:
        forest = self.forest
        cross_matcher = self.cross_matcher
        importer = self.importer

        if forest:
            @app.tool()
            def forest_list() -> str:
                """List all loaded family trees with person and family counts."""
                return forest.get_summary()

            @app.tool()
            def forest_search(query: str, tree_name: str = "") -> str:
                """Search for persons across all trees (or a specific tree)."""
                if tree_name:
                    qp = forest.find_person(query, tree_name=tree_name)
                    if qp:
                        return json.dumps(qp.to_dict(), indent=2)
                    return f"No person matching '{query}' in tree '{tree_name}'."
                results = forest.search_all(query)
                if not results:
                    return f"No persons found matching: {query}"
                lines = [f"Found {len(results)} persons across {len(forest)} trees:"]
                for qp in results[:30]:
                    lines.append(f"  {qp.display}")
                return "\n".join(lines)

        if cross_matcher and forest:
            @app.tool()
            def match_scan(tree_a: str, tree_b: str, min_score: float = 0.6) -> str:
                """Run heuristic cross-matching between two trees to find potential person matches."""
                candidates = cross_matcher.scan(tree_a, tree_b, min_score=min_score)
                if not candidates:
                    return f"No matches found between '{tree_a}' and '{tree_b}' above {min_score:.0%}."
                lines = [f"Found {len(candidates)} match candidates:"]
                for c in candidates[:20]:
                    lines.append(f"  {c.display}")
                return "\n".join(lines)

            @app.tool()
            def match_confirm(xref_a: str, xref_b: str) -> str:
                """Confirm a match between two qualified xrefs (e.g., 'toll:@I1@'), storing as same_as triple."""
                ts = self.triple_store
                if not ts:
                    return "Triple store not configured."
                ts.add(
                    subject=xref_a, predicate="same_as",
                    obj=xref_b, confidence=1.0, source="mcp_confirmed",
                )
                ts.remove(subject=xref_a, predicate="possible_match", obj=xref_b)
                return f"Confirmed: {xref_a} = {xref_b}"

        if importer and forest:
            @app.tool()
            def import_gedcom(path: str, name: str = "") -> str:
                """Import a GEDCOM file with sanity checking. Returns import status and any issues found."""
                result = importer.import_file(path, name=name or None)
                return result.display

            @app.tool()
            def export_gedcom(tree_name: str, path: str = "") -> str:
                """Export a tree back to GEDCOM format."""
                if not path:
                    path = f"data/{tree_name}_export.ged"
                try:
                    output = importer.export_gedcom(tree_name, path)
                    return f"Exported to {output}"
                except ValueError as e:
                    return str(e)


def main():
    parser = argparse.ArgumentParser(description="Genealogy MCP server")
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio"
    )
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)

    from khonliang.training import FeedbackStore, HeuristicPool
    from genealogy_agent.forest import load_forest_from_config
    from genealogy_agent.cross_matcher import CrossMatcher
    from genealogy_agent.importer import GedcomImporter
    from genealogy_agent.personalities import create_genealogy_registry

    forest = load_forest_from_config(config)
    tree = forest.default_tree

    knowledge_db = config["app"].get("knowledge_db", "data/knowledge.db")
    store = KnowledgeStore(knowledge_db)
    triples = TripleStore(knowledge_db)
    board = Blackboard()

    # Training components
    feedback_store = None
    heuristic_pool = None
    personality_registry = None
    if config.get("training", {}).get("feedback_enabled", True):
        feedback_store = FeedbackStore(db_path=knowledge_db)
    if config.get("training", {}).get("heuristics_enabled", True):
        heuristic_pool = HeuristicPool(db_path=knowledge_db)
    if config.get("personalities", {}).get("enabled", True):
        personality_registry = create_genealogy_registry()

    # Multi-tree components
    cross_matcher = CrossMatcher(forest)
    importer = GedcomImporter(forest, cross_matcher=cross_matcher)

    server = GenealogyMCPServer(
        tree=tree,
        knowledge_store=store,
        triple_store=triples,
        blackboard=board,
        feedback_store=feedback_store,
        heuristic_pool=heuristic_pool,
        personality_registry=personality_registry,
        forest=forest,
        cross_matcher=cross_matcher,
        importer=importer,
    )

    app = server.create_app()

    if args.transport == "stdio":
        logger.info("Starting genealogy MCP server (stdio)")
        app.run(transport="stdio")
    else:
        logger.info(f"Starting genealogy MCP server (http://{args.host}:{args.port})")
        app.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
