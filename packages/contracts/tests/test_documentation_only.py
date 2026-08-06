"""One list, two consumers.

The L1 gates ask "can this change break me"; the workflow asks "does the Tester
have anything to test". They must reach the SAME answer, and they used not to:
the Tester ran unconditionally, authored a `.spec.ts`, the checkpoint committed
it, and the change stopped being documentation-only — so L1's skip could never
fire. The optimisation disabled itself.
"""
from __future__ import annotations

import pytest

from dse_contracts.paths import DOC_EXTENSIONS, is_documentation_only


@pytest.mark.parametrize(
    "files",
    [
        {"CONTRIBUTING.md"},
        {"README.md", "docs/guide.rst"},
        {"notes.txt", "adr/0001.adoc", "site/page.mdx"},
    ],
)
def test_documentation_is_recognised(files):
    assert is_documentation_only(files) is True


@pytest.mark.parametrize(
    "files, why",
    [
        ({"src/app/fee.service.ts"}, "source"),
        ({"CONTRIBUTING.md", "src/app/fee.service.ts"}, "one source file is enough"),
        ({"package-lock.json"}, "a dependency bump breaks builds"),
        ({"tsconfig.json"}, "decides what typechecks"),
        ({"Dockerfile"}, "no extension, can affect anything"),
        ({"Makefile"}, "no extension"),
        ({"ci/entrypoint"}, "no extension"),
        (set(), "an empty change is not a licence to skip"),
        (None, "unknown scope means do the work"),
    ],
)
def test_everything_else_means_do_the_work(files, why):
    assert is_documentation_only(files) is False, why


def test_the_extension_list_is_the_only_one():
    """If a second copy appears anywhere, this is the one that is wrong."""
    assert ".md" in DOC_EXTENSIONS
    assert ".ts" not in DOC_EXTENSIONS and ".json" not in DOC_EXTENSIONS


def test_case_and_path_separators_do_not_fool_it():
    assert is_documentation_only({"docs/README.MD"}) is True
    assert is_documentation_only({"docs\\guide.rst"}) is True
    assert is_documentation_only({"docs\\app.ts"}) is False


def test_a_dotfile_without_a_real_extension_is_not_documentation():
    """`.gitignore` splits to an extension of "gitignore", which is not in the
    list — but the reasoning must not depend on that accident, so pin it."""
    assert is_documentation_only({".gitignore"}) is False
    assert is_documentation_only({".eslintrc"}) is False
