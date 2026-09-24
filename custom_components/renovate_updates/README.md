# Renovate Updates

Surfaces open [Renovate](https://docs.renovatebot.com/) pull requests as Home
Assistant **update entities**, so a dependency bump can be reviewed and merged
without opening GitHub.

Each dependency becomes one update entity:

| Update entity field | Source |
|---|---|
| `installed_version` | the `from` side of Renovate's change table |
| `latest_version` | the `to` side of the change table, or the installed version when nothing is pending |
| `title` | the dependency, e.g. `lscr.io/linuxserver/radarr` |
| `release_url` | the pull request |
| Release notes | the full pull request body, including the upstream changelogs Renovate collected |
| Install button | queues the pull request for merging (or merges it directly, see below) |

There is one permanent entity per dependency, not one per pull request. A
dependency with a pending pull request reports an update; one without reports
"up to date", with `installed_version` and `latest_version` equal. Entities are
never removed as pull requests merge, because entities that appear and disappear
make Home Assistant's registry churn and the UI flicker.

Dependencies are discovered from open pull requests and from Renovate's merged
history, and are then remembered in `.storage` so they survive both a restart
and a quiet period with nothing pending. The `unique_id` is keyed on the
dependency, so successive bumps of the same image reuse one entity.

Because the integration implements `async_release_notes()`, the whole changelog
renders natively in the more-info dialog. That is not reachable from YAML
template entities, whose `release_summary` is capped at 255 characters.

## Installation

### HACS

Add `https://github.com/fonix232/hacs-repo` as a custom repository of type
**Integration**, then install *Renovate Updates* and restart Home Assistant.

### Manual

Copy `custom_components/renovate_updates/` into your Home Assistant `config/`
directory and restart.

## Configuration

Add the integration from **Settings → Devices & services → Add integration →
Renovate Updates**.

| Field | Notes |
|---|---|
| Repository | the repository Renovate runs against, as `owner/name` |
| GitHub token | fine-grained PAT, see below |
| Pull request author | `renovate[bot]` for the hosted Mend app; leave empty to match every open pull request |
| Merge method | must be enabled on the repository or GitHub refuses the merge |
| When Install is pressed | *Add to the merge queue* (default) or *Merge immediately*, see below |

The token is a fine-grained personal access token scoped to the repository with:

| Permission | Level | Why |
|---|---|---|
| **Pull requests** | Read and write | Read the queue, and merge |
| **Contents** | Read and write | Merging writes to the branch |
| **Metadata** | Read | Implied by the above |

**Pull requests is the one that is easy to miss.** Without it GitHub answers
`FORBIDDEN` on the `pullRequests` field while still resolving the repository
itself, so a token with only Contents and Metadata looks valid but can never see
a single pull request. Setup rejects such a token rather than accepting it.

## The merge queue

GitHub's own merge queue is only offered on public repositories and paid plans,
so the integration carries a small one of its own. With **Add to the merge
queue** selected, pressing Install on several entities in a row queues their
pull requests and returns straight away; a background worker then merges them
one at a time, in the order they were pressed. The entity shows a progress
spinner while its pull request is queued, and its `queue_state`,
`queue_position` and `queue_error` attributes say where it is.

Merging one at a time is what makes a batch land without babysitting:

- After every merge GitHub recomputes the mergeability of every other open pull
  request, and refuses a merge call made in that window with `405 Base branch
  was modified`. The worker looks again a few seconds later instead of failing.
- Renovate pull requests that edit neighbouring lines of the same file conflict
  the moment the first of them merges. The worker ticks the pull request's
  *rebase/retry* checkbox to prompt Renovate, waits for the rebased head to
  become mergeable, then merges it. Renovate rebases conflicted pull requests
  on its own too; the checkbox just makes it explicit.

Polling backs off from a few seconds to five minutes while nothing is ready,
and a refresh (a poll or a webhook delivery) wakes the worker early. A queued
pull request that has not merged after six hours, or that GitHub calls
mergeable yet keeps refusing, is dropped and its `queue_error` says why. The
queue is saved to `.storage`, so a restart resumes it.

**Merge immediately** is the original behaviour: one merge call, with any
refusal shown as an error in the UI.

## Polling or webhook

**Settings → Devices & services → Renovate Updates → Configure** switches
between the two, along with the intervals, author and merge method.

**Poll** (default) checks GitHub every 30 minutes.

**Webhook** refreshes the moment GitHub reports a change. The options page shows
the payload URL to add under the repository's **Settings → Webhooks**, with
content type `application/json`, subscribed to **Pull requests**, **Pushes** and
**Check suites**.

The webhook payload is never read. A delivery is only a hint that something
changed; the authenticated API call that follows is the single source of truth.
A forged request can therefore do nothing beyond causing one extra fetch, which
is why the endpoint needs no signature verification. Bursts are coalesced by the
coordinator's request debouncer.

Webhook mode still performs a slow background poll — 60 minutes by default — so
a dropped delivery cannot leave the entity list stale indefinitely. Set the
fallback interval to `0` to rely on webhooks alone.

## Notes and limitations

- Only pull requests whose title matches Renovate's `update <dep> docker tag`
  form are mapped to a named dependency. Grouped pull requests, which cover
  several dependencies at once, fall back to one entity keyed on the pull
  request itself; those are not remembered once merged, since they do not
  correspond to a lasting dependency.
- A dependency is remembered for good. If you stop tracking one, delete its
  entity from the entity registry.
- The version shown for an up-to-date dependency is the best available guess:
  the `from` side of its last open pull request, or the version in the title of
  the last merged one. It becomes exact again as soon as a new pull request
  opens.
- Draft pull requests are ignored.
- If Renovate's body has no change table, `latest_version` falls back to the
  version in the title, which is lossy on major bumps (`v2` rather than
  `v2.0.1`), and `installed_version` is unknown.
- In *Merge immediately* mode a merge failure — a conflict, a disabled merge
  method, required checks not yet green — surfaces as an error in the UI with
  GitHub's response body. In queue mode the same information lands in the
  entity's `queue_error` attribute and the log.
- Ticking the rebase checkbox edits the pull request body, which needs the
  token's *Pull requests: Read and write* permission — the one it already has
  for merging.
- If the token later loses access, or was granted too little, the integration
  asks for a replacement through Home Assistant's reauthentication prompt rather
  than retrying forever.
- Brand assets are not included; those are only needed to list an integration in
  HACS's default store, not to install it as a custom repository.
