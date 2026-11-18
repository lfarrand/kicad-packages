"""kicad_cache.py against a made-up Launchpad and a made-up GitHub: no test touches the network."""
import hashlib
import io
import json
import os
import urllib.error

import pytest

import kicad_cache as kc

REPO = "lfarrand/kicad-packages"
KEY = "kicad-10.0.8-ubuntu24.04-amd64"
DAS = "https://api.launchpad.net/devel/ubuntu/noble/amd64"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "status %d" % code, {}, io.BytesIO(b""))


class Web:
    """urlopen stand-in: {url: bytes | Exception | [one of those per request]}; anything else is a 404"""

    def __init__(self, pages=None):
        self.pages, self.requests = dict(pages or {}), []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(url)
        assert request.get_header("User-agent") == kc.USER_AGENT
        page = self.pages.get(url)
        if isinstance(page, list):
            page = page.pop(0) if len(page) > 1 else page[0]
        if page is None:
            raise http_error(url, 404)
        if isinstance(page, Exception):
            raise page
        return io.BytesIO(page)


def net_for(web, attempts=3):
    log = []
    net = kc.Net(opener=web, attempts=attempts, wait=1, sleep=lambda s: log.append("sleep %s" % s), log=log.append)
    net.messages = log
    return net


def publications_url(name, series="10.0", codename="noble", arch="amd64"):
    from urllib.parse import urlencode
    return "%s/~kicad/+archive/ubuntu/kicad-%s-releases?%s" % (kc.API, series, urlencode({
        "ws.op": "getPublishedBinaries", "binary_name": name, "exact_match": "true", "ws.size": "300",
        "distro_arch_series": "%s/ubuntu/%s/%s" % (kc.API, codename, arch)}))


def publication(name, deb_version, n, status="Published", created="2026-10-29T00:00:00+00:00"):
    return {"self_link": "%s/pub/%d" % (kc.API, n), "binary_package_name": name, "binary_package_version": deb_version,
            "status": status, "date_created": created, "display_name": "%s %s in noble amd64" % (name, deb_version)}


def launchpad(packages, extra=None):
    """A Launchpad that publishes {name: (deb version, content)}; returns the Web's pages"""
    pages, n = {}, 0
    for name, (deb_version, content) in packages.items():
        n += 1
        arch = "amd64" if name == "kicad" else "all"
        file_url = "https://launchpad.net/~kicad/+archive/ubuntu/kicad-10.0-releases/+files/%s_%s_%s.deb" % (
            name, deb_version, arch)
        entries = [publication(name, deb_version, n)] + list((extra or {}).get(name, []))
        pages[publications_url(name)] = json.dumps({"entries": entries}).encode()
        pages["%s/pub/%d?ws.op=binaryFileUrls&include_meta=true" % (kc.API, n)] = json.dumps(
            [{"url": file_url, "sha256": sha(content), "sha1": "x", "size": len(content)}]).encode()
        pages[file_url] = content
    return pages


def github(packages, key=KEY, repo=REPO):
    """A store that holds {name: (deb version, content)} under key; returns the Web's pages"""
    entries, pages = {}, {}
    for name, (deb_version, content) in packages.items():
        asset = kc.asset_name("%s_%s_all.deb" % (name, deb_version))
        entries[name] = {"file": asset.replace(".ubuntu", "~ubuntu"), "asset": asset, "version": deb_version,
                         "sha256": sha(content), "size": len(content)}
        pages[kc.asset_url(repo, key, asset)] = content
    manifest = kc.build_manifest("10.0.8", "24.04", "noble", "amd64", entries)
    manifest["key"] = key
    pages[kc.asset_url(repo, key, "manifest.json")] = json.dumps(manifest).encode()
    return pages


V = "10.0.8~ubuntu24.04.1"
KICAD, DEMOS = b"kicad package bytes", b"demo boards"


def get(web, tmp_path, packages=("kicad", "kicad-demos"), version="10.0.8", **kw):
    log = []
    result = kc.get(version, list(packages), "24.04", "noble", "amd64", REPO, str(tmp_path / "debs"), net_for(web),
                    log=log.append, **kw)
    return result, log


# ---- names ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("deb, version", [
    ("10.0.8~ubuntu24.04.1", "10.0.8"), ("1:10.0.8-1", "10.0.8"), ("10.0.8", "10.0.8"), ("9.0.2+dfsg-1", "9.0.2"),
    ("10.0.0~rc1~ubuntu24.04.1", "10.0.0")])
def test_upstream(deb, version):
    assert kc.upstream(deb) == version


