import asyncio
import inspect
import sys

sys.path.insert(0, ".")
from dosty import (
    ResolveResult,
    SourceChoice,
    _run_async,
    extract_iframe_urls,
    extract_play_token,
    extract_source_choices,
    extract_stream_urls,
    resolve_with_nodriver,
    unique_keep_order,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
errors = []


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}" + (f": {detail}" if detail else ""))
        errors.append(name)


print("\n=== Test 1: extract_stream_urls ===")
html = '<source src="https://cdn.example.com/video.m3u8?token=abc">'
urls = extract_stream_urls(html)
check("finds .m3u8 URL", "https://cdn.example.com/video.m3u8?token=abc" in urls)
check("deduplicates", len(unique_keep_order(urls * 3)) == len(urls))

print("\n=== Test 2: extract_iframe_urls ===")
html2 = '<iframe src="https://dsvplay.com/e/abc123/"></iframe>'
iframes = extract_iframe_urls(html2, "https://example.com")
check("finds iframe src", "https://dsvplay.com/e/abc123/" in iframes)

print("\n=== Test 3: extract_play_token ===")
token_url = "https://streamingnow.mov/?play=MyToken123"
token = extract_play_token(token_url)
check("extracts play= token", token == "MyToken123")
no_token = extract_play_token("https://example.com/nope")
check("returns None when absent", no_token is None)

print("\n=== Test 4: extract_source_choices ===")
sample_html = """
<ul>
  <li data-id="VID001" data-server="89">
    <span class="quality">1080p</span> Server 1
  </li>
  <li data-id="VID002" data-server="21">
    <span class="quality">720p</span> Server 2
  </li>
</ul>
"""
sources = extract_source_choices(sample_html)
check("found 2 sources", len(sources) == 2, f"got {len(sources)}")
check("first source server_id", sources[0].server_id == "89", f"got {sources[0].server_id}")
check("second source quality", sources[1].quality == "720p", f"got {sources[1].quality}")

print("\n=== Test 5: ResolveResult dataclass ===")
r = ResolveResult(input_url="http://x.com", ok=True, status="ok")
check("default used_nodriver=False", r.used_nodriver is False)
check("default used_live_http=False", r.used_live_http is False)
d = r.to_jsonable()
check("to_jsonable has used_nodriver", "used_nodriver" in d)
check("to_jsonable has stream_urls", "stream_urls" in d)
check("to_jsonable sources is list", isinstance(d["sources"], list))

print("\n=== Test 6: resolve_with_nodriver is async ===")
check("is coroutinefunction", inspect.iscoroutinefunction(resolve_with_nodriver))


async def test_nodriver_graceful_error():
    """Test that resolve_with_nodriver returns a proper error dict if it fails."""
    # We won't actually launch Chrome; just verify the return shape when nodriver
    # raises (e.g. Chrome not found). We mock a quick failure path by calling with
    # an invalid URL scheme to let it error out quickly.
    # In real usage Chrome launches fine.
    result = await resolve_with_nodriver("about:blank", stream_timeout=3, cf_timeout=2)
    return result


print("\n=== Test 7: nodriver return shape ===")
result = _run_async(test_nodriver_graceful_error())
check("returns dict", isinstance(result, dict))
check("has 'ok' key", "ok" in result)
check("has 'stream_urls' key", "stream_urls" in result)
check("has 'steps' key", "steps" in result)
check("has 'errors' key", "errors" in result)
check("has 'elapsed_seconds' key", "elapsed_seconds" in result)
check("stream_urls is list", isinstance(result["stream_urls"], list))

print("\n=== Test 8: unique_keep_order ===")
deduped = unique_keep_order(["a", "b", "a", "c", "b"])
check("deduplication", deduped == ["a", "b", "c"])
check("empty stays empty", unique_keep_order([]) == [])
check("filters empty strings", unique_keep_order(["", "a", ""]) == ["a"])

print()
if errors:
    print(f"\033[91m{len(errors)} test(s) FAILED: {errors}\033[0m")
    sys.exit(1)
else:
    print(f"\033[92mAll tests passed!\033[0m")
    sys.exit(0)
