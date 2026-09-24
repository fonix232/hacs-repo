"""Tests for the renovate_updates coordinator.

Home Assistant is stubbed out, so this runs with nothing installed but Python:

    python3 tests/test_renovate_updates.py
"""

import asyncio
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "custom_components" / "renovate_updates"
PKG = Path(tempfile.mkdtemp()) / "rutest"

# Isolate coordinator.py + const.py so importing does not drag in __init__.py.
shutil.rmtree(PKG, ignore_errors=True)
PKG.mkdir(parents=True)
(PKG / "__init__.py").write_text("")
for f in ("const.py", "coordinator.py", "merge_queue.py"):
    shutil.copy(SRC / f, PKG / f)
sys.path.insert(0, str(PKG.parent))


# --- minimal Home Assistant stubs -------------------------------------------
class UpdateFailed(Exception): ...


class ConfigEntryAuthFailed(Exception): ...


class HomeAssistantError(Exception): ...


class ClientError(Exception): ...


class DataUpdateCoordinator:
    def __init__(
        self, hass, logger, name=None, update_interval=None, config_entry=None
    ):
        self.hass, self.logger, self.name = hass, logger, name
        self.update_interval, self.config_entry = update_interval, config_entry
        self.listener_updates = 0
        self.refresh_requests = 0

    def async_update_listeners(self):
        self.listener_updates += 1

    async def async_request_refresh(self):
        self.refresh_requests += 1

    def __class_getitem__(cls, item):
        return cls


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_mod("aiohttp", ClientError=ClientError, ClientSession=object)
_mod("homeassistant")
_mod("homeassistant.config_entries", ConfigEntry=object)
_mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_mod(
    "homeassistant.exceptions",
    ConfigEntryAuthFailed=ConfigEntryAuthFailed,
    HomeAssistantError=HomeAssistantError,
)


class Store:
    """In-memory stand-in for homeassistant.helpers.storage.Store."""

    _files: dict[str, object] = {}

    def __init__(self, hass, version, key):
        self._key = key

    def __class_getitem__(cls, item):
        return cls

    async def async_load(self):
        return Store._files.get(self._key)

    async def async_save(self, data):
        Store._files[self._key] = data

    async def async_remove(self):
        Store._files.pop(self._key, None)


_mod("homeassistant.helpers.storage", Store=Store)
_mod(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=DataUpdateCoordinator,
    UpdateFailed=UpdateFailed,
)
_mod("homeassistant.helpers")

from rutest.const import QUEUE_ENTRY_TIMEOUT_SECONDS  # noqa: E402
from rutest.coordinator import (  # noqa: E402
    Dependency,
    MergeRefused,
    RenovateCoordinator,
    _normalise_author,
    _parse_open,
)
from rutest.merge_queue import (  # noqa: E402
    STATE_MERGING,
    STATE_QUEUED,
    STATE_REBASING,
    MergeQueue,
)


def _parse(number, title, url, mergeable, body):
    """Old flat shape, so the parsing assertions below stay readable."""
    key, name, pull_request = _parse_open(
        {
            "number": number,
            "title": title,
            "url": url,
            "mergeable": mergeable,
            "body": body,
        }
    )
    return SimpleNamespace(
        key=key,
        name=name,
        number=pull_request.number,
        url=pull_request.url,
        mergeable=pull_request.mergeable,
        body=pull_request.body,
        installed_version=pull_request.installed_version,
        latest_version=pull_request.latest_version,
        has_conflict=pull_request.has_conflict,
    )


# --- fixtures ---------------------------------------------------------------
BODY = """This PR contains the following updates:

| Package | Type | Update | Change |
|---|---|---|---|
| [lscr.io/linuxserver/radarr](https://redirect.github.com/x) | final | minor | \
`6.2.1.10461-ls309` \u2192 `6.3.0.10500-ls310` |

---

### Release Notes

<details>
<summary>Radarr/Radarr (lscr.io/linuxserver/radarr)</summary>

### [`v6.3.0`](https://github.com/Radarr/Radarr/releases/tag/v6.3.0)

- Fixed `importedTrackedDownload` handling -> now retries
- Added `--no-update` flag

</details>
"""