@pytest.mark.parametrize("deb, release", [
    ("10.0.8~ubuntu24.04.1", True), ("10.0.0~rc1~ubuntu24.04.1", False), ("10.0.0~rc1.1~ubuntu24.04.1", False),
    ("1:10.0.8-1", True), ("10.0.8", True), ("10.99.0~alpha1", False), ("10.0.9~pre20261101", False)])
def test_is_release(deb, release):
    assert kc.is_release(deb) is release


def test_key_and_names():
    assert kc.release_key("10.0.8", "24.04", "amd64") == KEY
    assert kc.series_of("10.0.8") == "10.0"
    assert kc.version_tuple("10.0.10") > kc.version_tuple("10.0.9")
    assert kc.asset_name("kicad_10.0.8~ubuntu24.04.1_amd64.deb") == "kicad_10.0.8.ubuntu24.04.1_amd64.deb"
    assert kc.asset_name("a b/c:d.deb") == "a.b.c.d.deb"
    assert kc.asset_url("o/r", KEY, "x.deb") == "https://github.com/o/r/releases/download/%s/x.deb" % KEY


@pytest.mark.parametrize("version", ["10.0", "10", "", None, "10.0.8~x", "latest", "10.0.8 ", "v10.0.8"])
def test_version_must_be_exact(version):
    with pytest.raises(kc.CacheError, match="full release number"):
        kc.check_version(version)


def test_package_names():
    assert kc.check_packages(["kicad", "kicad-demos", "kicad"]) == ["kicad", "kicad-demos"]
    for bad in ([], ["../x"], ["Kicad"], ["kicad;rm"], ["-o"], ["k"]):
        with pytest.raises(kc.CacheError):
            kc.check_packages(bad)


@pytest.mark.parametrize("target", [("", "noble", "amd64"), ("24.04", "", "amd64"), ("24.04", "noble", None),
                                    ("24.04", "no/ble", "amd64"), ("24.04", "noble", "amd64 x")])
def test_target_must_be_plain_words(target):
    with pytest.raises(kc.CacheError):
        kc.check_target(*target)


@pytest.mark.parametrize("repo", ["", None, "kicad-packages", "a/b/c", "a/b?x", "a /b"])
def test_repository_must_be_owner_name(repo):
    with pytest.raises(kc.CacheError):
        kc.check_repo(repo)


# ---- network --------------------------------------------------------------------------------------------------------

def test_read_retries_a_503_then_succeeds():
    web = Web({"https://x/a": [http_error("https://x/a", 503), OSError("reset"), b"ok"]})
    net = net_for(web)
    assert net.read("https://x/a") == b"ok"
    assert len(web.requests) == 3
    assert [m for m in net.messages if m.startswith("sleep")] == ["sleep 1", "sleep 2"]


def test_read_gives_up_after_the_attempts():
    web = Web({"https://x/a": http_error("https://x/a", 503)})
    with pytest.raises(kc.CacheError, match="failed after 3 attempts.*503"):
        net_for(web).read("https://x/a")
    assert len(web.requests) == 3


def test_a_404_is_final_at_once():
    web = Web()
    with pytest.raises(kc.NotFound):
        net_for(web).read("https://x/none")
    assert len(web.requests) == 1


def test_read_json_refuses_what_is_not_json():
    with pytest.raises(kc.CacheError, match="did not answer with JSON"):
        net_for(Web({"https://x/a": b"<html>503</html>"})).read_json("https://x/a")


def test_download_checks_the_sha256_and_size(tmp_path):
    path = str(tmp_path / "f.deb")
    net_for(Web({"https://x/f": b"data"})).download("https://x/f", path, sha(b"data"), 4)
    assert open(path, "rb").read() == b"data"
    net_for(Web({"https://x/f": b"data"})).download("https://x/f", path, sha(b"data"))          # no size given


def test_download_retries_a_damaged_file_and_keeps_only_a_good_one(tmp_path):
    path = str(tmp_path / "f.deb")
    web = Web({"https://x/f": [b"dat", b"daXa", b"data"]})
    net = net_for(web)
    net.download("https://x/f", path, sha(b"data"), 4)
    assert open(path, "rb").read() == b"data" and len(web.requests) == 3
    assert any("3 bytes, expected 4" in m for m in net.messages) and any("SHA-256" in m for m in net.messages)


def test_download_of_a_wrong_file_leaves_nothing_behind(tmp_path):
    path = str(tmp_path / "f.deb")
    with pytest.raises(kc.CacheError, match="SHA-256"):
        net_for(Web({"https://x/f": b"evil"})).download("https://x/f", path, sha(b"data"), 4)
    assert os.listdir(str(tmp_path)) == []


