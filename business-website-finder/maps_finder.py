import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


@dataclass
class RawBusiness:
    name: str
    address: str
    phone: str
    category: str
    has_website: bool
    maps_url: str


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class MapsFinderBot:
    def __init__(self, config: dict) -> None:
        loc = config.get("location", {})
        self.city: str = loc.get("city", "")
        self.state: str = loc.get("state", "")
        search = config.get("search", {})
        self.business_types: list[str] = search.get("business_types", [])
        self.results_per_type: int = search.get("results_per_type", 60)
        self.headless: bool = search.get("headless", True)
        rl = config.get("rate_limits", {})
        self.scroll_delay: float = rl.get("maps_scroll_delay_sec", 2.5)
        self.page_load_delay: float = rl.get("maps_page_load_sec", 4.0)

    def _jitter(self, base: float) -> float:
        return base + random.uniform(-0.5, 0.5)

    async def run(self) -> list[RawBusiness]:
        from playwright.async_api import async_playwright

        results: list[RawBusiness] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 900},
            )
            for btype in self.business_types:
                logger.info(f"Searching: {btype} in {self.city}, {self.state}")
                found = await self._search_type(context, btype)
                no_site = [b for b in found if not b.has_website]
                logger.info(f"  {len(found)} results, {len(no_site)} without websites")
                results.extend(no_site)
            await browser.close()
        return results

    async def _search_type(self, context, business_type: str) -> list[RawBusiness]:
        page = await context.new_page()
        query = quote_plus(f"{business_type} {self.city} {self.state}")
        url = f"https://www.google.com/maps/search/{query}"
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(self._jitter(3.0))
            listing_urls = await self._scroll_results_panel(page)
            businesses = []
            for listing_url in listing_urls:
                biz = await self._scrape_listing(context, listing_url)
                if biz:
                    businesses.append(biz)
                await asyncio.sleep(self._jitter(self.page_load_delay))
            return businesses
        except Exception as e:
            logger.error(f"Error searching {business_type}: {e}")
            return []
        finally:
            await page.close()

    async def _scroll_results_panel(self, page) -> list[str]:
        hrefs: list[str] = []
        feed_selector = 'div[role="feed"]'
        try:
            await page.wait_for_selector(feed_selector, timeout=10000)
        except Exception:
            logger.warning("No results feed found on page")
            return hrefs

        max_scrolls = self.results_per_type // 5 + 10
        for _ in range(max_scrolls):
            cards = await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
            if len(cards) >= self.results_per_type:
                break
            feed = await page.query_selector(feed_selector)
            if feed:
                await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
            await asyncio.sleep(self._jitter(self.scroll_delay))
            end_text = await page.query_selector("text=You've reached the end of the list")
            if end_text:
                break

        cards = await page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
        seen: set[str] = set()
        for card in cards[: self.results_per_type]:
            href = await card.get_attribute("href")
            if href and href not in seen:
                seen.add(href)
                hrefs.append(href)
        return hrefs

    async def _scrape_listing(self, context, listing_url: str) -> Optional[RawBusiness]:
        page = await context.new_page()
        try:
            await page.goto(listing_url, wait_until="domcontentloaded")
            await asyncio.sleep(self._jitter(self.page_load_delay))

            name = await self._extract_text(page, 'h1[class*="fontHeadlineLarge"]') or ""
            if not name:
                name = await self._extract_text(page, "h1") or "Unknown"

            has_website = await self._detect_website(page)
            category = await self._extract_category(page)
            address = await self._extract_address(page)
            phone = await self._extract_phone(page)

            return RawBusiness(
                name=name.strip(),
                address=address,
                phone=phone,
                category=category,
                has_website=has_website,
                maps_url=page.url,
            )
        except Exception as e:
            logger.warning(f"Could not scrape {listing_url}: {e}")
            return None
        finally:
            await page.close()

    async def _detect_website(self, page) -> bool:
        for selector in (
            'a[data-item-id="authority"]',
            '[data-tooltip="Open website"]',
            'a[aria-label*="website"]',
        ):
            el = await page.query_selector(selector)
            if el:
                return True
        return False

    async def _extract_category(self, page) -> str:
        el = await page.query_selector('button[jsaction*="category"]')
        if el:
            return (await el.inner_text()).strip()
        el = await page.query_selector('[class*="fontBodyMedium"] button')
        if el:
            return (await el.inner_text()).strip()
        return ""

    async def _extract_address(self, page) -> str:
        el = await page.query_selector('[data-item-id="address"]')
        if el:
            return (await el.inner_text()).strip()
        return ""

    async def _extract_phone(self, page) -> str:
        el = await page.query_selector('[data-item-id^="phone:tel:"]')
        if el:
            return (await el.inner_text()).strip()
        return ""

    async def _extract_text(self, page, selector: str) -> Optional[str]:
        el = await page.query_selector(selector)
        if el:
            return await el.inner_text()
        return None