FAILS = []


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(label)
    print(
        f"  [{'ok' if ok else 'FAIL'}] {label}: {got!r}"
        + ("" if ok else f"  want {want!r}")
    )


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status, self._payload, self._text = status, payload, text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def patch(self, url, **kw):
        self.calls.append(("PATCH", url, kw))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class ScriptedSession(FakeSession):
    """A session whose answer depends on the call, for multi-step flows."""

    def __init__(self, handler):
        super().__init__(None)
        self._handler = handler

    async def _dispatch(self, method, url, kw):
        self.calls.append((method, url, kw))
        result = self._handler(method, url, kw)
        if isinstance(result, Exception):
            raise result
        return result

    async def post(self, url, **kw):
        return await self._dispatch("POST", url, kw)

    async def put(self, url, **kw):
        return await self._dispatch("PUT", url, kw)

    async def patch(self, url, **kw):
        return await self._dispatch("PATCH", url, kw)


class FakeEntry:
    def __init__(self, entry_id="test"):
        self.entry_id = entry_id


def make(session, author="renovate[bot]", entry_id="test"):
    return RenovateCoordinator(
        None,
        FakeEntry(entry_id),
        session,
        "octocat/infrastructure",
        "tok",
        author,
        "squash",
        None,
    )


def nodes(*ns, merged=()):
    return FakeResponse(
        200,
        {
            "data": {
                "repository": {
                    "open": {"nodes": list(ns)},
                    "merged": {"nodes": list(merged)},
                }
            }
        },
    )


def node(
    number, title, body="", draft=False, mergeable="MERGEABLE", author="renovate[bot]"
):
    return {
        "number": number,
        "title": title,
        "body": body,
        "isDraft": draft,
        "mergeable": mergeable,
        "url": f"https://github.com/octocat/infrastructure/pull/{number}",
        "author": {"login": author} if author is not None else None,
    }


run = asyncio.new_event_loop().run_until_complete

print("== _parse: change table gives exact from -> to ==")
pr = _parse(
    101,
    "chore(deps): update lscr.io/linuxserver/radarr docker tag to v6.3.0.10500-ls310",
    "u",
    "MERGEABLE",
    BODY,
)
check("installed", pr.installed_version, "6.2.1.10461-ls309")
check("latest", pr.latest_version, "6.3.0.10500-ls310")
check("name", pr.name, "lscr.io/linuxserver/radarr")
check("key", pr.key, "lscr_io_linuxserver_radarr")

print("\n== _parse: major bump, title lossy, table authoritative ==")
b = BODY.replace(
    "`6.2.1.10461-ls309` \u2192 `6.3.0.10500-ls310`", "`1.2.5.1` \u2192 `2.0.1`"
)
pr = _parse(
    104,
    "chore(deps): update ghcr.io/maziggy/bambuddy docker tag to v2",
    "u",
    "MERGEABLE",
    b,
)
check("latest from table not title", pr.latest_version, "2.0.1")
check("installed", pr.installed_version, "1.2.5.1")

print("\n== _parse: no table -> title fallback, installed unknown ==")
pr = _parse(
    105,
    "chore(deps): update ghcr.io/jellyfin/jellyfin docker tag to v10.12.0",
    "u",
    "MERGEABLE",
    "",
)
check("latest", pr.latest_version, "v10.12.0")
check("installed None", pr.installed_version, None)

print("\n== every arrow Renovate has emitted ==")
# U+2192 is what current Renovate writes; assuming ASCII "->" left
# installed_version unset, which renders the entity with no state at all.
for arrow, label in (
    ("\u2192", "U+2192 (current Renovate)"),
    ("->", "ASCII (older Renovate)"),
    ("&rarr;", "HTML entity"),
    ("&#8594;", "numeric entity"),
):
    b = f"| pkg | minor | `1.0.0` {arrow} `2.0.0` |"
    pr = _parse(1, "chore(deps): update x/y docker tag to v2.0.0", "u", "MERGEABLE", b)
    check(f"{label}", (pr.installed_version, pr.latest_version), ("1.0.0", "2.0.0"))