def test_download_of_a_missing_file_is_not_found(tmp_path):
    with pytest.raises(kc.NotFound):
        net_for(Web()).download("https://x/f", str(tmp_path / "f.deb"), sha(b"data"))
    assert os.listdir(str(tmp_path)) == []


def test_large_download_is_read_in_blocks(tmp_path):
    data = os.urandom((1 << 20) * 2 + 17)
    path = str(tmp_path / "big.deb")
    net_for(Web({"https://x/big": data})).download("https://x/big", path, sha(data), len(data))
    assert os.path.getsize(path) == len(data)


# ---- Launchpad ------------------------------------------------------------------------------------------------------

def test_publications_follow_the_next_page():
    first = publications_url("kicad")
    web = Web({first: json.dumps({"entries": [publication("kicad", V, 1)],
                                  "next_collection_link": "https://lp/next"}).encode(),
               "https://lp/next": json.dumps({"entries": [publication("kicad", "10.0.7~ubuntu24.04.1", 2)]}).encode()})
    pubs = kc.lp_publications("10.0", "noble", "amd64", "kicad", net_for(web))
    assert [p["binary_package_version"] for p in pubs] == [V, "10.0.7~ubuntu24.04.1"]


@pytest.mark.parametrize("answer", [b"[]", b'{"entries": 3}', b'"x"'])
def test_publications_refuse_an_unexpected_answer(answer):
    with pytest.raises(kc.CacheError, match="unexpected answer"):
        kc.lp_publications("10.0", "noble", "amd64", "kicad", net_for(Web({publications_url("kicad"): answer})))


def test_latest_version_is_the_newest_published_release():
    pubs = [publication("kicad", "10.0.9~ubuntu24.04.1", 1), publication("kicad", "10.0.10~ubuntu24.04.1", 2),
            publication("kicad", "10.0.11~ubuntu24.04.1", 3, status="Superseded"),
            publication("kicad", "10.1.0~rc1~ubuntu24.04.1", 4), publication("kicad", "nightly", 5)]
    assert kc.latest_version(pubs) == "10.0.10"
    assert kc.latest_version([]) is None
    assert kc.latest_version([publication("kicad", V, 1, status="Deleted")]) is None


def test_pick_takes_the_published_one_then_the_newest_and_never_a_release_candidate():
    old = publication("kicad", "10.0.0~ubuntu24.04.1", 1, status="Superseded", created="2026-03-20")
    newer = publication("kicad", "10.0.0~ubuntu24.04.2", 2, status="Superseded", created="2026-03-25")
    rc = publication("kicad", "10.0.0~rc1~ubuntu24.04.1", 3, status="Published", created="2026-02-12")
    live = publication("kicad", "10.0.0~ubuntu24.04.1", 4, status="Published", created="2026-03-01")
    odd = publication("kicad", "10.0.0~ubuntu24.04.3", 5, status="Unheard of", created="2026-12-01")
    assert kc.pick([old, newer, rc], "10.0.0") is newer
    assert kc.pick([old, newer, rc, live, odd], "10.0.0") is live
    assert kc.pick([old, odd], "10.0.0") is old
    assert kc.pick([rc], "10.0.0") is None
    assert kc.pick([old], "10.0.1") is None


def test_lp_file_names_the_deb():
    pages = launchpad({"kicad": (V, KICAD)})
    f = kc.lp_file(publication("kicad", V, 1), net_for(Web(pages)))
    assert f == {"file": "kicad_%s_amd64.deb" % V, "sha256": sha(KICAD), "size": len(KICAD), "version": V,
                 "url": "https://launchpad.net/~kicad/+archive/ubuntu/kicad-10.0-releases/+files/kicad_%s_amd64.deb" % V}


@pytest.mark.parametrize("files, message", [
    ([], "no longer has the file"),
    ({"x": 1}, "no longer has the file"),
    ([{"url": "https://l/a.deb", "sha256": "0" * 64, "size": 1}, {"url": "https://l/b.deb"}], "lists 2"),
    ([{"url": "https://l/a.deb", "sha256": "abc", "size": 1}], "no SHA-256 or size"),
    ([{"url": "https://l/a.deb", "sha256": "0" * 64}], "no SHA-256 or size"),
    ([{"url": "https://l/a.udeb"}, "junk"], "no longer has the file")])
def test_lp_file_refuses_what_it_cannot_verify(files, message):
    web = Web({"%s/pub/1?ws.op=binaryFileUrls&include_meta=true" % kc.API: json.dumps(files).encode()})
    with pytest.raises(kc.CacheError, match=message):
        kc.lp_file(publication("kicad", V, 1), net_for(web))


