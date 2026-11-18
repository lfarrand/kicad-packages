# Working in this repository (for agents and people)

This repository is the KiCad package store that the CI of Lee Farrand's other repositories installs KiCad from.
A mistake here breaks their KiCad jobs at once, or installs something that is not the KiCad they asked for.
Read `README.md` first.

## Rules

- **A stored version never changes.** `store` leaves the KiCad packages and manifest of a release that has a
  manifest alone. Do not delete, re-upload or edit them; a calling job trusts the key to mean the same files
  always. The dependency bundle (`dependencies-*.tar`, `dependencies.json`) is the one part that is replaced.
- **Nothing is installed unverified.** Every KiCad file is checked against a SHA-256: the manifest's for a stored
  file, Launchpad's API's for a fetched one. Do not add a path that skips it. The dependency bundle is only ever
  put into apt's download cache, where apt checks each file against its signed index; never install from it with
  `dpkg -i` or by path.
- **The dependency bundle must never fail a job.** Without it, or with a bad one, apt downloads the packages.
- **Do not use the PPA's download server** (`ppa.launchpadcontent.net`) or `add-apt-repository`: its outages are
  the reason this repository exists. A test refuses both.
- **Exact versions only.** A calling job names `10.0.8`, never `10.0` or "latest": the pin is the point.
- Workflow and action inputs reach shell steps through `env:`, never by `${{ }}` inside a `run:` script (a test
  checks this). Actions are on releases that run on Node 24.
- This repository is public. Nothing private goes in it: no board, no token, nothing from the private projects.
- Calling projects pin the action by commit SHA, so a change here reaches a project only when its pin moves.
- Tests for every change, coverage of `tools/` at 90 % or more (99 % today). Update `README.md` when behaviour
  changes.
- One branch and one pull request per change; Lee merges.
- After each piece of work: say what you checked, review your own diff as a fresh reviewer, flag what you would
  fix before it ships, and end with `result:` and a one-line headline.

## Checks

```
python3 -m pytest -q tests --cov=tools --cov-fail-under=90
```

The `install` job of `ci.yml` is the only test of the action on a real runner: read what it did (store hit or
miss, the KiCad version it printed), not only that it is green.