print("\n== _parse: changelog prose with backticks + arrow must not match ==")
pr = _parse(
    106,
    "chore(deps): update ghcr.io/donkie/spoolman docker tag to v0.25.0",
    "u",
    "MERGEABLE",
    "### Notes\n- Fixed `a` handling -> now retries\n",
)
check("falls back to title", pr.latest_version, "v0.25.0")

print("\n== _parse: beszel vs beszel-agent get distinct keys ==")
a = _parse(
    1, "chore(deps): update henrygd/beszel docker tag to v0.19", "u", "MERGEABLE", ""
)
c = _parse(
    2,
    "chore(deps): update henrygd/beszel-agent docker tag to v0.19",
    "u",
    "MERGEABLE",
    "",
)
check("beszel key", a.key, "henrygd_beszel")
check("agent key", c.key, "henrygd_beszel_agent")
check("keys differ", a.key != c.key, True)

print("\n== _parse: grouped PR falls back to per-PR key ==")
pr = _parse(7, "chore(deps): update all non-major dependencies", "u", "MERGEABLE", "")
check("key", pr.key, "pr_7")
check("name", pr.name, "update all non-major dependencies")

print("\n== _parse: digest updates handled ==")
pr = _parse(
    8, "chore(deps): update redis docker digest to abc1234", "u", "MERGEABLE", ""
)
check("key", pr.key, "redis")
check("latest", pr.latest_version, "abc1234")

print("\n== _parse: conflict flag ==")
check(
    "conflicting",
    _parse(9, "x docker tag to v1", "u", "CONFLICTING", "").has_conflict,
    True,
)
check(
    "mergeable",
    _parse(9, "x docker tag to v1", "u", "MERGEABLE", "").has_conflict,
    False,
)
check(
    "missing -> UNKNOWN",
    _parse(9, "x docker tag to v1", "u", "", "").mergeable,
    "UNKNOWN",
)

print("\n== fetch: drafts skipped, empty non-PR nodes skipped ==")
c = make(
    FakeSession(
        nodes(
            node(1, "chore(deps): update redis docker tag to v8.9.0"),
            node(2, "chore(deps): update apache/tika docker tag to v2.6.0", draft=True),
            {},
        )
    )
)
data = run(c._async_update_data())
check("only non-draft kept", sorted(data), ["redis"])
check("has a pending PR", data["redis"].pull_request is not None, True)

print("\n== fetch: two PRs for one dep -> newest wins ==")
c = make(
    FakeSession(
        nodes(
            node(10, "chore(deps): update redis docker tag to v8.9.0"),
            node(20, "chore(deps): update redis docker tag to v9.0.0"),
        )
    )
)
data = run(c._async_update_data())
check("one entity", list(data), ["redis"])
check("newer PR", data["redis"].pull_request.number, 20)

print("\n== fetch: error handling ==")
for status, exc, label in [
    (401, ConfigEntryAuthFailed, "401 -> ConfigEntryAuthFailed"),
    (403, ConfigEntryAuthFailed, "403 -> ConfigEntryAuthFailed"),
    (500, UpdateFailed, "500 -> UpdateFailed"),
]:
    c = make(FakeSession(FakeResponse(status, text="nope")))
    try:
        run(c._async_update_data())
        check(label, "no raise", exc.__name__)
    except exc:
        check(label, exc.__name__, exc.__name__)

c = make(FakeSession(FakeResponse(200, {"errors": [{"message": "bad"}]})))
try:
    run(c._async_update_data())
    check("graphql errors -> UpdateFailed", "no raise", "UpdateFailed")
except UpdateFailed:
    check("graphql errors -> UpdateFailed", "UpdateFailed", "UpdateFailed")

c = make(FakeSession(ClientError("boom")))
try:
    run(c._async_update_data())
    check("network error -> UpdateFailed", "no raise", "UpdateFailed")
except UpdateFailed:
    check("network error -> UpdateFailed", "UpdateFailed", "UpdateFailed")