def test_lp_fetch_downloads_under_the_asset_names(tmp_path):
    web = Web(launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}))
    entries = kc.lp_fetch("10.0.8", "noble", "amd64", ["kicad", "kicad-demos"], str(tmp_path), net_for(web))
    assert entries["kicad"] == {"file": "kicad_%s_amd64.deb" % V, "asset": "kicad_10.0.8.ubuntu24.04.1_amd64.deb",
                                "version": V, "sha256": sha(KICAD), "size": len(KICAD)}
    assert sorted(os.listdir(str(tmp_path))) == ["kicad-demos_10.0.8.ubuntu24.04.1_all.deb",
                                                 "kicad_10.0.8.ubuntu24.04.1_amd64.deb"]
    assert not any("launchpadcontent" in u for u in web.requests)      # never the PPA's download server


def test_lp_fetch_names_the_version_the_ppa_does_not_have(tmp_path):
    web = Web(launchpad({"kicad": (V, KICAD)}))
    with pytest.raises(kc.CacheError, match="has no kicad 10.0.7 for Ubuntu noble amd64"):
        kc.lp_fetch("10.0.7", "noble", "amd64", ["kicad"], str(tmp_path), net_for(web))


def test_lp_fetch_of_a_file_launchpad_removed(tmp_path):
    pages = launchpad({"kicad": (V, KICAD)})
    del pages["https://launchpad.net/~kicad/+archive/ubuntu/kicad-10.0-releases/+files/kicad_%s_amd64.deb" % V]
    with pytest.raises(kc.CacheError, match="no longer has the file of kicad 10.0.8"):
        kc.lp_fetch("10.0.8", "noble", "amd64", ["kicad"], str(tmp_path), net_for(Web(pages)))


# ---- get ------------------------------------------------------------------------------------------------------------

def test_get_takes_everything_from_the_store_and_never_asks_launchpad(tmp_path):
    web = Web(github({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS), "kicad-symbols": (V, b"s")}))
    result, log = get(web, tmp_path)
    assert result["cache-hit"] == "true" and result["key"] == KEY
    assert [os.path.basename(f) for f in result["files"]] == ["kicad_10.0.8.ubuntu24.04.1_all.deb",
                                                              "kicad-demos_10.0.8.ubuntu24.04.1_all.deb"]
    assert open(result["files"][0], "rb").read() == KICAD
    assert sorted(os.listdir(str(tmp_path / "debs"))) == sorted(os.path.basename(f) for f in result["files"])
    assert all(u.startswith("https://github.com/lfarrand/kicad-packages/releases/download/") for u in web.requests)
    assert not any("warning" in m for m in log)


def test_get_on_a_miss_fetches_from_launchpad(tmp_path):
    web = Web(launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}))
    result, log = get(web, tmp_path)
    assert result["cache-hit"] == "false"
    assert [open(f, "rb").read() for f in result["files"]] == [KICAD, DEMOS]
    assert any(m.startswith("::warning::KiCad package store miss for %s (kicad kicad-demos)" % KEY) for m in log)


def test_get_takes_from_launchpad_only_what_the_store_lacks(tmp_path):
    pages = github({"kicad": (V, KICAD)})
    pages.update(launchpad({"kicad-demos": (V, DEMOS)}))
    result, log = get(Web(pages), tmp_path)
    assert result["cache-hit"] == "partial"
    assert [open(f, "rb").read() for f in result["files"]] == [KICAD, DEMOS]
    assert any("store miss for %s (kicad-demos)" % KEY in m for m in log)


def test_get_without_fallback_fails_on_a_miss(tmp_path):
    with pytest.raises(kc.CacheError, match="not in the store under %s: kicad kicad-demos" % KEY):
        get(Web(launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)})), tmp_path, fallback=False)


def test_get_never_installs_a_stored_file_whose_sha256_is_wrong(tmp_path):
    pages = github({"kicad": (V, KICAD)})
    pages[kc.asset_url(REPO, KEY, "kicad_10.0.8.ubuntu24.04.1_all.deb")] = b"kicad package byteZ"
    with pytest.raises(kc.CacheError, match="SHA-256"):
        get(Web(dict(pages)), tmp_path, packages=["kicad"], fallback=False)
    assert os.listdir(str(tmp_path / "debs")) == []
    pages.update(launchpad({"kicad": (V, KICAD)}))                      # with fallback: Launchpad's copy instead
    result, log = get(Web(pages), tmp_path, packages=["kicad"])
    assert result["cache-hit"] == "false" and open(result["files"][0], "rb").read() == KICAD
    assert any("could not be taken from the store" in m for m in log)


