"""Settings file tests: merge order, list concatenation, unknown keys,
HARNESS_SETTINGS=off and missing files."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import settings as settings_module
from coding_harness.core.mode import Autonomy
from coding_harness.core.settings import Settings, SettingsError, load_settings


class TestLoadSettings(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.user_path = root / "home" / "settings.json"
        self.project = root / "proj"
        (self.project / ".git").mkdir(parents=True)
        self.sub = self.project / "pkg" / "deep"
        self.sub.mkdir(parents=True)
        self._patches = [
            mock.patch.object(settings_module, "USER_SETTINGS", self.user_path),
            mock.patch.dict(os.environ, {"HARNESS_SETTINGS": ""}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _write(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _project_file(self) -> Path:
        return self.project / ".bjorn" / "settings.json"

    def test_missing_files_yield_defaults(self) -> None:
        self.assertEqual(load_settings(str(self.sub)), Settings())

    def test_project_file_found_from_a_subdirectory_via_git_root(self) -> None:
        self._write(self._project_file(), {"autonomy": "medium"})
        self.assertEqual(load_settings(str(self.sub)).autonomy, Autonomy.MEDIUM)

    def test_cwd_without_git_root_uses_cwd(self) -> None:
        loose = Path(self.tmp.name) / "loose"
        self._write(loose / ".bjorn" / "settings.json", {"autonomy": "high"})
        self.assertEqual(load_settings(str(loose)).autonomy, Autonomy.HIGH)

    def test_project_scalar_wins_over_user(self) -> None:
        self._write(self.user_path, {"autonomy": "off", "sandbox": {"a": 1}})
        self._write(self._project_file(), {"autonomy": "LOW", "sandbox": {"b": 2}})
        loaded = load_settings(str(self.project))
        self.assertEqual(loaded.autonomy, Autonomy.LOW)
        self.assertEqual(loaded.sandbox, {"b": 2})

    def test_list_keys_concatenate_user_first(self) -> None:
        self._write(self.user_path, {
            "commandAllowlist": ["a"], "commandDenylist": ["d1"],
            "commandBlocklist": ["b1"], "denyWrite": ["~/w1"], "extraReadRoots": ["/r1"],
        })
        self._write(self._project_file(), {
            "commandAllowlist": ["b"], "commandDenylist": ["d2"],
            "commandBlocklist": ["b2"], "denyWrite": ["~/w2"], "extraReadRoots": ["/r2"],
        })
        loaded = load_settings(str(self.project))
        self.assertEqual(loaded.command_allowlist, ["a", "b"])
        self.assertEqual(loaded.command_denylist, ["d1", "d2"])
        self.assertEqual(loaded.command_blocklist, ["b1", "b2"])
        self.assertEqual(loaded.deny_write, ["~/w1", "~/w2"])
        self.assertEqual(loaded.extra_read_roots, ["/r1", "/r2"])

    def test_hooks_merge_per_event(self) -> None:
        self._write(self.user_path, {
            "hooks": {"PreToolUse": [{"command": "u"}], "Stop": [{"command": "s"}]},
        })
        self._write(self._project_file(), {"hooks": {"PreToolUse": [{"command": "p"}]}})
        loaded = load_settings(str(self.project))
        self.assertEqual(loaded.hooks, {
            "PreToolUse": [{"command": "u"}, {"command": "p"}],
            "Stop": [{"command": "s"}],
        })

    def test_unknown_key_names_the_key(self) -> None:
        self._write(self._project_file(), {"autonomyLevel": "low"})
        with self.assertRaises(SettingsError) as ctx:
            load_settings(str(self.project))
        self.assertIn("autonomyLevel", str(ctx.exception))

    def test_bad_value_types_raise(self) -> None:
        for data in (
            {"autonomy": "max"}, {"autonomy": 3}, {"commandAllowlist": "ls"},
            {"commandAllowlist": [1]}, {"hooks": []}, {"hooks": {"Stop": "x"}},
            {"sandbox": []},
        ):
            with self.subTest(data=data):
                self._write(self._project_file(), data)
                with self.assertRaises(SettingsError):
                    load_settings(str(self.project))

    def test_invalid_json_and_non_object_raise(self) -> None:
        path = self._project_file()
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(str(self.project))
        path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(SettingsError):
            load_settings(str(self.project))

    def test_env_off_skips_both_files(self) -> None:
        self._write(self.user_path, {"autonomy": "high"})
        self._write(self._project_file(), {"not-a-key": True})
        with mock.patch.dict(os.environ, {"HARNESS_SETTINGS": "off"}):
            self.assertEqual(load_settings(str(self.project)), Settings())


if __name__ == "__main__":
    unittest.main()
