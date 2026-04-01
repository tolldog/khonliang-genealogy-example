"""
Genealogy agent CLI — interactive family tree research.

Usage:
    genealogy load family.ged
    genealogy chat family.ged
    genealogy query family.ged "who were John Smith's parents?"
    genealogy summary family.ged
    genealogy search family.ged "Smith"

Multi-tree:
    genealogy trees family.ged [other.ged ...]
    genealogy scan family.ged other.ged
    genealogy match family.ged other.ged "John Smith"
    genealogy import family.ged --name toll
    genealogy export toll --output export.ged
    genealogy merge toll:@I1@ into smith:@I2@
"""

import argparse
import asyncio
import logging
import os

from khonliang import ModelPool

from genealogy_agent.config import load_config
from genealogy_agent.cross_matcher import CrossMatcher
from genealogy_agent.forest import TreeForest, load_forest_from_config
from genealogy_agent.gedcom_parser import GedcomTree
from genealogy_agent.importer import GedcomImporter
from genealogy_agent.merge import MergeEngine
from genealogy_agent.roles import FactCheckerRole, NarratorRole, ResearcherRole
from genealogy_agent.router import GenealogyRouter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def build_service(tree: GedcomTree, ollama_url: str):
    """Build the genealogy agent service."""
    pool = ModelPool(
        {
            "researcher": "llama3.2:3b",
            "fact_checker": "qwen2.5:7b",
            "narrator": "llama3.1:8b",
        },
        base_url=ollama_url,
    )

    roles = {
        "researcher": ResearcherRole(pool, tree=tree),
        "fact_checker": FactCheckerRole(pool, tree=tree),
        "narrator": NarratorRole(pool, tree=tree),
    }

    router = GenealogyRouter()
    return roles, router


async def handle_query(message: str, roles: dict, router: GenealogyRouter) -> str:
    """Route and handle a query."""
    role_name, reason = router.route_with_reason(message)
    logger.info(f"Routing -> {role_name} ({reason})")

    role = roles[role_name]
    result = await role.handle(message, session_id="cli")
    return result["response"]


# ------------------------------------------------------------------
# Single-tree commands
# ------------------------------------------------------------------

def cmd_load(args):
    """Load and summarize a GEDCOM file."""
    tree = GedcomTree.from_file(args.file)
    print(tree.get_summary())
    print(f"\nLoaded {len(tree.persons)} persons from {args.file}")


def cmd_summary(args):
    """Print detailed tree summary."""
    tree = GedcomTree.from_file(args.file)
    print(tree.get_summary())

    print("\n--- Persons ---")
    for person in sorted(tree.persons.values(), key=lambda p: p.surname):
        print(f"  {person.display}")


def cmd_search(args):
    """Search for persons in the tree."""
    tree = GedcomTree.from_file(args.file)
    results = tree.search_persons(args.query)

    if not results:
        print(f"No results for '{args.query}'")
        return

    print(f"Found {len(results)} results for '{args.query}':\n")
    for person in results:
        print(f"  {person.display}")
        parents = tree.get_parents(person.xref)
        if parents:
            print(f"    Parents: {', '.join(p.full_name for p in parents)}")


def cmd_query(args):
    """Run a single query against the tree."""
    tree = GedcomTree.from_file(args.file)
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    roles, router = build_service(tree, ollama_url)

    response = asyncio.run(handle_query(args.query, roles, router))
    print(response)