def test_get_when_github_is_down_falls_back_or_fails(tmp_path):
    url = kc.asset_url(REPO, KEY, "manifest.json")
    pages = launchpad({"kicad": (V, KICAD)})
    pages[url] = http_error(url, 503)
    result, log = get(Web(dict(pages)), tmp_path, packages=["kicad"])
    assert result["cache-hit"] == "false" and any("store could not be read" in m for m in log)
    with pytest.raises(kc.CacheError, match="503"):
        kc.get("10.0.8", ["kicad"], "24.04", "noble", "amd64", REPO, str(tmp_path / "other"), net_for(Web(pages)),
               fallback=False, log=lambda m: None)


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(key="kicad-9.0.0-ubuntu24.04-amd64"), "not a manifest for that key"),
    (lambda m: m.update(packages=["kicad"]), "not a manifest for that key"),
    (lambda m: m["packages"]["kicad"].update(asset="../../etc/passwd.deb"), "malformed"),
    (lambda m: m["packages"]["kicad"].update(asset="kicad.exe"), "malformed"),
    (lambda m: m["packages"]["kicad"].update(sha256="short"), "malformed"),
    (lambda m: m["packages"]["kicad"].update(size="12"), "malformed"),
    (lambda m: m["packages"].update(kicad="x.deb"), "malformed"),
    (lambda m: m["packages"]["kicad"].update(version="10.0.7~ubuntu24.04.1"), "is version 10.0.7~ubuntu24.04.1, not 10.0.8")])
def test_get_refuses_a_manifest_it_cannot_trust(tmp_path, change, message):
    pages = github({"kicad": (V, KICAD)})
    url = kc.asset_url(REPO, KEY, "manifest.json")
    manifest = json.loads(pages[url])
    change(manifest)
    pages[url] = json.dumps(manifest).encode()
    with pytest.raises(kc.CacheError, match=message):
        get(Web(pages), tmp_path, packages=["kicad"], fallback=False)


def test_get_accepts_a_manifest_entry_without_size_or_version(tmp_path):
    pages = github({"kicad": (V, KICAD)})
    url = kc.asset_url(REPO, KEY, "manifest.json")
    manifest = json.loads(pages[url])
    del manifest["packages"]["kicad"]["size"], manifest["packages"]["kicad"]["version"]
    pages[url] = json.dumps(manifest).encode()
    assert get(Web(pages), tmp_path, packages=["kicad"])[0]["cache-hit"] == "true"


def test_get_refuses_a_folder_that_already_holds_packages(tmp_path):
    os.makedirs(str(tmp_path / "debs"))
    open(str(tmp_path / "debs" / "old.deb"), "w").close()
    with pytest.raises(kc.CacheError, match="already holds .deb files"):
        get(Web(github({"kicad": (V, KICAD)})), tmp_path)


@pytest.mark.parametrize("kw", [dict(version="10.0"), dict(packages=["kicad; id"]), dict(packages=[])])
def test_get_checks_its_arguments_before_any_request(tmp_path, kw):
    web = Web()
    with pytest.raises(kc.CacheError):
        get(web, tmp_path, **kw)
    assert web.requests == []


# ---- store ----------------------------------------------------------------------------------------------------------

CONFIG = {"series": ["10.0"], "targets": [{"ubuntu": "24.04", "codename": "noble", "arch": "amd64"}],
          "packages": ["kicad", "kicad-demos"]}


class Gh:
    """gh stand-in: records every call; `release view` answers from self.exists"""

    def __init__(self, exists=False, fail=None, view=None):
        self.calls, self.exists, self.fail, self.view = [], exists, fail, view

    def __call__(self, argv):
        self.calls.append(argv)
        assert argv[0] == "gh" and argv[1] == "release"
        if argv[2] == "view":
            if self.view:
                return self.view
            return (0, '{"tagName":"x"}', "") if self.exists else (1, "", "release not found\n")
        if self.fail == argv[2]:
            return 1, "", "HTTP 502"
        for path in [a for a in argv if a.endswith((".deb", ".json", ".md"))]:
            assert os.path.isfile(path), path
        return 0, "", ""


def run_store(tmp_path, pages, gh, **kw):
    log = []
    done = kc.store(kw.pop("config", CONFIG), REPO, str(tmp_path / "work"), net_for(Web(pages)), run=gh,
                    log=log.append, **kw)
    return done, log


