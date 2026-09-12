"""Pure logic for expanding git submodules in a staged casket.

A GitHub codeload archive (/archive/<sha>.tar.gz) is built from `git archive`,
which writes a gitlink as an EMPTY DIRECTORY. Every casket tree that uses
submodules therefore ships the parent's build glue and none of the code: the
2026-08-19 fleet scan found 1052 trees carrying .gitmodules and all 2543 of
their submodule dirs empty. openshift/zero-trust-workload-identity-manager-
release is the worst shape of it -- the release repo is nothing BUT
Containerfiles plus five empty submodules, so the operator's CRDs and
controllers were absent from every casket that claimed to carry ZTWIM.

Two things are needed to fix it and only one is in the tarball:
  .gitmodules            path + url + branch      (in the archive)
  the pinned commit      mode 160000 tree entry   (NOT in the archive)
The pin comes from the tree API instead; where that is unavailable we fall
back to the .gitmodules branch head and mark the row APPROX, the same
exact/approx split phase-b-operand-fetch-source.sh already records.

Everything here is pure (no network, no filesystem) so it can be tested:
see tests/test_submodules.py.
"""
import posixpath
import re

GITHUB_HOSTS = ("github.com", "www.github.com")


class Submodule:
    """One [submodule] stanza from a .gitmodules file."""

    __slots__ = ("branch", "name", "path", "url")

    def __init__(self, name, path, url, branch=""):
        self.name = name
        self.path = path
        self.url = url
        self.branch = branch

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Submodule({self.name!r}, {self.path!r}, {self.url!r}, {self.branch!r})"

    def __eq__(self, other):
        return (isinstance(other, Submodule)
                and (self.name, self.path, self.url, self.branch)
                == (other.name, other.path, other.url, other.branch))


_SECTION = re.compile(r'^\[\s*submodule\s+"(?P<name>.*)"\s*\]\s*$')
_BARE_SECTION = re.compile(r'^\[\s*[^\]]+\s*\]\s*$')
_KEY = re.compile(r'^(?P<key>[A-Za-z0-9_-]+)\s*=\s*(?P<val>.*)$')


def parse_gitmodules(text):
    """Parse .gitmodules into Submodule objects, in file order.

    Tolerant on purpose: a stanza missing `path` or `url` is dropped rather
    than raised, because one malformed .gitmodules must never cost a casket
    the other 2542 submodules (same rule as deplib's per-manifest guard).
    """
    out, cur, name = [], None, ""

    def flush():
        if cur is not None and cur.get("path") and cur.get("url"):
            out.append(Submodule(name or cur["path"], cur["path"].strip("/"),
                                 cur["url"], cur.get("branch", "")))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = _SECTION.match(line)
        if m:
            flush()
            cur, name = {}, m.group("name")
            continue
        if _BARE_SECTION.match(line):
            # some other section ([core], ...) — close the current submodule
            flush()
            cur, name = None, ""
            continue
        if cur is None:
            continue
        m = _KEY.match(line)
        if m:
            key = m.group("key").lower()
            if key in ("path", "url", "branch"):
                cur[key] = m.group("val").strip().strip('"')
    flush()
    return out


def github_slug(url, parent_slug=""):
    """Map a submodule URL to "owner/repo", or "" when it is not on GitHub.

    Handles the three forms that actually occur in the fleet --
    https://github.com/o/r(.git), git@github.com:o/r.git, and relative
    ../r.git resolved against the parent -- and deliberately returns "" for
    everything else (9 gitlab.com refs today). Callers report the "" rows in
    SUBMODULES-uncovered.txt; they must never be dropped silently.
    """
    if not url:
        return ""
    u = url.strip()

    if u.startswith(("./", "../")):
        if not parent_slug:
            return ""
        # relative to the parent repo's location on the same host
        joined = posixpath.normpath(posixpath.join(parent_slug, u))
        if joined.startswith(".."):
            return ""
        u = "https://github.com/" + joined

    m = re.match(r'^[A-Za-z0-9_+.-]+@([^:/]+):(.+)$', u)   # scp-style
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r'^(?:[A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$', u)
        if not m:
            return ""
        host, path = m.group(1), m.group(2)

    if host.lower() not in GITHUB_HOSTS:
        return ""
    parts = [p for p in path.strip("/").removesuffix(".git").split("/") if p]
    if len(parts) < 2:
        return ""
    return "/".join(parts[:2])


def gitlinks_from_tree(tree_json):
    """{path: sha} for every mode-160000 entry of a git trees API response.

    This is the only place the pinned commit exists: the contents API reports
    a submodule as type "file" and the codeload archive omits it entirely.
    """
    out = {}
    for e in (tree_json or {}).get("tree", []) or []:
        if str(e.get("mode")) == "160000" and e.get("path") and e.get("sha"):
            out[e["path"].strip("/")] = e["sha"]
    return out


class Pin:
    """A submodule resolved to something fetchable."""

    __slots__ = ("exact", "path", "ref", "slug", "url")

    def __init__(self, path, slug, ref, exact, url):
        self.path = path
        self.slug = slug
        self.ref = ref
        self.exact = exact
        self.url = url

    def __eq__(self, other):
        return (isinstance(other, Pin)
                and (self.path, self.slug, self.ref, self.exact, self.url)
                == (other.path, other.slug, other.ref, other.exact, other.url))

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"Pin({self.path!r}, {self.slug!r}, {self.ref!r}, "
                f"exact={self.exact}, {self.url!r})")


def plan_submodules(entries, gitlinks, parent_slug="", allow_branch=True):
    """(pins, skipped) for one tree's submodules.

    `gitlinks` is gitlinks_from_tree() output for the parent commit; an empty
    dict is the normal offline/rate-limited case, not an error. A submodule
    with no gitlink falls back to its .gitmodules branch and is marked
    exact=False so the row reads APPROX downstream -- a branch head is
    whatever it is today, which is emphatically not the commit that was built.
    """
    pins, skipped = [], []
    for sm in entries:
        slug = github_slug(sm.url, parent_slug)
        if not slug:
            skipped.append((sm.path, sm.url, "not-github"))
            continue
        sha = gitlinks.get(sm.path)
        if sha:
            pins.append(Pin(sm.path, slug, sha, True, archive_url(slug, sha)))
        elif allow_branch and sm.branch and sm.branch != ".":
            pins.append(Pin(sm.path, slug, sm.branch, False,
                            archive_url(slug, sm.branch)))
        else:
            skipped.append((sm.path, sm.url, "no-pinned-commit"))
    return pins, skipped


def archive_url(slug, ref):
    return f"https://github.com/{slug}/archive/{ref}.tar.gz"


def tree_api_url(slug, ref):
    return f"https://api.github.com/repos/{slug}/git/trees/{ref}?recursive=1"
