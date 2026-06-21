#!/usr/bin/env python3
"""
Local testing API for resolving embed pages into final stream URLs.

This file is intentionally dependency-light for its raw-HTTP path.
The optional nodriver mode requires:
    pip install nodriver

It does not try to bypass CAPTCHA, DRM, paywalls, or access controls.

Run (server mode):
    python dosty.py --serve --port 8787

Run (CLI mode, raw HTTP):
    python dosty.py https://multiembed.mov/?video_id=45050&tmdb=1

Run (CLI mode, nodriver browser - handles Cloudflare):
    python dosty.py --browser https://multiembed.mov/?video_id=45050&tmdb=1

Use (API):
    http://127.0.0.1:8787/resolve?url=https%3A%2F%2Fmultiembed.mov%2F%3Fvideo_id%3D280%26tmdb%3D1
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TIMEOUT = 20
STREAMINGNOW_BASE = "https://streamingnow.mov"
DEFAULT_INPUT_URL = "https://multiembed.mov/?video_id=45050&tmdb=1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# How long to wait (seconds) for stream URLs to appear in a nodriver session
NODRIVER_STREAM_TIMEOUT = 45
# How long to wait (seconds) for Cloudflare to clear
NODRIVER_CF_TIMEOUT = 30


STREAM_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mpd|mp4)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)
PLAYER_FILE_RE = re.compile(
    r"""(?:file|src)\s*:\s*(['"])(?P<url>https?://.*?\.(?:m3u8|mpd|mp4)(?:\?.*?)?)\1""",
    re.IGNORECASE | re.DOTALL,
)
PLAY_TOKEN_RE = re.compile(r"""[?&]play=([^&"'<>]+)""", re.IGNORECASE)
LOAD_SOURCES_RE = re.compile(r"""load_sources\((['"])(?P<token>[^'"]+)\1\)""")
IFRAME_SRC_RE = re.compile(r"""<iframe\b[^>]*\bsrc=(['"])(?P<src>.*?)\1""", re.IGNORECASE | re.DOTALL)
SOURCE_LI_RE = re.compile(r"""<li\b(?P<attrs>[^>]*\bdata-id=[^>]*)>""", re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r"""([\:\w-]+)\s*=\s*(['"])(.*?)\2""", re.DOTALL)

# Matches any https:// URL inside a JS/HTML string quote — catches player embed
# URLs that are injected via JavaScript rather than a literal <iframe> tag.
EMBED_URL_RE = re.compile(
    r"""['"`](?P<url>https?://[^\s'"`,<>{}]+/e/[^\s'"`,<>{}]{4,})['"` ]""",
    re.IGNORECASE,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class SourceChoice:
    video_id: str
    server_id: str
    label: str = ""
    quality: str = ""


@dataclass
class ResolveResult:
    input_url: str
    ok: bool
    status: str
    play_url: Optional[str] = None
    play_token: Optional[str] = None
    sources: List[SourceChoice] = field(default_factory=list)
    embed_urls: List[str] = field(default_factory=list)
    stream_urls: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    used_live_http: bool = False
    used_nodriver: bool = False

    def to_jsonable(self) -> Dict[str, Any]:
        data = asdict(self)
        data["sources"] = [asdict(item) for item in self.sources]
        return data


def unique_keep_order(items) -> List[str]:
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def request_headers(referer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
    allow_redirects: bool = True,
) -> Tuple[int, str, Dict[str, str], str]:
    opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=request_headers(referer))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.geturl(), dict(resp.headers.items()), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, url, dict(exc.headers.items()), body


def http_post_form(
    url: str,
    form: Dict[str, str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    referer: Optional[str] = None,
) -> Tuple[int, str, Dict[str, str], str]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = request_headers(referer)
    headers.update(
        {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}",
        }
    )
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        return resp.status, resp.geturl(), dict(resp.headers.items()), text


def attrs_to_dict(raw_attrs: str) -> Dict[str, str]:
    return {name.lower(): html.unescape(value) for name, _, value in ATTR_RE.findall(raw_attrs)}


def extract_play_token(url_or_html: str) -> Optional[str]:
    match = PLAY_TOKEN_RE.search(url_or_html)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = LOAD_SOURCES_RE.search(url_or_html)
    if match:
        return match.group("token")
    return None


def extract_stream_urls(text: str) -> List[str]:
    urls = [html.unescape(m.group("url")) for m in PLAYER_FILE_RE.finditer(text)]
    urls.extend(html.unescape(m.group(0)) for m in STREAM_URL_RE.finditer(text))
    return unique_keep_order(urls)


def extract_iframe_urls(text: str, base_url: str) -> List[str]:
    urls = []
    for match in IFRAME_SRC_RE.finditer(text):
        src = html.unescape(match.group("src")).strip()
        if src:
            urls.append(urllib.parse.urljoin(base_url, src))
    return unique_keep_order(urls)


def extract_embed_urls(text: str) -> List[str]:
    """Extract player embed URLs from JavaScript/JSON strings.

    Catches URLs like https://dsvplay.com/e/abc123/ that appear inside
    JS string literals (single, double, or backtick quotes) rather than
    in a literal <iframe src=...> HTML attribute.
    """
    urls = []
    for m in EMBED_URL_RE.finditer(text):
        url = html.unescape(m.group("url")).rstrip("/") + "/"
        urls.append(url)
    return unique_keep_order(urls)


def clean_text(fragment: str) -> str:
    fragment = re.sub(r"<script\b.*?</script>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<style\b.*?</style>", " ", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return " ".join(html.unescape(fragment).split())


def extract_source_choices(response_html: str) -> List[SourceChoice]:
    sources = []
    matches = list(SOURCE_LI_RE.finditer(response_html))
    for index, match in enumerate(matches):
        attrs = attrs_to_dict(match.group("attrs"))
        video_id = attrs.get("data-id")
        server_id = attrs.get("data-server")
        if not video_id or not server_id:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else response_html.find("</ul>", match.end())
        if end < 0:
            end = min(len(response_html), match.end() + 500)
        fragment = response_html[match.end() : end]
        quality_match = re.search(r"""<span\b[^>]*class=(['"])[^'"]*\bquality\b[^'"]*\1[^>]*>(.*?)</span>""", fragment, re.I | re.S)
        quality = clean_text(quality_match.group(2)) if quality_match else ""
        label = clean_text(fragment)
        sources.append(SourceChoice(video_id=video_id, server_id=server_id, label=label, quality=quality))
    return sources


def choose_source(sources: List[SourceChoice], preferred_server: Optional[str] = None) -> Optional[SourceChoice]:
    if not sources:
        return None
    if preferred_server:
        for source in sources:
            if source.server_id == preferred_server:
                return source
    for wanted in ("89", "88", "90", "92", "91"):
        for source in sources:
            if source.server_id == wanted:
                return source
    return sources[0]


def resolve_live_raw(input_url: str, preferred_server: Optional[str] = None) -> ResolveResult:
    result = ResolveResult(input_url=input_url, ok=False, status="live_raw")
    result.used_live_http = True

    try:
        status, final_url, headers, body = http_get(input_url, allow_redirects=False)
        result.steps.append(f"live: initial GET returned HTTP {status}")

        location = headers.get("Location") or headers.get("location")
        if location:
            result.play_url = urllib.parse.urljoin(input_url, location)
            result.play_token = extract_play_token(result.play_url)
            result.steps.append("live: found redirect Location play URL")
        elif final_url != input_url:
            result.play_url = final_url
            result.play_token = extract_play_token(final_url)
            result.steps.append("live: final URL contains play token")
        else:
            result.play_url = input_url
            result.play_token = extract_play_token(input_url) or extract_play_token(body)

        result.stream_urls.extend(extract_stream_urls(body))
        result.embed_urls.extend(extract_iframe_urls(body, final_url))

        if not result.play_url:
            result.errors.append("Raw HTTP did not find a play URL.")
            return result

        status, final_url, headers, page = http_get(result.play_url, referer=input_url)
        result.steps.append(f"live: play page GET returned HTTP {status}")
        result.play_token = result.play_token or extract_play_token(page)
        result.stream_urls.extend(extract_stream_urls(page))
        result.embed_urls.extend(extract_iframe_urls(page, final_url))

        blocked_markers = ("turnstile", "cf_clearance", "challenge-platform", "captcha")
        if any(marker in page.lower() for marker in blocked_markers):
            result.errors.append(
                "Raw HTTP reached a browser/CAPTCHA challenge; "
                "use --browser for nodriver mode which handles Cloudflare automatically."
            )

        if result.play_token:
            response_url = urllib.parse.urljoin(result.play_url, "/response.php")
            status, _, _, response_html = http_post_form(
                response_url,
                {"token": result.play_token},
                referer=result.play_url,
            )
            result.steps.append(f"live: response.php POST returned HTTP {status}")
            result.sources = extract_source_choices(response_html)

            source = choose_source(result.sources, preferred_server)
            if source:
                playvideo_url = urllib.parse.urljoin(
                    result.play_url,
                    f"/playvideo.php?video_id={urllib.parse.quote(source.video_id)}"
                    f"&server_id={urllib.parse.quote(source.server_id)}"
                    f"&token={urllib.parse.quote(result.play_token)}&init=1",
                )
                status, playvideo_final_url, _, playvideo_html = http_get(playvideo_url, referer=result.play_url)
                result.steps.append(f"live: playvideo.php GET returned HTTP {status}")
                # If playvideo.php redirected to the embed player, record that URL directly
                if playvideo_final_url and playvideo_final_url != playvideo_url:
                    result.embed_urls.append(playvideo_final_url)
                    result.steps.append(f"live: playvideo.php redirected to {playvideo_final_url}")
                # Extract iframes using the actual landing URL as base
                result.embed_urls.extend(extract_iframe_urls(playvideo_html, playvideo_final_url))
                # Also search JS/JSON strings for player embed URLs (e.g. dsvplay.com/e/...)
                result.embed_urls.extend(extract_embed_urls(playvideo_html))
                result.stream_urls.extend(extract_stream_urls(playvideo_html))

                # If this is a vipstream response, follow its internal iframe/API page.
                for embed_url in list(result.embed_urls):
                    if "vipstream" in embed_url or "streamingnow.mov" in embed_url:
                        status, _, _, embed_html = http_get(embed_url, referer=playvideo_url)
                        result.steps.append(f"live: embed GET returned HTTP {status} for {embed_url}")
                        result.stream_urls.extend(extract_stream_urls(embed_html))

        result.embed_urls = unique_keep_order(result.embed_urls)
        result.stream_urls = unique_keep_order(result.stream_urls)
        result.ok = bool(result.stream_urls or result.embed_urls or result.sources)
        result.status = "ok" if result.ok else "blocked_or_not_found"
        return result
    except Exception as exc:
        result.status = "error"
        result.errors.append(f"{type(exc).__name__}: {exc}")
        return result


# ---------------------------------------------------------------------------
# nodriver-based resolver  (pip install nodriver)
# nodriver communicates directly via Chrome DevTools Protocol — no WebDriver
# binary, so it is resistant to Cloudflare / Turnstile anti-bot checks.
# ---------------------------------------------------------------------------

async def resolve_with_nodriver(
    input_url: str,
    *,
    stream_timeout: int = NODRIVER_STREAM_TIMEOUT,
    cf_timeout: int = NODRIVER_CF_TIMEOUT,
    headless: bool = False,
) -> Dict[str, Any]:
    """
    Use nodriver (undetected Chrome via CDP) to load an embed page and collect
    any .m3u8 / .mpd / .mp4 stream URLs seen in network requests.

    Parameters
    ----------
    input_url     : The embed URL to open.
    stream_timeout: Seconds to wait for at least one stream URL to appear.
    cf_timeout    : Extra seconds to wait if a Cloudflare challenge is detected.
    headless      : Run Chrome in headless mode. Default False so you can see
                    the browser and solve any manual challenge if needed.

    Returns
    -------
    A dict with keys: ok, input_url, stream_urls, embed_urls, observed_urls,
    steps, errors, elapsed_seconds.
    """
    try:
        import nodriver as uc
    except ImportError as exc:
        return {
            "ok": False,
            "input_url": input_url,
            "stream_urls": [],
            "embed_urls": [],
            "observed_urls": [],
            "steps": [],
            "errors": [
                f"nodriver is not installed: {exc}. "
                "Install it with:  pip install nodriver"
            ],
            "elapsed_seconds": 0.0,
        }

    stream_urls: List[str] = []
    observed_urls: List[str] = []
    steps: List[str] = []
    errors: List[str] = []
    embed_urls: List[str] = []
    started = time.time()

    try:
        browser = await uc.start(headless=headless)
        steps.append("nodriver: browser started")

        tab = await browser.get(input_url)
        steps.append(f"nodriver: navigated to {input_url}")

        # Register a network-request handler to capture stream URLs
        async def on_request(event):
            url = getattr(event, "url", None) or getattr(event, "request", {}).get("url", "")
            if url:
                observed_urls.append(url)
                if STREAM_URL_RE.search(url):
                    stream_urls.append(url)

        # nodriver uses add_handler on the tab for CDP events
        try:
            import nodriver.cdp.network as cdp_network
            tab.add_handler(cdp_network.RequestWillBeSent, on_request)
            steps.append("nodriver: network handler registered")
        except Exception as handler_exc:
            errors.append(f"nodriver: could not register network handler ({handler_exc}); falling back to page scrape only")

        # Check for / handle a Cloudflare Turnstile / challenge page
        deadline_cf = time.time() + cf_timeout
        cf_detected = False
        while time.time() < deadline_cf:
            content = await tab.get_content()
            cf_markers = ("challenge-platform", "turnstile", "cf-browser-verification", "cf_clearance")
            if any(m in content.lower() for m in cf_markers):
                if not cf_detected:
                    steps.append("nodriver: Cloudflare challenge detected — calling tab.verify_cf()")
                    cf_detected = True
                try:
                    await tab.verify_cf()
                    steps.append("nodriver: tab.verify_cf() completed")
                except Exception as cf_exc:
                    errors.append(f"nodriver: verify_cf raised {cf_exc} (may be fine if already passed)")
                break
            await tab.sleep(1)

        # Wait for stream URLs to appear
        deadline = time.time() + stream_timeout
        while time.time() < deadline and not stream_urls:
            # Also scrape the current page source for stream URLs as a fallback
            try:
                content = await tab.get_content()
                found = extract_stream_urls(content)
                stream_urls.extend(f for f in found if f not in stream_urls)
            except Exception:
                pass
            if not stream_urls:
                await tab.sleep(1)

        steps.append(f"nodriver: collected {len(stream_urls)} stream URL(s) after {round(time.time() - started, 1)}s")

        # Harvest iframe src URLs from page content
        try:
            final_content = await tab.get_content()
            final_url = tab.url if hasattr(tab, "url") else input_url
            embed_urls = extract_iframe_urls(final_content, str(final_url))
        except Exception:
            pass

        await browser.stop()
        steps.append("nodriver: browser stopped")
        # Allow subprocess transport cleanup on Windows before event loop closes
        await asyncio.sleep(0.3)

    except Exception as exc:
        errors.append(f"nodriver: fatal error — {type(exc).__name__}: {exc}")

    return {
        "ok": bool(stream_urls),
        "input_url": input_url,
        "stream_urls": unique_keep_order(stream_urls),
        "embed_urls": unique_keep_order(embed_urls),
        "observed_urls": unique_keep_order(observed_urls),
        "steps": steps,
        "errors": errors,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def resolve(
    input_url: str,
    *,
    live: bool = True,
    preferred_server: Optional[str] = None,
) -> ResolveResult:
    if live:
        return resolve_live_raw(input_url, preferred_server)
    return ResolveResult(input_url=input_url, ok=False, status="skipped")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "StreamResolverTesting/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/health":
                self.write_json({"ok": True, "service": "dosty.py"})
                return

            if parsed.path == "/resolve":
                input_url = (params.get("url") or [""])[0]
                if not input_url:
                    self.write_json({"ok": False, "error": "Missing url query parameter."}, status=400)
                    return
                live = (params.get("live") or ["1"])[0] not in ("0", "false", "False")
                server_id = (params.get("server") or [None])[0]
                use_browser = (params.get("browser") or ["0"])[0] not in ("0", "false", "False")

                if use_browser:
                    payload = _run_async(resolve_with_nodriver(input_url))
                    self.write_json(payload)
                    return

                result = resolve(input_url, live=live, preferred_server=server_id)
                self.write_json(result.to_jsonable())
                return

            self.write_json(
                {
                    "ok": False,
                    "error": "Not found.",
                    "endpoints": [
                        "/health",
                        "/resolve?url=<embed-url>&live=1&server=89",
                        "/resolve?url=<embed-url>&browser=1  (nodriver / Cloudflare-aware)",
                    ],
                },
                status=404,
            )
        except Exception as exc:
            self.write_json(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
                status=500,
            )

    def log_message(self, fmt: str, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def write_json(self, payload: Dict[str, Any], status: int = 200):
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def _run_async(coro):
    """
    Run an async coroutine to completion with proper Windows subprocess cleanup.
    On Windows, asyncio.run() can leave subprocess pipe transports unclosed when
    the event loop shuts down, causing noisy but harmless tracebacks. This helper
    manually drains pending callbacks before closing the loop.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(asyncio.sleep(0))
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


def serve(host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Serving on http://{host}:{port}")
    print("Endpoints:")
    print(f"  http://{host}:{port}/resolve?url=" + urllib.parse.quote("https://multiembed.mov/?video_id=280&tmdb=1", safe=""))
    print(f"  http://{host}:{port}/resolve?url=<url>&browser=1   # nodriver / Cloudflare-aware")
    httpd.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Testing API/extractor for embed stream URLs.")
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_INPUT_URL,
        help=f"Embed URL to resolve (CLI mode). Default: {DEFAULT_INPUT_URL}",
    )
    parser.add_argument("--serve", action="store_true", help="Start the local JSON API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--no-live", action="store_true", help="Skip live raw HTTP (returns empty result).")
    parser.add_argument("--server-id", default=None, help="Preferred server id, e.g. 89.")
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "Use nodriver (undetected Chrome via CDP) instead of raw HTTP. "
            "Handles Cloudflare Turnstile / anti-bot challenges automatically. "
            "Requires: pip install nodriver"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run nodriver in headless mode (only applies when --browser is used).",
    )
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.host, args.port)
        return 0

    if args.browser:
        payload = _run_async(
            resolve_with_nodriver(
                args.url,
                headless=args.headless,
            )
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok") else 2

    # Default: raw HTTP
    result = resolve(
        args.url,
        live=not args.no_live,
        preferred_server=args.server_id,
    )
    print(json.dumps(result.to_jsonable(), indent=2, ensure_ascii=False))
    return 0 if result.ok else 2


if __name__ == "__main__":
    import asyncio
    raise SystemExit(main())
