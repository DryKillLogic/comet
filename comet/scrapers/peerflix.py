import re

from comet.utils.general import (
    size_to_bytes,
    log_scraper_error,
    fetch_with_proxy_fallback,
)

async def get_peerflix(manager, url: str):
    torrents = []
    try:
        results = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in results["streams"]:
            title_full = torrent["title"]
            title = title_full.split("\n")[0]

            seeders = None
            size = 0
            tracker = None

            if "👤" in title_full:
                matchSeeders = re.search(r"👤\s*(\d+)", title_full)
                if matchSeeders:
                    seeders = int(matchSeeders.group(1))

            if "💾" in title_full:
                matchSize = re.search(r"💾\s*([\d.]+\s*[KMGT]B)", title_full)
                if matchSize:
                    size = size_to_bytes(matchSize.group(1))

            if "🌐" in title_full:
                matchTracker = re.search(r"🌐\s*([^\n\r]+)", title_full)
                if matchTracker:
                    tracker = matchTracker.group(1).strip()

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": seeders,
                    "size": size,
                    "tracker": f"Peerflix|{tracker}",
                    "sources": torrent.get("sources", []),
                }
            )
    except Exception as e:
        log_scraper_error("Peerflix", url, manager.media_id, e)

    await manager.filter_manager(torrents)
