import os
import shlex
import shutil
import string
import subprocess
import unittest
from unittest import mock

import common  # noqa: F401
from mhbm import cli, i18n
from mhbm.commands.ops import completion_spec, render_fish, render_zsh
from test_cli import CliCase


class Catalogs(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_language_has_every_key_with_the_same_placeholders(self):
        en = i18n.MESSAGES["en"]
        fields = lambda s: {x[1] for x in string.Formatter().parse(s) if x[1]}  # noqa: E731
        for lang, msgs in i18n.MESSAGES.items():
            self.assertEqual(set(msgs), set(en), lang)
            for key, text in msgs.items():
                self.assertEqual(fields(text), fields(en[key]), f"{lang}:{key}")
                text.format(**dict.fromkeys(fields(text), "x"))            # formats without error

    def test_language_detection_order(self):
        with mock.patch.dict(os.environ, {"MHBM_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "fa_IR.UTF-8"}):
            self.assertEqual(i18n.detect(), "fa")
            self.assertEqual(i18n.detect("de"), "de")                     # explicit wins
        with mock.patch.dict(os.environ, {"MHBM_LANG": "fr", "LANG": "de_DE.UTF-8"}):
            self.assertEqual(i18n.detect(), "fr")
        with mock.patch.dict(os.environ, {"MHBM_LANG": "", "LC_ALL": "", "LC_MESSAGES": "", "LANG": "C.UTF-8"}):
            self.assertEqual(i18n.detect(), "en")
        self.assertEqual(i18n.detect("klingon"), "en")

    def test_missing_keys_fall_back_to_english(self):
        i18n.MESSAGES["xx"] = {"results": "{n} xx-results"}                 # a partial catalog
        try:
            i18n.set_language("xx")
            self.assertEqual(i18n.language(), "xx")
            self.assertEqual(i18n.t("results", n=2), "2 xx-results")
            self.assertIn("duplicates", i18n.t("dup_summary", n=1, g=1, level="exact"))   # missing key -> English
        finally:
            del i18n.MESSAGES["xx"]

    def test_bidi_wraps_only_arabic_script_lines_in_rtl_languages(self):
        with mock.patch.dict(os.environ, {"MHBM_BIDI": "wrap"}):
            i18n.set_language("fa")
            self.assertEqual(i18n.bidi("سلام hello"), "⁧سلام hello⁩")
            self.assertEqual(i18n.bidi("plain english"), "plain english")
            i18n.set_language("de")
            self.assertEqual(i18n.bidi("سلام"), "سلام")
        with mock.patch.dict(os.environ, {"MHBM_BIDI": "off"}):
            i18n.set_language("fa")
            self.assertEqual(i18n.bidi("سلام"), "سلام")


class LocalisedCli(CliCase):
    def test_lang_flag_changes_the_summary_text(self):
        self.run_cli("import", self.write("in.csv", "url,title\nhttps://a.com,A\n"))
        self.assertIn("نشانک", self.run_cli("--lang", "fa", "stats"))
        self.assertIn("Lesezeichen", self.run_cli("--lang", "de", "stats"))
        self.assertIn("Bookmarks:", self.run_cli("stats"))
        with mock.patch.dict(os.environ, {"MHBM_LANG": "es"}):
            self.assertIn("Marcadores", self.run_cli("stats"))
        self.assertEqual(self.js("--lang", "fa", "stats")["total"], 1)            # JSON stays language-neutral

    def test_bidi_isolates_appear_in_rtl_output_by_default(self):
        self.run_cli("import", self.write("in.csv", "url,title\nhttps://a.com,A\n"))
        with mock.patch.dict(os.environ, {"MHBM_BIDI": "wrap"}):
            self.assertIn("⁧", self.run_cli("--lang", "fa", "stats"))


class Completion(CliCase):
    def spec(self):
        return completion_spec(cli.build_parser())

    def test_spec_lists_every_command_option_and_choice(self):
        spec = self.spec()
        self.assertGreaterEqual(len(spec["commands"]), 40)
        exp = {o["dest"]: o for o in spec["commands"]["export"]["options"]}
        self.assertIn("raindrop", exp["format"]["choices"])
        self.assertTrue(exp["out"]["files"])
        self.assertTrue(any("--json" in o["flags"] for o in spec["global"]))

    def test_bash_script_is_valid(self):
        script = self.dir / "c.bash"
        script.write_text(self.run_cli("completion", "bash"))
        if shutil.which("bash") and os.name != "nt":   # Windows "bash" is often WSL, which cannot read the temp path
            self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)

    def test_zsh_script_structure_and_quoting(self):
        text = render_zsh(self.spec())
        self.assertTrue(text.startswith("#compdef mhbm"))
        for name in self.spec()["commands"]:
            self.assertIn(f"        {name})", text)
        for line in text.splitlines():
            if line.strip().startswith(("'", "_arguments")) or "_arguments" in line:
                body = line.rstrip()[:-1] if line.rstrip().endswith(" \\") else line                 # drop only a trailing line continuation
                shlex.split(body)                                                                  # quotes balance on every line
        self.assertIn("'chrome-path:alias of `browsers`'", text)                              # backticks stay inside quotes
        self.assertIn("bookmark'\\''s", text)                                                  # apostrophes are escaped
        self.assertNotIn("'--timeout[]:timeout:_files'", text)                                # "timeout" is not an output file
        if shutil.which("zsh"):
            f = self.dir / "_mhbm"
            f.write_text(text)
            self.assertEqual(subprocess.run(["zsh", "-n", str(f)]).returncode, 0)

    def test_fish_script_structure_and_quoting(self):
        text = render_fish(self.spec())
        for name in self.spec()["commands"]:
            self.assertIn(f"-a {name} -d", text)
        for line in text.splitlines():
            if line.startswith("complete"):
                self.assertTrue(shlex.split(line))
        self.assertIn("-l format", text)
        if shutil.which("fish"):
            f = self.dir / "mhbm.fish"
            f.write_text(text)
            self.assertEqual(subprocess.run(["fish", "-n", str(f)]).returncode, 0)

    def test_cli_prints_each_shell(self):
        for shell in ("bash", "zsh", "fish"):
            self.assertIn("mhbm", self.run_cli("completion", shell))


if __name__ == "__main__":
    unittest.main()