def test_store_publishes_the_ppas_current_version_once(tmp_path):
    gh = Gh()
    done, log = run_store(tmp_path, launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), gh)
    assert done == [(KEY, "stored")]
    view, create, upload = gh.calls
    assert view[:4] == ["gh", "release", "view", KEY]
    assert create[:6] == ["gh", "release", "create", KEY, "--repo", REPO] and "--latest=false" in create
    assert [os.path.basename(a) for a in create if a.endswith(".deb")] == [
        "kicad_10.0.8.ubuntu24.04.1_amd64.deb", "kicad-demos_10.0.8.ubuntu24.04.1_all.deb"]
    assert not any(a.endswith("manifest.json") for a in create)          # the manifest goes up last
    assert upload[:3] == ["gh", "release", "upload"] and upload[-1].endswith("manifest.json")
    manifest = json.load(open(upload[-1]))
    assert manifest["key"] == KEY and manifest["kicad_version"] == "10.0.8" and manifest["schema"] == 1
    assert manifest["source"] == "ppa:kicad/kicad-10.0-releases"
    assert manifest["packages"]["kicad"]["sha256"] == sha(KICAD)
    notes = open(create[create.index("--notes-file") + 1]).read()
    assert "GNU GPL" in notes and sha(DEMOS) in notes and "kicad-10.0-releases/+packages" in notes


def test_what_store_publishes_is_what_get_reads(tmp_path):
    """a release made by store, served back as GitHub would, is a full hit for get"""
    gh = Gh()
    run_store(tmp_path, launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), gh)
    pages = {}
    for call in gh.calls[1:]:
        for path in [a for a in call if a.endswith((".deb", ".json"))]:
            pages[kc.asset_url(REPO, KEY, os.path.basename(path))] = open(path, "rb").read()
    result, log = get(Web(pages), tmp_path)
    assert result["cache-hit"] == "true" and [open(f, "rb").read() for f in result["files"]] == [KICAD, DEMOS]


def test_store_leaves_a_stored_version_alone(tmp_path):
    gh = Gh()
    pages = launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)})
    pages.update(github({"kicad": (V, b"the stored copy")}))
    done, log = run_store(tmp_path, pages, gh)
    assert done == [(KEY, "already stored")] and gh.calls == []
    assert not os.path.exists(str(tmp_path / "work"))                     # nothing was downloaded either


def test_store_completes_a_release_that_has_no_manifest(tmp_path):
    gh = Gh(exists=True)
    done, log = run_store(tmp_path, launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), gh)
    assert done == [(KEY, "stored")]
    assert [c[2] for c in gh.calls] == ["view", "upload", "upload"] and "--clobber" in gh.calls[1]
    assert gh.calls[2][-1].endswith("manifest.json")


def test_store_of_a_named_version(tmp_path):
    old = "10.0.7~ubuntu24.04.1"
    pages = launchpad({"kicad": (old, KICAD), "kicad-demos": (old, DEMOS)})
    gh = Gh()
    done, log = run_store(tmp_path, pages, gh, version="10.0.7")
    assert done == [("kicad-10.0.7-ubuntu24.04-amd64", "stored")]
    with pytest.raises(kc.CacheError, match="no series 9.0"):
        run_store(tmp_path, pages, Gh(), version="9.0.3")
    with pytest.raises(kc.CacheError, match="full release number"):
        run_store(tmp_path, pages, Gh(), version="10.0")


def test_store_fails_when_the_ppa_publishes_nothing(tmp_path):
    pages = {publications_url("kicad"): json.dumps({"entries": []}).encode()}
    with pytest.raises(kc.CacheError, match="publishes no kicad"):
        run_store(tmp_path, pages, Gh())


def test_store_publishes_no_manifest_when_a_package_is_missing(tmp_path):
    gh = Gh()
    pages = launchpad({"kicad": (V, KICAD)})
    pages[publications_url("kicad-demos")] = json.dumps({"entries": []}).encode()
    with pytest.raises(kc.CacheError, match="has no kicad-demos 10.0.8"):
        run_store(tmp_path, pages, gh)
    assert gh.calls == []


@pytest.mark.parametrize("fail", ["create", "upload"])
def test_store_fails_when_gh_fails(tmp_path, fail):
    with pytest.raises(kc.CacheError, match="HTTP 502"):
        run_store(tmp_path, launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), Gh(fail=fail))


def test_store_does_not_take_a_gh_error_for_no_release(tmp_path):
    gh = Gh(view=(1, "", "HTTP 401: Bad credentials"))
    with pytest.raises(kc.CacheError, match="Bad credentials"):
        run_store(tmp_path, launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), gh)
    assert [c[2] for c in gh.calls] == ["view"]


