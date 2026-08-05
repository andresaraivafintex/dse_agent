"""The Tester's copy of the sandbox workspace must not evict the worker.

Real incident, 2026-08-05 on the Angular testbed: `run_tester_turn` ran
`kubectl cp <pod>:/workspace <local>` into the orchestrator's own /tmp. After
the L1 gate's `npm ci`, that tree carries the whole `node_modules`; /tmp is an
emptyDir with `sizeLimit: 256Mi`, so kubelet EVICTED the Pod mid-activity —
five times in five minutes, each replacement refilling the volume. Temporal
reported it as "Worker is shutting down and this activity did not complete in
time", which points nowhere near the cause.

Nothing downstream ever wanted those bytes: `_tester_repo_context` walks the
tree and skips `/node_modules` and `/.git` explicitly.

The archive is also untrusted — it is produced inside the Pod that just ran
model-authored code against the customer's repository — so extraction is
checked, not assumed.
"""
from __future__ import annotations

import io
import os
import tarfile

from sandbox_runtime.activities import _safe_extract, _tester_repo_context


def _tar_with(members: list[tuple[str, bytes]]) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


def _tar_with_link(name: str, target: str, kind: str) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE
        info.linkname = target
        tf.addfile(info)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r")


# ---------------------------------------------------------------------------
# The archive comes out of the sandbox. Treat it as hostile.
# ---------------------------------------------------------------------------
def test_a_member_escaping_the_destination_is_refused(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    outside = tmp_path / "pwned"
    _safe_extract(_tar_with([("../pwned", b"owned")]), str(dest))
    assert not outside.exists(), "a ../ member escaped the destination"


def test_an_absolute_member_is_refused(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    _safe_extract(_tar_with([("/etc/dse-pwned", b"owned")]), str(dest))
    assert not os.path.exists("/etc/dse-pwned")


def test_a_symlink_is_dropped_rather_than_recreated(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    _safe_extract(_tar_with_link("link-to-root", "/", "sym"), str(dest))
    assert not (dest / "link-to-root").exists()
    assert not (dest / "link-to-root").is_symlink()


def test_a_hardlink_is_dropped(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    _safe_extract(_tar_with_link("hard", "/etc/passwd", "lnk"), str(dest))
    assert not (dest / "hard").exists()


def test_ordinary_files_still_arrive(tmp_path):
    dest = tmp_path / "ws"
    dest.mkdir()
    _safe_extract(
        _tar_with([("./package.json", b'{"name":"x"}'), ("./src/a.spec.ts", b"it()")]),
        str(dest),
    )
    assert (dest / "package.json").read_bytes() == b'{"name":"x"}'
    assert (dest / "src" / "a.spec.ts").read_bytes() == b"it()"


# ---------------------------------------------------------------------------
# The bytes we stopped shipping are the bytes nobody read.
# ---------------------------------------------------------------------------
def test_the_context_reader_ignores_the_two_excluded_trees(tmp_path):
    """Pins the premise of the exclusion: if this ever starts reading
    node_modules or .git, excluding them from the stream becomes wrong."""
    ws = tmp_path / "ws"
    (ws / "node_modules" / "left-pad").mkdir(parents=True)
    (ws / "node_modules" / "left-pad" / "index.spec.js").write_text("vendored")
    (ws / ".git").mkdir()
    (ws / ".git" / "config.spec.js").write_text("internal")
    (ws / "src").mkdir()
    (ws / "src" / "real.spec.ts").write_text("the repo's own test")
    (ws / "package.json").write_text('{"name":"app"}')

    pkg, example, existing = _tester_repo_context(str(ws))

    assert pkg == '{"name":"app"}'
    assert existing == {os.path.join("src", "real.spec.ts")}, (
        "a vendored or internal file leaked into the tester's context"
    )
    assert "vendored" not in example and "internal" not in example
