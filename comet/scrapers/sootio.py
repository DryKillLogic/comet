import re
import json

from comet.core.logger import log_scraper_error
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from urllib.parse import quote


DATA_PATTERN = re.compile(
    r"^(?P<title>.*?)(?:\n💾\s*.*?\|\s*(?P<tracker>[^|\n]+))?(?:\s*\|.*)?$",
    re.DOTALL,
)

class SootioScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            config = quote(json.dumps({"DebridServices": [{"provider": settings.SOOTIO_PROVIDER, "apiKey": settings.SOOTIO_PROVIDER_KEY}]}))

            async with self.session.get(
                f"{self.url}/{config}/stream/{request.media_type}/{request.media_only_id}.json",
            ) as response:
                data = await response.json()

            for torrent in data["streams"]:
                raw_title = torrent["title"]
                match = DATA_PATTERN.match(raw_title.strip())
                clean_title = (
                    match.group("title").strip()
                    if match and match.group("title")
                    else raw_title.split("\n")[0].strip()
                )
                tracker = (
                    match.group("tracker").strip()
                    if match and match.group("tracker")
                    else None
                )
                torrents.append(
                    {
                        "title": clean_title,
                        "infoHash": torrent["_hash"],
                        "fileIndex": None,
                        "seeders": None,
                        "size": torrent["_size"],
                        "tracker": f"Sootio|{tracker}" if tracker else "Sootio",
                        "sources": None,
                    }
                )
        except Exception as e:
            log_scraper_error(
                "Sootio", settings.SOOTIO_PROVIDER, request.media_only_id, e
            )

        return torrents
