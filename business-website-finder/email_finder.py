import asyncio
import logging
import re
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_OBFUSCATED_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*[\[\(]at[\]\)]\s*[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class EmailFinderBot:
    def __init__(self, config: dict, session: aiohttp.ClientSession) -> None:
        rl = config.get("rate_limits", {})
        self.yelp_delay: float = rl.get("yelp_request_delay_sec", 3.0)
        self.fb_delay: float = rl.get("facebook_request_delay_sec", 4.0)
        loc = config.get("location", {})
        self.city: str = loc.get("city", "")
        self.state: str = loc.get("state", "")
        self.session = session

    async def find_email(self, name: str, address: str, category: str, maps_url: str) -> tuple[str, str]:
        email = await self._from_maps_description(maps_url)
        if email:
            return email, "maps"

        await asyncio.sleep(self.yelp_delay)
        email = await self._from_yelp(name)
        if email:
            return email, "yelp"

        await asyncio.sleep(self.fb_delay)
        email = await self._from_facebook(name)
        if email:
            return email, "facebook"

        return "", ""

    async def _fetch_html(self, url: str) -> str:
        try:
            async with self.session.get(
                url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
        except Exception as e:
            logger.debug(f"Fetch failed for {url}: {e}")
        return ""

    async def _from_maps_description(self, maps_url: str) -> str:
        html = await self._fetch_html(maps_url)
        return self._extract_email_from_html(html)

    async def _from_yelp(self, name: str) -> str:
        search_url = (
            f"https://www.yelp.com/search?"
            f"find_desc={quote_plus(name)}&find_loc={quote_plus(self.city + ' ' + self.state)}"
        )
        html = await self._fetch_html(search_url)
        if not html or "accessDenied" in html or "captcha" in html.lower():
            logger.debug("Yelp blocked or CAPTCHA detected")
            return ""
        soup = BeautifulSoup(html, "lxml")
        link = soup.find("a", href=re.compile(r"/biz/"))
        if link:
            biz_url = "https://www.yelp.com" + link["href"]
            biz_html = await self._fetch_html(biz_url)
            return self._extract_email_from_html(biz_html)
        return ""

    async def _from_facebook(self, name: str) -> str:
        url = f"https://www.facebook.com/search/pages/?q={quote_plus(name + ' ' + self.city)}"
        html = await self._fetch_html(url)
        if not html or "login" in html.lower()[:2000]:
            logger.debug("Facebook login wall encountered")
            return ""
        soup = BeautifulSoup(html, "lxml")
        link = soup.find("a", href=re.compile(r"facebook\.com/[^/?]+$"))
        if link:
            about_url = link["href"].rstrip("/") + "/about"
            about_html = await self._fetch_html(about_url)
            return self._extract_email_from_html(about_html)
        return self._extract_email_from_html(html)

    def _extract_email_from_html(self, html: str) -> str:
        if not html:
            return ""
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ")

        obf_match = _OBFUSCATED_RE.search(text)
        if obf_match:
            raw = obf_match.group(0)
            normalized = re.sub(r"\s*[\[\(]at[\]\)]\s*", "@", raw)
            return normalized.strip()

        for tag in soup.find_all("a", href=re.compile(r"^mailto:")):
            addr = tag["href"].replace("mailto:", "").split("?")[0].strip()
            if addr and "@" in addr:
                return addr

        match = _EMAIL_RE.search(text)
        if match:
            return match.group(0)
        return ""
