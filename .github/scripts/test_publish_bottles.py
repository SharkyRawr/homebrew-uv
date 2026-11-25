import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from publish_bottles import publish, publish_assets


class PartialPublishTest(unittest.TestCase):
    def test_merge_replaces_one_checksum_and_preserves_the_other_platform(self):
        root = "https://example.org/bottles-1"
        mac_file = "uv-1.arm64_tahoe.bottle.2.tar.gz"
        mac = {"url": f"{root}/{mac_file}", "cellar": ":any", "sha256": "old"}
        linux = {"url": f"{root}/uv-1.x86_64_linux.bottle.2.tar.gz", "cellar": "/linux/cellar", "sha256": "keep"}
        old_files = {"arm64_tahoe": mac, "x86_64_linux": linux}
        digest = hashlib.sha256(b"rebuilt").hexdigest()
        new_files = dict(old_files, arm64_tahoe=dict(mac, sha256=digest))
        info = [{"versions": {"stable": "1"}, "bottle": {"stable": {"files": files, "rebuild": 2}}}
                for files in (old_files, new_files)]

        with TemporaryDirectory() as directory:
            staging = Path(directory)
            (staging / "local.tar.gz").write_bytes(b"rebuilt")
            manifest = {"test/uv/uv": {"formula": {"pkg_version": "1"}, "bottle": {
                "root_url": root, "rebuild": 2, "cellar": "any", "tags": {"arm64_tahoe": {
                    "local_filename": "local.tar.gz", "filename": mac_file, "sha256": digest}}}}}
            (staging / "uv.bottle.json").write_text(json.dumps(manifest))
            env = {"FORMULA": "uv", "TAP_NAME": "test/uv", "TAP_DIR": directory, "BOTTLES_DIR": directory,
                   "RELEASE_VERSION": "1", "RELEASE_TAG": "bottles-1", "ROOT_URL": root, "GITHUB_REF_NAME": "main"}
            with (patch.dict("os.environ", env), patch("publish_bottles.brew_info", side_effect=info),
                  patch("publish_bottles.run"), patch("publish_bottles.publish_assets"),
                  patch("subprocess.check_output", return_value=b"")):
                publish(staging)
            bottle = json.loads((staging / "merged.json").read_text())["test/uv/uv"]["bottle"]
            self.assertEqual(bottle["rebuild"], 2)
            self.assertEqual(bottle["tags"], {"arm64_tahoe": {"cellar": "any", "sha256": digest},
                                              "x86_64_linux": {"cellar": "/linux/cellar", "sha256": "keep"}})

    def test_only_changed_assets_are_uploaded_and_all_formula_urls_are_checked(self):
        root = "https://example.org/bottles-1"
        expected = {"mac": (root + "/mac.tgz", "new"), "linux": (root + "/linux.tgz", "keep")}
        assets = [{"url": url, "digest": "sha256:" + sha} for url, sha in expected.values()]
        assets.append({"url": root + "/notes.txt", "digest": "sha256:unrelated"})
        after = {"assets": assets, "isDraft": False}
        before = dict(after, assets=[dict(assets[0], digest="sha256:old"), *assets[1:]])
        staging = Path("staging")

        for initial, should_upload in [(before, True), (after, False)]:
            with self.subTest(should_upload=should_upload), \
                    patch("publish_bottles.release_info", side_effect=[initial, after]), \
                    patch("publish_bottles.run") as run:
                publish_assets("bottles-1", root, staging, {"mac.tgz": "new"}, expected)
                commands = [call.args for call in run.call_args_list]
                uploads = [args for args in commands if args[0] == "gh"]
                self.assertEqual(uploads, [
                    ("gh", "release", "upload", "bottles-1", "--clobber", staging / "mac.tgz")
                ] if should_upload else [])
                self.assertEqual([args[-1] for args in commands if args[0] == "curl"],
                                 [root + "/mac.tgz", root + "/linux.tgz"])

    def test_missing_retained_asset_stops_before_upload(self):
        expected = {"linux": ("https://example.org/linux.tgz", "keep")}
        with patch("publish_bottles.release_info", return_value=None), patch("publish_bottles.run") as run:
            with self.assertRaisesRegex(ValueError, "Cannot retain"):
                publish_assets("bottles-1", "https://example.org", Path("."), {"mac.tgz": "new"}, expected)
            run.assert_not_called()
