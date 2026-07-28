# Renovate Updates

Surfaces open [Renovate](https://docs.renovatebot.com/) pull requests as Home
Assistant **update entities**, so a dependency bump can be reviewed and merged
without opening GitHub.

Each open pull request becomes one update entity:

| Update entity field | Source |
|---|---|
| `installed_version` | the `from` side of Renovate's change table |
| `latest_version` | the `to` side of the change table |
| `title` | the dependency, e.g. `lscr.io/linuxserver/radarr` |
| `release_url` | the pull request |
| Release notes | the full pull request body, including the upstream changelogs Renovate collected |
| Install button | merges the pull request |

Entities are created when a pull request opens and removed when it is merged or
closed, so the Updates card only ever shows work that is actually pending. The
`unique_id` is keyed on the dependency rather than the pull request number, so
successive bumps of the same image reuse the same entity.

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
  request itself.
- Draft pull requests are ignored.
- If Renovate's body has no change table, `latest_version` falls back to the
  version in the title, which is lossy on major bumps (`v2` rather than
  `v2.0.1`), and `installed_version` is unknown.
- A merge failure — a conflict, a disabled merge method, required checks not yet
  green — surfaces as an error in the UI with GitHub's response body.
- If the token later loses access, or was granted too little, the integration
  asks for a replacement through Home Assistant's reauthentication prompt rather
  than retrying forever.
- Brand assets are not included; those are only needed to list an integration in
  HACS's default store, not to install it as a custom repository.
