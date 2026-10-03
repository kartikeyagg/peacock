"""
The real-world corpus: one large, popular repository per supported language.

This is not a test module. It is the manifest plus the download/cache helper
that `test_corpus.py` runs against.

Why real repositories at all, when `test_languages.py` already pins hand-picked
excerpts? Because the failures this parser has actually shipped were never
visible in a ten-line snippet. A `/*` inside a string literal that swallowed
107 files, a directory-name rule that deleted 245 of them, indentation acting
as a return type and fabricating 25,002 symbols — every one of those needed
volume and variety to show up. Snippets prove the regexes match what they were
written for; a corpus proves they do not match everything else.

Selection rule (the user-facing constraint this suite was written to): each
repository has more than 2,000 GitHub stars and more than 20,000 lines of code
in its language. Star counts are recorded below as of the pin date and are not
asserted — they are provenance, not behaviour. The line count *is* asserted,
from the checkout, so the claim cannot quietly stop being true.

Every repo is pinned to a commit SHA. An unpinned corpus is a test whose
expected values change when someone else pushes.

    PEACOCK_CORPUS=1 python3 -m unittest discover -s tests

Downloads (~600 MB) land in ``~/.cache/peacock-corpus`` and are reused; set
``PEACOCK_CORPUS_DIR`` to move them. Nothing here imports anything outside the
standard library, same rule as the tool.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import urllib.request

CACHE_ROOT = os.path.expanduser(
    os.environ.get("PEACOCK_CORPUS_DIR", "~/.cache/peacock-corpus"))

# Set PEACOCK_CORPUS=1 to opt in. The suite is skipped by default because a
# test that silently needs the network is a test that fails for reasons that
# have nothing to do with the code under it.
ENABLED = os.environ.get("PEACOCK_CORPUS", "").lower() in ("1", "true", "yes")

# Only download what is being asked for: PEACOCK_CORPUS_ONLY=go,rust
ONLY = {s.strip().lower() for s in
        os.environ.get("PEACOCK_CORPUS_ONLY", "").split(",") if s.strip()}


class Repo:
    """One pinned upstream repository, and what the parser must make of it.

    ``anchors`` are the spot checks: a file that exists at this SHA and a
    symbol the parser has to find in it. Aggregates alone can stay green while
    a language quietly degrades — 90% of files still yielding *a* symbol says
    nothing about whether the right ones came out. An anchor names a specific
    declaration, in a specific shape (generic, annotated, wrapped, extension
    method), and fails loudly when that shape stops parsing.
    """

    def __init__(self, language, slug, sha, stars, min_loc, exts,
                 anchors=(), note=""):
        self.language = language
        self.slug = slug              # "owner/name" on github.com
        self.sha = sha
        self.stars = stars            # thousands, at pin date; provenance only
        self.min_loc = min_loc        # asserted against the real checkout
        self.exts = exts              # extensions that count as this language
        self.anchors = anchors        # ((path, symbol, kind), ...)
        self.note = note

    @property
    def name(self):
        return self.slug.split("/")[-1]

    @property
    def dir_name(self):
        return f"{self.slug.replace('/', '__')}-{self.sha[:12]}"

    @property
    def url(self):
        return f"https://codeload.github.com/{self.slug}/tar.gz/{self.sha}"

    def __repr__(self):
        return f"<Repo {self.language} {self.slug}@{self.sha[:7]}>"


# Pinned 2026-08-10. Star counts rounded, from github.com on that date.
REPOS = [
    Repo("python", "django/django",
         "b5388a3a80cafcce2e34196d8e81cf5b48eb33bb", 84, 200_000, [".py"],
         anchors=[
             ("django/http/response.py", "HttpResponse", "class"),
             ("django/urls/base.py", "reverse", "function"),
             ("django/db/models/query.py", "QuerySet", "class"),
         ]),
    Repo("javascript", "facebook/react",
         "8366f3389d3717d718b447ebff0e6a785c7e2d4d", 240, 100_000,
         [".js", ".jsx", ".mjs", ".cjs"],
         anchors=[
             ("packages/react/src/ReactHooks.js", "useState", "function"),
             ("packages/react/src/ReactHooks.js", "useContext", "function"),
         ]),
    Repo("typescript", "nestjs/nest",
         "5df8d9d41a98fd7f587b2e043a12b3d5109971e4", 70, 40_000,
         [".ts", ".tsx"],
         anchors=[
             ("packages/core/nest-factory.ts", "NestFactoryStatic", "class"),
             ("packages/common/decorators/core/injectable.decorator.ts",
              "Injectable", "function"),
         ]),
    Repo("java", "spring-projects/spring-boot",
         "86f1eac082d88b7b368b693fe7db279971d22020", 78, 400_000, [".java"],
         anchors=[
             ("core/spring-boot/src/main/java/org/springframework/boot/"
              "SpringApplication.java", "SpringApplication", "class"),
             # the method that was invisible until wrapped signatures folded
             ("core/spring-boot/src/main/java/org/springframework/boot/"
              "SpringApplication.java", "prepareEnvironment", "function"),
         ],
         note="the repo docs/06 measures; wrapped signatures live here"),
    Repo("kotlin", "square/okhttp",
         "8d446ec84c20bbcd599ab9858e2d36aaad5d5247", 46, 40_000,
         [".kt", ".kts"],
         anchors=[
             ("okhttp/src/commonJvmAndroid/kotlin/okhttp3/OkHttpClient.kt",
              "OkHttpClient", "class"),
         ]),
    Repo("go", "gohugoio/hugo",
         "8a55df7af2e6da31297245cc54fa2e3b521d93e8", 82, 150_000, [".go"],
         anchors=[
             ("commands/hugobuilder.go", "hugoBuilder", "class"),
         ]),
    Repo("rust", "tokio-rs/tokio",
         "af9376300907dd187e0fdca793ccda2fa62de5ec", 29, 100_000, [".rs"],
         anchors=[
             ("tokio/src/runtime/runtime.rs", "Runtime", "class"),
             ("tokio/src/sync/mutex.rs", "Mutex", "class"),
         ]),
    Repo("c", "redis/redis",
         "4f20cb48934463db5970bd476461b2d57af3f38d", 70, 150_000,
         [".c", ".h"],
         anchors=[
             ("src/server.c", "main", "function"),
         ]),
    Repo("cpp", "bitcoin/bitcoin",
         "1d386c250f222ad12b61a530af1ad673b3621ad5", 84, 200_000,
         [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".c++"],
         anchors=[
             ("src/dbwrapper.cpp", "CBitcoinLevelDBLogger", "class"),
             ("src/util/strencodings.cpp", "IsHex", "function"),
         ],
         note="bitcoin puts its C++ headers in .h, which is C's extension"),
    Repo("csharp", "jellyfin/jellyfin",
         "e6e3099da9fb82feda846f2fa8cdadcc8738ad46", 39, 200_000, [".cs"],
         anchors=[
             ("Emby.Server.Implementations/ApplicationHost.cs",
              "ApplicationHost", "class"),
         ]),
    Repo("ruby", "rails/rails",
         "cf6d592c55b443ea2aabf31052ac424a204cd9f2", 57, 200_000, [".rb"],
         anchors=[
             ("actionpack/lib/action_controller/base.rb",
              "ActionController", "class"),
         ]),
    Repo("php", "laravel/framework",
         "c223b1be83994680005bf9367888719cecb068ed", 33, 150_000, [".php"],
         anchors=[
             ("src/Illuminate/Support/Str.php", "Str", "class"),
         ]),
    Repo("swift", "Alamofire/Alamofire",
         "0455bfb650893e86ad07ace16e5f2d36dadf46f4", 42, 20_000, [".swift"],
         anchors=[
             ("Source/Core/Session.swift", "Session", "class"),
         ]),
    Repo("scala", "scala/scala",
         "5de9bf89cd861b232ffb5baeb83051f140e18204", 15, 200_000,
         [".scala", ".sc"],
         anchors=[
             ("src/library/scala/Option.scala", "Option", "class"),
         ]),
    Repo("dart", "flutter/packages",
         "1861b68e9695df1f592bd1b922888cdc120d1d7a", 4, 200_000, [".dart"],
         anchors=[
             ("packages/camera/camera/lib/src/camera_controller.dart",
              "CameraController", "class"),
         ]),
    Repo("shell", "ohmyzsh/ohmyzsh",
         "97b27bb2ec0701330b18c2d3e340b22e742b3fa8", 180, 20_000,
         [".sh", ".bash", ".zsh"],
         anchors=[
             ("tools/theme_chooser.sh", "theme_preview", "function"),
         ],
         note="house style is `_omz::name()`, which the parser cannot see"),
    Repo("lua", "Kong/kong",
         "fa9c3b695af72668f135cb17bbb84a8b4dc511d2", 41, 150_000, [".lua"],
         anchors=[
             ("kong/init.lua", "Kong.init", "function"),
         ]),
    Repo("r", "tidyverse/ggplot2",
         "6870419aa6e106c3580c45c81d5b688cb31758bd", 7, 20_000, [".r", ".R"],
         anchors=[
             ("R/aes-delayed-eval.R", "after_stat", "function"),
             ("R/annotation-custom.R", "annotation_custom", "function"),
         ]),
    Repo("elixir", "elixir-lang/elixir",
         "024beb1bfcca0a24e859b2586059dd803a086615", 25, 100_000,
         [".ex", ".exs"],
         anchors=[
             ("lib/elixir/lib/enum.ex", "Enum", "class"),
         ]),
]

BY_LANGUAGE = {r.language: r for r in REPOS}


def selected():
    """The repos this run should touch, honouring PEACOCK_CORPUS_ONLY."""
    if not ONLY:
        return list(REPOS)
    return [r for r in REPOS if r.language in ONLY or r.name.lower() in ONLY]


def _safe_extract(tar, dest):
    """Extract without letting a member escape `dest`.

    Python 3.12 ships the `data` filter for exactly this; on older versions the
    check is done by hand rather than trusted away.
    """
    try:
        tar.extractall(dest, filter="data")            # 3.12+
        return
    except TypeError:
        pass
    dest_abs = os.path.abspath(dest)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(dest, member.name))
        if not target.startswith(dest_abs + os.sep):
            raise RuntimeError(f"tar member escapes destination: {member.name}")
        if member.issym() or member.islnk():
            link = os.path.abspath(
                os.path.join(os.path.dirname(target), member.linkname))
            if not link.startswith(dest_abs + os.sep):
                raise RuntimeError(f"unsafe link: {member.name}")
    tar.extractall(dest)


def ensure_repo(repo, log=lambda *_: None):
    """Return the local path to `repo`, downloading it once if needed.

    The marker file is written last and is what "present" means. A directory
    that exists because an extraction died halfway would otherwise be reused
    forever, and every downstream count would be wrong by however much of the
    tree is missing.
    """
    dest = os.path.join(CACHE_ROOT, repo.dir_name)
    marker = os.path.join(dest, ".peacock-corpus-ok")
    if os.path.exists(marker):
        return dest
    if os.path.isdir(dest):
        shutil.rmtree(dest)

    os.makedirs(CACHE_ROOT, exist_ok=True)
    log(f"fetching {repo.slug}@{repo.sha[:7]} ...")
    staging = tempfile.mkdtemp(prefix="peacock-corpus-", dir=CACHE_ROOT)
    try:
        tarball = os.path.join(staging, "src.tar.gz")
        req = urllib.request.Request(
            repo.url, headers={"User-Agent": "peacock-tests"})
        with urllib.request.urlopen(req, timeout=180) as resp, \
                open(tarball, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with tarfile.open(tarball, "r:gz") as tar:
            _safe_extract(tar, staging)
        os.remove(tarball)

        # GitHub tarballs wrap everything in one <name>-<sha> directory.
        entries = [e for e in os.listdir(staging) if e != "src.tar.gz"]
        if len(entries) != 1:
            raise RuntimeError(f"unexpected tarball layout: {entries}")
        inner = os.path.join(staging, entries[0])
        with open(os.path.join(inner, ".peacock-corpus-ok"), "w") as fh:
            fh.write(f"{repo.slug} {repo.sha}\n")
        os.rename(inner, dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return dest


def cached_path(repo):
    """Where `repo` lives if it is already downloaded, else None."""
    dest = os.path.join(CACHE_ROOT, repo.dir_name)
    return dest if os.path.exists(os.path.join(dest, ".peacock-corpus-ok")) else None


if __name__ == "__main__":                          # pre-warm the cache
    import sys
    wanted = selected()
    for r in wanted:
        path = ensure_repo(r, log=lambda m: print(m, file=sys.stderr))
        print(f"{r.language:11} {r.slug:28} {path}")