print("\n== fetch: query is scoped correctly ==")
s = FakeSession(nodes())
run(make(s)._async_update_data())
sent = s.calls[0][2]["json"]
check("owner", sent["variables"]["owner"], "octocat")
check("name", sent["variables"]["name"], "infrastructure")
check("no search connection", "search(" in sent["query"], False)
check("reads repository", "repository(" in sent["query"], True)
check("auth header", s.calls[0][2]["headers"]["Authorization"], "Bearer tok")

print("\n== author normalisation accepts every spelling ==")
for spelling in ("renovate[bot]", "app/renovate", "Renovate", "  renovate[bot] "):
    check(f"{spelling!r} normalises", _normalise_author(spelling), "renovate")

print("\n== author filtering ==")
c = make(
    FakeSession(
        nodes(
            node(1, "chore(deps): update redis docker tag to v8.9.0"),
            node(2, "feat: something I wrote", author="fonix232"),
        )
    )
)
check("only the bot's PRs", sorted(run(c._async_update_data())), ["redis"])

# An entry configured before the switch stores the search-syntax spelling.
c = make(
    FakeSession(nodes(node(1, "chore(deps): update redis docker tag to v8.9.0"))),
    author="app/renovate",
)
check(
    "legacy app/renovate still matches", sorted(run(c._async_update_data())), ["redis"]
)

c = make(
    FakeSession(
        nodes(
            node(1, "chore(deps): update redis docker tag to v8.9.0"),
            node(
                2,
                "chore(deps): update apache/tika docker tag to v2.6.0",
                author="someone-else",
            ),
        )
    ),
    author="",
)
check(
    "empty author matches everything",
    sorted(run(c._async_update_data())),
    ["apache_tika", "redis"],
)

c = make(FakeSession(nodes(node(1, "x docker tag to v1", author=None))))
check("null author does not crash", run(c._async_update_data()), {})

print("\n== token lacking the Pull requests permission ==")
# Exactly what GitHub returns for a fine-grained token with only Metadata and
# Contents: HTTP 200, repository nulled by error propagation, FORBIDDEN on the
# pullRequests path. Must surface as reauth, not an endless UpdateFailed retry.
forbidden = FakeResponse(
    200,
    {
        "data": {"repository": None},
        "errors": [
            {
                "type": "FORBIDDEN",
                "path": ["repository", "pullRequests"],
                "message": "Resource not accessible by personal access token",
            }
        ],
    },
)
c = make(FakeSession(forbidden))
try:
    run(c._async_update_data())
    check("FORBIDDEN raises for reauth", "no raise", "ConfigEntryAuthFailed")
except ConfigEntryAuthFailed as err:
    check("FORBIDDEN raises for reauth", "Pull requests" in str(err), True)
except UpdateFailed:
    check("FORBIDDEN raises for reauth", "UpdateFailed", "ConfigEntryAuthFailed")

# Same shape, but with only a message and no type field.
message_only = FakeResponse(
    200,
    {
        "data": {"repository": None},
        "errors": [{"message": "Resource not accessible by personal access token"}],
    },
)
try:
    run(make(FakeSession(message_only))._async_update_data())
    check("message-only FORBIDDEN raises", "no raise", "ConfigEntryAuthFailed")
except ConfigEntryAuthFailed:
    check(
        "message-only FORBIDDEN raises",
        "ConfigEntryAuthFailed",
        "ConfigEntryAuthFailed",
    )

# An unrelated GraphQL error is a transient failure, not an auth problem.
other = FakeResponse(
    200, {"errors": [{"message": "timeout", "type": "SERVICE_UNAVAILABLE"}]}
)
try:
    run(make(FakeSession(other))._async_update_data())
    check("other errors stay UpdateFailed", "no raise", "UpdateFailed")
except UpdateFailed:
    check("other errors stay UpdateFailed", "UpdateFailed", "UpdateFailed")
except ConfigEntryAuthFailed:
    check("other errors stay UpdateFailed", "ConfigEntryAuthFailed", "UpdateFailed")

