"""A browser session (headless Chromium through Playwright). It needs `pip install "datahandles[browser]"`.

Each handle is one session: its own browser context and page. A session lives only in this process
(persistence `ephemeral`). One thread with an asyncio loop owns the browser. Every call goes to
that loop as a task, so a cancel or a time limit cancels the task that waits for the page.

A session can reach every http and https address that this process can reach. Other URL schemes
(file, data, javascript) are refused.
"""
import asyncio
import atexit
import json
import threading
from concurrent.futures import CancelledError
from urllib.parse import urlsplit

from ...core import DEFAULT_MAX_OUTPUT_BYTES, HandleError, capability, new_id
from ...runtime.adapter import Derived, ResourceAdapter, ResourceRef, Value

_URL = {"type": "string", "description": "An http or https URL."}
_SELECTOR = {"type": "string", "description": "A CSS or text selector, for example `#submit` or `text=Next`."}
# Bytes kept for the JSON keys around a list or a text in a truncated output.
_ENVELOPE = 256


def _caps(timeout_ms: int | None) -> list[dict]:
    act = {"write": True, "external": True, "idempotent": False}
    caps = [
        capability("browser.goto", "Open a URL in this session. Return the final URL, the title, and the HTTP status.", {"url": _URL}, required=["url"], **act),
        capability("browser.click", "Click the first element that matches. Return the URL and the title after the click.", {"selector": _SELECTOR}, required=["selector"], **act),
        capability("browser.fill", "Type a value into the first input that matches.", {"selector": _SELECTOR, "value": {"type": "string"}}, required=["selector", "value"], **act),
        capability("browser.text", "Return the visible text of the page or of one element. The output stops at the size limit and says so.", {"selector": _SELECTOR}),
        capability("browser.links", "List the links of the page. The output stops at the size limit and says so."),
    ]
    if timeout_ms:  # the session was opened with a time limit; it applies to each action
        for cap in caps[:3]:
            cap["limits"] = {**cap.get("limits", {}), "timeout_ms": timeout_ms}
    return caps


def _check_url(url: str) -> str:
    if urlsplit(url).scheme not in ("http", "https"):
        raise HandleError("INVALID_INPUT", f"only http and https URLs: {url!r}")
    return url


def _fit(text: str, budget: int) -> str:
    while len(json.dumps(text).encode()) > budget:
        text = text[: len(text) * 3 // 4]
    return text


class Adapter(ResourceAdapter):
    name = "browser"
    persistence = "ephemeral"

    def __init__(self, state_dir=None):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="datahandles-browser", daemon=True).start()
        self._browser, self._playwright, self._pages = None, None, {}
        atexit.register(self.close)

    def _run(self, coro, call=None):
        """Run a coroutine on the browser loop. A cancel of `call` cancels the task."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if call is not None:
            call.on_cancel(future.cancel)
        try:
            return future.result()
        except CancelledError:
            call.check()
            raise

    async def _start(self):
        if self._browser is None:
            from playwright.async_api import async_playwright  # an optional dependency
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()

    def close(self) -> None:
        async def stop():
            if self._browser is not None:
                await self._browser.close()
                await self._playwright.stop()
                self._browser = None
        if self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(stop(), self._loop).result(10)
            except Exception:  # the process ends; a browser that does not close ends with it
                pass

    def lock_key(self, ref: ResourceRef):
        return ref.locator["session"]  # sessions do not share a page

    def open(self, source: dict) -> ResourceRef:
        timeout_ms = source.get("timeout_ms")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or timeout_ms < 1):
            raise HandleError("INVALID_INPUT", "timeout_ms must be a positive integer")

        async def new_page():
            await self._start()
            context = await self._browser.new_context()
            return await context.new_page()
        session = new_id("ses")
        self._pages[session] = self._run(new_page())
        ref = ResourceRef("browser_session", {"session": session, "timeout_ms": timeout_ms}, "0")
        if source.get("url"):
            self._run(self._pages[session].goto(_check_url(source["url"])))
        return ref

    def _page(self, ref: ResourceRef):
        page = self._pages.get(ref.locator["session"])
        if page is None:
            raise HandleError("HANDLE_RELEASED", "the browser session ended")
        return page

    def describe(self, ref: ResourceRef) -> dict:
        return {"description": "A browser session with one page. Actions change the page; reads return bounded text.",
                "schema": {"timeout_ms": ref.locator["timeout_ms"]}}

    def discover(self, ref: ResourceRef) -> list[dict]:
        return _caps(ref.locator["timeout_ms"])

    def observe(self, ref: ResourceRef) -> dict:
        # A live page can change with no action of this session, so it has no stable version. A new
        # version on each observe means a graph never reuses a result that came from the page.
        page = self._page(ref)
        return {"version": new_id("v"), "url": page.url}

    def release(self, ref: ResourceRef, kind: str) -> None:
        page = self._pages.pop(ref.locator["session"], None)
        if page is not None:
            self._run(page.context.close())

    def invoke(self, ref: ResourceRef, capability: str, input: dict, *, eager: bool, call) -> Value | Derived:
        page = self._page(ref)

        async def act():
            if capability == "browser.goto":
                response = await page.goto(_check_url(input["url"]))
                return {"url": page.url, "title": await page.title(), "status": response and response.status}
            if capability == "browser.click":
                await page.click(input["selector"])
                await page.wait_for_load_state()
                return {"url": page.url, "title": await page.title()}
            if capability == "browser.fill":
                await page.fill(input["selector"], input["value"])
                return {"filled": input["selector"]}
            budget = DEFAULT_MAX_OUTPUT_BYTES - _ENVELOPE
            if capability == "browser.text":
                text = await page.inner_text(input.get("selector") or "body")
                cut = _fit(text, budget)
                return {"text": cut, "truncated": len(cut) < len(text), "url": page.url}
            if capability == "browser.links":
                links = await page.eval_on_selector_all(
                    "a[href]", "els => els.map(a => ({text: a.innerText.trim().slice(0, 200), href: a.href}))")
                kept = []
                for link in links:
                    budget -= len(json.dumps(link).encode()) + 2
                    if budget < 0:
                        break
                    kept.append(link)
                return {"links": kept, "total": len(links), "truncated": len(kept) < len(links)}
            raise HandleError("CAPABILITY_NOT_FOUND", capability)

        try:
            out = self._run(act(), call)
        except HandleError:
            raise
        except Exception as e:  # a Playwright error: no element, a navigation that failed
            raise HandleError("INVALID_INPUT" if "Timeout" not in type(e).__name__ else "ADAPTER_ERROR", f"{type(e).__name__}: {e}".splitlines()[0]) from e
        return Value(out, page.url)
