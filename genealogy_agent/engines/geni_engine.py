"""
Geni research engine — wraps GeniClient as a BaseEngine.

Searches Geni's world family tree for profiles and relationships.
Requires OAuth2 credentials (GENI_API_KEY, GENI_API_SECRET env vars).
"""

import logging
import os
import re
from typing import Any, Dict, List

from khonliang.research.engine import BaseEngine, EngineResult

from genealogy_agent.engines.geni import GeniClient

logger = logging.getLogger(__name__)


class GeniEngine(BaseEngine):
    """
    Search engine backed by the Geni.com API.

    Authenticates on first use, searches for profiles, and formats
    results for the research pipeline. Rate limited for unapproved apps.
    """

    name = "geni"
    max_threads = 1  # Geni has strict rate limits
    rate_limit = 2.0  # Be gentle with unapproved app
    timeout = 15.0

    def __init__(
        self,
        app_id: str = "",
        api_key: str = "",
        api_secret: str = "",
    ):
        super().__init__()
        self.client = GeniClient(
            app_id=app_id,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._authenticated = False

    def _ensure_auth(self) -> bool:
        """Authenticate if not already done."""
        if self._authenticated:
            return True
        if self.client.authenticate():
            self._authenticated = True
            return True
        logger.warning("Geni authentication failed — engine disabled")
        return False

    async def execute(
        self, query: str, **kwargs: Any
    ) -> List[EngineResult]:
        """Search Geni for persons matching the query."""
        if not await self.run_sync(self._ensure_auth):
            return []

        results = []

        # Extract name for search
        clean_name = self._clean_query(query)
        if not clean_name:
            return results

        # Search Geni
        search_results = await self.run_sync(
            self.client.search, names=clean_name
        )

        if search_results:
            for profile in search_results[:5]:
                if not isinstance(profile, dict):
                    continue
                name = profile.get("name", "Unknown")
                profile_url = profile.get("profile_url", "")
                guid = profile.get("guid", "")

                # Extract dates
                birth = profile.get("birth", {})
                death = profile.get("death", {})
                bd = ""
                dd = ""
                if isinstance(birth, dict):
                    bd = birth.get("date", {}).get("formatted_date", "")
                if isinstance(death, dict):
                    dd = death.get("date", {}).get("formatted_date", "")

                content = self.client.format_profile(profile)

                results.append(EngineResult(
                    title=f"Geni: {name}",
                    content=content,
                    url=profile_url,
                    metadata={
                        "geni_guid": guid,
                        "birth_date": bd,
                        "death_date": dd,
                    },
                ))

        return results

    @staticmethod
    def _clean_query(query: str) -> str:
        """Extract a searchable name from a query."""
        clean = re.sub(r'["\']', '', query)
        clean = re.sub(
            r'\b(genealogy|parents|family|born|died|records|before|after)\b',
            '', clean, flags=re.IGNORECASE,
        )
        # Remove years
        clean = re.sub(r'\b\d{4}\b', '', clean)
        return clean.strip()