def test_store_covers_every_series_and_target(tmp_path):
    config = {"series": ["10.0"], "packages": ["kicad"],
              "targets": [{"ubuntu": "24.04", "codename": "noble", "arch": "amd64"},
                          {"ubuntu": "24.04", "codename": "noble", "arch": "arm64"}]}
    pages = launchpad({"kicad": (V, KICAD)})
    arm = publications_url("kicad", arch="arm64")
    pages[arm] = pages[publications_url("kicad")]
    done, log = run_store(tmp_path, pages, Gh(), config=config)
    assert done == [(KEY, "stored"), ("kicad-10.0.8-ubuntu24.04-arm64", "stored")]


@pytest.mark.parametrize("config", [
    {}, {"series": ["10.0"], "targets": [], "packages": ["kicad"]}, {"series": [], "targets": [{}], "packages": ["kicad"]},
    {"series": ["10"], "targets": [{"ubuntu": "24.04", "codename": "noble", "arch": "amd64"}], "packages": ["kicad"]},
    {"series": ["10.0"], "targets": [{"ubuntu": "24.04"}], "packages": ["kicad"]},
    {"series": ["10.0"], "targets": [{"ubuntu": "24.04", "codename": "noble", "arch": "amd64"}], "packages": []},
    {"series": ["10.0"], "targets": "noble", "packages": ["kicad"]}, {"series": 10, "targets": [], "packages": ["kicad"]}])
def test_store_refuses_a_malformed_configuration(tmp_path, config):
    with pytest.raises(kc.CacheError):
        run_store(tmp_path, {}, Gh(), config=config)


def test_the_repositorys_own_configuration_is_valid():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    wanted, packages = kc.targets(json.load(open(os.path.join(root, "store.json"))))
    assert ("10.0", "24.04", "noble", "amd64") in wanted and "kicad" in packages and "kicad-demos" in packages


def test_run_command_runs():
    code, out, err = kc.run_command(["python3", "-c", "import sys; print('o'); sys.stderr.write('e'); sys.exit(3)"])
    assert (code, out.strip(), err) == (3, "o", "e")


# ---- this machine ---------------------------------------------------------------------------------------------------

def os_release(tmp_path, text='ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nNAME="Ubuntu"\n\njunk line\n'):
    path = tmp_path / "os-release"
    path.write_text(text)
    return str(path)


def dpkg(answer=(0, "amd64\n", "")):
    def run(argv):
        if isinstance(answer, Exception):
            raise answer
        return answer
    return run


def test_platform(tmp_path):
    assert kc.platform(os_release(tmp_path), dpkg()) == ("24.04", "noble", "amd64")


def test_platform_refuses_what_is_not_ubuntu(tmp_path):
    with pytest.raises(kc.CacheError, match="this is debian"):
        kc.platform(os_release(tmp_path, "ID=debian\nVERSION_ID=13\n"), dpkg())
    with pytest.raises(kc.CacheError, match="not Ubuntu"):
        kc.platform(os_release(tmp_path, ""), dpkg())
    with pytest.raises(kc.CacheError, match="cannot read"):
        kc.platform(str(tmp_path / "none"), dpkg())


@pytest.mark.parametrize("answer", [(1, "", "boom"), (0, " \n", ""), OSError("no dpkg")])
def test_platform_needs_dpkg(tmp_path, answer):
    with pytest.raises(kc.CacheError, match="dpkg"):
        kc.platform(os_release(tmp_path), dpkg(answer))


def installed(table):
    def run(argv):
        assert argv[:3] == ["dpkg-query", "-W", "-f=${db:Status-Abbrev}${Version}"]
        return table.get(argv[3], (1, "", "no packages found"))
    return run


def test_check_installed():
    table = {"kicad": (0, "ii " + V, ""), "kicad-demos": (0, "ii " + V, ""),
             "kicad-symbols": (0, "ii 10.0.7~ubuntu24.04.1", ""), "kicad-templates": (0, "rc " + V, "")}
    kc.check_installed("10.0.8", ["kicad", "kicad-demos"], installed(table))
    with pytest.raises(kc.CacheError, match="kicad-symbols 10.0.7~ubuntu24.04.1 is installed, not KiCad 10.0.8"):
        kc.check_installed("10.0.8", ["kicad", "kicad-symbols"], installed(table))
    with pytest.raises(kc.CacheError, match="kicad-footprints is not installed"):
        kc.check_installed("10.0.8", ["kicad-footprints"], installed(table))
    with pytest.raises(kc.CacheError, match="kicad-templates is not installed"):       # removed, config files left
        kc.check_installed("10.0.8", ["kicad-templates"], installed(table))
    with pytest.raises(kc.CacheError, match="cannot run dpkg-query"):
        kc.check_installed("10.0.8", ["kicad"], dpkg(OSError("none")))