print("\n== the regression that caused the silent failure ==")
# A repository the token cannot see comes back null with no GraphQL error.
# Treating that as "no open PRs" is what produced no entities and no logs.
c = make(FakeSession(FakeResponse(200, {"data": {"repository": None}})))
try:
    run(c._async_update_data())
    check("null repository raises", "no raise", "ConfigEntryAuthFailed")
except ConfigEntryAuthFailed as err:
    check("null repository raises", "access" in str(err), True)

print("\n== dependencies persist once their PR is merged ==")
# The whole point of this design: an entity must not appear and disappear with
# its pull request, because Home Assistant handles transient entities badly.
session = FakeSession(nodes(node(1, "chore(deps): update redis docker tag to v8.9.0")))
c = make(session, entry_id="persist")
first = run(c._async_update_data())
check("present while pending", sorted(first), ["redis"])
check(
    "pending shows an update",
    first["redis"].installed_version != first["redis"].latest_version,
    True,
)

# Next poll: the PR has been merged, so it is gone from the open set. The
# dependency obviously still exists and its entity must not vanish with it.
session._response = nodes()
second = run(c._async_update_data())
check("still present after merge", sorted(second), ["redis"])
check("no pull request", second["redis"].pull_request, None)
check(
    "reports up to date",
    second["redis"].installed_version == second["redis"].latest_version,
    True,
)

print("\n== merged history discovers dependencies with nothing pending ==")
c = make(
    FakeSession(
        nodes(
            node(1, "chore(deps): update redis docker tag to v8.9.0"),
            merged=[
                node(90, "chore(deps): update apache/tika docker tag to v2.5.0"),
                node(
                    91,
                    "chore(deps): update ghcr.io/jellyfin/jellyfin docker tag"
                    " to v10.11.0",
                ),
            ],
        )
    ),
    entry_id="history",
)
data = run(c._async_update_data())
check(
    "all three known",
    sorted(data),
    ["apache_tika", "ghcr_io_jellyfin_jellyfin", "redis"],
)
check("historic one is up to date", data["apache_tika"].pull_request, None)
check("historic version from title", data["apache_tika"].installed_version, "v2.5.0")
check("pending one still pending", data["redis"].pull_request is not None, True)

print("\n== grouped PRs are not remembered as dependencies ==")
c = make(
    FakeSession(
        nodes(merged=[node(80, "chore(deps): update all non-major dependencies")]),
    ),
    entry_id="grouped",
)
check("no phantom entity", sorted(run(c._async_update_data())), [])

print("\n== remembered across a restart ==")
c = make(
    FakeSession(nodes(node(1, "chore(deps): update redis docker tag to v8.9.0"))),
    entry_id="restart",
)
run(c._async_update_data())
fresh = make(FakeSession(nodes()), entry_id="restart")
run(fresh.async_load_known())
check(
    "survives a reload with nothing pending",
    sorted(run(fresh._async_update_data())),
    ["redis"],
)

print("\n== Dependency version fallbacks ==")
d = Dependency(key="x", name="x", current_version=None, pull_request=None)
check(
    "unknown rather than None",
    (d.installed_version, d.latest_version),
    ("unknown", "unknown"),
)
check("equal means up to date", d.installed_version == d.latest_version, True)

print("\n== merge ==")
s = FakeSession(FakeResponse(200, text="{}"))
run(make(s).async_merge_pull_request(42))
check(
    "PUT url",
    s.calls[0][1],
    "https://api.github.com/repos/octocat/infrastructure/pulls/42/merge",
)
check("merge method", s.calls[0][2]["json"], {"merge_method": "squash"})

for status in (405, 409, 403):
    s = FakeSession(FakeResponse(status, text="refused"))
    try:
        run(make(s).async_merge_pull_request(42))
        check(f"merge {status} raises", "no raise", "HomeAssistantError")
    except HomeAssistantError as err:
        check(f"merge {status} raises", "refused" in str(err), True)

s = FakeSession(ClientError("down"))
try:
    run(make(s).async_merge_pull_request(42))
    check("merge network error raises", "no raise", "HomeAssistantError")
