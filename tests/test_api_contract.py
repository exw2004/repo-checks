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
print("arguments read as paths")

# --- raw URLs -------------------------------------------------------------------
assert paths("<?php curl_init(rtrim($cfg['url'], '/') . '/feedback/app');") == [("ANY", "/feedback/app")]
assert paths("<?php curl_init('https://challenges.cloudflare.com/turnstile/v0/siteverify');") == []
assert paths("<?php curl_init($url);") == []  # the client's own helper
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
print("problems OK")

# --- config ---------------------------------------------------------------------
assert read_config("# client\namf_get GET\ncurl_init  ANY  # raw\n\n") == {"amf_get": "GET", "curl_init": "ANY"}
for bad in ("amf_get", "amf_get FETCH", "amf-get GET", "a GET b"):
    try:
        read_config(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted {bad!r}")
print("config OK")

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
    (r / "page.php").write_text("<?php amf_get('/event/' . $id);\n")
    assert main(["--routes", routes, d]) == 1
    (r / ".api-contract").write_text("amf_get FETCH\n")
    assert main(["--routes", routes, d]) == 1  # a bad config fails, not passes
    assert main(["--routes", routes, str(r / "missing")]) == 1
print("end to end OK")
