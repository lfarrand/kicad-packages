#!/usr/bin/env python3
"""KiCad's Ubuntu packages, kept as GitHub release assets with one release per KiCad version.

The KiCad PPA's download server (ppa.launchpadcontent.net) answers 503 for minutes at a time, and it
keeps only the newest KiCad of a series. This keeps each version's .deb files in a release of a
public GitHub repository, named by a version key:

    kicad-<KiCad version>-ubuntu<Ubuntu release>-<architecture>      kicad-10.0.8-ubuntu24.04-amd64

  get     (a CI job, through action.yml) downloads the packages of one KiCad version into a folder:
          from the release with that key if there is one, otherwise from Launchpad's file store
          (launchpad.net/.../+files, which is not the PPA's download server). Every file is checked
          against its SHA-256.
  store   (this repository's store workflow) finds the KiCad the PPA publishes now, and if there is
          no release with its key yet, downloads its packages once and publishes them as that release.
  check   fails unless the installed packages are the KiCad version asked for.
  key     prints the version key for this machine.

A published release is never changed. Python 3.9 or later, standard library only; `store` also
needs the GitHub CLI (`gh`) with a token that can write releases.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.launchpad.net/devel"
ARCHIVE = "~kicad/+archive/ubuntu/kicad-{series}-releases"
DEFAULT_REPO = "lfarrand/kicad-packages"
USER_AGENT = "kicad-packages (+https://github.com/lfarrand/kicad-packages)"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.]*$")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.deb$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# which publication of a version to take when Launchpad lists several
STATUS_ORDER = {"Published": 0, "Pending": 1, "Superseded": 2, "Obsolete": 3, "Deleted": 4}


class CacheError(Exception):
    """the packages could not be fetched, stored or verified"""


class NotFound(CacheError):
    """the server answered 404: the thing does not exist (not retried)"""


class Mismatch(OSError):
    """a download whose size or SHA-256 is not the expected one (retried like a network error)"""


# ---- names ----------------------------------------------------------------------------------------------------------

def upstream(deb_version):
    """KiCad's own version in a package version: "10.0.8" from "10.0.8~ubuntu24.04.1" or "1:10.0.8-1" """
    return re.split(r"[~+-]", deb_version.split(":", 1)[-1], maxsplit=1)[0]


def is_release(deb_version):
    """False for a pre-release package such as "10.0.0~rc1~ubuntu24.04.1", whose upstream() is a release's"""
    rest = deb_version.split(":", 1)[-1][len(upstream(deb_version)):]
    return re.search(r"rc|alpha|beta|pre|dev|git", rest) is None


def version_tuple(version):
    return tuple(int(n) for n in version.split("."))


def series_of(version):
    """ "10.0" for "10.0.8": the release series, which names the PPA """
    return ".".join(version.split(".")[:2])


def release_key(version, ubuntu, arch):
    return "kicad-%s-ubuntu%s-%s" % (version, ubuntu, arch)


def asset_name(filename):
    """the name a file has as a release asset: GitHub replaces characters such as "~" itself, so do it first"""
    return re.sub(r"[^A-Za-z0-9._-]", ".", filename)


def asset_url(repo, key, name):
    return "https://github.com/%s/releases/download/%s/%s" % (repo, key, name)


def check_version(version):
    if not VERSION_RE.match(version or ""):
        raise CacheError("KiCad version must be a full release number such as 10.0.8, not %r" % (version,))
    return version


def check_packages(packages):
    names = list(dict.fromkeys(packages))
    if not names:
        raise CacheError("no packages named")
    for name in names:
        if not PACKAGE_RE.match(name):
            raise CacheError("not a package name: %r" % (name,))
    return names


def check_target(ubuntu, codename, arch):
    for what, value in (("Ubuntu release", ubuntu), ("Ubuntu codename", codename), ("architecture", arch)):
        if not WORD_RE.match(value or ""):
            raise CacheError("%s is missing or malformed: %r" % (what, value))


def check_repo(repo):
    if not REPO_RE.match(repo or ""):
        raise CacheError("repository must be owner/name, not %r" % (repo,))
    return repo


# ---- network --------------------------------------------------------------------------------------------------------