except HomeAssistantError:
    check("merge network error raises", "HomeAssistantError", "HomeAssistantError")

print("\n== merge refusals: which are worth retrying ==")
for status, transient in ((405, True), (409, True), (403, False), (422, False)):
    s = FakeSession(FakeResponse(status, text="refused"))
    try:
        run(make(s).async_merge_pull_request(42))
        check(f"{status} raises MergeRefused", "no raise", "MergeRefused")
    except MergeRefused as err:
        check(f"{status} transient", err.transient, transient)

print("\n== rebase request ticks exactly Renovate's checkbox ==")
RENOVATE_FOOTER = (
    "### Configuration\n\n"
    "- [ ] <!-- rebase-check -->If you want to rebase/retry this PR, check this box\n"
    "- [ ] some other box\n"
)
s = FakeSession(FakeResponse(200, text="{}"))
check("asked", run(make(s).async_request_rebase(7, RENOVATE_FOOTER)), True)
check(
    "PATCH url",
    s.calls[0][1],
    "https://api.github.com/repos/octocat/infrastructure/pulls/7",
)
sent_body = s.calls[0][2]["json"]["body"]
check("ticked", "- [x] <!-- rebase-check -->If you want" in sent_body, True)
check("other box untouched", "- [ ] some other box" in sent_body, True)

s = FakeSession(FakeResponse(200, text="{}"))
check(
    "no checkbox -> not asked",
    run(make(s).async_request_rebase(7, "plain body")),
    False,
)
check("no checkbox -> no call", s.calls, [])

already = RENOVATE_FOOTER.replace("- [ ] <!--", "- [x] <!--")
s = FakeSession(FakeResponse(200, text="{}"))
check(
    "already ticked -> not asked", run(make(s).async_request_rebase(7, already)), False
)

s = FakeSession(FakeResponse(403, text="nope"))
try:
    run(make(s).async_request_rebase(7, RENOVATE_FOOTER))
    check("edit refused raises", "no raise", "HomeAssistantError")
except HomeAssistantError:
    check("edit refused raises", "HomeAssistantError", "HomeAssistantError")

print("\n== merge queue ==")
PR_URL = "https://api.github.com/repos/octocat/infrastructure/pulls/"


def pr_status(state="OPEN", mergeable="MERGEABLE", sha="aaa", body="", draft=False):
    return FakeResponse(
        200,
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "state": state,
                        "mergeable": mergeable,
                        "isDraft": draft,
                        "headRefOid": sha,
                        "body": body,
                    }
                }
            }
        },
    )


class GitHub:
    """A scripted GitHub: per-PR status, and a log of merges and edits.

    Merges succeed unless `refuse` is set, in which case that status is
    returned; a successful merge flips the PR to MERGED, as GitHub does.
    """

    def __init__(self, prs):
        self.prs = prs  # number -> FakeResponse from pr_status()
        self.refuse = None  # HTTP status to answer merges with, or None
        self.merged = []
        self.edited = []

    def __call__(self, method, url, kw):
        if method == "POST":
            number = kw["json"]["variables"]["number"]
            if number not in self.prs:
                return FakeResponse(
                    200, {"data": {"repository": {"pullRequest": None}}}
                )
            return self.prs[number]
        number = int(url.removeprefix(PR_URL).split("/")[0])
        if method == "PUT":
            if self.refuse:
                return FakeResponse(self.refuse, text="Base branch was modified")
            self.merged.append(number)
            self.prs[number] = pr_status(state="MERGED")
            return FakeResponse(200, text="{}")
        if method == "PATCH":
            self.edited.append((number, kw["json"]["body"]))
            return FakeResponse(200, text="{}")
        raise AssertionError(f"unexpected {method} {url}")


def make_queue(github, entry_id="queue", clock=None):
    session = ScriptedSession(github)
    coordinator = make(session, entry_id=entry_id)
    queue = MergeQueue(None, coordinator, Store(None, 1, f"queue.{entry_id}"))
    if clock is not None:
        queue._now = lambda: clock[0]
    return queue, coordinator


