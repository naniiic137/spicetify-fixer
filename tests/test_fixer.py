"""Tests for spicetify_fixer.py. Everything is faked: no real command runs, no real file outside a
temp folder is touched. Run with:  py -m unittest -v"""

import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spicetify_fixer as sf  # noqa: E402

STORE_DIR_NAME = "SpotifyAB.SpotifyMusic_1.310.2586.0_x64__zpdnekdrzrea0"
FAMILY = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0"

WINGET_SHOW_FR = """Trouvé Spotify [Spotify.Spotify]
Version : 1.3.10.2586.g81a118a22460
Publisher : Spotify AB
Page d’accueil : https://www.spotify.com/download/windows/
"""

WINGET_SHOW_EN = """Found Spotify [Spotify.Spotify]
Version: 1.2.52.442.g01893f6a
Publisher: Spotify AB
"""


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def config_text(spotify_path, prefs_path, backup_version="1.3.10.2586.g81a118a22460", backup_with="2.46.7",
                custom_apps="marketplace"):
    return f"""[Preprocesses]
disable_sentry        = 1

[AdditionalOptions]
extensions            =
custom_apps           = {custom_apps}

[Setting]
prefs_path              = {prefs_path}
current_theme           = marketplace
spotify_launch_flags    =
spotify_path            = {spotify_path}

; DO NOT CHANGE!
[Backup]
version = {backup_version}
with    = {backup_with}
"""


class FakeRunner(sf.Runner):
    """Records every command. Commands marked `changes` are never 'executed' in check mode.
    `responses` is a list of (substring, Result or callable(cmd) -> Result)."""

    def __init__(self, check=False, responses=None):
        super().__init__(check=check)
        self.calls = []
        self.executed = []
        self.responses = list(responses or [])

    def run(self, cmd, *, changes, timeout, input_text=None, env=None, label=None):
        joined = " ".join(cmd)
        self._current = {"cmd": list(cmd), "joined": joined, "changes": changes, "input": input_text,
                         "timeout": timeout, "label": label}
        self.calls.append(self._current)
        assert timeout and timeout > 0, "every command needs a timeout"
        # The real Runner decides what --check skips; only _execute (the real subprocess) is faked.
        return super().run(cmd, changes=changes, timeout=timeout, input_text=input_text, env=env, label=label)

    def _execute(self, cmd, *, timeout, input_text, env, shown):
        call = self._current
        joined = call["joined"]
        self.executed.append(call)
        for key, res in self.responses:
            if key in joined:
                return res(cmd) if callable(res) else res
        return sf.Result(code=0, out="")

    def executed_changes(self):
        return [c for c in self.executed if c["changes"]]

    def find(self, text):
        return [c for c in self.calls if text in c["joined"] or text in (c["label"] or "")]