class Net:
    """HTTP GET with retries. `opener` and `sleep` are replaced in tests."""

    def __init__(self, opener=urllib.request.urlopen, attempts=5, wait=10, timeout=120, sleep=time.sleep, log=print):
        self.opener, self.attempts, self.wait, self.timeout, self.sleep, self.log = (
            opener, attempts, wait, timeout, sleep, log)

    def open(self, url):
        try:
            return self.opener(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound("not found: %s" % url)
            raise

    def retry(self, fn, what):
        """fn(), tried again after a network error or a bad download; a 404 is final at once"""
        last = None
        for attempt in range(1, self.attempts + 1):
            try:
                return fn()
            except OSError as e:           # URLError, HTTPError, timeouts, resets, Mismatch
                last = e
                if attempt < self.attempts:
                    self.log("%s failed (attempt %d of %d: %s), trying again" % (what, attempt, self.attempts, e))
                    self.sleep(self.wait * attempt)
        raise CacheError("%s failed after %d attempts: %s" % (what, self.attempts, last))

    def read(self, url):
        def once():
            with self.open(url) as r:
                return r.read()
        return self.retry(once, "GET " + url)

    def read_json(self, url):
        data = self.read(url)
        try:
            return json.loads(data)
        except ValueError as e:
            raise CacheError("%s did not answer with JSON: %s" % (url, e))

    def download(self, url, path, sha256, size=None):
        """url into path, kept only if its SHA-256 (and size, when given) is the expected one"""
        part = path + ".part"

        def once():
            digest, n = hashlib.sha256(), 0
            with self.open(url) as r, open(part, "wb") as out:
                while True:
                    block = r.read(1 << 20)
                    if not block:
                        break
                    digest.update(block)
                    out.write(block)
                    n += len(block)
            if size is not None and n != size:
                raise Mismatch("%d bytes, expected %d" % (n, size))
            if digest.hexdigest() != sha256:
                raise Mismatch("SHA-256 %s, expected %s" % (digest.hexdigest(), sha256))
            os.replace(part, path)

        try:
            self.retry(once, "download of " + url)
        finally:
            if os.path.exists(part):
                os.remove(part)


# ---- Launchpad ------------------------------------------------------------------------------------------------------

def lp_publications(series, codename, arch, name, net):
    """every publication of one binary package in the PPA of a series, for one Ubuntu release and architecture"""
    query = urllib.parse.urlencode({
        "ws.op": "getPublishedBinaries", "binary_name": name, "exact_match": "true", "ws.size": "300",
        "distro_arch_series": "%s/ubuntu/%s/%s" % (API, codename, arch)})
    url, entries = "%s/%s?%s" % (API, ARCHIVE.format(series=series), query), []
    while url:
        page = net.read_json(url)
        if not isinstance(page, dict) or not isinstance(page.get("entries"), list):
            raise CacheError("unexpected answer from Launchpad for %s" % name)
        entries += page["entries"]
        url = page.get("next_collection_link")
    return entries


def latest_version(publications):
    """the newest released KiCad version that is published now, or None"""
    versions = [upstream(p["binary_package_version"]) for p in publications
                if p.get("status") == "Published" and is_release(p["binary_package_version"])
                and VERSION_RE.match(upstream(p["binary_package_version"]))]
    return max(versions, key=version_tuple) if versions else None


def pick(publications, version):
    """the publication of `version`: the published one if there is one, else the newest; None if there is none"""
    found = [p for p in publications
             if is_release(p["binary_package_version"]) and upstream(p["binary_package_version"]) == version]
    found.sort(key=lambda p: p.get("date_created") or "", reverse=True)
    found.sort(key=lambda p: STATUS_ORDER.get(p.get("status"), 9))
    return found[0] if found else None


def lp_file(publication, net):
    """{file, url, sha256, size, version} of the one .deb of a publication"""
    files = net.read_json(publication["self_link"] + "?ws.op=binaryFileUrls&include_meta=true")
    debs = [f for f in files if isinstance(f, dict) and str(f.get("url", "")).endswith(".deb")] \
        if isinstance(files, list) else []
    if not debs:
        raise CacheError("Launchpad no longer has the file of %s (%s): only a stored copy can supply it"
                         % (publication.get("display_name"), publication.get("status")))
    if len(debs) != 1:
        raise CacheError("expected one .deb for %s, Launchpad lists %d" % (publication.get("display_name"), len(debs)))
    f = debs[0]
    if not SHA256_RE.match(str(f.get("sha256"))) or not isinstance(f.get("size"), int):
        raise CacheError("Launchpad gave no SHA-256 or size for %s" % f["url"])
    return {"file": urllib.parse.unquote(f["url"].rsplit("/", 1)[-1]), "url": f["url"], "sha256": f["sha256"],
            "size": f["size"], "version": publication["binary_package_version"]}


def lp_fetch(version, codename, arch, packages, dest, net):
    """Download the packages of one KiCad version from Launchpad into dest.
    {package: {file, asset, version, sha256, size}}; CacheError if Launchpad does not have one of them."""
    entries = {}
    for name in packages:
        publication = pick(lp_publications(series_of(version), codename, arch, name, net), version)
        if publication is None:
            raise CacheError("the KiCad %s PPA has no %s %s for Ubuntu %s %s"
                             % (series_of(version), name, version, codename, arch))
        f = lp_file(publication, net)
        asset = asset_name(f["file"])
        try:
            net.download(f["url"], os.path.join(dest, asset), f["sha256"], f["size"])
        except NotFound:
            raise CacheError("Launchpad no longer has the file of %s %s (%s): only a stored copy can supply it"
                             % (name, version, publication.get("status")))
        entries[name] = {"file": f["file"], "asset": asset, "version": f["version"], "sha256": f["sha256"],
                         "size": f["size"]}
    return entries


# ---- the store on GitHub --------------------------------------------------------------------------------------------

def build_manifest(version, ubuntu, codename, arch, entries):
    return {"schema": 1, "key": release_key(version, ubuntu, arch), "kicad_version": version, "ubuntu": ubuntu,
            "codename": codename, "arch": arch, "source": "ppa:kicad/kicad-%s-releases" % series_of(version),
            "packages": entries}


def check_entry(name, entry):
    """a manifest's entry for one package, refused unless it names a plain .deb asset and a SHA-256"""
    ok = (isinstance(entry, dict) and ASSET_RE.match(str(entry.get("asset"))) and SHA256_RE.match(str(entry.get("sha256")))
          and (entry.get("size") is None or isinstance(entry.get("size"), int)))
    if not ok:
        raise CacheError("the stored manifest's entry for %s is malformed" % name)
    return entry


def cache_manifest(repo, key, net):
    """the manifest of the release `key`, or None when there is no such release (a cache miss)"""
    try:
        manifest = net.read_json(asset_url(repo, key, "manifest.json"))
    except NotFound:
        return None
    if not isinstance(manifest, dict) or manifest.get("key") != key or not isinstance(manifest.get("packages"), dict):
        raise CacheError("the manifest of %s in %s is not a manifest for that key" % (key, repo))
    return manifest


def get(version, packages, ubuntu, codename, arch, repo, dest, net, fallback=True, log=print):
    """Put the .deb files of `packages` at KiCad `version` into dest: from the store, and whatever the store
    does not hold from Launchpad (unless fallback is off). {key, cache-hit: true|partial|false, files}."""
    check_version(version)
    packages = check_packages(packages)
    check_target(ubuntu, codename, arch)
    check_repo(repo)
    key = release_key(version, ubuntu, arch)
    os.makedirs(dest, exist_ok=True)
    if any(f.endswith(".deb") for f in os.listdir(dest)):
        raise CacheError("%s already holds .deb files: give an empty folder" % dest)
    try:
        manifest = cache_manifest(repo, key, net)
    except CacheError as e:
        if not fallback:
            raise
        log("::warning::the package store could not be read (%s)" % e)
        manifest = None
    stored = manifest["packages"] if manifest else {}
    files, missing = [], []
    for name in packages:
        if name not in stored:
            missing.append(name)
            continue
        try:
            entry = check_entry(name, stored[name])
            if upstream(str(entry.get("version", version))) != version:
                raise CacheError("the stored %s is version %s, not %s" % (name, entry.get("version"), version))
            path = os.path.join(dest, entry["asset"])
            net.download(asset_url(repo, key, entry["asset"]), path, entry["sha256"], entry.get("size"))
            files.append(path)
            log("%s from the store (%s)" % (name, key))
        except CacheError as e:
            if not fallback:
                raise
            log("::warning::%s could not be taken from the store (%s)" % (name, e))
            missing.append(name)
    if missing:
        if not fallback:
            raise CacheError("not in the store under %s: %s" % (key, " ".join(missing)))
        log("::warning::KiCad package store miss for %s (%s): fetching from Launchpad. The store workflow of %s "
            "stores the PPA's current version by itself; run it by hand with this version to store another."
            % (key, " ".join(missing), repo))
        fetched = lp_fetch(version, codename, arch, missing, dest, net)
        files += [os.path.join(dest, fetched[name]["asset"]) for name in missing]
    hit = "true" if not missing else ("partial" if len(missing) < len(packages) else "false")
    return {"key": key, "cache-hit": hit, "files": files}


def run_command(argv):
    """(exit code, stdout, stderr)"""
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def gh(run, *args):
    code, out, err = run(["gh"] + list(args))
    if code != 0:
        raise CacheError("gh %s failed (exit %d): %s" % (" ".join(args[:3]), code, (err or out).strip()))
    return out


def release_exists(repo, key, run):
    code, out, err = run(["gh", "release", "view", key, "--repo", repo, "--json", "tagName"])
    if code == 0:
        return True
    if "not found" in (err + out).lower():
        return False
    raise CacheError("gh release view %s failed (exit %d): %s" % (key, code, (err or out).strip()))


def release_notes(manifest):
    lines = [
        "KiCad %s for Ubuntu %s (%s) %s, as published in `%s`." % (
            manifest["kicad_version"], manifest["ubuntu"], manifest["codename"], manifest["arch"], manifest["source"]),
        "",
        "Unmodified copies of the PPA's packages, stored so that CI does not depend on the PPA's download server. "
        "KiCad is free software under the GNU GPL version 3 or later; the source of these packages is the PPA's "
        "source packages at https://launchpad.net/~kicad/+archive/ubuntu/kicad-%s-releases/+packages and KiCad's "
        "repositories at https://gitlab.com/kicad (tag %s)." % (series_of(manifest["kicad_version"]),
                                                              manifest["kicad_version"]),
        "",
        "| Package | Version | Asset | SHA-256 |",
        "|---|---|---|---|",
    ]
    for name in sorted(manifest["packages"]):
        e = manifest["packages"][name]
        lines.append("| %s | %s | `%s` | `%s` |" % (name, e["version"], e["asset"], e["sha256"]))
    return "\n".join(lines) + "\n"


def targets(config):
    """[(series, ubuntu, codename, arch)] and the package list of a store configuration"""
    try:
        packages = check_packages(config["packages"])
        out = []
        for series in config["series"]:
            if not re.match(r"^\d+\.\d+$", series):
                raise CacheError("a series is two numbers such as 10.0, not %r" % (series,))
            for t in config["targets"]:
                check_target(t["ubuntu"], t["codename"], t["arch"])
                out.append((series, t["ubuntu"], t["codename"], t["arch"]))
    except (KeyError, TypeError) as e:
        raise CacheError("the store configuration is malformed: %r" % (e,))
    if not out:
        raise CacheError("the store configuration names no series or no target")
    return out, packages


def store(config, repo, work, net, run=run_command, version=None, log=print):
    """For every target of the configuration: the PPA's current KiCad (or `version`), stored as a release
    unless a release with its key and a manifest is already there. [(key, "stored" | "already stored")]."""
    check_repo(repo)
    if version is not None:
        check_version(version)
    wanted, packages = targets(config)
    done = []
    for series, ubuntu, codename, arch in wanted:
        if version is not None and series_of(version) != series:
            continue
        v = version or latest_version(lp_publications(series, codename, arch, "kicad", net))
        if v is None:
            raise CacheError("the KiCad %s PPA publishes no kicad for Ubuntu %s %s" % (series, codename, arch))
        key = release_key(v, ubuntu, arch)
        if cache_manifest(repo, key, net) is not None:
            log("%s: already stored" % key)
            done.append((key, "already stored"))
            continue
        folder = os.path.join(work, key)
        os.makedirs(folder, exist_ok=True)
        manifest = build_manifest(v, ubuntu, codename, arch, lp_fetch(v, codename, arch, packages, folder, net))
        debs = [os.path.join(folder, manifest["packages"][name]["asset"]) for name in packages]
        # The manifest goes up last: a release without one is not a stored version, and the next run fills it in.
        if release_exists(repo, key, run):
            gh(run, "release", "upload", key, "--repo", repo, "--clobber", *debs)
        else:
            notes = os.path.join(folder, "notes.md")
            with open(notes, "w", encoding="utf-8", newline="\n") as f:
                f.write(release_notes(manifest))
            gh(run, "release", "create", key, "--repo", repo, "--title", "KiCad %s, Ubuntu %s %s" % (v, ubuntu, arch),
               "--notes-file", notes, "--latest=false", *debs)
        path = os.path.join(folder, "manifest.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
        gh(run, "release", "upload", key, "--repo", repo, "--clobber", path)
        log("%s: stored (%s)" % (key, " ".join(packages)))
        done.append((key, "stored"))
    if not done:
        raise CacheError("the store configuration has no series %s" % series_of(version))
    return done


# ---- this machine ---------------------------------------------------------------------------------------------------

def platform(os_release="/etc/os-release", run=run_command):
    """(Ubuntu release, codename, architecture) of this machine"""
    values = {}
    try:
        with open(os_release, encoding="utf-8") as f:
            for line in f:
                name, eq, value = line.strip().partition("=")
                if eq:
                    values[name] = value.strip("\"'")
    except OSError as e:
        raise CacheError("cannot read %s: %s" % (os_release, e))
    if values.get("ID") != "ubuntu":
        raise CacheError("these are Ubuntu packages, and this is %s" % (values.get("ID") or "not Ubuntu"))
    try:
        code, out, err = run(["dpkg", "--print-architecture"])
    except OSError as e:
        raise CacheError("cannot run dpkg: %s" % e)
    if code != 0 or not out.strip():
        raise CacheError("dpkg --print-architecture failed: %s" % (err or out).strip())
    return values.get("VERSION_ID", ""), values.get("VERSION_CODENAME", ""), out.strip()


def check_installed(version, packages, run=run_command):
    """CacheError unless every package is installed at KiCad `version`"""
    check_version(version)
    for name in check_packages(packages):
        try:
            code, out, err = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}${Version}", name])
        except OSError as e:
            raise CacheError("cannot run dpkg-query: %s" % e)
        if code != 0 or not out.startswith("ii "):
            raise CacheError("%s is not installed" % name)
        if upstream(out[3:].strip()) != version:
            raise CacheError("%s %s is installed, not KiCad %s" % (name, out[3:].strip(), version))


# ---- command line ---------------------------------------------------------------------------------------------------

def main(argv=None, net=None, run=run_command, out=print):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def target_options(p):
        p.add_argument("--version", required=True, help="KiCad version, for example 10.0.8")
        p.add_argument("--ubuntu", help="Ubuntu release, for example 24.04 (default: this machine's)")
        p.add_argument("--codename", help="Ubuntu codename, for example noble (default: this machine's)")
        p.add_argument("--arch", help="architecture, for example amd64 (default: this machine's)")
        p.add_argument("--os-release", default="/etc/os-release", help=argparse.SUPPRESS)

    g = sub.add_parser("get", help="download the packages of one KiCad version into a folder")
    target_options(g)
    g.add_argument("--packages", default="kicad", help="package names, separated by spaces or commas")
    g.add_argument("--repo", default=DEFAULT_REPO)
    g.add_argument("--dest", required=True, help="empty folder for the .deb files")
    g.add_argument("--no-fallback", action="store_true", help="fail on a store miss instead of asking Launchpad")
    g.add_argument("--github-output", help="file to append key= and cache-hit= to ($GITHUB_OUTPUT)")
    s = sub.add_parser("store", help="store the PPA's current KiCad as a release, unless it is there already")
    s.add_argument("--config", required=True, help="store.json")
    s.add_argument("--repo", default=DEFAULT_REPO)
    s.add_argument("--work", required=True, help="folder for the downloads")
    s.add_argument("--version", help="store this KiCad version instead of the newest (if Launchpad still has it)")
    c = sub.add_parser("check", help="fail unless the installed packages are this KiCad version")
    c.add_argument("--version", required=True)
    c.add_argument("--packages", default="kicad")
    k = sub.add_parser("key", help="print the version key for this machine")
    target_options(k)
    args = ap.parse_args(argv)
    net = net or Net()

    def target():
        if args.ubuntu and args.codename and args.arch:
            return args.ubuntu, args.codename, args.arch
        ubuntu, codename, arch = platform(args.os_release, run)
        return args.ubuntu or ubuntu, args.codename or codename, args.arch or arch

    def names(text):
        return text.replace(",", " ").split()

    try:
        if args.command == "get":
            ubuntu, codename, arch = target()
            result = get(args.version, names(args.packages), ubuntu, codename, arch, args.repo, args.dest, net,
                         fallback=not args.no_fallback, log=out)
            if args.github_output:
                with open(args.github_output, "a", encoding="utf-8") as f:
                    f.write("key=%s\ncache-hit=%s\n" % (result["key"], result["cache-hit"]))
            out("%s: %d package file(s) in %s (store hit: %s)"
                % (result["key"], len(result["files"]), args.dest, result["cache-hit"]))
        elif args.command == "store":
            try:
                with open(args.config, encoding="utf-8") as f:
                    config = json.load(f)
            except (OSError, ValueError) as e:
                raise CacheError("cannot read %s: %s" % (args.config, e))
            store(config, args.repo, args.work, net, run=run, version=args.version or None, log=out)
        elif args.command == "check":
            check_installed(args.version, names(args.packages), run)
            out("KiCad %s is installed (%s)" % (args.version, " ".join(names(args.packages))))
        else:
            ubuntu, codename, arch = target()
            check_target(ubuntu, codename, arch)
            out(release_key(check_version(args.version), ubuntu, arch))
    except CacheError as e:
        out("::error::%s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