def cmd_chat(args):
    """Interactive chat with the genealogy agent."""
    tree = GedcomTree.from_file(args.file)
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    roles, router = build_service(tree, ollama_url)

    print(f"Genealogy Agent — {len(tree.persons)} persons loaded")
    print(f"Tree: {tree.get_summary().split(chr(10))[0]}")
    print("Commands: 'search <name>', 'summary', 'exit'\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break

        if not user_input:
            continue

        if user_input.lower() == "summary":
            print(tree.get_summary())
            continue

        if user_input.lower().startswith("search "):
            query = user_input[7:].strip()
            results = tree.search_persons(query)
            if results:
                for p in results:
                    print(f"  {p.display}")
            else:
                print(f"  No results for '{query}'")
            continue

        response = asyncio.run(handle_query(user_input, roles, router))
        print(f"\nAgent: {response}\n")


# ------------------------------------------------------------------
# Multi-tree commands
# ------------------------------------------------------------------

def _build_forest(files):
    """Build a TreeForest from a list of file paths."""
    forest = TreeForest()
    for path in files:
        from pathlib import Path
        import re
        name = Path(path).stem.lower().replace(" ", "_")
        name = re.sub(r"[^a-z0-9_]", "_", name)
        forest.load(name, path)
    return forest


def cmd_trees(args):
    """List all loaded trees with summaries."""
    forest = _build_forest(args.files)
    print(forest.get_summary())


def cmd_scan(args):
    """Run heuristic cross-matching between two trees."""
    forest = TreeForest()
    from pathlib import Path
    import re

    name_a = Path(args.file_a).stem.lower().replace(" ", "_")
    name_a = re.sub(r"[^a-z0-9_]", "_", name_a)
    name_b = Path(args.file_b).stem.lower().replace(" ", "_")
    name_b = re.sub(r"[^a-z0-9_]", "_", name_b)

    forest.load(name_a, args.file_a)
    forest.load(name_b, args.file_b)

    matcher = CrossMatcher(forest)
    min_score = args.min_score or 0.6
    candidates = matcher.scan(name_a, name_b, min_score=min_score)

    if not candidates:
        print(f"No matches found above {min_score:.0%}.")
        return

    print(f"Cross-match: {name_a} vs {name_b} — {len(candidates)} candidates\n")
    for c in candidates:
        conflict_str = f"  [{', '.join(c.conflicts)}]" if c.conflicts else ""
        print(f"  {c.display}{conflict_str}")
        print(
            f"    name={c.name_score:.0%}  date={c.date_score:.0%}  "
            f"place={c.place_score:.0%}  family={c.family_score:.0%}"
        )


def cmd_match(args):
    """Compare a specific person across two trees."""
    forest = TreeForest()
    from pathlib import Path
    import re

    name_a = Path(args.file_a).stem.lower().replace(" ", "_")
    name_a = re.sub(r"[^a-z0-9_]", "_", name_a)
    name_b = Path(args.file_b).stem.lower().replace(" ", "_")
    name_b = re.sub(r"[^a-z0-9_]", "_", name_b)

    forest.load(name_a, args.file_a)
    forest.load(name_b, args.file_b)

    # Find the person in tree A
    tree_a = forest.get_tree(name_a)
    person_a = tree_a.find_person(args.name)
    if not person_a:
        print(f"Person '{args.name}' not found in {name_a}.")
        return

    # Search tree B for matches
    tree_b = forest.get_tree(name_b)
    matches = tree_b.search_persons(person_a.surname)
    if not matches:
        print(f"No persons with surname '{person_a.surname}' in {name_b}.")
        return

    matcher = CrossMatcher(forest)

    print(f"Matching {person_a.full_name} ({name_a}) against {name_b}:\n")
    results = []
    for person_b in matches:
        candidate = matcher.compare(
            f"{name_a}:{person_a.xref}", f"{name_b}:{person_b.xref}"
        )
        if candidate:
            results.append(candidate)

    results.sort(key=lambda c: -c.score)
    for c in results:
        conflict_str = f"  [{', '.join(c.conflicts)}]" if c.conflicts else ""
        print(f"  {c.person_b.person.full_name}: {c.score:.0%}{conflict_str}")
        print(
            f"    name={c.name_score:.0%}  date={c.date_score:.0%}  "
            f"place={c.place_score:.0%}  family={c.family_score:.0%}"
        )


def cmd_import(args):
    """Import a GEDCOM file with sanity checking."""
    forest = TreeForest()
    importer = GedcomImporter(forest)
    result = importer.import_file(args.file, name=args.name)
    print(result.display)


def cmd_export(args):
    """Export a tree to GEDCOM format."""
    config = load_config()
    forest = load_forest_from_config(config)

    if args.tree_name not in forest:
        print(f"Tree '{args.tree_name}' not found. Available: {', '.join(forest.tree_names)}")
        return

    importer = GedcomImporter(forest)
    output = args.output or f"data/{args.tree_name}_export.ged"
    importer.export_gedcom(args.tree_name, output)
    print(f"Exported '{args.tree_name}' to {output}")


def cmd_merge(args):
    """Merge a person from source tree into target tree."""
    config = load_config()
    forest = load_forest_from_config(config)

    from khonliang.knowledge.triples import TripleStore
    knowledge_db = config["app"].get("knowledge_db", "data/knowledge.db")
    triple_store = TripleStore(knowledge_db)

    engine = MergeEngine(forest, triple_store=triple_store)
    result = engine.merge_person(
        args.source, args.target, strategy=args.strategy
    )
    print(result.display)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="genealogy",
        description="LLM-backed genealogy research tool",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Single-tree commands ---

    p_load = sub.add_parser("load", help="Load and summarize a GEDCOM file")
    p_load.add_argument("file", help="Path to GEDCOM file")

    p_summary = sub.add_parser("summary", help="Print detailed tree summary")
    p_summary.add_argument("file", help="Path to GEDCOM file")

    p_search = sub.add_parser("search", help="Search for persons")
    p_search.add_argument("file", help="Path to GEDCOM file")
    p_search.add_argument("query", help="Search query")

    p_query = sub.add_parser("query", help="One-shot query")
    p_query.add_argument("file", help="Path to GEDCOM file")
    p_query.add_argument("query", help="Question to ask")

    p_chat = sub.add_parser("chat", help="Interactive chat")
    p_chat.add_argument("file", help="Path to GEDCOM file")

    # --- Multi-tree commands ---

    p_trees = sub.add_parser("trees", help="List multiple trees with summaries")
    p_trees.add_argument("files", nargs="+", help="GEDCOM files to load")

    p_scan = sub.add_parser("scan", help="Cross-match persons between two trees")
    p_scan.add_argument("file_a", help="First GEDCOM file")
    p_scan.add_argument("file_b", help="Second GEDCOM file")
    p_scan.add_argument(
        "--min-score", type=float, default=0.6,
        help="Minimum match score (default: 0.6)",
    )

    p_match = sub.add_parser("match", help="Find a specific person across two trees")
    p_match.add_argument("file_a", help="Source GEDCOM file")
    p_match.add_argument("file_b", help="Target GEDCOM file")
    p_match.add_argument("name", help="Person name to match")

    p_import = sub.add_parser("import", help="Import GEDCOM with sanity checking")
    p_import.add_argument("file", help="GEDCOM file to import")
    p_import.add_argument("--name", default=None, help="Tree name (derived from filename if omitted)")

    p_export = sub.add_parser("export", help="Export a tree to GEDCOM format")
    p_export.add_argument("tree_name", help="Tree name to export (from config)")
    p_export.add_argument("--output", default=None, help="Output path")

    p_merge = sub.add_parser("merge", help="Merge a person from source into target")
    p_merge.add_argument("source", help="Source qualified xref (tree:@I1@)")
    p_merge.add_argument("target", help="Target qualified xref (tree:@I2@)")
    p_merge.add_argument(
        "--strategy", default="prefer_target",
        choices=["prefer_target", "prefer_source", "merge_all"],
        help="Merge strategy (default: prefer_target)",
    )

    args = parser.parse_args()

    commands = {
        "load": cmd_load,
        "summary": cmd_summary,
        "search": cmd_search,
        "query": cmd_query,
        "chat": cmd_chat,
        "trees": cmd_trees,
        "scan": cmd_scan,
        "match": cmd_match,
        "import": cmd_import,
        "export": cmd_export,
        "merge": cmd_merge,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
