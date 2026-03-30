"""
Genealogy agent roles — LLM-backed family history research.

- ResearcherRole: answers questions about the family tree
- FactCheckerRole: validates relationships, dates, and flags contradictions
- NarratorRole: generates family stories from genealogy data
"""

import contextvars
import re
from typing import Any, Dict, List, Optional

from khonliang.roles.base import BaseRole

from genealogy_agent.gedcom_parser import GedcomTree, Person

# Per-request session context using contextvars (async-safe).
# Set by the chat server before each handle() call.
_session_context_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_session_context_var", default=""
)


def _build_context_with_session(
    tree: GedcomTree, message: str, max_persons: int = 10
) -> str:
    """Build tree context + session context for multi-turn coherence."""
    result = _build_multi_context(tree, message, max_persons)
    session_ctx = _session_context_var.get("")
    if session_ctx:
        result = f"{result}\n\n[SESSION CONTEXT]\n{session_ctx}"
    return result


def _build_multi_context(
    tree: GedcomTree, message: str, max_persons: int = 10
) -> str:
    """
    Smart context builder — handles both single and multi-person queries.

    Tries multiple strategies:
    1. Search for all matching persons (handles "all the Tims", "Toll family")
    2. Find a specific person mentioned by name
    3. Fall back to tree summary
    """
    # Strategy 1: broad search — look for name-like words in the query
    # Skip common non-name words
    skip = {
        "the", "a", "an", "all", "of", "in", "about", "tell", "me",
        "who", "were", "was", "are", "is", "what", "when", "where",
        "how", "did", "do", "does", "my", "our", "their", "his", "her",
        "family", "tree", "parents", "children", "ancestors", "descendants",
        "grandparents", "siblings", "married", "born", "died", "lived",
        "from", "to", "and", "with", "for", "this", "that", "these",
        "those", "have", "had", "been", "any", "some", "every", "each",
        "story", "history", "migration", "check", "verify", "validate",
        "narrative", "describe", "explain", "find", "search", "list",
        "show", "give", "get", "can", "you", "please", "could",
    }

    # Extract candidate search terms
    # Strip punctuation, possessives, and normalize quotes
    words = message.split()
    candidates = []
    for w in words:
        # Strip punctuation and possessive forms
        cleaned = re.sub(r"['\u2019]s$", "", w)  # Tim's -> Tim
        cleaned = re.sub(r"[.,!?'\"\u2018\u2019\u201c\u201d]", "", cleaned)
        cleaned = cleaned.strip()
        if cleaned.lower() not in skip and len(cleaned) > 1:
            candidates.append(cleaned)

    # Try each candidate as a search term
    all_matches: List[Person] = []
    seen_xrefs = set()
    for term in candidates:
        for person in tree.search_persons(term):
            if person.xref not in seen_xrefs:
                seen_xrefs.add(person.xref)
                all_matches.append(person)

    # If we found multiple matches, return a list context
    if len(all_matches) > 1:
        lines = [f"Found {len(all_matches)} matching persons:"]
        for p in all_matches[:max_persons]:
            lines.append(f"\n- {p.display}")
            parents = tree.get_parents(p.xref)
            if parents:
                lines.append(
                    f"  Parents: {', '.join(par.full_name for par in parents)}"
                )
            spouses = tree.get_spouses(p.xref)
            if spouses:
                lines.append(
                    f"  Spouse: {', '.join(s.full_name for s in spouses)}"
                )
        if len(all_matches) > max_persons:
            lines.append(
                f"\n... and {len(all_matches) - max_persons} more"
            )
        return "\n".join(lines)

    # Strategy 2: single person — try multi-word name matching
    for i in range(len(words)):
        for j in range(i + 1, min(i + 4, len(words) + 1)):
            name_guess = " ".join(words[i:j])
            person = tree.find_person(name_guess)
            if person:
                return tree.build_context(person.xref, depth=2)

    # Strategy 3: if we found exactly one match from search
    if len(all_matches) == 1:
        return tree.build_context(all_matches[0].xref, depth=2)

    # Fallback: tree summary
    return tree.get_summary()


