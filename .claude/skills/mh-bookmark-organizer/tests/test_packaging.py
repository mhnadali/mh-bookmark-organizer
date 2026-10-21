"""What gets published: the project metadata, the file lists of the sdist and the wheel, and the release gate.

The metadata tests need ``tomllib`` (Python 3.11+); the build tests need the ``build`` and ``setuptools`` packages (the CI ``package`` job and
``release`` workflow have them; a plain test environment does not, and they are skipped there).  ``MHBM_RELEASE=1`` turns on the checks that
must hold before anything is uploaded to PyPI - at the moment that means a licence, which only the owner of the repository can choose.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import common  # noqa: F401
import mhbm

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]
try:
    import tomllib
except ImportError:                      # Python 3.9 and 3.10
    tomllib = None
HAS_BUILD = bool(importlib.util.find_spec("build") and importlib.util.find_spec("setuptools"))
LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"mhbm_tool_{name}", SKILL / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(tomllib, "needs tomllib (Python 3.11+)")
class Metadata(unittest.TestCase):
    def setUp(self):
        self.cfg = tomllib.loads((SKILL / "pyproject.toml").read_text(encoding="utf-8"))
        self.project = self.cfg["project"]

    def test_the_engine_has_no_runtime_dependencies(self):
        self.assertNotIn("dependencies", self.project)
        self.assertEqual(set(self.project["optional-dependencies"]), {"fast"})
        self.assertTrue(all(d.startswith("numpy") for d in self.project["optional-dependencies"]["fast"]))

    def test_python_versions_and_classifiers_agree_with_the_ci_matrix(self):
        self.assertEqual(self.project["requires-python"], ">=3.9")
        self.assertEqual(self.cfg["tool"]["ruff"]["target-version"], "py39")
        versions = {c.rsplit(" ", 1)[1] for c in self.project["classifiers"] if re.search(r"Python :: 3\.\d+$", c)}
        self.assertEqual(versions, {"3.9", "3.10", "3.11", "3.12", "3.13"})
        workflow = REPO / ".github" / "workflows" / "test.yml"
        if workflow.exists():
            for v in versions:
                self.assertIn(f'"{v}"', workflow.read_text(encoding="utf-8"), f"CI does not test Python {v}")

    def test_console_scripts_point_at_real_functions(self):
        scripts = self.project["scripts"]
        self.assertEqual(set(scripts), {"mhbm", "mhbm-mcp"})
        for target in scripts.values():
            module, func = target.split(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), target)

    def test_urls_and_the_version_source(self):
        for key in ("Homepage", "Source", "Issues", "Changelog"):
            self.assertRegex(self.project["urls"][key], r"^https://github\.com/mhnadali/mh-bookmark-organizer")
        self.assertEqual(self.project["dynamic"], ["version"])
        self.assertEqual(self.cfg["tool"]["setuptools"]["dynamic"]["version"], {"attr": "mhbm.__version__"})
        self.assertRegex(mhbm.__version__, r"^\d+\.\d+\.\d+((a|b|rc)\d+)?$")

    def test_no_licence_is_claimed_that_does_not_exist(self):
        """Licence metadata and the LICENSE file go in together, when the owner has chosen."""
        has_file = any((SKILL / n).exists() for n in LICENSE_FILES)
        claimed = "license" in self.project or "license-files" in self.project or any(c.startswith("License ::") for c in self.project["classifiers"])
        self.assertEqual(claimed, has_file, "add the licence metadata together with the LICENSE file (or remove both)")

    def test_readme_has_no_relative_links_because_pypi_cannot_resolve_them(self):
        text = (SKILL / "README.md").read_text(encoding="utf-8")
        relative = [u for u in re.findall(r"\]\(([^)\s]+)\)", text) if not re.match(r"^(https?://|mailto:|#)", u)]
        self.assertEqual(relative, [])
        self.assertEqual(self.project["readme"], "README.md")

    def test_every_file_the_manifest_names_exists(self):
        for line in (SKILL / "MANIFEST.in").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[0] == "include":
                for pattern in parts[1:]:
                    if "*" not in pattern:
                        self.assertTrue((SKILL / pattern).exists(), f"MANIFEST.in includes {pattern}, which is missing")


@unittest.skipUnless(os.environ.get("MHBM_RELEASE") == "1", "the release gate is checked by the release workflow (MHBM_RELEASE=1)")
@unittest.skipUnless(tomllib, "needs tomllib (Python 3.11+)")
class ReleaseGate(unittest.TestCase):
    def test_a_licence_file_and_licence_metadata_exist(self):
        project = tomllib.loads((SKILL / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertTrue(any((SKILL / n).exists() for n in LICENSE_FILES),
                        "nothing is uploaded without a LICENSE file in the skill folder (the build root; a copy at the repository root is not in the "
                        "sdist or the wheel): the owner of the repository has to choose the licence")
        self.assertTrue("license" in project or "license-files" in project, "pyproject.toml must name the licence")


class VersionAgreement(unittest.TestCase):
    def setUp(self):
        self.tool = load_tool("release_check")

    def test_the_current_tree_agrees_with_its_own_tag(self):
        version, found = self.tool.problems(f"v{mhbm.__version__}", SKILL)
        self.assertEqual((version, found), (mhbm.__version__, []))
        notes = self.tool.changelog_section(mhbm.__version__, SKILL)
        self.assertTrue(notes and len(notes) > 40)

    def test_disagreements_are_each_reported(self):
        root = Path(tempfile.mkdtemp(prefix="mhbm-rel-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("scripts/mhbm/__init__.py", "extension/manifest.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "README.md", "CHANGELOG.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SKILL / rel, root / rel)
        self.assertEqual(self.tool.problems(f"v{mhbm.__version__}", root)[1], [])
        for tag in ("1.2.3", "v1.2", "vx.y.z", "v1.2.3-beta"):
            self.assertEqual(len(self.tool.problems(tag, root)[1]), 1, tag)
        _, found = self.tool.problems("v9.9.9", root)
        self.assertEqual(len(found), 6, found)                          # code, extension manifest, both plugin manifests, README, CHANGELOG
        manifest = root / "extension" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["version"] = "0.0.1"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(len(self.tool.problems(f"v{mhbm.__version__}", root)[1]), 1)

    def test_versions_match_whole_not_by_prefix(self):
        root = Path(tempfile.mkdtemp(prefix="mhbm-rel2-"))
        self.addCleanup(shutil.rmtree, root, True)
        for rel in ("scripts/mhbm/__init__.py", "extension/manifest.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SKILL / rel, root / rel)
        v = mhbm.__version__
        (root / "README.md").write_text(f"Version {v}0 only\n")                       # 1.5.20 is not 1.5.2
        (root / "CHANGELOG.md").write_text(f"## {v}.1\n\nx\n\n## {v}-rc\n\ny\n")      # neither is 1.5.2.1 or 1.5.2-rc
        found = self.tool.problems(f"v{v}", root)[1]
        self.assertEqual(len(found), 2, found)
        self.assertIsNone(self.tool.changelog_section(v, root))
        (root / "README.md").write_text(f"Version {v}.\n")
        (root / "CHANGELOG.md").write_text(f"## {v} (2026)\n\nreal notes\n\n## {v}.1\n\nother\n")
        self.assertEqual(self.tool.problems(f"v{v}", root)[1], [])
        self.assertEqual(self.tool.changelog_section(v, root), "real notes")

    def test_post_and_dev_versions_are_not_release_tags(self):
        for tag in ("v1.6.0.post1", "v1.6.0.dev1", "v1.6.0-rc1"):
            self.assertEqual(len(self.tool.problems(tag, SKILL)[1]), 1, tag)

    def test_pre_release_tags_are_understood(self):
        for tag in ("v1.6.0rc1", "v2.0.0a3", "v1.0.0b2"):
            self.assertEqual(self.tool.TAG.match(tag).group(1), tag[1:])


@unittest.skipUnless(HAS_BUILD, "needs the build and setuptools packages")
class Artifacts(unittest.TestCase):
    """Build once, look inside."""

    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="mhbm-dist-")
        src = tempfile.mkdtemp(prefix="mhbm-src-")          # build from a copy: nothing is written into the checkout
        cls.src = Path(src) / "skill"
        shutil.copytree(SKILL, cls.src, ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info", ".coverage*"))
        env = dict(os.environ, SOURCE_DATE_EPOCH="1760000000", PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", cls.out, str(cls.src)], capture_output=True, text=True, env=env, timeout=600)
        if p.returncode:
            raise AssertionError(p.stdout[-2000:] + p.stderr[-2000:])
        cls.wheel = next(Path(cls.out).glob("*.whl"))
        cls.sdist = next(Path(cls.out).glob("*.tar.gz"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)
        shutil.rmtree(cls.src.parent, True)

    def test_file_names_carry_the_version(self):
        self.assertEqual(self.wheel.name, f"mh_bookmark_organizer-{mhbm.__version__}-py3-none-any.whl")
        self.assertEqual(self.sdist.name, f"mh_bookmark_organizer-{mhbm.__version__}.tar.gz")

    def test_wheel_is_the_engine_and_nothing_else(self):
        names = zipfile.ZipFile(self.wheel).namelist()
        top = {n.split("/")[0] for n in names}
        self.assertEqual(top, {"mhbm", f"mh_bookmark_organizer-{mhbm.__version__}.dist-info"})
        for must in ("mhbm/__main__.py", "mhbm/cli.py", "mhbm/data/taxonomy.json", "mhbm/mcp/server.py", "mhbm/mcp/__main__.py", "mhbm/commands/ops.py"):
            self.assertIn(must, names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc") or "/tests/" in n])
        entry = zipfile.ZipFile(self.wheel).read(f"mh_bookmark_organizer-{mhbm.__version__}.dist-info/entry_points.txt").decode()
        self.assertIn("mhbm = mhbm.cli:entry", entry)
        self.assertIn("mhbm-mcp = mhbm.mcp.cli:main", entry)

    def test_wheel_metadata(self):
        meta = zipfile.ZipFile(self.wheel).read(f"mh_bookmark_organizer-{mhbm.__version__}.dist-info/METADATA").decode()
        self.assertIn("Requires-Python: >=3.9", meta)
        self.assertEqual(re.findall(r"(?m)^Requires-Dist: (.*)$", meta), ['numpy>=1.21; extra == "fast"'])
        self.assertIn("Provides-Extra: fast", meta)
        self.assertIn(f"Version: {mhbm.__version__}", meta)
        self.assertIn("Description-Content-Type: text/markdown", meta)

    def test_sdist_is_complete_enough_to_run_the_tests_and_load_the_extension_and_plugin(self):
        with tarfile.open(self.sdist) as tar:
            names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
        for must in ("pyproject.toml", "README.md", "CHANGELOG.md", "SKILL.md", "MANIFEST.in", "extension/manifest.json", "extension/background.js",
                     ".claude-plugin/plugin.json", ".mcp.json", ".codex-plugin/plugin.json", ".codex-mcp.json",
                     "scripts/codex_mcp.py", "codex/skills/mh-bookmark-organizer/SKILL.md", "codex/skills/mh-bookmark-organizer/agents/openai.yaml", "agents/mh-bookmark-organizer.md", "tests/common.py", "tests/fixtures/messy-library.html",
                     "tests/fixtures/mcp-golden/session-2025-11-25.jsonl", "tools/release_check.py", "scripts/mhbm/mcp/registry.py", "requirements-release.txt"):
            self.assertIn(must, names)
        self.assertFalse([n for n in names if any(x in n for x in ("node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"))])

    def test_a_second_build_is_byte_identical(self):
        again = tempfile.mkdtemp(prefix="mhbm-dist2-")
        self.addCleanup(shutil.rmtree, again, True)
        env = dict(os.environ, SOURCE_DATE_EPOCH="1760000000", PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", again, str(self.src)], capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertEqual(next(Path(again).glob("*.whl")).read_bytes(), self.wheel.read_bytes())


if __name__ == "__main__":
    unittest.main()
