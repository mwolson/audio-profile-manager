import importlib.machinery
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module():
    loader = importlib.machinery.SourceFileLoader("aproman_module", str(ROOT / "aproman" / "aproman.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Failed to create import spec for aproman")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


APROMAN = load_module()

PACTL_CARDS_OUTPUT = """Card #0
\tName: alsa_card.pci-0000_00_1f.3
\tActive Profile: output:analog-stereo
\tProperties:
\t\talsa.card_name = "HDA Intel PCH"
\tports:
\t\tanalog-output-lineout: Line Out
\t\t\tProperties:
\t\t\t\tport.type = "analog"
Card #1
\tName: alsa_card.pci-0000_01_00.1
\tActive Profile: output:hdmi-stereo
\tProperties:
\t\talsa.card_name = "HDA NVidia"
\tports:
\t\thdmi-output-0: HDMI / DisplayPort 1
\t\t\tProperties:
\t\t\t\tport.type = "hdmi"
"""

PACTL_CARDS_NO_HDMI = """Card #0
\tName: alsa_card.pci-0000_00_1f.3
\tActive Profile: output:analog-stereo
\tProperties:
\t\talsa.card_name = "HDA Intel PCH"
\tports:
\t\tanalog-output-lineout: Line Out
\t\t\tProperties:
\t\t\t\tport.type = "analog"
"""

PACTL_CARDS_WITH_PROFILES = """Card #1
\tName: alsa_card.pci-0000_01_00.1
\tActive Profile: off
\tProfiles:
\t\toutput:hdmi-stereo: Digital Stereo (HDMI) Output (sinks: 1, sources: 0, priority: 5900, available: yes)
\t\toutput:hdmi-surround: Digital Surround 5.1 (HDMI) Output (sinks: 1, sources: 0, priority: 800, available: yes)
\t\tpro-audio: Pro Audio (sinks: 4, sources: 0, priority: 1, available: yes)
\t\toff: Off (sinks: 0, sources: 0, priority: 0, available: yes)
\tports:
\t\thdmi-output-0: HDMI / DisplayPort 1
\t\t\tProperties:
\t\t\t\tport.type = "hdmi"
"""


class ApromanTests(unittest.TestCase):
    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_OUTPUT)
    def test_detect_hdmi_card_uses_hdmi_port_type(self, _check_output):
        self.assertEqual("alsa_card.pci-0000_01_00.1", APROMAN.detect_hdmi_card())

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_OUTPUT)
    def test_get_active_profile_reads_card_block(self, _check_output):
        profile = APROMAN.get_active_profile("alsa_card.pci-0000_01_00.1")
        self.assertEqual("output:hdmi-stereo", profile)

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    def test_get_profiles_returns_sorted_by_priority(self, _check_output):
        profiles = APROMAN.get_profiles("alsa_card.pci-0000_01_00.1")
        names = [p[0] for p in profiles]
        self.assertEqual(["output:hdmi-stereo", "output:hdmi-surround", "pro-audio", "off"], names)
        self.assertEqual(5900, profiles[0][1])
        self.assertEqual("yes", profiles[0][2])

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    def test_detect_best_profile_picks_highest_priority(self, _check_output):
        profile = APROMAN.detect_best_profile("alsa_card.pci-0000_01_00.1")
        self.assertEqual("output:hdmi-stereo", profile)

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    def test_detect_best_profile_excludes_off(self, _check_output):
        profile = APROMAN.detect_best_profile("alsa_card.pci-0000_01_00.1")
        self.assertNotEqual("off", profile)

    def test_positive_float_rejects_non_positive_values(self):
        self.assertEqual(3.5, APROMAN.positive_float("3.5"))
        with self.assertRaises(APROMAN.argparse.ArgumentTypeError):
            APROMAN.positive_float("0")
        with self.assertRaises(APROMAN.argparse.ArgumentTypeError):
            APROMAN.positive_float("-1")

    @mock.patch.object(APROMAN.time, "sleep")
    @mock.patch.object(APROMAN, "set_card_profile", side_effect=[True, True])
    @mock.patch.object(APROMAN, "restart_pipewire")
    @mock.patch.object(APROMAN, "get_active_profile", return_value="pro-audio")
    @mock.patch("builtins.print")
    def test_cycle_profile_cycles_through_off_without_restarting_pipewire(
        self,
        _print,
        _get_active_profile,
        restart_pipewire_mock,
        set_card_profile_mock,
        sleep_mock,
    ):
        APROMAN.cycle_profile("alsa_card.pci-0000_01_00.1", "output:hdmi-stereo")

        restart_pipewire_mock.assert_not_called()
        self.assertEqual(
            [
                mock.call("alsa_card.pci-0000_01_00.1", "off", attempts=20, retry_delay=0.25),
                mock.call(
                    "alsa_card.pci-0000_01_00.1",
                    "output:hdmi-stereo",
                    attempts=20,
                    retry_delay=0.25,
                ),
            ],
            set_card_profile_mock.call_args_list,
        )
        sleep_mock.assert_called_once_with(1.0)

    @mock.patch.object(APROMAN.time, "sleep")
    @mock.patch.object(APROMAN.subprocess, "run")
    def test_set_card_profile_retries_until_success(self, run_mock, sleep_mock):
        run_mock.side_effect = [
            mock.Mock(returncode=1),
            mock.Mock(returncode=1),
            mock.Mock(returncode=0),
        ]

        success = APROMAN.set_card_profile("alsa_card.pci-0000_01_00.1", "off", attempts=3, retry_delay=0.5)

        self.assertTrue(success)
        self.assertEqual(3, run_mock.call_count)
        self.assertEqual([mock.call(0.5), mock.call(0.5)], sleep_mock.call_args_list)

    def test_decode_command_accepts_cycle(self):
        self.assertEqual("cycle", APROMAN.decode_command(b"cycle"))
        self.assertEqual("cycle pro-audio", APROMAN.decode_command(b"cycle pro-audio"))

    def test_decode_command_accepts_reload(self):
        self.assertEqual("reload", APROMAN.decode_command(b"reload"))

    def test_decode_command_rejects_unknown(self):
        with self.assertRaises(ValueError):
            APROMAN.decode_command(b"invalid")

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_OUTPUT)
    def test_detect_hdmi_card_name_returns_hdmi_card(self, _check_output):
        self.assertEqual("alsa_card.pci-0000_01_00.1", APROMAN.detect_hdmi_card_name())

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_NO_HDMI)
    def test_detect_hdmi_card_name_returns_none_without_hdmi(self, _check_output):
        self.assertIsNone(APROMAN.detect_hdmi_card_name())

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_OUTPUT)
    @mock.patch("builtins.print")
    def test_run_list_cards_shows_card_label_and_hdmi_marker(self, print_mock, _check_output):
        args = mock.Mock(card=None)
        APROMAN.run_list_cards(args)
        lines = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(2, len(lines))
        self.assertIn("(HDA Intel PCH)", lines[0])
        self.assertNotIn("hdmi", lines[0])
        self.assertNotIn("*", lines[0])
        self.assertIn("(HDA NVidia)", lines[1])
        self.assertIn("hdmi", lines[1])
        self.assertIn("*", lines[1])

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_OUTPUT)
    @mock.patch("builtins.print")
    def test_run_list_cards_marks_explicit_card(self, print_mock, _check_output):
        args = mock.Mock(card="alsa_card.pci-0000_00_1f.3")
        APROMAN.run_list_cards(args)
        lines = [call.args[0] for call in print_mock.call_args_list]
        self.assertIn("*", lines[0])
        self.assertNotIn("*", lines[1])

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    @mock.patch("builtins.print")
    def test_run_list_profiles_marks_active(self, print_mock, _check_output):
        args = mock.Mock(card="alsa_card.pci-0000_01_00.1")
        APROMAN.run_list_profiles(args)
        lines = [call.args[0] for call in print_mock.call_args_list]
        self.assertTrue(any("off" in line and "*" in line for line in lines))
        self.assertFalse(any("output:hdmi-stereo" in line and "*" in line for line in lines))

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    def test_resolve_cycle_profile_uses_args_profile(self, _check_output):
        args = mock.Mock(profile="explicit")
        result = APROMAN.resolve_cycle_profile(args, "alsa_card.pci-0000_01_00.1")
        self.assertEqual("explicit", result)

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value=PACTL_CARDS_WITH_PROFILES)
    @mock.patch("builtins.print")
    def test_resolve_cycle_profile_falls_back_to_best_when_off(self, _print, _check_output):
        args = mock.Mock(profile=None)
        result = APROMAN.resolve_cycle_profile(args, "alsa_card.pci-0000_01_00.1")
        self.assertEqual("output:hdmi-stereo", result)

    @mock.patch.object(APROMAN, "cycle_profile")
    @mock.patch("builtins.print")
    def test_handle_cycle_command_uses_state_profile(self, _print, cycle_mock):
        state = {"card_name": "card1", "profile": "pro-audio"}
        APROMAN.handle_cycle_command(state, "cycle")
        cycle_mock.assert_called_once_with("card1", "pro-audio")

    @mock.patch.object(APROMAN, "cycle_profile")
    @mock.patch("builtins.print")
    def test_handle_cycle_command_uses_override_profile(self, _print, cycle_mock):
        state = {"card_name": "card1", "profile": "pro-audio"}
        APROMAN.handle_cycle_command(state, "cycle output:hdmi-stereo")
        cycle_mock.assert_called_once_with("card1", "output:hdmi-stereo")


class ConfTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_conf_path = APROMAN.CONF_PATH
        APROMAN.CONF_PATH = os.path.join(self._tmpdir, "aproman.conf")

    def tearDown(self):
        APROMAN.CONF_PATH = self._orig_conf_path
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_conf_returns_empty_when_no_file(self):
        self.assertEqual({}, APROMAN.load_conf())

    def test_load_conf_reads_profile(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=pro-audio\n")
        self.assertEqual({"profile": "pro-audio"}, APROMAN.load_conf())

    def test_load_conf_reads_card_and_profile(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=alsa_card.pci-0000_01_00.1\n--profile=pro-audio\n")
        self.assertEqual(
            {"card": "alsa_card.pci-0000_01_00.1", "profile": "pro-audio"},
            APROMAN.load_conf(),
        )

    def test_load_conf_ignores_comments_and_blank_lines(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("# this is a comment\n\n--profile=hdmi-stereo\n")
        self.assertEqual({"profile": "hdmi-stereo"}, APROMAN.load_conf())

    @mock.patch.object(APROMAN, "warn")
    def test_load_conf_rejects_unknown_flags(self, _warn):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--wake-delay=5\n")
        with self.assertRaises(SystemExit):
            APROMAN.load_conf()

    @mock.patch.object(APROMAN, "warn")
    def test_load_conf_rejects_malformed_lines(self, _warn):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("garbage\n")
        with self.assertRaises(SystemExit):
            APROMAN.load_conf()

    @mock.patch.object(APROMAN, "signal_daemon")
    @mock.patch("builtins.print")
    def test_set_default_profile_creates_file(self, _print, _signal):
        APROMAN.run_set_default("profile", "pro-audio")
        with open(APROMAN.CONF_PATH) as f:
            self.assertEqual("--profile=pro-audio\n", f.read())

    @mock.patch.object(APROMAN, "signal_daemon")
    @mock.patch("builtins.print")
    def test_set_default_profile_replaces_existing(self, _print, _signal):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=old-profile\n")
        APROMAN.run_set_default("profile", "new-profile")
        with open(APROMAN.CONF_PATH) as f:
            self.assertEqual("--profile=new-profile\n", f.read())

    @mock.patch.object(APROMAN, "signal_daemon")
    @mock.patch("builtins.print")
    def test_set_default_card_creates_file(self, _print, _signal):
        APROMAN.run_set_default("card", "alsa_card.pci-0000_01_00.1")
        with open(APROMAN.CONF_PATH) as f:
            self.assertEqual("--card=alsa_card.pci-0000_01_00.1\n", f.read())

    @mock.patch.object(APROMAN, "signal_daemon")
    @mock.patch("builtins.print")
    def test_set_default_card_preserves_other_flags(self, _print, _signal):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=pro-audio\n")
        APROMAN.run_set_default("card", "alsa_card.pci-0000_01_00.1")
        with open(APROMAN.CONF_PATH) as f:
            self.assertEqual("--profile=pro-audio\n--card=alsa_card.pci-0000_01_00.1\n", f.read())

    @mock.patch("builtins.print")
    def test_get_default_profile_prints_profile(self, print_mock):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=pro-audio\n")
        APROMAN.run_get_default("profile")
        print_mock.assert_called_once_with("pro-audio")

    @mock.patch("builtins.print")
    def test_get_default_card_prints_card(self, print_mock):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=alsa_card.pci-0000_01_00.1\n")
        APROMAN.run_get_default("card")
        print_mock.assert_called_once_with("alsa_card.pci-0000_01_00.1")

    @mock.patch.object(APROMAN, "warn")
    def test_get_default_profile_exits_when_no_file(self, warn_mock):
        with self.assertRaises(SystemExit):
            APROMAN.run_get_default("profile")
        messages = " ".join(call.args[0] for call in warn_mock.call_args_list)
        self.assertNotIn("auto-detects", messages)

    @mock.patch.object(APROMAN, "warn")
    def test_get_default_card_mentions_hdmi_fallback(self, warn_mock):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=pro-audio\n")
        with self.assertRaises(SystemExit):
            APROMAN.run_get_default("card")
        messages = " ".join(call.args[0] for call in warn_mock.call_args_list)
        self.assertIn("auto-detects", messages)
        self.assertIn("HDMI", messages)

    def test_parse_args_subcommand_cycle(self):
        with mock.patch("sys.argv", ["aproman", "cycle"]):
            args = APROMAN.parse_args({})
        self.assertEqual("cycle", args.command)

    def test_parse_args_subcommand_set_default_profile(self):
        with mock.patch("sys.argv", ["aproman", "set-default-profile", "pro-audio"]):
            args = APROMAN.parse_args({})
        self.assertEqual("set-default-profile", args.command)
        self.assertEqual("pro-audio", args.value)

    def test_parse_args_no_subcommand_is_daemon(self):
        with mock.patch("sys.argv", ["aproman"]):
            args = APROMAN.parse_args({})
        self.assertIsNone(args.command)

    @mock.patch("builtins.print")
    def test_reload_conf_updates_profile(self, _print):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=new-profile\n")
        state = {"card_name": "card", "cli_card": None, "cli_profile": None, "profile": "old-profile"}
        APROMAN.reload_conf(state)
        self.assertEqual("new-profile", state["profile"])

    @mock.patch("builtins.print")
    def test_reload_conf_updates_card(self, _print):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=new-card\n")
        state = {"card_name": "old-card", "cli_card": None, "cli_profile": None, "profile": "p"}
        APROMAN.reload_conf(state)
        self.assertEqual("new-card", state["card_name"])

    @mock.patch("builtins.print")
    def test_reload_conf_cli_profile_takes_precedence(self, _print):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=file-profile\n")
        state = {"card_name": "c", "cli_card": None, "cli_profile": "cli-profile", "profile": "cli-profile"}
        APROMAN.reload_conf(state)
        self.assertEqual("cli-profile", state["profile"])

    @mock.patch("builtins.print")
    def test_reload_conf_cli_card_takes_precedence(self, _print):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=file-card\n")
        state = {"card_name": "cli-card", "cli_card": "cli-card", "cli_profile": None, "profile": "p"}
        APROMAN.reload_conf(state)
        self.assertEqual("cli-card", state["card_name"])

    def test_conf_profile_used_as_default_in_parse_args(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=conf-profile\n")
        file_args = APROMAN.load_conf()
        with mock.patch("sys.argv", ["aproman"]):
            args = APROMAN.parse_args(file_args)
        self.assertEqual("conf-profile", args.profile)
        self.assertIsNone(args.cli_profile)

    def test_conf_card_used_as_default_in_parse_args(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=conf-card\n")
        file_args = APROMAN.load_conf()
        with mock.patch("sys.argv", ["aproman"]):
            args = APROMAN.parse_args(file_args)
        self.assertEqual("conf-card", args.card)
        self.assertIsNone(args.cli_card)

    def test_cli_profile_overrides_conf(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--profile=conf-profile\n")
        file_args = APROMAN.load_conf()
        with mock.patch("sys.argv", ["aproman", "--profile", "cli-profile"]):
            args = APROMAN.parse_args(file_args)
        self.assertEqual("cli-profile", args.profile)
        self.assertEqual("cli-profile", args.cli_profile)

    def test_cli_card_overrides_conf(self):
        with open(APROMAN.CONF_PATH, "w") as f:
            f.write("--card=conf-card\n")
        file_args = APROMAN.load_conf()
        with mock.patch("sys.argv", ["aproman", "--card", "cli-card"]):
            args = APROMAN.parse_args(file_args)
        self.assertEqual("cli-card", args.card)
        self.assertEqual("cli-card", args.cli_card)


class InitSystemTests(unittest.TestCase):
    @mock.patch.object(APROMAN.shutil, "which", side_effect=lambda cmd: "/usr/bin/systemctl" if cmd == "systemctl" else None)
    def test_detect_init_system_systemd(self, _which):
        self.assertEqual("systemd", APROMAN.detect_init_system())

    @mock.patch.object(APROMAN.shutil, "which", side_effect=lambda cmd: "/usr/bin/rc-service" if cmd == "rc-service" else None)
    @mock.patch.object(APROMAN, "get_openrc_version", return_value=(0, 60))
    def test_detect_init_system_openrc_user(self, _version, _which):
        self.assertEqual("openrc-user", APROMAN.detect_init_system())

    @mock.patch.object(APROMAN.shutil, "which", side_effect=lambda cmd: "/usr/bin/rc-service" if cmd == "rc-service" else None)
    @mock.patch.object(APROMAN, "get_openrc_version", return_value=(0, 54))
    def test_detect_init_system_openrc_system(self, _version, _which):
        self.assertEqual("openrc-system", APROMAN.detect_init_system())

    @mock.patch.object(APROMAN.shutil, "which", return_value=None)
    def test_detect_init_system_none(self, _which):
        self.assertIsNone(APROMAN.detect_init_system())

    @mock.patch.object(APROMAN.subprocess, "check_output", return_value="openrc (OpenRC) 0.60.1\n")
    def test_get_openrc_version_parses_output(self, _check_output):
        self.assertEqual((0, 60, 1), APROMAN.get_openrc_version())

    @mock.patch.object(APROMAN.subprocess, "check_output", side_effect=FileNotFoundError)
    def test_get_openrc_version_returns_zero_when_missing(self, _check_output):
        self.assertEqual((0, 0), APROMAN.get_openrc_version())

    @mock.patch.object(APROMAN.os, "geteuid", return_value=1000)
    def test_require_root_fails_as_non_root(self, _geteuid):
        with self.assertRaises(SystemExit):
            APROMAN.require_root()

    @mock.patch.object(APROMAN.os, "geteuid", return_value=0)
    def test_require_root_passes_as_root(self, _geteuid):
        APROMAN.require_root()

    @mock.patch.object(APROMAN.os, "geteuid", return_value=0)
    def test_require_non_root_fails_as_root(self, _geteuid):
        with self.assertRaises(SystemExit):
            APROMAN.require_non_root()

    @mock.patch.object(APROMAN.os, "geteuid", return_value=1000)
    def test_require_non_root_passes_as_non_root(self, _geteuid):
        APROMAN.require_non_root()


class RestartPipewireTests(unittest.TestCase):
    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch.object(APROMAN.shutil, "which", side_effect=lambda cmd: "/usr/bin/systemctl" if cmd == "systemctl" else None)
    def test_restart_pipewire_uses_systemctl(self, _which, run_mock):
        APROMAN.restart_pipewire()
        self.assertEqual(
            [
                mock.call(["systemctl", "--user", "restart", "pipewire.service"], check=True),
                mock.call(["systemctl", "--user", "restart", "pipewire-pulse.service"], check=True),
            ],
            run_mock.call_args_list,
        )

    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch.object(APROMAN.shutil, "which", side_effect=lambda cmd: "/usr/bin/rc-service" if cmd == "rc-service" else None)
    def test_restart_pipewire_uses_openrc(self, _which, run_mock):
        APROMAN.restart_pipewire()
        self.assertEqual(
            [
                mock.call(["rc-service", "--user", "pipewire", "restart"], check=True),
                mock.call(["rc-service", "--user", "pipewire-pulse", "restart"], check=True),
            ],
            run_mock.call_args_list,
        )

    @mock.patch("builtins.print")
    @mock.patch.object(APROMAN.shutil, "which", return_value=None)
    def test_restart_pipewire_warns_when_no_service_manager(self, _which, _print):
        APROMAN.restart_pipewire()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_get_service_source_returns_valid_unit(self):
        content = APROMAN.get_service_source()
        self.assertIn("[Unit]", content)
        self.assertIn("[Service]", content)
        self.assertIn("[Install]", content)
        self.assertIn("ExecStart=", content)

    def test_get_service_source_matches_repo_file(self):
        content = APROMAN.get_service_source()
        repo_path = ROOT / "systemd" / "aproman.service"
        with open(repo_path) as f:
            expected = f.read()
        self.assertEqual(expected, content)

    def test_get_openrc_system_source_returns_valid_script(self):
        content = APROMAN.get_openrc_system_source()
        self.assertIn("#!/sbin/openrc-run", content)
        self.assertIn("command=", content)
        self.assertIn("depend()", content)

    def test_get_openrc_system_source_matches_repo_file(self):
        content = APROMAN.get_openrc_system_source()
        repo_path = ROOT / "openrc-system" / "aproman"
        with open(repo_path) as f:
            expected = f.read()
        self.assertEqual(expected, content)

    def test_get_openrc_user_source_returns_valid_script(self):
        content = APROMAN.get_openrc_user_source()
        self.assertIn("#!/sbin/openrc-run", content)
        self.assertIn("command=", content)

    def test_get_openrc_user_source_matches_repo_file(self):
        content = APROMAN.get_openrc_user_source()
        repo_path = ROOT / "openrc-user" / "aproman"
        with open(repo_path) as f:
            expected = f.read()
        self.assertEqual(expected, content)

    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch("builtins.print")
    def test_install_service_creates_file_and_enables(self, _print, run_mock):
        systemd_dir = os.path.join(self._tmpdir, "systemd", "user")
        with mock.patch.object(APROMAN, "SYSTEMD_USER_DIR", systemd_dir):
            APROMAN.run_install_service()

        service_path = os.path.join(systemd_dir, "aproman.service")
        self.assertTrue(os.path.exists(service_path))
        with open(service_path) as f:
            content = f.read()
        self.assertIn("[Service]", content)

        self.assertEqual(
            [
                mock.call(["systemctl", "--user", "daemon-reload"], check=True),
                mock.call(["systemctl", "--user", "enable", "aproman.service"], check=True),
            ],
            run_mock.call_args_list,
        )

    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch("builtins.print")
    def test_install_service_is_idempotent(self, _print, run_mock):
        systemd_dir = os.path.join(self._tmpdir, "systemd", "user")
        with mock.patch.object(APROMAN, "SYSTEMD_USER_DIR", systemd_dir):
            APROMAN.run_install_service()
            APROMAN.run_install_service()

        service_path = os.path.join(systemd_dir, "aproman.service")
        self.assertTrue(os.path.exists(service_path))

    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch("builtins.print")
    def test_uninstall_service_removes_file(self, _print, run_mock):
        systemd_dir = os.path.join(self._tmpdir, "systemd", "user")
        os.makedirs(systemd_dir)
        service_path = os.path.join(systemd_dir, "aproman.service")
        with open(service_path, "w") as f:
            f.write("[Service]\n")

        with mock.patch.object(APROMAN, "SYSTEMD_USER_DIR", systemd_dir):
            APROMAN.run_uninstall_service()

        self.assertFalse(os.path.exists(service_path))
        self.assertEqual(
            [
                mock.call(
                    ["systemctl", "--user", "disable", "--now", "aproman.service"],
                    check=False,
                ),
                mock.call(["systemctl", "--user", "daemon-reload"], check=True),
            ],
            run_mock.call_args_list,
        )

    @mock.patch.object(APROMAN.subprocess, "run")
    @mock.patch("builtins.print")
    def test_uninstall_service_handles_missing_file(self, _print, run_mock):
        systemd_dir = os.path.join(self._tmpdir, "systemd", "user")
        os.makedirs(systemd_dir)
        with mock.patch.object(APROMAN, "SYSTEMD_USER_DIR", systemd_dir):
            APROMAN.run_uninstall_service()

    def test_parse_args_subcommand_install_service(self):
        with mock.patch("sys.argv", ["aproman", "install-service"]):
            args = APROMAN.parse_args({})
        self.assertEqual("install-service", args.command)

    def test_parse_args_subcommand_uninstall_service(self):
        with mock.patch("sys.argv", ["aproman", "uninstall-service"]):
            args = APROMAN.parse_args({})
        self.assertEqual("uninstall-service", args.command)


class SignalDaemonTests(unittest.TestCase):
    @mock.patch.object(APROMAN, "send_command")
    @mock.patch("builtins.print")
    def test_signal_daemon_sends_reload(self, _print, send_mock):
        APROMAN.signal_daemon()
        send_mock.assert_called_once_with("reload")

    @mock.patch.object(APROMAN, "send_command", side_effect=OSError("no socket"))
    def test_signal_daemon_ignores_oserror(self, _send):
        APROMAN.signal_daemon()


class NodeErrorTests(unittest.TestCase):
    @mock.patch.object(APROMAN, "restart_pipewire")
    @mock.patch("builtins.print")
    def test_handle_node_error_restarts_pipewire(self, _print, restart_mock):
        state = {"last_pipewire_restart": 0.0}
        APROMAN.handle_node_error(state, "node bluez_input.test-1 entered error state")
        restart_mock.assert_called_once()

    @mock.patch.object(APROMAN, "restart_pipewire")
    @mock.patch("builtins.print")
    def test_handle_node_error_respects_cooldown(self, _print, restart_mock):
        state = {"last_pipewire_restart": APROMAN.time.monotonic()}
        APROMAN.handle_node_error(state, "node bluez_input.test-1 entered error state")
        restart_mock.assert_not_called()

    @mock.patch.object(APROMAN, "restart_pipewire")
    @mock.patch("builtins.print")
    def test_handle_node_error_updates_timestamp(self, _print, _restart):
        state = {"last_pipewire_restart": 0.0}
        before = APROMAN.time.monotonic()
        APROMAN.handle_node_error(state, "node test entered error state")
        self.assertGreaterEqual(state["last_pipewire_restart"], before)

    @mock.patch.object(APROMAN.shutil, "which", return_value=None)
    def test_spawn_pw_monitor_returns_none_without_pw_dump(self, _which):
        self.assertIsNone(APROMAN.spawn_pw_monitor())

    @mock.patch.object(APROMAN.subprocess, "Popen", side_effect=OSError("exec failed"))
    @mock.patch.object(APROMAN.shutil, "which", return_value="/usr/bin/pw-dump")
    def test_spawn_pw_monitor_returns_none_on_exec_failure(self, _which, _popen):
        self.assertIsNone(APROMAN.spawn_pw_monitor())


class ExtractPwErrorsTests(unittest.TestCase):
    def _node_json(self, node_id, name, state):
        return {
            "id": node_id,
            "type": "PipeWire:Interface:Node",
            "info": {
                "state": state,
                "props": {"node.name": name},
            },
        }

    def test_skips_initial_dump(self):
        import json
        initial = json.dumps([self._node_json(1, "test", "error")])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(initial, False, seen)
        self.assertTrue(done)
        self.assertEqual(errors, [])

    def test_initial_dump_seeds_seen_set(self):
        import json
        initial = json.dumps([self._node_json(1, "test", "error")])
        seen = set()
        APROMAN.extract_pw_errors(initial, False, seen)
        self.assertIn(1, seen)

    def test_detects_error_after_initial(self):
        import json
        initial = json.dumps([self._node_json(1, "test", "running")])
        change = json.dumps([self._node_json(1, "test", "error")])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(initial + change, False, seen)
        self.assertTrue(done)
        self.assertEqual(len(errors), 1)
        self.assertIn("test", errors[0])

    def test_suppresses_duplicate_error_for_same_node(self):
        import json
        initial = json.dumps([])
        change1 = json.dumps([self._node_json(1, "test", "error")])
        change2 = json.dumps([self._node_json(1, "test", "error")])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(
            initial + change1 + change2, False, seen
        )
        self.assertEqual(len(errors), 1)

    def test_re_reports_after_recovery(self):
        import json
        seen = set()
        initial = json.dumps([])
        err = json.dumps([self._node_json(1, "test", "error")])
        APROMAN.extract_pw_errors(initial + err, False, seen)
        self.assertIn(1, seen)
        recover = json.dumps([self._node_json(1, "test", "running")])
        APROMAN.extract_pw_errors(recover, True, seen)
        self.assertNotIn(1, seen)
        err2 = json.dumps([self._node_json(1, "test", "error")])
        _, _, errors = APROMAN.extract_pw_errors(err2, True, seen)
        self.assertEqual(len(errors), 1)

    def test_ignores_non_error_state(self):
        import json
        initial = json.dumps([])
        change = json.dumps([self._node_json(1, "test", "running")])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(initial + change, False, seen)
        self.assertEqual(errors, [])

    def test_ignores_non_node_types(self):
        import json
        initial = json.dumps([])
        change = json.dumps([{"id": 1, "type": "PipeWire:Interface:Link", "info": {"state": "error"}}])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(initial + change, False, seen)
        self.assertEqual(errors, [])

    def test_returns_incomplete_buffer(self):
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors("[{", False, seen)
        self.assertFalse(done)
        self.assertEqual(errors, [])
        self.assertEqual(buf, "[{")

    def test_falls_back_to_id_when_no_node_name(self):
        import json
        initial = json.dumps([])
        node = {"id": 42, "type": "PipeWire:Interface:Node", "info": {"state": "error", "props": {}}}
        change = json.dumps([node])
        seen = set()
        buf, done, errors = APROMAN.extract_pw_errors(initial + change, False, seen)
        self.assertEqual(len(errors), 1)
        self.assertIn("id=42", errors[0])


if __name__ == "__main__":
    unittest.main()