# ---- command line ---------------------------------------------------------------------------------------------------

def cli(argv, pages=None, run=None):
    out = []
    code = kc.main(argv, net=net_for(Web(pages or {})), run=run or dpkg(), out=out.append)
    return code, out


def test_cli_get_writes_the_outputs(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("earlier=1\n")
    code, out = cli(["get", "--version", "10.0.8", "--packages", "kicad, kicad-demos", "--dest", str(tmp_path / "d"),
                     "--os-release", os_release(tmp_path), "--github-output", str(output)],
                    github({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}))
    assert code == 0 and out[-1] == "%s: 2 package file(s) in %s (store hit: true)" % (KEY, tmp_path / "d")
    assert output.read_text() == "earlier=1\nkey=%s\ncache-hit=true\n" % KEY


def test_cli_get_with_the_target_given_does_not_ask_the_machine(tmp_path):
    code, out = cli(["get", "--version", "10.0.8", "--dest", str(tmp_path / "d"), "--ubuntu", "24.04",
                     "--codename", "noble", "--arch", "amd64", "--no-fallback"],
                    github({"kicad": (V, KICAD)}), run=dpkg(OSError("must not be called")))
    assert code == 0


def test_cli_get_reports_a_miss_without_fallback_as_an_error(tmp_path):
    code, out = cli(["get", "--version", "10.0.8", "--dest", str(tmp_path / "d"), "--no-fallback",
                     "--os-release", os_release(tmp_path)])
    assert code == 1 and out == ["::error::not in the store under %s: kicad" % KEY]


def test_cli_key(tmp_path):
    assert cli(["key", "--version", "10.0.8", "--os-release", os_release(tmp_path)]) == (0, [KEY])
    assert cli(["key", "--version", "10.0.8", "--arch", "arm64", "--os-release", os_release(tmp_path)]) == (
        0, ["kicad-10.0.8-ubuntu24.04-arm64"])
    code, out = cli(["key", "--version", "10.0", "--os-release", os_release(tmp_path)])
    assert code == 1 and "full release number" in out[0]
    code, out = cli(["key", "--version", "10.0.8", "--os-release", os_release(tmp_path, "ID=ubuntu\n")])
    assert code == 1 and "Ubuntu release is missing" in out[0]


def test_cli_check():
    run = installed({"kicad": (0, "ii " + V, "")})
    assert cli(["check", "--version", "10.0.8"], run=run) == (0, ["KiCad 10.0.8 is installed (kicad)"])
    code, out = cli(["check", "--version", "10.0.9"], run=run)
    assert code == 1 and out == ["::error::kicad %s is installed, not KiCad 10.0.9" % V]


def test_cli_store(tmp_path):
    config = tmp_path / "store.json"
    config.write_text(json.dumps(CONFIG))
    gh = Gh()
    code, out = cli(["store", "--config", str(config), "--work", str(tmp_path / "w")],
                    launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), run=gh)
    assert code == 0 and out == ["%s: stored (kicad kicad-demos)" % KEY]
    assert gh.calls[1][4:6] == ["--repo", kc.DEFAULT_REPO]
    code, out = cli(["store", "--config", str(config), "--work", str(tmp_path / "w2"), "--version", "10.0.8",
                     "--repo", "me/mine"], launchpad({"kicad": (V, KICAD), "kicad-demos": (V, DEMOS)}), run=Gh())
    assert code == 0


def test_cli_store_with_a_bad_configuration_file(tmp_path):
    bad = tmp_path / "store.json"
    bad.write_text("{not json")
    code, out = cli(["store", "--config", str(bad), "--work", str(tmp_path / "w")])
    assert code == 1 and out[0].startswith("::error::cannot read")
    code, out = cli(["store", "--config", str(tmp_path / "none.json"), "--work", str(tmp_path / "w")])
    assert code == 1 and out[0].startswith("::error::cannot read")


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        cli([])


def test_store_series_none_done_is_reported_for_the_asked_version(tmp_path):
    """every target of another series is passed over, and that is an error rather than a silent success"""
    config = dict(CONFIG, series=["9.0"])
    with pytest.raises(kc.CacheError, match="no series 10.0"):
        run_store(tmp_path, {}, Gh(), config=config, version="10.0.8")