gh = GitHub({1: pr_status(), 2: pr_status()})
q, c = make_queue(gh, "basic")
check("enqueue", run(q.async_enqueue(1, "redis")), True)
check("enqueue again is a no-op", run(q.async_enqueue(1, "redis")), False)
run(q.async_enqueue(2, "tika"))
check("positions", (q.position(1), q.position(2), q.position(3)), (1, 2, None))
check("starts queued", q.entry_for(1).state, STATE_QUEUED)
check("entities told", c.listener_updates >= 2, True)
check(
    "persisted", [e["number"] for e in Store._files["queue.basic"]["entries"]], [1, 2]
)

check("first pass merges one", run(q.async_process_once()), True)
check("merged #1 only", gh.merged, [1])
check("#2 still queued at the front", (q.position(2), len(q)), (1, 1))
check("refresh requested after merge", c.refresh_requests, 1)
check("second pass merges the other", run(q.async_process_once()), True)
check("both merged in order", gh.merged, [1, 2])
check("queue drained", len(q), 0)
check("store drained", Store._files["queue.basic"]["entries"], [])
check("no failures", (q.failure_for(1), q.failure_for(2)), (None, None))

print("\n-- base branch moved: 405 is retried, not failed --")
gh = GitHub({1: pr_status()})
gh.refuse = 405
q, c = make_queue(gh, "retry")
run(q.async_enqueue(1, "redis"))
check("pass without merge", run(q.async_process_once()), False)
check("still queued", q.position(1), 1)
check("back to queued", q.entry_for(1).state, STATE_QUEUED)
check("one refusal counted", q.entry_for(1).refusals, 1)
gh.refuse = None
check("merges once GitHub catches up", run(q.async_process_once()), True)
check("merged", gh.merged, [1])

print("\n-- but not forever --")
gh = GitHub({1: pr_status()})
gh.refuse = 405
q, c = make_queue(gh, "give-up")
run(q.async_enqueue(1, "redis"))
for _ in range(5):
    run(q.async_process_once())
check("dropped after repeated refusals", len(q), 0)
check("reason kept", "405" in (q.failure_for(1) or ""), True)
check("re-queue clears the failure", run(q.async_enqueue(1, "redis")), True)
check("failure cleared", q.failure_for(1), None)

print("\n-- a permanent refusal fails at once --")
gh = GitHub({1: pr_status()})
gh.refuse = 403
q, c = make_queue(gh, "perm")
run(q.async_enqueue(1, "redis"))
run(q.async_process_once())
check("dropped", len(q), 0)
check("reason", "403" in (q.failure_for(1) or ""), True)

print("\n-- conflict: ask Renovate once per head, merge after the rebase --")
gh = GitHub({1: pr_status(mergeable="CONFLICTING", sha="old", body=RENOVATE_FOOTER)})
q, c = make_queue(gh, "conflict")
run(q.async_enqueue(1, "redis"))
check("no merge", run(q.async_process_once()), False)
check("state", q.entry_for(1).state, STATE_REBASING)
check("checkbox ticked once", len(gh.edited), 1)
check("ticked body", "- [x] <!-- rebase-check -->" in gh.edited[0][1], True)
check("remembered for this head", q.entry_for(1).rebase_requested_for, "old")
check(
    "remembered across a restart",
    Store._files["queue.conflict"]["entries"][0]["rebase_requested_for"],
    "old",
)
run(q.async_process_once())
check("not asked again for the same head", len(gh.edited), 1)
check("no merge attempted while conflicting", gh.merged, [])
# Renovate rebased: new head, mergeable again.
gh.prs[1] = pr_status(mergeable="MERGEABLE", sha="new", body=RENOVATE_FOOTER)
check("merges the rebased head", run(q.async_process_once()), True)
check("merged", gh.merged, [1])

