"""Opt-in lexical SARIF source roots preserve identities and gates."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote_from_bytes

from flowwrench.n8n_refs.cli import main
from .helpers import TEST_TMP, node, workflow

ROOT = Path(__file__).resolve().parent
OPTION = "--sarif-source-root"
BASE_ID = "SRCROOT"


class SourceRootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "checkout # 雪"
        self.root.mkdir()
        self.paths = {}
        for name, data in {
            "clean": workflow([node("Use")]),
            "hazard": workflow([node("Use", "={{ $items('Ghost') }}")]),
            "incomplete": workflow([node("Use", "={{ $items('Ghost') }} {{ $(dynamic) }}")]),
            "identity": workflow([node("Use", id=None)]),
            "structure": [],
        }.items():
            path = self.root / "nested 雪" / (name + " # %:é.json")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(data))
            self.paths[name] = path
        self.paths["missing"] = self.root / "missing 雪.json"
        bad = self.root / "malformed.json"
        bad.write_text('{"secret": secret-source-text')
        self.paths["malformed"] = bad
        outside = self.directory / "checkout # 雪-sibling" / "out.json"
        outside.parent.mkdir()
        outside.write_bytes(self.paths["hazard"].read_bytes())
        self.paths["outside"] = outside

    def cli(self, args, cwd=ROOT):
        env = dict(os.environ, PYTHONPATH=str(ROOT.parents[1] / "src"))
        return subprocess.run([sys.executable, "-m", "flowwrench.n8n_refs", *map(str, args)],
                              cwd=cwd, env=env, capture_output=True, timeout=15)

    def artifact(self, path, root=None):
        path, root = Path(os.path.abspath(path)), root or self.root
        try:
            relative = path.relative_to(root)
        except ValueError:
            return {"uri": path.as_uri()}
        return {"uri": quote_from_bytes(os.fsencode(relative.as_posix()), safe="/") if relative.parts else "./", "uriBaseId": BASE_ID}

    def mapped_pair(self, args, expected, root=None):
        root = root or self.root
        plain = self.cli([*args, "--format", "sarif", "--sarif-automation-id", "CI/refs/"])
        mapped = self.cli([*args, "--format", "sarif", "--sarif-automation-id", "CI/refs/", OPTION, root])
        self.assertEqual((plain.returncode, mapped.returncode), (expected, expected), mapped.stderr)
        self.assertEqual((plain.stderr, mapped.stderr), (b"", b""))
        old, new = json.loads(plain.stdout)["runs"][0], json.loads(mapped.stdout)["runs"][0]
        expected_run = deepcopy(old)
        base_uri = Path(os.path.abspath(root)).as_uri()
        expected_run["originalUriBaseIds"] = {BASE_ID: {"uri": base_uri if base_uri.endswith("/") else base_uri + "/"}}
        # Independently enumerate every provenance surface. All other data must
        # remain byte-for-byte equivalent after JSON serialization.
        from urllib.parse import unquote_to_bytes, urlsplit
        def location(value):
            return self.artifact(os.fsdecode(unquote_to_bytes(urlsplit(value).path)), Path(os.path.abspath(root)))
        for result in expected_run["results"]:
            for loc in result["locations"] + result.get("relatedLocations", []):
                artifact = loc["physicalLocation"]["artifactLocation"]
                loc["physicalLocation"]["artifactLocation"] = location(artifact["uri"])
        for notification in expected_run["invocations"][0]["toolExecutionNotifications"]:
            for loc in notification.get("locations", []):
                physical = loc["physicalLocation"]
                physical["artifactLocation"] = location(physical["artifactLocation"]["uri"])
        properties = expected_run["properties"]
        eligibility = properties["fingerprintEligibility"]
        for artifact in eligibility.get("artifacts", eligibility.get("sides", {}).values()):
            artifact.update(location(artifact["uri"]))
        if "comparison" in properties:
            for side in ("baseline", "candidate"):
                properties["comparison"][side] = location(properties["comparison"][side])
        self.assertEqual(new, expected_run)
        return new

    def test_check_all_inputs_and_comparison_all_states_and_failures(self):
        p = self.paths
        for names, code in ((("clean",), 0), (("hazard",), 1), (("identity",), 0),
                            (("incomplete",), 1), (("missing",), 2), (("malformed",), 2),
                            (("structure",), 2), (("outside",), 1),
                            (("missing", "hazard", "incomplete", "outside", "hazard"), 2)):
            with self.subTest(names=names):
                self.mapped_pair(["check", *(p[n] for n in names)], code)
        for left, right, extra, code in (
            ("clean", "hazard", (), 0), ("clean", "hazard", ("--fail-on-new",), 1),
            ("hazard", "hazard", ("--fail-on-new",), 0), ("hazard", "clean", (), 0),
            ("hazard", "outside", (), 0), ("clean", "incomplete", (), 2),
            ("incomplete", "hazard", (), 2), ("identity", "hazard", (), 2),
            ("missing", "hazard", (), 2), ("hazard", "malformed", (), 2),
            ("structure", "missing", (), 2), ("outside", "missing", (), 2),
        ):
            with self.subTest(left=left, right=right):
                self.mapped_pair(["compare", p[left], p[right], *extra], code)
        for cmd, code in ((["check", ROOT / "fixtures/branch-ref.json", "--fail-on", "warning"], 1),
                          (["compare", ROOT / "fixtures/comparison/baseline.json",
                            ROOT / "fixtures/comparison/candidate.json", "--fail-on-new"], 1)):
            self.mapped_pair(cmd, code, ROOT)

    def test_relocation_preserves_relative_provenance_and_fingerprints(self):
        relocated = self.directory / "second checkout"
        import shutil
        shutil.copytree(self.root, relocated)
        for names in (("hazard",), ("hazard", "hazard"), ("incomplete", "hazard")):
            command = "check" if len(names) == 1 else "compare"
            reports = []
            for root in (self.root, relocated):
                paths = [root / self.paths[n].relative_to(self.root) for n in names]
                result = self.cli([command, *paths, "--format", "sarif", OPTION, root,
                                   "--sarif-automation-id", "CI/refs/"])
                self.assertEqual(result.returncode, 1 if command == "check" else 2 if names[0] == "incomplete" else 0,
                                 result.stderr)
                run = json.loads(result.stdout)["runs"][0]
                run.pop("originalUriBaseIds")
                reports.append(run)
            self.assertEqual(*reports)

    def test_lexical_relative_dot_segments_symlinks_and_root_itself(self):
        # The symlink target is outside; lexical input provenance stays inside.
        link = self.root / "link.json"
        link.symlink_to(self.paths["outside"])
        run = self.mapped_pair(["check", link], 1)
        self.assertEqual(run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"],
                         {"uri": "link.json", "uriBaseId": BASE_ID})
        alias = self.directory / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.mapped_pair(["check", alias / "link.json", self.paths["hazard"]], 1, alias)
        result = self.cli(["check", "../link.json", "--format", "sarif", OPTION, ".."],
                          cwd=self.root / "nested 雪")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["runs"][0]["properties"]["fingerprintEligibility"]["artifacts"][0]["uri"],
                         "link.json")
        self.mapped_pair(["check", self.paths["hazard"]], 1, self.root / "nested 雪" / "..")
        run = self.mapped_pair(["check", self.root], 2)
        self.assertEqual(run["properties"]["fingerprintEligibility"]["artifacts"][0]["uri"], "./")
        run = self.mapped_pair(["check", self.paths["hazard"]], 1, Path("/"))
        self.assertEqual(run["originalUriBaseIds"][BASE_ID]["uri"], "file:///")

    def byte_paths(self):
        # Real POSIX bytes, including the root, not a fabricated surrogate string.
        self.root = Path(os.fsdecode(os.fsencode(self.directory) + b"/checkout-\xff"))
        nested = self.root / os.fsdecode(b"flows-\x80")
        nested.mkdir(parents=True)
        paths = {}
        for name in ("clean", "hazard", "incomplete", "identity", "malformed", "structure"):
            path = nested / os.fsdecode(name.encode() + b"-\xff # %:.json")
            path.write_bytes(self.paths[name].read_bytes())
            paths[name] = path
        paths["missing"] = nested / os.fsdecode(b"missing-\xfe.json")
        outside = self.directory / os.fsdecode(b"checkout-\xff-sibling")
        outside.mkdir()
        paths["outside"] = outside / os.fsdecode(b"outside-\xff.json")
        paths["outside"].write_bytes(paths["hazard"].read_bytes())
        return paths

    @unittest.skipUnless(os.name == "posix", "real non-UTF-8 filename bytes require POSIX")
    def test_posix_byte_paths_all_provenance_and_failure_surfaces(self):
        p = self.byte_paths()
        for name, code in (("clean", 0), ("hazard", 1), ("incomplete", 1),
                           ("identity", 0), ("missing", 2), ("malformed", 2),
                           ("structure", 2), ("outside", 1)):
            with self.subTest(check=name):
                self.mapped_pair(["check", p[name]], code)
        self.mapped_pair(["check", p["missing"], p["hazard"], p["malformed"],
                          p["missing"], p["outside"]], 2)
        for left, right, gate, code in (
            ("clean", "hazard", (), 0), ("clean", "hazard", ("--fail-on-new",), 1),
            ("hazard", "hazard", ("--fail-on-new",), 0), ("hazard", "clean", (), 0),
            ("outside", "hazard", (), 0), ("hazard", "outside", (), 0),
            ("incomplete", "hazard", (), 2), ("hazard", "incomplete", (), 2),
            ("identity", "hazard", (), 2), ("hazard", "identity", (), 2),
            ("missing", "hazard", (), 2), ("hazard", "missing", (), 2),
            ("malformed", "structure", (), 2), ("missing", "missing", (), 2),
        ):
            with self.subTest(left=left, right=right):
                self.mapped_pair(["compare", p[left], p[right], *gate], code)

    @unittest.skipUnless(os.name == "posix", "real non-UTF-8 filename bytes require POSIX")
    def test_posix_byte_paths_round_trip_and_utf8_distinction(self):
        from urllib.parse import unquote_to_bytes
        p = self.byte_paths()
        for raw, uri in ((b"flow-\xff.json", "flow-%FF.json"),
                         ("flow-ÿ.json".encode("utf-8"), "flow-%C3%BF.json"),
                         (b"flow-\xc3(\x80%FF:# ?.json", "flow-%C3%28%80%25FF%3A%23%20%3F.json")):
            path = self.root / os.fsdecode(raw)
            path.write_bytes(p["hazard"].read_bytes())
            run = self.mapped_pair(["check", path], 1)
            actual = run["properties"]["fingerprintEligibility"]["artifacts"][0]["uri"]
            self.assertEqual(actual, uri)
            self.assertEqual(unquote_to_bytes(actual), raw)
        run = self.mapped_pair(["check", self.root], 2)
        self.assertEqual(run["properties"]["fingerprintEligibility"]["artifacts"][0]["uri"], "./")

    @unittest.skipUnless(os.name == "posix", "real non-UTF-8 filename bytes require POSIX")
    def test_posix_byte_paths_lexical_symlinks_and_dot_segments(self):
        p = self.byte_paths()
        link = self.root / os.fsdecode(b"link-\xff.json")
        link.symlink_to(p["outside"])
        broken = self.root / os.fsdecode(b"broken-\xfe.json")
        broken.symlink_to(self.directory / "absent")
        run = self.mapped_pair(["check", link, broken], 2)
        entries = run["properties"]["fingerprintEligibility"]["artifacts"]
        self.assertEqual([a["uri"] for a in entries], ["link-%FF.json", "broken-%FE.json"])
        alias = self.directory / os.fsdecode(b"alias-\x80")
        alias.symlink_to(self.root, target_is_directory=True)
        self.mapped_pair(["compare", alias / link.name, p["hazard"]], 0, alias)
        # Dot segments and relative root/input paths use invocation cwd.
        result = self.cli(["check", "../" + link.name, "--format", "sarif", OPTION, ".."],
                          cwd=p["hazard"].parent)
        self.assertEqual((result.returncode, result.stderr), (1, b""))
        actual = json.loads(result.stdout)["runs"][0]["results"][0]["locations"][0]
        self.assertEqual(actual["physicalLocation"]["artifactLocation"],
                         {"uri": "link-%FF.json", "uriBaseId": BASE_ID})

    def test_invalid_roots_rejected_before_reads(self):
        dangling = self.directory / "dangling"
        dangling.symlink_to(self.directory / "absent")
        loop = self.directory / "loop"
        loop.symlink_to(loop)
        for command in (["check", str(self.paths["missing"])],
                        ["compare", str(self.paths["missing"]), str(self.paths["hazard"])]):
            for root in ("", str(self.directory / "absent"), str(self.paths["hazard"]),
                         str(dangling), str(loop), "bad\x00path", "~/not-expanded"):
                with self.subTest(command=command[0], root=root):
                    out, err = StringIO(), StringIO()
                    with patch("flowwrench.n8n_refs.cli.load_workflow") as load, redirect_stdout(out), redirect_stderr(err):
                        with self.assertRaises(SystemExit) as error:
                            main([*command, "--format", "sarif", OPTION + "=" + root])
                    self.assertEqual(error.exception.code, 2)
                    self.assertEqual(out.getvalue(), "")
                    self.assertIn(OPTION, err.getvalue())
                    load.assert_not_called()

    def test_sarif_only_missing_value_and_rename_controls(self):
        for command in (["check", self.paths["clean"]], ["compare", self.paths["clean"], self.paths["clean"]]):
            for format_args in ([], ["--format", "text"], ["--format", "json"]):
                result = self.cli([*command, *format_args, OPTION, self.root])
                self.assertEqual((result.returncode, result.stdout), (2, b""))
                self.assertIn(b"requires --format sarif", result.stderr)
            result = self.cli([*command, "--format", "sarif", OPTION])
            self.assertEqual((result.returncode, result.stdout), (2, b""))
            self.assertIn(b"expected one argument", result.stderr)
        before = self.paths["clean"].read_bytes()
        result = self.cli(["rename", self.paths["clean"], "--from", "Use", "--to", "Changed",
                           "--in-place", OPTION, self.root])
        self.assertEqual((result.returncode, result.stdout), (2, b""))
        self.assertEqual(self.paths["clean"].read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
