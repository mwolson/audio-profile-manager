#!/usr/bin/env python3

import argparse
import datetime as dt
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

VERSION = "0.5.3"

ALLOWED_CONF_FLAGS = {"--card", "--profile"}

RESTART_COOLDOWN = 30.0

CONF_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "aproman.conf",
)
OPENRC_SYSTEM_INIT_DIR = "/etc/init.d"
OPENRC_USER_INIT_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "rc",
    "init.d",
)
SYSTEMD_USER_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "systemd",
    "user",
)


def main():
    file_args = load_conf()
    args = parse_args(file_args)

    command = getattr(args, "command", None)
    if command == "cycle":
        require_commands(["pactl"])
        run_cycle(args)
    elif command == "get-default-card":
        run_get_default("card")
    elif command == "get-default-profile":
        run_get_default("profile")
    elif command == "install-service":
        init_system = detect_init_system()
        if init_system == "systemd":
            require_non_root()
            run_install_service()
        elif init_system == "openrc-user":
            require_non_root()
            run_install_openrc_user_service()
        elif init_system == "openrc-system":
            require_root()
            run_install_openrc_service()
        else:
            warn("Error: No supported init system found.")
            warn("aproman requires either systemd or OpenRC.")
            sys.exit(1)
    elif command == "list-cards":
        require_commands(["pactl"])
        run_list_cards(args)
    elif command == "list-profiles":
        require_commands(["pactl"])
        run_list_profiles(args)
    elif command == "set-default-card":
        run_set_default("card", args.value)
    elif command == "set-default-profile":
        run_set_default("profile", args.value)
    elif command == "uninstall-service":
        init_system = detect_init_system()
        if init_system == "systemd":
            require_non_root()
            run_uninstall_service()
        elif init_system == "openrc-user":
            require_non_root()
            run_uninstall_openrc_user_service()
        elif init_system == "openrc-system":
            require_root()
            run_uninstall_openrc_service()
        else:
            warn("Error: No supported init system found.")
            warn("aproman requires either systemd or OpenRC.")
            sys.exit(1)
    else:
        require_commands(["dbus-monitor", "pactl"])
        run_daemon(args)


def run_list_cards(args):
    cards = list(iter_card_blocks())
    if not cards:
        warn("Error: No audio cards found.")
        sys.exit(1)
    selected = args.card or detect_hdmi_card_name()
    for name, block in cards:
        card_label = None
        active = None
        has_hdmi = False
        for line in block:
            match = re.match(r'\s*alsa\.card_name\s*=\s*"(.+)"', line)
            if match:
                card_label = match.group(1)
            match = re.match(r"\s*Active Profile:\s*(.+)", line)
            if match:
                active = match.group(1).strip()
            if 'port.type = "hdmi"' in line:
                has_hdmi = True
        parts = [name]
        if card_label:
            parts.append(f"({card_label})")
        if active:
            parts.append(f"profile: {active}")
        if has_hdmi:
            parts.append("hdmi")
        if name == selected:
            parts.append("*")
        print("  ".join(parts))


def run_list_profiles(args):
    card_name = args.card or detect_hdmi_card()
    active = get_active_profile(card_name)
    profiles = get_profiles(card_name)
    if not profiles:
        warn(f"Error: No profiles found for card '{card_name}'.")
        sys.exit(1)
    for name, priority, available in profiles:
        marker = " *" if name == active else ""
        print(f"{name}  (priority: {priority}, available: {available}){marker}")


def run_get_default(key):
    flag = f"--{key}"
    hint = f"Use 'aproman list-{key}s' to see available options, then 'aproman set-default-{key}' to set one."
    hdmi_note = "Without a default, aproman auto-detects the first HDMI card." if key == "card" else ""

    if not os.path.exists(CONF_PATH):
        warn(f"No config file found at {CONF_PATH}")
        if hdmi_note:
            warn(hdmi_note)
        warn(hint)
        sys.exit(1)

    for conf_flag, value in iter_conf_entries(CONF_PATH):
        if conf_flag == flag:
            print(value)
            return

    warn(f"No {flag} entry found in {CONF_PATH}")
    if hdmi_note:
        warn(hdmi_note)
    warn(hint)
    sys.exit(1)


