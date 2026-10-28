"""api_contract: PHP calls read as path patterns, matched against a route list."""
"""Run through `make test`, which installs this checkout as the package."""
import subprocess
import tempfile
from pathlib import Path

from repo_checks.api_contract import (
    HOLE, Call, check, code_only, find_calls, main, matches, read_config, read_routes,
)

FNS = {"amf_get": "GET", "amf_post": "POST", "amf_delete": "DELETE", "curl_init": "ANY"}
ROUTES = read_routes(
    "GET /events\n"
    "GET /events/{event_id}\n"
    "POST /orders/{order_id}/lines/{product_id}/pickup\n"
    "DELETE /orders/{order_id}/lines/{product_id}/pickup\n"
    "POST /subs/{sub_id}/shipments\n"
    "POST /feedback/app\n"
)


def paths(src: str) -> list[tuple[str, str | None]]:
    """(method, path with {} for each computed part) for every call in src."""
    return [
        (c.method, None if c.path is None else c.path.replace(HOLE, "{}"))
        for c in find_calls(src, "x.php", FNS)
    ]


def problems(src: str) -> list[str]:
    return [p.message for p in check(find_calls(src, "x.php", FNS), ROUTES)]


# --- only code is read ----------------------------------------------------------
src = "<p>Don't amf_get('/nope')</p>\n<?php // amf_get('/nope')\n# amf_get('/nope')\n/* amf_get('/nope') */\n"
assert code_only(src).strip() == "" and code_only(src).count("\n") == src.count("\n")
assert paths("<p>it's</p><?php amf_get('/events'); ?><p>don't</p><?= amf_get('/events') ?>") == [
    ("GET", "/events"), ("GET", "/events")]
assert paths("<?php $js = <<<'JS'\namf_get('/nope');\nJS;\namf_get('/events');") == [("GET", "/events")]
assert paths("<?php #[Attr]\namf_get('/events');") == [("GET", "/events")]  # #[ is not a comment
assert paths("<?php function amf_get(string $path) {}") == []  # the definition
assert paths("<?php $x->amf_get('/a'); X::amf_get('/a'); my_amf_get('/a');") == []
print("comments, HTML, heredocs and definitions skipped")

# --- arguments → path patterns --------------------------------------------------
assert paths("<?php amf_get('/events/' . $id);") == [("GET", "/events/{}")]
assert paths("<?php amf_get('/events/' . (int) $id, [], []);") == [("GET", "/events/{}")]
assert paths('<?php amf_get("/events/{$id}");') == [("GET", "/events/{}")]
assert paths('<?php amf_get("/events/$id");') == [("GET", "/events/{}")]
assert paths("<?php amf_get('/events/' . rawurlencode($slug));") == [("GET", "/events/{}")]
assert paths("<?php amf_get(\n    '/events'\n);") == [("GET", "/events")]
assert paths("<?php amf_get('/events?store_id=' . $s);") == [("GET", "/events")]
assert paths('<?php amf_post("/subs/{$id}/shipments{$qs}", []);') == [("POST", "/subs/{}/shipments")]
# a variable assigned once in the file is read through; one assigned twice is not
src = "<?php $line = '/orders/' . $o . '/lines/' . $p;\namf_post($line . '/pickup');"
assert paths(src) == [("POST", "/orders/{}/lines/{}/pickup")]
src = "<?php $line = '/a';\n$line = '/orders/1/lines/2';\namf_post($line . '/pickup');"
assert paths(src) == [("POST", "{}/pickup")]
src = "<?php $p = '/events';\n$p .= '/x';\namf_get($p);"
assert paths(src) == [("GET", None)]
assert paths("<?php amf_get($path);") == [("GET", None)]
# a computed part with more path after it is a segment, not a suffix to drop
assert paths("<?php amf_get('/users/u' . $id . '/gone');") == [("GET", "/users/u{}/gone")]
assert paths('<?php amf_get("/events/${id}/tickets");') == [("GET", "/events/{}/tickets")]
# read-through stays inside its function: a parameter is the caller's value
src = "<?php function a() { $p = '/events/1'; }\nfunction b($p) { amf_get($p . '/x'); }"
assert paths(src) == [("GET", "{}/x")]
src = "<?php function a() { $p = '/events/1'; }\nfunction b() { amf_get($p . '/x'); }"
assert paths(src) == [("GET", "{}/x")]  # a's $p is not b's
src = "<?php function b() {\n  $p = '/orders/' . $o;\n  $f = function () use ($p) { amf_get($p); };\n  amf_get($p . '/x');\n}"
assert paths(src) == [("GET", None), ("GET", "/orders/{}/x")]
for bind in ("foreach ($ps as $p) {}", "foreach ($ps as $k => $p) {}",
             "try {} catch (E $p) {}", "f(&$p);", "$p .= '/x';"):
    src = f"<?php $p = '/events';\n{bind}\namf_get($p);"
    assert paths(src) == [("GET", None)], bind