def args(**overrides):
    ns = sf.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class FakePC(unittest.TestCase):
    """A temp folder laid out like the owner's PC: Store Spotify + Spicetify 2.46.7 + Marketplace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.paths = sf.Paths(appdata=os.path.join(root, "Roaming"), localappdata=os.path.join(root, "Local"))
        self.store_dir = os.path.join(root, "WindowsApps", STORE_DIR_NAME)
        write(os.path.join(self.store_dir, "Apps", "xpui.spa"))
        write(os.path.join(self.store_dir, "Apps", "login.spa"))
        self.store_prefs = self.paths.store_prefs(FAMILY)
        write(self.store_prefs, 'app.last-launched-version="1.3.10.2586.g81a118a22460"\n')
        os.makedirs(os.path.join(self.paths.spicetify_userdata, "AppX", "xpui"))
        write(os.path.join(self.paths.spicetify_userdata, "Backup", "xpui.spa"))
        write(os.path.join(self.paths.spicetify_userdata, "CustomApps", "marketplace", "index.js"))
        write(self.paths.spicetify_exe)
        self.config = self.paths.default_config
        write(self.config, config_text(self.store_dir, self.store_prefs))
        self.appx_json = json.dumps({"Name": "SpotifyAB.SpotifyMusic", "Version": "1.310.2586.0",
                                     "PackageFamilyName": FAMILY, "InstallLocation": self.store_dir})
        self.patches = [
            mock.patch.object(sf.Fixer, "latest_spicetify", return_value=(2, 46, 7, 0)),
            mock.patch.object(sf.Fixer, "winget", return_value="winget"),
            mock.patch.object(sf.Fixer, "start_process"),
            mock.patch.object(sf.time, "sleep"),
            mock.patch.object(sf.webbrowser, "open"),
            mock.patch.object(sf, "get_file_version", return_value=None),
            mock.patch.object(sf, "ask_yes_no", return_value=True),
        ]
        self.mocks = [p.start() for p in self.patches]
        self.start_process = self.mocks[2]
        self.browser = self.mocks[4]
        self.ask = self.mocks[6]
        sf.log.handlers = [logging.NullHandler()]
        sf.log.propagate = False

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for handler in list(sf.log.handlers):
            handler.close()
        sf.log.handlers = [logging.NullHandler()]
        self.tmp.cleanup()

    def responses(self, store=True, extra=()):
        return [
            *extra,
            ("Get-AppxPackage -Name", sf.Result(0, self.appx_json if store else "")),
            ("spicetify.exe -v", sf.Result(0, "2.46.7\n")),
            ("spicetify.exe --bypass-admin -v", sf.Result(0, "2.46.7\n")),
            ("spicetify.exe -c", sf.Result(0, self.config + "\n")),
            ("spicetify.exe --bypass-admin -c", sf.Result(0, self.config + "\n")),
            ("winget show", sf.Result(0, WINGET_SHOW_FR)),
            ("tasklist", sf.Result(0, 'INFO: No tasks are running which match the specified criteria.\n')),
        ]

    def fixer(self, runner, admin=False, state=None, **arg_overrides):
        return sf.Fixer(args=args(**arg_overrides), paths=self.paths, runner=runner, admin=admin,
                        state=state if state is not None else {})

    def make_desktop(self, version="1.3.10.2586.g81a118a22460", apps="stock"):
        write(self.paths.desktop_exe)
        write(self.paths.desktop_prefs, f'app.last-launched-version="{version}"\n')
        apps_dir = os.path.join(self.paths.desktop_dir, "Apps")
        if apps == "stock":
            write(os.path.join(apps_dir, "xpui.spa"))
        elif apps == "applied":
            os.makedirs(os.path.join(apps_dir, "xpui"), exist_ok=True)


# ------------------------------------------------------------------------------------------------

class TestVersions(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(sf.parse_version("1.2.50.335.g1234abcd"), (1, 2, 50, 335))
        self.assertEqual(sf.parse_version('"1.3.10.2586.g81a118a22460"'), (1, 3, 10, 2586))
        self.assertEqual(sf.parse_version("v2.46.7"), (2, 46, 7, 0))
        self.assertIsNone(sf.parse_version(""))
        self.assertIsNone(sf.parse_version("no version here"))

    def test_store_version_is_unsqueezed(self):
        self.assertEqual(sf.normalize_store_version("1.310.2586.0"), (1, 3, 10, 2586))
        self.assertEqual(sf.normalize_store_version("1.250.335.0"), (1, 2, 50, 335))
        self.assertEqual(sf.normalize_store_version("1.310.2586.0"),
                         sf.parse_version("1.3.10.2586.g81a118a22460"))

    def test_compare(self):
        self.assertEqual(sf.compare_versions((1, 2, 50, 335), (1, 3, 10, 2586)), -1)
        self.assertEqual(sf.compare_versions((1, 3, 10, 2586), (1, 3, 10, 2586)), 0)
        self.assertEqual(sf.compare_versions((1, 3, 10, 2586), (1, 2, 99, 999)), 1)
        self.assertIsNone(sf.compare_versions(None, (1, 2, 3, 4)))

    def test_winget_show_any_language(self):
        self.assertEqual(sf.parse_winget_version(WINGET_SHOW_FR), (1, 3, 10, 2586))
        self.assertEqual(sf.parse_winget_version(WINGET_SHOW_EN), (1, 2, 52, 442))
        self.assertEqual(sf.parse_winget_version("Versión del paquete 1.2.3.4.gabcdef0 x"), (1, 2, 3, 4))
        self.assertIsNone(sf.parse_winget_version("No package found"))

    def test_winget_ok_codes(self):
        self.assertTrue(sf.winget_ok(0))
        self.assertTrue(sf.winget_ok(0x8A15002B))
        self.assertTrue(sf.winget_ok(-1978335189))  # same code, signed
        self.assertFalse(sf.winget_ok(1))
        self.assertFalse(sf.winget_ok(None))


class TestDetection(FakePC):
    def test_store_detected(self):
        runner = FakeRunner(responses=self.responses())
        found = sf.detect_store(runner)
        self.assertEqual(found.kind, "store")
        self.assertEqual(found.version, (1, 3, 10, 2586))
        self.assertEqual(found.family_name, FAMILY)
        self.assertEqual(found.folder, self.store_dir)
        self.assertFalse(runner.calls[0]["changes"])

    def test_store_list_json_and_missing(self):
        runner = FakeRunner(responses=[("Get-AppxPackage", sf.Result(0, "[" + self.appx_json + "]"))])
        self.assertEqual(sf.detect_store(runner).kind, "store")
        self.assertIsNone(sf.detect_store(FakeRunner(responses=[("Get-AppxPackage", sf.Result(0, ""))])))
        self.assertIsNone(sf.detect_store(FakeRunner(responses=[("Get-AppxPackage", sf.Result(1, "{}"))])))
        self.assertIsNone(sf.detect_store(FakeRunner(responses=[("Get-AppxPackage", sf.Result(0, "garbage"))])))

    def test_desktop_detected(self):
        self.assertIsNone(sf.detect_desktop(self.paths))
        self.make_desktop()
        with mock.patch.object(sf, "get_file_version", return_value="1.3.10.2586"):
            found = sf.detect_desktop(self.paths)
        self.assertEqual(found.kind, "desktop")
        self.assertEqual(found.version, (1, 3, 10, 2586))
        self.assertEqual(found.prefs, self.paths.desktop_prefs)

    def test_desktop_version_falls_back_to_prefs(self):
        self.make_desktop(version="1.2.50.335.gabc")
        self.assertEqual(sf.detect_desktop(self.paths).version, (1, 2, 50, 335))

    def test_find_spotify_prefers_desktop(self):
        self.make_desktop()
        fixer = self.fixer(FakeRunner(responses=self.responses()))
        fixer.find_spotify()
        self.assertEqual(fixer.store.kind, "store")
        self.assertEqual(fixer.store.prefs, self.store_prefs)
        self.assertEqual(fixer.active.kind, "desktop")

    def test_find_spotify_store_only(self):
        fixer = self.fixer(FakeRunner(responses=self.responses()))
        fixer.find_spotify()
        self.assertEqual(fixer.active.kind, "store")


class TestConfig(FakePC):
    def test_read_config(self):
        cfg = sf.read_config(self.config)
        self.assertEqual(sf.cfg_get(cfg, "Setting", "spotify_path"), self.store_dir)
        self.assertEqual(sf.cfg_get(cfg, "Backup", "version"), "1.3.10.2586.g81a118a22460")
        self.assertEqual(sf.cfg_get(cfg, "AdditionalOptions", "custom_apps"), "marketplace")
        self.assertEqual(sf.cfg_get(cfg, "Nope", "x"), "")
        self.assertIsNone(sf.read_config(os.path.join(self.tmp.name, "missing.ini")))

    def desktop_target(self):
        return sf.SpotifyInstall("desktop", (1, 3, 10, 2586), self.paths.desktop_dir, self.paths.desktop_prefs,
                                 self.paths.desktop_exe)

    def test_store_paths_need_fixing_for_desktop(self):
        self.make_desktop()
        reasons = sf.config_fix_needed(sf.read_config(self.config), self.desktop_target())
        self.assertTrue(any("Store package" in r and "spotify_path" in r for r in reasons))
        self.assertTrue(any("prefs_path" in r for r in reasons))

    def test_correct_desktop_paths_need_nothing(self):
        self.make_desktop()
        write(self.config, config_text(self.paths.desktop_dir, self.paths.desktop_prefs))
        self.assertEqual(sf.config_fix_needed(sf.read_config(self.config), self.desktop_target()), [])

    def test_missing_folder_needs_fixing(self):
        old = os.path.join(self.tmp.name, "WindowsApps", "SpotifyAB.SpotifyMusic_1.200.0.0_x64__zpdnekdrzrea0")
        write(self.config, config_text(old, self.store_prefs))
        store = sf.detect_store(FakeRunner(responses=self.responses()))
        store.prefs = self.store_prefs
        reasons = sf.config_fix_needed(sf.read_config(self.config), store)
        self.assertTrue(any("missing" in r for r in reasons))

    def test_kept_store_with_right_paths_is_fine(self):
        store = sf.detect_store(FakeRunner(responses=self.responses()))
        store.prefs = self.store_prefs
        self.assertEqual(sf.config_fix_needed(sf.read_config(self.config), store), [])

    def test_fix_config_runs_spicetify_config(self):
        self.make_desktop()
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner)
        fixer.find_spotify()
        fixer.spicetify = self.paths.spicetify_exe
        fixer.fix_config()
        calls = runner.find("spicetify config spotify_path")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cmd"][1:], ["config", "spotify_path", self.paths.desktop_dir,
                                               "prefs_path", self.paths.desktop_prefs])
        self.assertTrue(calls[0]["changes"])
        self.assertTrue(fixer.paths_changed)


class TestPatchDecision(unittest.TestCase):
    def st(self, source="applied", dest=None, backup="backed", backup_with="2.46.7", spicetify="2.46.7"):
        return sf.PatchState(source=source, dest=dest or source, backup=backup, backup_with=backup_with,
                             spicetify_version=spicetify)

    def test_all_good_means_apply(self):
        self.assertEqual(sf.decide_patch(self.st()), sf.APPLY)

    def test_new_spicetify_means_restore_backup_apply(self):
        self.assertEqual(sf.decide_patch(self.st(backup_with="2.40.0")), sf.RESTORE_BACKUP_APPLY)

    def test_spotify_updated_means_backup_apply_without_restore(self):
        # Fresh stock files after an update: restore would copy old files back, so never restore here.
        for source in ("stock", "mixed"):
            for backup in ("empty", "backed", "outdated"):
                self.assertEqual(sf.decide_patch(self.st(source=source, backup=backup)), sf.BACKUP_APPLY)

    def test_patched_without_backup_means_reinstall(self):
        self.assertEqual(sf.decide_patch(self.st(backup="empty")), sf.REINSTALL)
        self.assertEqual(sf.decide_patch(self.st(backup="outdated")), sf.REINSTALL)

    def test_missing_spotify_means_reinstall(self):
        self.assertEqual(sf.decide_patch(self.st(source="missing")), sf.REINSTALL)
        self.assertEqual(sf.decide_patch(self.st(source="invalid")), sf.REINSTALL)

    def test_store_build(self):
        # Store: Spotify's own folder always stays stock, the patched copy lives in AppX.
        self.assertEqual(sf.decide_patch(self.st(source="stock", dest="applied")), sf.APPLY)
        self.assertEqual(sf.decide_patch(self.st(source="stock", dest="applied", backup="outdated")),
                         sf.BACKUP_APPLY)
        self.assertEqual(sf.decide_patch(self.st(source="stock", dest="invalid", backup="empty")),
                         sf.BACKUP_APPLY)

    def test_retry(self):
        self.assertEqual(sf.retry_plan(sf.APPLY, self.st()), sf.RESTORE_BACKUP_APPLY)
        self.assertEqual(sf.retry_plan(sf.RESTORE_BACKUP_APPLY, self.st(source="stock")), sf.BACKUP_APPLY)
        self.assertIsNone(sf.retry_plan(sf.RESTORE_BACKUP_APPLY, self.st()))
        self.assertIsNone(sf.retry_plan(sf.BACKUP_APPLY, self.st(source="stock", backup="outdated")))

    def test_apps_and_backup_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sf.apps_state(os.path.join(tmp, "nope")), "missing")
            self.assertEqual(sf.apps_state(tmp), "invalid")
            write(os.path.join(tmp, "xpui.spa"))
            write(os.path.join(tmp, "native-licenses.html"))
            self.assertEqual(sf.apps_state(tmp), "stock")
            os.makedirs(os.path.join(tmp, "xpui"))
            self.assertEqual(sf.apps_state(tmp), "mixed")
            os.remove(os.path.join(tmp, "xpui.spa"))
            self.assertEqual(sf.apps_state(tmp), "applied")

            backup = os.path.join(tmp, "Backup")
            self.assertEqual(sf.backup_state(backup, "1.2.3.4.gabc", (1, 2, 3, 4)), "empty")
            write(os.path.join(backup, "xpui.spa"))
            self.assertEqual(sf.backup_state(backup, "1.2.3.4.gabc", (1, 2, 3, 4)), "backed")
            self.assertEqual(sf.backup_state(backup, "1.2.3.4.gabc", (1, 3, 0, 0)), "outdated")
            self.assertEqual(sf.backup_state(backup, "", (1, 3, 0, 0)), "outdated")


class TestPatchFlow(FakePC):
    def test_store_kept_runs_apply_with_answers(self):
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner, state={"store_switch": "no"})
        fixer.run()
        apply_calls = [c for c in runner.executed if c["cmd"][1:] == ["apply", "-n"]]
        self.assertEqual(len(apply_calls), 1)
        self.assertIn("y\n", apply_calls[0]["input"])
        # The Store build is launched with the patched AppX folder, like Spicetify itself does.
        launched = self.start_process.call_args[0][0]
        self.assertTrue(any(a.startswith("--app-directory=") for a in launched) or launched[0] == "explorer.exe")

    def test_failed_apply_retries_with_restore_backup_apply(self):
        runner = FakeRunner(responses=self.responses(extra=[("spicetify.exe apply -n", sf.Result(1, "boom"))]))
        fixer = self.fixer(runner, state={"store_switch": "no"})
        fixer.find_spotify()
        fixer.spicetify = self.paths.spicetify_exe
        fixer.spicetify_version = "2.46.7"
        fixer.patch()
        plans = [c["cmd"][1:] for c in runner.executed if "spicetify.exe" in c["cmd"][0] and "-n" in c["cmd"]]
        self.assertEqual(plans, [["apply", "-n"], ["restore", "backup", "apply", "-n"]])

    def test_desktop_after_update_runs_backup_apply(self):
        self.make_desktop(apps="stock")
        write(self.config, config_text(self.paths.desktop_dir, self.paths.desktop_prefs,
                                       backup_version="1.2.0.0.gold"))
        apps = os.path.join(self.paths.desktop_dir, "Apps")

        def apply_ok(cmd):  # pretend Spicetify patched the files
            for name in os.listdir(apps):
                os.remove(os.path.join(apps, name))
            os.makedirs(os.path.join(apps, "xpui"), exist_ok=True)
            return sf.Result(0, "")

        runner = FakeRunner(responses=self.responses(store=False, extra=[("backup apply -n", apply_ok)]))
        fixer = self.fixer(runner)
        fixer.find_spotify()
        fixer.spicetify = self.paths.spicetify_exe
        fixer.spicetify_version = "2.46.7"
        fixer.patch()
        self.assertTrue(runner.find("spicetify.exe backup apply -n"))
        self.assertFalse(runner.find("restore"))


class TestAnswers(FakePC):
    def test_every_spicetify_call_gets_yes_answers(self):
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner, state={"store_switch": "no"})
        fixer.run()
        spicetify_calls = [c for c in runner.calls if c["cmd"][0].endswith("spicetify.exe")]
        self.assertTrue(spicetify_calls)
        for call in spicetify_calls:
            self.assertEqual(call["input"], sf.SPICETIFY_ANSWERS)
            self.assertTrue(call["input"].startswith("y\n"))

    def test_installer_answers(self):
        self.assertEqual(sf.installer_answers(False).splitlines()[0], "y")  # Marketplace? yes
        admin = sf.installer_answers(True).splitlines()
        self.assertEqual(admin[:2], ["n", "y"])  # abort? no; Marketplace? yes

    def test_missing_spicetify_uses_official_installer_with_answers(self):
        os.remove(self.paths.spicetify_exe)

        def installed(cmd):
            write(self.paths.spicetify_exe)
            return sf.Result(0, "spicetify was successfully installed!")

        runner = FakeRunner(responses=self.responses(extra=[("install.ps1 | iex", installed)]))
        fixer = self.fixer(runner)
        with mock.patch.object(sf.shutil, "which", return_value=None):
            fixer.setup_spicetify()
        call = runner.find("spicetify/cli/main/install.ps1")[0]
        self.assertIn("iwr -useb https://raw.githubusercontent.com/spicetify/cli/main/install.ps1 | iex",
                      call["joined"])
        self.assertEqual(call["input"], sf.installer_answers(False))
        self.assertEqual(fixer.spicetify, self.paths.spicetify_exe)

    def test_marketplace_installed_when_missing(self):
        os.remove(os.path.join(self.paths.spicetify_userdata, "CustomApps", "marketplace", "index.js"))

        def installed(cmd):
            write(os.path.join(self.paths.spicetify_userdata, "CustomApps", "marketplace", "index.js"))
            return sf.Result(0, "Done!")

        runner = FakeRunner(responses=self.responses(extra=[("marketplace/main/resources/install.ps1", installed)]))
        fixer = self.fixer(runner, state={"store_switch": "no"})
        fixer.find_spotify()
        fixer.spicetify = self.paths.spicetify_exe
        fixer.spicetify_version = "2.46.7"
        fixer.marketplace()
        self.assertTrue(runner.find("marketplace/main/resources/install.ps1"))
        self.assertTrue([c for c in runner.executed if c["cmd"][1:] == ["apply", "-n"]])


class TestStateFile(FakePC):
    def test_round_trip_and_damaged_file(self):
        sf.save_state(self.paths.state_file, {"store_switch": "no"})
        self.assertEqual(sf.load_state(self.paths.state_file)["store_switch"], "no")
        write(self.paths.state_file, "{not json")
        self.assertEqual(sf.load_state(self.paths.state_file), {})
        self.assertEqual(sf.load_state(os.path.join(self.tmp.name, "none.json")), {})

    def test_answer_is_remembered_and_never_asked_again(self):
        self.ask.return_value = False
        fixer = self.fixer(FakeRunner(responses=self.responses()))
        fixer.find_spotify()
        fixer.store_switch()
        self.assertEqual(self.ask.call_count, 1)
        self.assertEqual(sf.load_state(self.paths.state_file)["store_switch"], "no")
        again = self.fixer(FakeRunner(responses=self.responses()), state=sf.load_state(self.paths.state_file))
        again.find_spotify()
        again.store_switch()
        self.assertEqual(self.ask.call_count, 1)  # not asked a second time

    def test_never_switch_flag(self):
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner, never_switch=True)
        fixer.find_spotify()
        fixer.store_switch()
        self.ask.assert_not_called()
        self.assertEqual(sf.load_state(self.paths.state_file)["store_switch"], "no")
        self.assertFalse(runner.find("Remove-AppxPackage"))

    def test_switch_flag_skips_question_and_overrides_no(self):
        def installed(cmd):
            self.make_desktop()
            return sf.Result(0, "Successfully installed")

        runner = FakeRunner(responses=self.responses(extra=[("winget install", installed)]))
        fixer = self.fixer(runner, state={"store_switch": "no"}, switch_to_desktop=True)
        fixer.find_spotify()
        fixer.store_switch()
        self.ask.assert_not_called()
        order = [c["label"] or c["joined"] for c in runner.executed_changes()]
        remove = next(i for i, x in enumerate(order) if "Remove-AppxPackage" in x)
        install = next(i for i, x in enumerate(order) if "winget install" in x)
        self.assertLess(remove, install)
        self.assertEqual(fixer.active.kind, "desktop")
        install_cmd = runner.find("winget install")[0]["cmd"]
        for flag in ("--id", "Spotify.Spotify", "-e", "--silent", "--accept-package-agreements",
                     "--accept-source-agreements"):
            self.assertIn(flag, install_cmd)

    def test_winget_failure_opens_download_page(self):
        runner = FakeRunner(responses=self.responses(extra=[("winget install", sf.Result(1, "failed"))]))
        fixer = self.fixer(runner, switch_to_desktop=True)
        fixer.find_spotify()
        with self.assertRaises(sf.FixerError):
            fixer.store_switch()
        self.browser.assert_called_with(sf.DOWNLOAD_PAGE)


class TestCheckMode(FakePC):
    @staticmethod
    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def run_main(self, *argv, admin=False):
        runner = FakeRunner(check=True, responses=self.responses())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sf.main(["--check", "--no-pause", *argv], paths=self.paths, runner=runner, admin=admin)
        return code, out.getvalue(), runner

    def test_check_changes_nothing(self):
        before = self.digest(self.config)
        tree_before = sorted(os.walk(self.paths.appdata))
        code, out, runner = self.run_main()
        self.assertEqual(code, 0, out)
        self.assertEqual(runner.executed_changes(), [])
        self.assertFalse(os.path.exists(self.paths.state_file))
        self.assertEqual(self.digest(self.config), before)
        self.assertEqual(sorted(os.walk(self.paths.appdata)), tree_before)
        self.start_process.assert_not_called()
        self.browser.assert_not_called()
        self.ask.assert_not_called()

    def test_check_reports_the_plan(self):
        code, out, runner = self.run_main()
        self.assertIn("Found the Microsoft Store Spotify", out)
        self.assertIn("spotify_path points at the Store package", out)
        self.assertIn("WOULD ask once", out)
        self.assertIn("WOULD run: Get-AppxPackage SpotifyAB.SpotifyMusic | Remove-AppxPackage", out)
        self.assertIn("WOULD run: winget install", out)
        self.assertIn("WOULD run: spicetify backup apply -n", out)
        self.assertIn("WOULD start", out)
        # The changing commands were still recorded, just not executed.
        self.assertTrue(runner.find("Remove-AppxPackage"))

    def test_check_with_shortcut(self):
        code, out, runner = self.run_main("--make-shortcut")
        self.assertEqual(code, 0)
        self.assertEqual(runner.executed_changes(), [])
        self.assertIn("WOULD run: create", out)


class TestAdmin(FakePC):
    def test_is_admin_paths(self):
        fake = mock.MagicMock()
        fake.shell32.IsUserAnAdmin.return_value = 1
        with mock.patch.object(sf.ctypes, "windll", fake, create=True):
            self.assertTrue(sf.is_admin())
        fake.shell32.IsUserAnAdmin.return_value = 0
        with mock.patch.object(sf.ctypes, "windll", fake, create=True):
            self.assertFalse(sf.is_admin())
        fake.shell32.IsUserAnAdmin.side_effect = OSError("no shell32")
        with mock.patch.object(sf.ctypes, "windll", fake, create=True):
            self.assertFalse(sf.is_admin())
        with mock.patch.object(sf.ctypes, "windll", None, create=True):
            self.assertFalse(sf.is_admin())  # None has no shell32 -> AttributeError -> False

    def test_bypass_admin_only_when_admin(self):
        runner = FakeRunner(responses=self.responses())
        self.assertNotIn("--bypass-admin", self.fixer(runner).spicetify_cmd("apply"))
        self.assertEqual(self.fixer(runner, admin=True).spicetify_cmd("apply", "-n")[1:],
                         ["--bypass-admin", "apply", "-n"])

    def test_admin_refuses_switch_before_removing_anything(self):
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner, admin=True, switch_to_desktop=True)
        fixer.find_spotify()
        with self.assertRaises(sf.FixerError) as ctx:
            fixer.store_switch()
        self.assertIn("administrator", str(ctx.exception))
        self.assertFalse(runner.find("Remove-AppxPackage"))
        self.assertFalse(runner.find("winget install"))

    def test_admin_run_warns(self):
        runner = FakeRunner(responses=self.responses())
        fixer = self.fixer(runner, admin=True, state={"store_switch": "no"})
        fixer.run()
        self.assertTrue(any("administrator" in w for w in fixer.warnings))
        self.assertTrue(any("--bypass-admin" in w for w in fixer.warnings))

    def test_main_failure_returns_1(self):
        runner = FakeRunner(responses=self.responses())
        with contextlib.redirect_stdout(io.StringIO()):
            code = sf.main(["--switch-to-desktop", "--no-pause"], paths=self.paths, runner=runner, admin=True)
        self.assertEqual(code, 1)


class TestCommandLine(unittest.TestCase):
    def test_flags(self):
        ns = sf.build_parser().parse_args(["--check", "--make-shortcut", "--skip-spotify-update",
                                           "--skip-spicetify-update", "--no-launch", "--verbose",
                                           "--block-spotify-updates", "--no-pause"])
        self.assertTrue(ns.check and ns.make_shortcut and ns.skip_spotify_update and ns.no_launch)
        self.assertIsInstance(ns, argparse.Namespace)

    def test_conflicting_switch_flags(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sf.main(["--switch-to-desktop", "--never-switch"])


if __name__ == "__main__":
    unittest.main()