def run_set_default(key, value):
    flag = f"--{key}"
    flag_prefix = f"{flag}="
    lines = []
    replaced = False

    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as f:
            for line in f:
                if line.strip().startswith(flag_prefix):
                    lines.append(f"{flag_prefix}{value}\n")
                    replaced = True
                else:
                    lines.append(line)

    if not replaced:
        lines.append(f"{flag_prefix}{value}\n")

    os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
    with open(CONF_PATH, "w") as f:
        f.writelines(lines)

    log(f"Wrote {flag_prefix}{value} to {CONF_PATH}")
    signal_daemon()


def signal_daemon():
    try:
        send_command("reload")
        log("Signaled daemon to reload config")
    except OSError:
        pass


def detect_init_system():
    if shutil.which("systemctl"):
        return "systemd"
    if shutil.which("rc-service"):
        if get_openrc_version() >= (0, 60):
            return "openrc-user"
        return "openrc-system"
    return None


def get_openrc_version():
    try:
        output = subprocess.check_output(
            ["openrc", "--version"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return (0, 0)
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
    if not match:
        return (0, 0)
    parts = match.group(1).split(".")
    return tuple(int(p) for p in parts)


def require_root():
    if os.geteuid() != 0:
        warn("Error: System-level services require root. Try running with sudo.")
        sys.exit(1)


def require_non_root():
    if os.geteuid() == 0:
        warn("Error: User-level services should not be installed as root.")
        sys.exit(1)


def run_install_service():
    content = get_service_source()
    service_path = os.path.join(SYSTEMD_USER_DIR, "aproman.service")

    os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
    with open(service_path, "w") as f:
        f.write(content)
    log(f"Installed {service_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "aproman.service"], check=True)
    log("Enabled aproman.service")

    log("")
    log("To start immediately:")
    log("  systemctl --user start aproman.service")
    log("")
    log("To check status:")
    log("  systemctl --user status aproman.service")
    log("  journalctl --user -u aproman.service -f")


def get_service_source():
    try:
        from importlib.resources import files

        return (files("aproman") / "systemd" / "aproman.service").read_text()
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "systemd", "aproman.service")
    with open(path) as f:
        return f.read()


def run_install_openrc_service():
    content = get_openrc_system_source()
    service_path = os.path.join(OPENRC_SYSTEM_INIT_DIR, "aproman")

    match = re.search(r'^command="(.+)"', content, re.MULTILINE)
    if match and not os.path.exists(match.group(1)):
        warn(f"Error: aproman not found at {match.group(1)}")
        warn("The OpenRC system service expects a system-wide installation.")
        warn("Try: sudo uv pip install --system --break-system-packages aproman")
        sys.exit(1)

    try:
        with open(service_path, "w") as f:
            f.write(content)
        os.chmod(service_path, 0o755)
    except PermissionError:
        warn(f"Error: Permission denied writing to {service_path}")
        sys.exit(1)
    log(f"Installed {service_path}")

    subprocess.run(["rc-update", "add", "aproman", "default"], check=True)
    log("Added aproman to default runlevel")

    log("")
    log("To start immediately:")
    log("  rc-service aproman start")
    log("")
    log("To check status:")
    log("  rc-service aproman status")


def get_openrc_system_source():
    try:
        from importlib.resources import files

        return (files("aproman") / "openrc-system" / "aproman").read_text()
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "openrc-system", "aproman")
    with open(path) as f:
        return f.read()


def run_install_openrc_user_service():
    content = get_openrc_user_source()
    service_dir = OPENRC_USER_INIT_DIR
    runlevel_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "rc",
        "runlevels",
        "default",
    )
    service_path = os.path.join(service_dir, "aproman")

    os.makedirs(service_dir, exist_ok=True)
    with open(service_path, "w") as f:
        f.write(content)
    os.chmod(service_path, 0o755)
    log(f"Installed {service_path}")

    os.makedirs(runlevel_dir, exist_ok=True)
    subprocess.run(["rc-update", "--user", "add", "aproman", "default"], check=True)
    log("Added aproman to user default runlevel")

    log("")
    log("To start immediately:")
    log("  rc-service --user aproman start")
    log("")
    log("To check status:")
    log("  rc-service --user aproman status")


