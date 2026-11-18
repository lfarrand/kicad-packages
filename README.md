# kicad-packages

KiCad's Ubuntu packages, kept as GitHub release assets with one release per KiCad version, and an action that
installs them. It is a cache for CI: a job that needs KiCad no longer depends on the KiCad PPA's download server
(`ppa.launchpadcontent.net`), which answers 503 for minutes at a time, and it gets the exact KiCad version it names
even after the PPA has moved on (the PPA keeps only the newest release of a series).

Public, so that every repository of this account, private ones included, can read it with no token and no set-up.

## Use it in a workflow

```yaml
    runs-on: ubuntu-24.04
    steps:
      - uses: lfarrand/kicad-packages@<full commit SHA>
        with:
          version: "10.0.8"                 # exact; this is the pin
          packages: kicad kicad-demos       # default: kicad
```

That replaces `add-apt-repository ppa:kicad/kicad-10.0-releases` and `apt-get install kicad`. Afterwards `kicad-cli`
and the `pcbnew` Python module of the system Python are installed, at exactly that version (the action checks).

| Input | Default | |
|---|---|---|
| `version` | (required) | the KiCad version in full, for example `10.0.8`; a series such as `10.0` is refused |
| `packages` | `kicad` | packages to install, separated by spaces |
| `repository` | `lfarrand/kicad-packages` | the repository whose releases hold the packages |
| `fallback` | `true` | `false` fails the job on a store miss instead of fetching from Launchpad |

| Output | |
|---|---|
| `cache-hit` | `true` every package came from the store, `partial` some did, `false` none did |
| `key` | the version key, which is the release tag |

Pin the action by commit SHA. Moving to a new KiCad is a change of `version` in the calling workflow.

## How it works

**The version key** is `kicad-<KiCad version>-ubuntu<Ubuntu release>-<architecture>`, for example
`kicad-10.0.8-ubuntu24.04-amd64`. It is the tag of the release that holds that version's `.deb` files and a
`manifest.json` with each file's name, package version, size and SHA-256.

**A job that installs KiCad** (`action.yml`, which runs `tools/kicad_cache.py get`):

1. works out the key from `version` and the runner's Ubuntu release and architecture;
2. reads the release's `manifest.json`, downloads the packages asked for and checks each against its SHA-256;
3. for whatever the store does not hold (no release with that key, or a package it lacks), fetches the file from
   Launchpad's file store instead (`launchpad.net/~kicad/.../+files`, with the SHA-256 Launchpad's API gives) and
   prints a warning. That is a different server from the PPA's download server, and the job carries on;
4. installs the files with `apt-get install --no-install-recommends`, so KiCad's dependencies come from Ubuntu's
   own archive, and checks that the installed packages are the version asked for.

The PPA's download server and `add-apt-repository` are not used anywhere.

**The store fills itself** (`.github/workflows/store.yml`, which runs `tools/kicad_cache.py store`): four times a
day, when `store.json` changes, and by hand. For every target in `store.json` it asks Launchpad which KiCad the PPA
publishes now; if there is no release with that key yet it downloads the packages once and publishes the release.
The manifest is uploaded last, and a release with a manifest is never changed, so a version, once stored, stays as
it was stored. A calling job has no token that could write here, which is why storing is this repository's own job
and not the calling job's: a new KiCad release is stored within hours of appearing, and until then a job that asks
for it gets it from Launchpad (step 3).

To store a version by hand: Actions, *Store KiCad packages*, *Run workflow*, with the version (or empty for the
newest). An older version can be stored only while Launchpad still has its files; it removes them some time after a
version is superseded.

`store.json` says what is stored:

| Key | Now | |
|---|---|---|
| `series` | `10.0` | the PPAs to follow (`kicad-<series>-releases`) |
| `targets` | Ubuntu 24.04 `noble` `amd64` | Ubuntu release, its codename, and architecture |
| `packages` | `kicad`, `kicad-demos`, `kicad-symbols`, `kicad-footprints`, `kicad-templates`, `kicad-packages3d`, `kicad-libraries` | about 410 MB per version. Not stored: `kicad-dbg` (1 GB) and the manuals (`kicad-doc-*`); a job that asks for one of those gets it from Launchpad |

## What to know

- **Ubuntu only.** These are the PPA's packages. Jobs that run in the `kicad/kicad` Docker image do not use the
  PPA and do not need this.
- **Runner image.** The key carries the Ubuntu release, so a job on `ubuntu-26.04` needs a `26.04` target in
  `store.json` (codename `resolute`); until then it is a store miss served from Launchpad.
- **A new KiCad release** appears in the PPA as `kicad` first and the libraries some hours later. Until all the
  packages of `store.json` are there the store run fails and stores nothing; the next run completes it.
- **Scheduled workflows stop** after 60 days without a commit in a public repository (GitHub sends a mail first).
  Re-enable it under Actions, or run it by hand.
- **Licence of the stored packages.** They are unmodified copies of the PPA's packages. KiCad is free software
  under the GNU GPL version 3 or later; each release's notes name the source (the PPA's source packages and KiCad's
  repositories at the release tag). The source itself is not copied here.

## Layout

| Path | What |
|---|---|
| `action.yml` | the action a job uses |
| `tools/kicad_cache.py` | everything else: `get`, `store`, `check`, `key` (Python 3.9+, standard library; `store` needs `gh`) |
| `store.json` | which series, Ubuntu releases and packages are stored |
| `.github/workflows/store.yml` | fills the store |
| `.github/workflows/ci.yml` | the tests, and the action on a real runner (installs KiCad, runs `kicad-cli` and `pcbnew`) |
| `tests/` | pytest, against a made-up Launchpad and GitHub: no test touches the network |

```
python3 -m pip install pytest pytest-cov pyyaml
python3 -m pytest -q tests --cov=tools --cov-fail-under=90
```
