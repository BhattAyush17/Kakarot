import asyncio
from urllib.parse import urlparse

import trafilatura

from kakarot_daemon.scrubber import sanitize_text


async def fetch_web_content(url: str) -> str:
    parsed = urlparse(url if isinstance(url, str) else "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Error: Invalid URL. Only http(s) URLs with a valid host are supported."

    def _fetch_and_extract():
        try:
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None
            return trafilatura.extract(downloaded, include_links=True, include_formatting=True)
        except Exception:
            # Network errors, 403/404s, bot walls and parser failures must never
            # escape the worker thread.
            return None

    try:
        text = await asyncio.wait_for(asyncio.to_thread(_fetch_and_extract), timeout=30.0)
    except (asyncio.TimeoutError, TimeoutError):
        return "Error: Web extraction timed out after 30 seconds."
    except Exception as e:
        return f"Error: Web extraction failed: {e}"

    if not text:
        return "Error: Could not extract readable text from this URL. It may require a full JavaScript browser or be bot-protected."

    sanitized = sanitize_text(text)
    return sanitized[:4000]
