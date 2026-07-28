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

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "custom_components" / "renovate_updates"
PKG = Path(tempfile.mkdtemp()) / "rutest"

# Isolate coordinator.py + const.py so importing does not drag in __init__.py.
shutil.rmtree(PKG, ignore_errors=True)
PKG.mkdir(parents=True)
(PKG / "__init__.py").write_text("")
for f in ("const.py", "coordinator.py"):
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
_mod("homeassistant.core", HomeAssistant=object)
_mod(
    "homeassistant.exceptions",
    ConfigEntryAuthFailed=ConfigEntryAuthFailed,
    HomeAssistantError=HomeAssistantError,
)
_mod(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=DataUpdateCoordinator,
    UpdateFailed=UpdateFailed,
)
_mod("homeassistant.helpers")

from rutest.coordinator import (  # noqa: E402
    RenovateCoordinator,
    _normalise_author,
    _parse,
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


def make(session, author="renovate[bot]"):
    return RenovateCoordinator(
        None,
        None,
        session,
        "octocat/infrastructure",
        "tok",
        author,
        "squash",
        None,
    )


def nodes(*ns):
    return FakeResponse(
        200, {"data": {"repository": {"pullRequests": {"nodes": list(ns)}}}}
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


run = asyncio.get_event_loop().run_until_complete

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
check("newer PR", data["redis"].number, 20)

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

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}")
sys.exit(1 if FAILS else 0)