def get_openrc_user_source():
    try:
        from importlib.resources import files

        return (files("aproman") / "openrc-user" / "aproman").read_text()
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        pass

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "openrc-user", "aproman")
    with open(path) as f:
        return f.read()


def run_uninstall_service():
    service_path = os.path.join(SYSTEMD_USER_DIR, "aproman.service")

    subprocess.run(
        ["systemctl", "--user", "disable", "--now", "aproman.service"],
        check=False,
    )

    try:
        os.remove(service_path)
        log(f"Removed {service_path}")
    except FileNotFoundError:
        log(f"No service file at {service_path}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    log("Uninstalled aproman.service")


def run_uninstall_openrc_service():
    service_path = os.path.join(OPENRC_SYSTEM_INIT_DIR, "aproman")

    subprocess.run(["rc-service", "aproman", "stop"], check=False)
    subprocess.run(["rc-update", "del", "aproman", "default"], check=False)

    try:
        os.remove(service_path)
        log(f"Removed {service_path}")
    except FileNotFoundError:
        log(f"No init script at {service_path}")
    except PermissionError:
        warn(f"Error: Permission denied removing {service_path}")
        sys.exit(1)

    log("Uninstalled aproman service")


def run_uninstall_openrc_user_service():
    service_path = os.path.join(OPENRC_USER_INIT_DIR, "aproman")

    subprocess.run(["rc-service", "--user", "aproman", "stop"], check=False)
    subprocess.run(["rc-update", "--user", "del", "aproman", "default"], check=False)

    try:
        os.remove(service_path)
        log(f"Removed {service_path}")
    except FileNotFoundError:
        log(f"No init script at {service_path}")

    log("Uninstalled aproman user service")


def run_cycle(args):
    card_name = args.card or detect_hdmi_card()
    profile = resolve_cycle_profile(args, card_name)

    try:
        send_command(f"cycle {profile}")
        log(f"Sent cycle request to daemon (profile: {profile})")
    except OSError as exc:
        warn(f"Warning: daemon unavailable, running cycle directly ({exc}).")
        log(f"Cycling profile on {card_name}: -> off -> {profile}")
        cycle_profile(card_name, profile)
        log("Done.")


def resolve_cycle_profile(args, card_name):
    if args.profile:
        return args.profile

    current_profile = get_active_profile(card_name)
    if current_profile and current_profile != "off":
        return current_profile

    best = detect_best_profile(card_name)
    if not best:
        warn(f"Error: Card '{card_name}' has no available profiles. Use --profile to specify one.")
        sys.exit(1)
    log(f"Active profile is 'off', selected best available: {best}")
    return best


def run_daemon(args):
    card_name = args.card or detect_hdmi_card()
    current_profile = get_active_profile(card_name)
    if not current_profile:
        warn(f"Error: Card '{card_name}' not found or has no active profile.")
        sys.exit(1)

    profile = args.profile or current_profile

    if not args.card:
        log("No --card specified, auto-detecting HDMI audio card...")
        log(f"Detected: {card_name}")
    if not args.profile:
        log(f"No --profile specified, using active profile: {profile}")

    log(f"Card: {card_name}")
    log(f"Current profile: {current_profile}")
    log(f"Target profile: {profile}")

    state = {
        "card_name": card_name,
        "cli_card": args.cli_card,
        "cli_profile": args.cli_profile,
        "profile": profile,
    }

    def handle_sighup(_signum, _frame):
        reload_conf(state)

    signal.signal(signal.SIGHUP, handle_sighup)

    socket_path = get_socket_path()
    cleanup_socket(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(socket_path)

    def handle_exit(_signum, _frame):
        cleanup_socket(socket_path)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    log(f"Listening on {socket_path}")
    log("Monitoring for suspend/resume events...")

    try:
        monitor_loop(args.wake_delay, state, server)
    finally:
        server.close()
        cleanup_socket(socket_path)


def reload_conf(state):
    log(f"{timestamp()} Reloading config...")
    try:
        file_args = load_conf()
    except SystemExit:
        warn(f"{timestamp()} Warning: Failed to reload config, keeping current settings.")
        return

    changed = False

    new_card = file_args.get("card")
    if new_card and new_card != state["card_name"]:
        if state["cli_card"]:
            log(f"{timestamp()} CLI --card takes precedence, keeping: {state['card_name']}")
        else:
            log(f"{timestamp()} Updated card: {state['card_name']} -> {new_card}")
            state["card_name"] = new_card
            changed = True

    new_profile = file_args.get("profile")
    if new_profile and new_profile != state["profile"]:
        if state["cli_profile"]:
            log(f"{timestamp()} CLI --profile takes precedence, keeping: {state['profile']}")
        else:
            log(f"{timestamp()} Updated target profile: {state['profile']} -> {new_profile}")
            state["profile"] = new_profile
            changed = True

    if not changed:
        log(f"{timestamp()} Config reloaded, no changes.")


def parse_args(file_args):
    parser = argparse.ArgumentParser(
        prog="aproman",
        description=(
            "Fix HDMI audio after suspend/resume on PipeWire + WirePlumber. "
            "With no command, runs as a daemon monitoring D-Bus for suspend/resume events."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--card", help="PipeWire/PulseAudio card name")
    parser.add_argument("--profile", help="desired audio profile")
    parser.add_argument(
        "--wake-delay",
        type=positive_float,
        default=3.0,
        help="seconds to wait after wake before cycling (default: %(default)s)",
    )

    sub = parser.add_subparsers(dest="command", title="commands")
    sub.add_parser("cycle", help="cycle the card profile off and back on once, then exit")
    sub.add_parser("get-default-card", help="print the default card from the config file")
    sub.add_parser("get-default-profile", help="print the default profile from the config file")
    sub.add_parser("install-service", help="install and enable the service (systemd or OpenRC)")
    sub.add_parser("list-cards", help="list available audio cards")
    sub.add_parser("list-profiles", help="list available profiles for the card")
    p = sub.add_parser("set-default-card", help="set the default card and signal the daemon")
    p.add_argument("value", metavar="CARD")
    p = sub.add_parser("set-default-profile", help="set the default profile and signal the daemon")
    p.add_argument("value", metavar="PROFILE")
    sub.add_parser("uninstall-service", help="disable and remove the service (systemd or OpenRC)")

    args = parser.parse_args()

    args.cli_card = args.card
    args.cli_profile = args.profile
    if not args.card and "card" in file_args:
        args.card = file_args["card"]
    if not args.profile and "profile" in file_args:
        args.profile = file_args["profile"]

    return args


def load_conf():
    if not os.path.exists(CONF_PATH):
        return {}

    result = {}
    for flag, value in iter_conf_entries(CONF_PATH):
        if flag not in ALLOWED_CONF_FLAGS:
            warn(f"Error: Unsupported flag '{flag}' in {CONF_PATH}")
            sys.exit(1)
        if flag == "--card":
            result["card"] = value
        elif flag == "--profile":
            result["profile"] = value
    return result


def iter_conf_entries(path):
    with open(path) as f:
        for line_num, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(--[a-z][a-z0-9-]*)=(.+)$", line)
            if not match:
                warn(f"Error: Malformed line {line_num} in {path}: {line}")
                sys.exit(1)
            yield match.group(1), match.group(2)


def positive_float(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid positive number: {value}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"invalid positive number: {value}")
    return parsed


def require_commands(commands):
    missing = [command for command in commands if not shutil.which(command)]
    if missing:
        for command in missing:
            warn(f"Error: '{command}' is required but not found in PATH.")
        sys.exit(1)


def detect_hdmi_card_name():
    for name, block in iter_card_blocks():
        if any('port.type = "hdmi"' in line for line in block):
            return name
    return None


def detect_hdmi_card():
    name = detect_hdmi_card_name()
    if name:
        return name
    warn("Error: No HDMI audio card detected. Use --card to specify one manually.")
    sys.exit(1)


def get_active_profile(card_name):
    for name, block in iter_card_blocks():
        if name != card_name:
            continue
        for line in block:
            match = re.match(r"\s*Active Profile:\s*(.+)", line)
            if match:
                return match.group(1).strip()
        return None
    return None


def get_profiles(card_name):
    profiles = []
    for name, block in iter_card_blocks():
        if name != card_name:
            continue
        for line in block:
            match = re.match(
                r"\s+(\S+):.*priority:\s*(\d+),\s*available:\s*(yes|no)",
                line,
            )
            if match:
                profiles.append((match.group(1), int(match.group(2)), match.group(3)))
        break
    profiles.sort(key=lambda p: p[1], reverse=True)
    return profiles


def detect_best_profile(card_name):
    best_name = None
    best_priority = -1
    for name, block in iter_card_blocks():
        if name != card_name:
            continue
        for line in block:
            match = re.match(
                r"\s+(\S+):.*priority:\s*(\d+),\s*available:\s*yes",
                line,
            )
            if match:
                profile_name = match.group(1)
                priority = int(match.group(2))
                if profile_name != "off" and priority > best_priority:
                    best_name = profile_name
                    best_priority = priority
        break
    return best_name


def iter_card_blocks():
    try:
        output = subprocess.check_output(["pactl", "list", "cards"], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return
    card_name = None
    block = []
    for line in output.splitlines():
        if line.startswith("Card #"):
            if card_name:
                yield card_name, block
            card_name = None
            block = []
            continue
        block.append(line)
        match = re.match(r"\s*Name:\s*(\S+)", line)
        if match:
            card_name = match.group(1)
    if card_name:
        yield card_name, block


def handle_resume(state, wake_delay):
    card_name = state["card_name"]
    profile = state["profile"]
    log(f"{timestamp()} Waking from sleep, waiting {wake_delay:g}s for HDMI to renegotiate...")
    time.sleep(wake_delay)
    cycle_profile(card_name, profile)


def handle_node_error(state, line):
    now = time.monotonic()
    last = state.get("last_pipewire_restart", 0.0)
    if now - last < RESTART_COOLDOWN:
        log(f"{timestamp()} Node error detected but within cooldown, skipping restart.")
        return

    log(f"{timestamp()} PipeWire node error detected: {line.strip()}")
    log(f"{timestamp()} Restarting PipeWire to recover...")
    state["last_pipewire_restart"] = now
    restart_pipewire()


def handle_cycle_command(state, command):
    parts = command.split(None, 1)
    profile = parts[1] if len(parts) > 1 else state["profile"]
    card_name = state["card_name"]
    log(f"{timestamp()} Received cycle command (profile: {profile})")
    cycle_profile(card_name, profile)


def restart_pipewire():
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "restart", "pipewire.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "pipewire-pulse.service"], check=True)
        return

    if shutil.which("rc-service"):
        try:
            subprocess.run(["rc-service", "--user", "pipewire", "restart"], check=True)
        except subprocess.CalledProcessError:
            subprocess.run(["rc-service", "pipewire", "restart"], check=False)
        try:
            subprocess.run(["rc-service", "--user", "pipewire-pulse", "restart"], check=True)
        except subprocess.CalledProcessError:
            subprocess.run(["rc-service", "pipewire-pulse", "restart"], check=False)
        return

    warn("Warning: No service manager found to restart PipeWire. Proceeding with profile cycle only.")


def cycle_profile(card_name, target_profile):
    current_profile = get_active_profile(card_name)
    if not current_profile:
        warn(f"Warning: Could not determine active profile for {card_name}. Skipping cycle.")
        return

    log(f"Cycling profile on {card_name}: {current_profile} -> off -> {target_profile}")
    if not set_card_profile(card_name, "off", attempts=20, retry_delay=0.25):
        warn("Warning: Failed to set profile to 'off'. Will retry next cycle.")
        return

    time.sleep(1.0)

    if not set_card_profile(card_name, target_profile, attempts=20, retry_delay=0.25):
        warn(f"Warning: Failed to restore profile to '{target_profile}'. Will retry next cycle.")
        return

    log(f"Profile restored to {target_profile}.")


def set_card_profile(card_name, profile, attempts=1, retry_delay=0.25):
    for attempt in range(attempts):
        result = subprocess.run(
            ["pactl", "set-card-profile", card_name, profile],
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return True
        if attempt + 1 < attempts:
            time.sleep(retry_delay)
    return False


def get_socket_path():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime_dir, "aproman.sock")


def cleanup_socket(socket_path):
    try:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
    except FileNotFoundError:
        pass


def send_command(command):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(command.encode(), get_socket_path())
    finally:
        client.close()


def decode_command(data):
    command = data.decode().strip()
    if command == "reload":
        return command
    if not command.startswith("cycle"):
        raise ValueError(f"Unsupported command: {command}")
    return command


def spawn_pw_monitor():
    if not shutil.which("pw-dump"):
        return None
    try:
        proc = subprocess.Popen(
            ["pw-dump", "--monitor", "--no-colors"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.stdout is None:
            proc.terminate()
            return None
        log("Monitoring PipeWire for node errors...")
        return proc
    except OSError:
        return None


def extract_pw_errors(buffer, initial_done, seen_error_ids):
    decoder = json.JSONDecoder()
    errors = []
    while True:
        stripped = buffer.lstrip()
        if not stripped:
            return "", initial_done, errors
        try:
            obj, end = decoder.raw_decode(stripped)
        except json.JSONDecodeError:
            return stripped, initial_done, errors
        buffer = stripped[end:]
        if not isinstance(obj, list):
            continue
        for item in obj:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "PipeWire:Interface:Node":
                continue
            node_id = item.get("id")
            info = item.get("info", {})
            state = info.get("state")
            if state == "error":
                if initial_done and node_id not in seen_error_ids:
                    props = info.get("props", {})
                    node_name = props.get("node.name", f"id={node_id}")
                    errors.append(f"node {node_name} entered error state")
                seen_error_ids.add(node_id)
            else:
                seen_error_ids.discard(node_id)
        if not initial_done:
            initial_done = True


def monitor_loop(wake_delay, state, server):
    dbus_filter = (
        "interface=org.freedesktop.login1.Manager,"
        "sender=org.freedesktop.login1,"
        "member=PrepareForSleep"
    )
    dbus_proc = subprocess.Popen(
        ["dbus-monitor", "--system", "--monitor", dbus_filter],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    pw_proc = spawn_pw_monitor()

    assert dbus_proc.stdout is not None
    dbus_fd = dbus_proc.stdout.fileno()
    pw_fd = pw_proc.stdout.fileno() if pw_proc and pw_proc.stdout else None
    sock_fd = server.fileno()
    expect_state = False
    dbus_buffer = ""
    pw_buffer = ""
    pw_initial_done = False
    pw_seen_errors: set[int] = set()

    watch_fds: list[int] = [dbus_fd, sock_fd]
    if pw_fd is not None:
        watch_fds.append(pw_fd)

    try:
        while True:
            try:
                readable, _, _ = select.select(watch_fds, [], [])
            except InterruptedError:
                continue

            for fd in readable:
                if fd == sock_fd:
                    try:
                        data = server.recv(256)
                        command = decode_command(data)
                        if command == "reload":
                            reload_conf(state)
                        else:
                            handle_cycle_command(state, command)
                    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                        warn(f"Warning: {exc}")
                elif fd == dbus_fd:
                    chunk = os.read(dbus_fd, 4096)
                    if not chunk:
                        return
                    dbus_buffer += chunk.decode()
                    while "\n" in dbus_buffer:
                        raw_line, dbus_buffer = dbus_buffer.split("\n", 1)
                        line = raw_line.strip()
                        if "member=PrepareForSleep" in line:
                            expect_state = True
                            continue
                        if not expect_state or "boolean " not in line:
                            continue

                        expect_state = False
                        if "boolean true" in line:
                            log(f"{timestamp()} Going to sleep.")
                        elif "boolean false" in line:
                            handle_resume(state, wake_delay)
                        else:
                            log(f"{timestamp()} Unknown state after PrepareForSleep: {line}")
                elif pw_fd is not None and fd == pw_fd:
                    chunk = os.read(pw_fd, 4096)
                    if not chunk:
                        watch_fds = [f for f in watch_fds if f != pw_fd]
                        pw_fd = None
                        continue
                    pw_buffer += chunk.decode()
                    pw_buffer, pw_initial_done, errors = extract_pw_errors(
                        pw_buffer, pw_initial_done, pw_seen_errors
                    )
                    for desc in errors:
                        handle_node_error(state, desc)
    finally:
        if dbus_proc.poll() is None:
            dbus_proc.terminate()
            dbus_proc.wait(timeout=5)
        if pw_proc and pw_proc.poll() is None:
            pw_proc.terminate()
            pw_proc.wait(timeout=5)


def timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    print(message, flush=True)


def warn(message):
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
