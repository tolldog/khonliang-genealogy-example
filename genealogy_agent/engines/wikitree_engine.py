"""
WikiTree research engine — wraps WikiTreeClient as a BaseEngine.

Searches WikiTree for person profiles, ancestors, and biographical data.
No auth required — just an appId for rate limiting.
"""

import logging
import re
from typing import Any, Dict, List

from khonliang.research.engine import BaseEngine, EngineResult

from genealogy_agent.engines.wikitree import WikiTreeClient

logger = logging.getLogger(__name__)


class WikiTreeEngine(BaseEngine):
    """
    Search engine backed by the WikiTree API.

    Searches for persons by name, retrieves profiles with biographical
    text, and formats results for the research pipeline.
    """

    name = "wikitree"
    max_threads = 2
    rate_limit = 1.0  # WikiTree asks for reasonable rate limiting
    timeout = 15.0

    def __init__(self, app_id: str = "khonliang-genealogy"):
        super().__init__()
        self.client = WikiTreeClient(app_id=app_id)

    async def execute(
        self, query: str, **kwargs: Any
    ) -> List[EngineResult]:
        """Search WikiTree for persons matching the query."""
        results = []

        # Extract name parts for search
        first, last = self._split_name(query)

        if not last:
            return results

        # Search by name
        search_results = await self.run_sync(
            self.client.search_person,
            first, last,
        )

        if search_results:
            for person in search_results[:5]:
                if not isinstance(person, dict):
                    continue
                profile_text = self.client.format_person(person)
                wiki_id = person.get("Name", "")
                url = f"https://www.wikitree.com/wiki/{wiki_id}" if wiki_id else ""

                results.append(EngineResult(
                    title=f"WikiTree: {profile_text.split('(')[0].strip()}",
                    content=profile_text,
                    url=url,
                    metadata={"wiki_id": wiki_id},
                ))

        # Also try direct profile lookup if query looks like a WikiTree ID
        if "-" in query and not results:
            profile = await self.run_sync(self.client.get_profile, query)
            if profile:
                results.append(EngineResult(
                    title=f"WikiTree: {self.client.format_person(profile)}",
                    content=self.client.format_person(profile),
                    url=f"https://www.wikitree.com/wiki/{query}",
                ))

        return results

    @staticmethod
    def _split_name(query: str) -> tuple:
        """Extract first and last name from a query string."""
        # Strip quotes and genealogy keywords
        clean = re.sub(r'["\']', '', query)
        clean = re.sub(
            r'\b(genealogy|parents|family|born|died|records)\b',
            '', clean, flags=re.IGNORECASE,
        ).strip()

        parts = clean.split()
        if len(parts) >= 2:
            return parts[0], parts[-1]
        elif len(parts) == 1:
            return "", parts[0]
        return "", ""
