"""Things about the repository itself that must not quietly regress: pinned CI, one version everywhere, a security policy."""
import json
import re
import unittest
from pathlib import Path

import common  # noqa: F401
import mhbm

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "test.yml"
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))


@unittest.skipUnless(WORKFLOW.exists(), "not running from a checkout of the repository")
class Pins(unittest.TestCase):
    def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(self):
        self.assertIn("release.yml", [w.name for w in WORKFLOWS])
        for workflow in WORKFLOWS:
            uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", workflow.read_text(), re.M)
            self.assertTrue(uses, workflow.name)
            for ref, rest in uses:
                with self.subTest(f"{workflow.name}: {ref}"):
                    self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", "pin actions by commit SHA, not by a movable tag")
                    self.assertRegex(rest, r"#\s*v\d+(\.\d+)*", "keep the human-readable version next to the SHA (Dependabot reads it)")

    def test_workflows_are_least_privilege(self):
        for workflow in WORKFLOWS:
            text = workflow.read_text()
            with self.subTest(workflow.name):
                self.assertRegex(text, r"(?m)^permissions:\n  contents: read$")
                self.assertNotIn("pull_request_target", text)
                self.assertEqual(text.count("persist-credentials: false"), text.count("actions/checkout@"))

    def test_only_the_release_jobs_that_publish_get_to_write_anything(self):
        """id-token: write only in the attestation job and the two upload jobs (each bound to an environment); contents: write only for the GitHub release."""
        text = (REPO / ".github" / "workflows" / "release.yml").read_text()
        jobs = re.split(r"(?m)^  (?=[a-z][\w-]*:\n)", text.split("\njobs:\n", 1)[1])
        self.assertGreaterEqual(len(jobs), 8)             # the split found the jobs
        for job in jobs:
            name = job.split(":", 1)[0]
            if "id-token: write" in job:
                self.assertIn(name, {"attest", "publish-testpypi", "publish-pypi"}, name)
                if name != "attest":
                    self.assertRegex(job, r"environment:", name)
            if "contents: write" in job:
                self.assertEqual(name, "github-release")
        self.assertNotIn("id-token", (REPO / ".github" / "workflows" / "test.yml").read_text())

    def test_dev_tools_are_pinned(self):
        lines = [x.strip() for x in (SKILL / "requirements-dev.txt").read_text().splitlines() if x.strip() and not x.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[\w.-]+==\d+(\.\d+)*$", line)

    def test_dependabot_covers_actions_and_tools(self):
        text = (REPO / ".github" / "dependabot.yml").read_text()
        self.assertIn("package-ecosystem: github-actions", text)
        self.assertIn("package-ecosystem: pip", text)

    def test_security_policy_exists_and_says_how_to_report(self):
        text = (REPO / "SECURITY.md").read_text()
        self.assertIn("Report a vulnerability", text)
        self.assertIn("Supported versions", text)


class OneVersion(unittest.TestCase):
    def test_version_is_the_same_everywhere(self):
        v = mhbm.__version__
        self.assertEqual(json.loads((SKILL / "extension" / "manifest.json").read_text())["version"], v)
        self.assertRegex((SKILL / "README.md").read_text(), rf"Version {re.escape(v)}(?![\w+-]|\.\d)")
        self.assertRegex((SKILL / "CHANGELOG.md").read_text(), rf"(?m)^## {re.escape(v)}(?![\w+-]|\.\d)")
        self.assertEqual(json.loads((SKILL / ".claude-plugin" / "plugin.json").read_text())["version"], v)
        self.assertEqual(json.loads((SKILL / ".codex-plugin" / "plugin.json").read_text())["version"], v)