print("\n-- conflict without a checkbox: just wait --")
gh = GitHub({1: pr_status(mergeable="CONFLICTING", sha="old", body="hand-made PR")})
q, c = make_queue(gh, "no-box")
run(q.async_enqueue(1, "pr_1"))
run(q.async_process_once())
check("no edit", gh.edited, [])
check(
    "still queued, waiting", (q.position(1), q.entry_for(1).state), (1, STATE_REBASING)
)

print("\n-- UNKNOWN: GitHub is still thinking --")
gh = GitHub({1: pr_status(mergeable="UNKNOWN")})
q, c = make_queue(gh, "unknown")
run(q.async_enqueue(1, "redis"))
run(q.async_process_once())
check("nothing merged", gh.merged, [])
check("waits", q.entry_for(1).state, STATE_QUEUED)
gh.prs[1] = pr_status()
check("merges once known", run(q.async_process_once()), True)

print("\n-- merged by hand, closed, deleted, draft --")
gh = GitHub(
    {
        1: pr_status(state="MERGED"),
        2: pr_status(state="CLOSED"),
        4: pr_status(draft=True),
        5: pr_status(),
    }
)
q, c = make_queue(gh, "gone")
for number in (1, 2, 3, 4, 5):
    run(q.async_enqueue(number, f"pr_{number}"))
check("the live one merges", run(q.async_process_once()), True)
check("queue empty", len(q), 0)
check("merged by hand is not a failure", q.failure_for(1), None)
check("closed is", "closed" in (q.failure_for(2) or ""), True)
check("deleted is not a failure", q.failure_for(3), None)
check("draft is", "draft" in (q.failure_for(4) or ""), True)
check("only the live PR was merged", gh.merged, [5])

print("\n-- a PR nobody rebases is eventually dropped --")
clock = [1_000_000.0]
gh = GitHub({1: pr_status(mergeable="CONFLICTING", body="x")})
q, c = make_queue(gh, "timeout", clock)
run(q.async_enqueue(1, "redis"))
run(q.async_process_once())
check("waiting", len(q), 1)
clock[0] += QUEUE_ENTRY_TIMEOUT_SECONDS + 1
run(q.async_process_once())
check("dropped", len(q), 0)
check("reason", "gave up" in (q.failure_for(1) or ""), True)

print("\n-- GitHub unreachable: the pass aborts and nothing is lost --")
gh = GitHub({1: pr_status(), 2: pr_status()})
q, c = make_queue(gh, "down")
run(q.async_enqueue(1, "redis"))
run(q.async_enqueue(2, "tika"))
good = gh.__call__
gh_calls = [0]


def flaky(method, url, kw):
    gh_calls[0] += 1
    return ClientError("down")


q._coordinator._session._handler = flaky
check("no merge", run(q.async_process_once()), False)
check("stopped at the first failure", gh_calls[0], 1)
check("entries intact", (q.position(1), q.position(2)), (1, 2))
q._coordinator._session._handler = good
run(q.async_process_once())
run(q.async_process_once())
check("recovers", gh.merged, [1, 2])

print("\n-- the queue survives a restart --")
gh = GitHub({1: pr_status(), 2: pr_status()})
q, c = make_queue(gh, "restart-queue")
run(q.async_enqueue(1, "redis"))
run(q.async_enqueue(2, "tika"))
q2, c2 = make_queue(gh, "restart-queue")
check("empty before load", len(q2), 0)
run(q2.async_load())
check("restored in order", [e.number for e in q2.entries], [1, 2])
check("keys restored", [e.key for e in q2.entries], ["redis", "tika"])
run(q2.async_process_once())
run(q2.async_process_once())
check("resumes", gh.merged, [1, 2])

print("\n-- state while merging is visible --")
# The merge call itself is where the entity would read "merging" from; make
# GitHub check the state mid-call.
seen = []
gh = GitHub({1: pr_status()})
q, c = make_queue(gh, "state")
inner = gh.__call__


def spy(method, url, kw):
    if method == "PUT":
        seen.append(q.entry_for(1).state)
    return inner(method, url, kw)


q._coordinator._session._handler = spy
run(q.async_enqueue(1, "redis"))
run(q.async_process_once())
check("merging during the call", seen, [STATE_MERGING])

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