class ResearcherRole(BaseRole):
    """
    Answers questions about the family tree using parsed GEDCOM data.

    Injects relevant family context into the prompt so the LLM can
    answer questions like "who were John's grandparents?" or "where
    did the Smith family come from?"
    """

    def __init__(self, model_pool, tree: GedcomTree, **kwargs):
        super().__init__(role="researcher", model_pool=model_pool, **kwargs)
        self.tree = tree
        self._system_prompt = (
            "You are a genealogy research assistant. You have access to a "
            "parsed family tree database. Answer questions about family "
            "relationships, dates, and places based on the provided data. "
            "Be precise about what the records show vs what is uncertain. "
            "If the data doesn't contain the answer, say so."
        )

    def build_context(
        self, message: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        return _build_context_with_session(self.tree, message)

    async def handle(
        self,
        message: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = self.build_context(message, context)
        prompt = (
            f"Family tree data:\n{ctx}\n\n"
            f"Question: {message}\n\nAnswer:"
        )

        response, elapsed_ms = await self._timed_generate(
            prompt=prompt, system=self.system_prompt
        )
        return {
            "response": response.strip(),
            "metadata": {
                "role": self.role,
                "generation_time_ms": elapsed_ms,
            },
        }


class FactCheckerRole(BaseRole):
    """
    Validates genealogy data for contradictions and anomalies.

    Checks things like:
    - Birth before parent's birth
    - Death before birth
    - Unreasonable age gaps
    - Missing data patterns
    """

    def __init__(self, model_pool, tree: GedcomTree, **kwargs):
        super().__init__(role="fact_checker", model_pool=model_pool, **kwargs)
        self.tree = tree
        self._system_prompt = (
            "You are a genealogy fact-checker. Analyze the provided family "
            "data for inconsistencies, contradictions, or suspicious patterns. "
            "Look for: impossible dates (child born before parent), unreasonable "
            "age gaps, missing data, and historical plausibility issues. "
            "Be specific about what looks wrong and suggest corrections."
        )

    def build_context(
        self, message: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        return _build_context_with_session(self.tree, message, max_persons=15)

    async def handle(
        self,
        message: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = self.build_context(message, context)
        prompt = (
            f"Family tree data to validate:\n{ctx}\n\n"
            f"Request: {message}\n\nAnalysis:"
        )

        response, elapsed_ms = await self._timed_generate(
            prompt=prompt, system=self.system_prompt
        )
        return {
            "response": response.strip(),
            "metadata": {
                "role": self.role,
                "generation_time_ms": elapsed_ms,
            },
        }


class NarratorRole(BaseRole):
    """
    Generates readable family narratives from genealogy data.

    Turns dry dates and places into engaging family stories.
    """

    def __init__(self, model_pool, tree: GedcomTree, knowledge_store=None, **kwargs):
        super().__init__(role="narrator", model_pool=model_pool, **kwargs)
        self.tree = tree
        self.knowledge_store = knowledge_store
        self._system_prompt = (
            "You are a genealogy narrator. Your job is to present family tree "
            "data in a readable, engaging way.\n\n"
            "STRICT RULES:\n"
            "1. ONLY state facts that appear in the provided data. Do not invent "
            "names, dates, places, occupations, or events.\n"
            "2. If historical context is provided in the KNOWLEDGE section, you "
            "may weave it into the narrative. Do NOT invent historical context.\n"
            "3. You may make SIMPLE inferences (e.g., 'the family moved westward' "
            "if birth places show that pattern) but label them: 'Based on the "
            "records, it appears that...'\n"
            "4. Never fabricate county names, military service, causes of death, "
            "or occupations unless they appear in the data.\n"
            "5. If you don't have enough data for a rich narrative, say so "
            "briefly rather than padding with speculation.\n"
            "6. Format: Use the person's actual dates and places. Organize "
            "chronologically. Keep it concise."
        )

    def build_context(
        self, message: str, context: Optional[Dict[str, Any]] = None
    ) -> str:
        # Tree data
        tree_ctx = _build_context_with_session(self.tree, message)

        # Knowledge store data (previously researched facts)
        knowledge_ctx = ""
        if self.knowledge_store:
            knowledge_ctx = self.knowledge_store.build_context(
                query=message, max_chars=2000, include_axioms=False
            )

        parts = [tree_ctx]
        if knowledge_ctx:
            parts.append(f"\n[KNOWLEDGE]\n{knowledge_ctx}")
        return "\n".join(parts)

    async def handle(
        self,
        message: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = self.build_context(message, context)

        # Collect facts that went into the context — evaluator checks these
        referenced_persons = self._extract_referenced_persons(message)

        prompt = (
            f"Family tree data:\n{ctx}\n\n"
            f"Request: {message}\n\nNarrative:"
        )

        response, elapsed_ms = await self._timed_generate(
            prompt=prompt, system=self.system_prompt
        )
        return {
            "response": response.strip(),
            "metadata": {
                "role": self.role,
                "generation_time_ms": elapsed_ms,
                "referenced_persons": referenced_persons,
            },
        }

    def _extract_referenced_persons(self, message: str) -> List[Dict]:
        """Extract persons referenced in the query for evaluator context.

        Uses per-token and bigram searches (via search_persons) so that
        natural-language queries like "Tell me about John Smith" match
        correctly, rather than passing the whole sentence as a query.
        """
        persons: List[Dict] = []

        # Heuristic: extract name-like tokens and search per token / n-gram.
        tokens = re.findall(r"[A-Za-z][A-Za-z'-]*", message)

        # Skip common non-name words (mirrors _build_multi_context skip set)
        skip = {
            "the", "a", "an", "all", "of", "in", "about", "tell", "me",
            "who", "were", "was", "are", "is", "what", "when", "where",
            "how", "did", "do", "does", "my", "our", "their", "his", "her",
            "family", "tree", "parents", "children", "ancestors", "descendants",
            "grandparents", "siblings", "married", "born", "died", "lived",
            "from", "to", "and", "with", "for", "this", "that", "these",
            "those", "have", "had", "been", "any", "some", "every", "each",
            "story", "history", "migration", "check", "verify", "validate",
            "narrative", "describe", "explain", "find", "search", "list",
            "show", "give", "get", "can", "you", "please", "could",
        }

        name_tokens = [t for t in tokens if t.lower() not in skip]

        # Build candidate queries: unigrams and bigrams
        queries: List[str] = []
        for t in name_tokens:
            if len(t) >= 2:
                queries.append(t)
        for i in range(len(name_tokens) - 1):
            queries.append(f"{name_tokens[i]} {name_tokens[i + 1]}")

        # Search per query and accumulate unique persons (by xref)
        seen_queries: set = set()
        persons_by_xref: Dict[Any, Person] = {}
        for q in queries:
            q_key = q.lower()
            if q_key in seen_queries:
                continue
            seen_queries.add(q_key)

            for p in self.tree.search_persons(q):
                if p.xref not in persons_by_xref:
                    persons_by_xref[p.xref] = p
                if len(persons_by_xref) >= 10:
                    break
            if len(persons_by_xref) >= 10:
                break

        for p in persons_by_xref.values():
            year = None
            if p.birth_date:
                match = re.search(r"\d{4}", p.birth_date)
                if match:
                    year = int(match.group())
            persons.append({
                "name": p.full_name,
                "birth_year": year,
                "birth_place": p.birth_place,
                "xref": p.xref,
            })
        return persons
