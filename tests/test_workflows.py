"""The action and the workflows as written: they parse, and they are wired the way the README says."""
import os
import re

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# majors of the actions used here that run on Node 20, which GitHub has deprecated
NODE20 = {"actions/checkout": {"v2", "v3", "v4"}, "actions/setup-python": {"v4", "v5"}}


def load(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def steps_of(document):
    if "runs" in document:
        return document["runs"]["steps"]
    return [s for job in document["jobs"].values() for s in job["steps"]]


FILES = [("action.yml",), (".github", "workflows", "store.yml"), (".github", "workflows", "ci.yml")]


def test_no_expression_is_expanded_inside_a_script():
    """inputs reach shell steps through env:, so nobody can inject shell through a version or package name"""
    for path in FILES:
        for step in steps_of(load(*path)):
            assert "${{" not in step.get("run", ""), (path, step.get("name"))


def test_no_node_20_actions():
    for path in FILES:
        for step in steps_of(load(*path)):
            if "uses" in step and "@" in step["uses"]:
                name, ref = step["uses"].split("@")
                assert ref not in NODE20.get(name, set()), step["uses"]


def test_the_action_passes_every_input_on_and_declares_its_outputs():
    action = load("action.yml")
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["version"]["required"] is True
    assert action["inputs"]["packages"]["default"] == "kicad"
    assert action["inputs"]["repository"]["default"] == "lfarrand/kicad-packages"
    get, install, check = action["runs"]["steps"]
    assert get["id"] == "get" and all(s["shell"] == "bash" for s in (get, install, check))
    assert get["env"] == {"KICAD_VERSION": "${{ inputs.version }}", "KICAD_PACKAGES": "${{ inputs.packages }}",
                          "KICAD_STORE": "${{ inputs.repository }}", "KICAD_FALLBACK": "${{ inputs.fallback }}"}
    for word in ('--version "$KICAD_VERSION"', '--packages "$KICAD_PACKAGES"', '--repo "$KICAD_STORE"',
                 '--github-output "$GITHUB_OUTPUT"', "--no-fallback", "$GITHUB_ACTION_PATH/tools/kicad_cache.py"):
        assert word in get["run"], word
    assert set(action["outputs"]) == {"cache-hit", "key"}
    for name in action["outputs"]:
        assert action["outputs"][name]["value"] == "${{ steps.get.outputs.%s }}" % name
    assert "--no-install-recommends" in install["run"] and '"$KICAD_DEBS"/*.deb' in install["run"]
    assert "exit 1" in install["run"]
    assert "check --version" in check["run"]


def test_nothing_uses_the_ppas_download_server():
    for path in FILES + [("tools", "kicad_cache.py")]:
        with open(os.path.join(ROOT, *path), encoding="utf-8") as f:
            text = f.read()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "add-apt-repository" not in code and not re.search(r"https?://ppa\.launchpadcontent", code), path


def test_the_store_workflow_can_write_releases_and_never_runs_twice_at_once():
    store = load(".github", "workflows", "store.yml")
    triggers = store[True]                       # YAML reads the key `on` as true
    assert set(triggers) == {"schedule", "push", "workflow_dispatch"}
    assert store["permissions"] == {"contents": "write"}
    assert store["concurrency"] == {"group": "store", "cancel-in-progress": False}
    run = [s for s in steps_of(store) if "run" in s][0]
    assert run["env"]["GH_TOKEN"] == "${{ github.token }}" and run["env"]["KICAD_VERSION"] == "${{ inputs.version }}"
    assert run["run"].index("set -o pipefail") < run["run"].index("| tee")       # a failed store fails the job
    assert '--repo "$GITHUB_REPOSITORY"' in run["run"] and "--config store.json" in run["run"]


def test_ci_only_reads_and_installs_with_the_action_itself():
    ci = load(".github", "workflows", "ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    uses = [s for s in ci["jobs"]["install"]["steps"] if s.get("uses") == "./"]
    assert len(uses) == 1 and uses[0]["with"]["version"] == "10.0.8"