# the name inside a string is not a call
assert paths('<?php throw new E("amf_get(): bad path"); $js = "amf_get(1)";') == []
# a fully qualified call is still the function
assert paths("<?php \\amf_get('/events');") == [("GET", "/events")]
print("arguments read as paths")

# --- raw URLs -------------------------------------------------------------------
assert paths("<?php curl_init(rtrim($cfg['url'], '/') . '/feedback/app');") == [("ANY", "/feedback/app")]
assert paths("<?php curl_init('https://challenges.cloudflare.com/turnstile/v0/siteverify');") == []
assert paths("<?php curl_init($url);") == []  # the client's own helper
# a computed part after the base: matched on the end
assert paths("<?php curl_init($base . $prefix . '/pickup');") == [("ANY", "{}/pickup")]
assert problems("<?php curl_init($base . $prefix . '/pickup');") == []
src = "<?php function f() {\n  $url = rtrim($cfg['url'], '/') . '/feedback/app';\n  curl_init($url);\n}"
assert paths(src) == [("ANY", "/feedback/app")]
print("raw URLs: the base is dropped, other hosts ignored")

# --- matching -------------------------------------------------------------------
assert matches("/events/" + HOLE, "/events/{event_id}")
assert matches("/events/" + HOLE, "/events/upcoming")  # a computed part may be anything
assert matches("/events/12", "/events/{event_id}")
assert not matches("/events", "/events/{event_id}")
assert not matches("/event", "/events")
assert matches(HOLE + "/pickup", "/orders/{order_id}/lines/{product_id}/pickup")
assert not matches(HOLE + "/pickup", "/pickup")  # the unknown start is at least one segment
assert not matches(HOLE + "/pickups", "/orders/{order_id}/lines/{product_id}/pickup")
print("matching OK")

# --- problems -------------------------------------------------------------------
assert problems("<?php amf_get('/events/' . $id); amf_post('/feedback/app');") == []
assert problems("<?php amf_get('/event/' . $id);") == ["GET /event/{…}: the API has no such route."]
assert problems("<?php amf_delete('/events');") == ["DELETE /events: the API has no such route."]
assert problems("<?php curl_init($base . '/feedback/app');") == []  # any method
p = problems("<?php amf_get($path);")
assert len(p) == 1 and "computed path" in p[0]
assert problems("<?php amf_get($path);  // api-contract: skip the client's own pass-through") == []
# a request made another way, declared on its line
assert problems("<?php $j = file_get_contents($api . '/events'); // api-contract: GET /events") == []
assert problems("<?php // api-contract: GET /events/{id}/nope") == [
    "GET /events/{…}/nope: the API has no such route."]
c = find_calls("<?php\n\namf_get('/nope');", "a/b.php", FNS)
assert c == [Call("a/b.php", 3, "GET", "/nope")]
# line numbers count "\n" only, so a form feed in a string does not shift the skip comment
src = "<?php $s = 'a\x0cb c';\namf_get($path); // api-contract: skip pass-through"
assert problems(src) == []
print("problems OK")

# --- config ---------------------------------------------------------------------
assert read_config("# client\namf_get GET\ncurl_init  ANY  # raw\n\n") == {"amf_get": "GET", "curl_init": "ANY"}
for bad in ("amf_get", "amf_get FETCH", "amf-get GET", "a GET b", "# nothing\n\n"):
    try:
        read_config(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted {bad!r}")
assert read_routes("# routes\nGET /a\n\npost /b/{id}\n") == [("GET", "/a"), ("POST", "/b/{id}")]
for bad in ("GET", "GET a", "GET /a extra"):
    try:
        read_routes(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted {bad!r}")
print("config and routes OK")

# --- end to end on a temp git repo ----------------------------------------------
with tempfile.TemporaryDirectory() as d:
    r = Path(d)
    subprocess.run(["git", "init", "-q", d], check=True)
    (r / ".api-contract").write_text("amf_get GET\n")
    (r / "routes.txt").write_text("GET /events\nGET /events/{event_id}\n")
    (r / "page.php").write_text("<?php amf_get('/events/' . $id);\n")
    (r / "untracked.php").write_text("<?php amf_get('/nope');\n")
    subprocess.run(["git", "-C", d, "add", ".api-contract", "page.php"], check=True)
    routes = str(r / "routes.txt")
    assert main(["--routes", routes, d]) == 0  # an untracked file is not read
    (r / "sub").mkdir()
    assert main(["--routes", routes, str(r / "sub")]) == 0  # from a subdirectory: the repo
    (r / "bad.txt").write_text("GET\n")
    assert main(["--routes", str(r / "bad.txt"), d]) == 1  # an error, not a traceback
    assert main(["--routes", str(r / "none.txt"), d]) == 1
    (r / "page.php").write_text("<?php amf_get('/event/' . $id);\n")
    assert main(["--routes", routes, d]) == 1
    (r / ".api-contract").write_text("amf_get FETCH\n")
    assert main(["--routes", routes, d]) == 1  # a bad config fails, not passes
    assert main(["--routes", routes, str(r / "missing")]) == 1
print("end to end OK")
