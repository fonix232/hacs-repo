# hacs-repo

Home Assistant custom integrations, published for installation through
[HACS](https://hacs.xyz/).

## Installation

Add this repository to HACS as a custom repository:

**HACS → ⋮ → Custom repositories** → URL `https://github.com/fonix232/hacs-repo`,
type **Integration**. The integrations below then appear in the HACS store.

Alternatively, copy the wanted directory out of `custom_components/` into your
Home Assistant `config/custom_components/` and restart.

## Contents

| Integration | Description |
|---|---|
| [Renovate Updates](custom_components/renovate_updates/) | Surfaces a repository's open [Renovate](https://docs.renovatebot.com/) pull requests as Home Assistant update entities, with the changelog as native release notes and a one-click merge. |

## Development

The tests stub Home Assistant out, so they run with nothing installed:

```sh
python3 tests/test_renovate_updates.py
```

## Licence

[MIT](LICENSE).
