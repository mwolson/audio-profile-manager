# aproman

Fix HDMI audio after suspend/resume on Linux systems running PipeWire +
WirePlumber.

## The Problem

When a Linux system resumes from suspend, HDMI audio devices often lose their
connection. WirePlumber tries to link to stale node proxies, resulting in
silence. The only manual fix is to open your audio settings and switch the card
profile away (for example to `off`) and back, forcing a full teardown and
rebuild of the audio nodes.

## How It Works

`aproman` runs as a service (systemd or OpenRC) and:

1. Auto-detects your HDMI audio card, or uses the one saved in the config file
2. Monitors D-Bus for `PrepareForSleep` signals from systemd-logind (or elogind)
3. On wake, waits briefly for HDMI to renegotiate, then cycles the card profile
   off and back on. The profile cycle alone is usually enough to force
   WirePlumber to rebuild fresh nodes and restore audio.
4. Separately, monitors PipeWire via `pw-dump` for nodes entering an error
   state. If one is detected, restarts PipeWire to recover (with a 30-second
   cooldown to prevent restart loops). This reactive path is a safety net for
   cases where the profile cycle alone is not enough; it is deliberately kept
   off the happy resume path because restarting PipeWire churns every client
   connected to it, and some clients (e.g. quickshell-based shells) handle the
   churn poorly.

## Requirements

- PipeWire with WirePlumber, or PulseAudio compatibility via PipeWire
- `pactl`
- `dbus-monitor`
- `pw-dump` (optional, for node error monitoring)
- A Linux distribution with systemd or OpenRC (elogind for OpenRC)

## Installation

### systemd

```bash
uv tool install aproman
aproman install-service
systemctl --user start aproman.service
```

This installs `aproman` to `~/.local/bin/`, copies the systemd user service into
place, and enables it.

### OpenRC user service (0.60+, Alpine edge, etc.)

```bash
uv tool install aproman
aproman install-service
rc-service --user aproman start
```

On OpenRC 0.60 or newer, `install-service` automatically installs a user-level
service to `~/.config/rc/init.d/aproman`. Make sure `~/.local/bin` is on your
PATH.

### OpenRC system service (older OpenRC)

```bash
sudo uv pip install --system --break-system-packages aproman
sudo aproman install-service
sudo rc-service aproman start
```

On OpenRC versions before 0.60, `install-service` installs a system-level init
script to `/etc/init.d/aproman` and adds it to the default runlevel. The service
uses `supervise-daemon` for process supervision with automatic restart.

To configure the user and environment for the daemon, create
`/etc/conf.d/aproman`:

```sh
command_user="youruser"
supervise_daemon_args="--env XDG_RUNTIME_DIR=/run/user/1000"
```

Replace `1000` with your user's UID (`id -u youruser`).

### Alternative: install.sh (systemd)

```bash
git clone https://github.com/mwolson/aproman-py.git
cd aproman-py
./install.sh
systemctl --user start aproman.service
```

This copies `aproman` to `~/.local/bin/` and installs and enables the user
service.

### Optional: set defaults

After installing, you can optionally save your preferred card and profile so
that aproman uses them instead of auto-detecting:

```bash
aproman list-cards
aproman set-default-card alsa_card.pci-0000_01_00.1

aproman list-profiles
aproman set-default-profile pro-audio
```

These write to `~/.config/aproman.conf` and signal the running daemon to pick up
the changes. Without defaults, aproman auto-detects the first HDMI card and uses
its active profile at startup.

## Usage

The service runs automatically. To check status:

#### systemd

```bash
systemctl --user status aproman.service
journalctl --user -u aproman.service -f
```

#### OpenRC (user, 0.60+)

```bash
rc-service --user aproman status
```

#### OpenRC (system, older)

```bash
rc-service aproman status
```

### Commands

aproman uses subcommands for one-off operations. With no subcommand, it runs as
a daemon.

```text
aproman                              Run as a daemon (default)
aproman cycle                        Cycle the card profile off and back on
aproman get-default-card             Print the default card from the config file
aproman get-default-profile          Print the default profile from the config file
aproman install-service              Install and enable the service (systemd or OpenRC)
aproman list-cards                   List available audio cards
aproman list-profiles                List available profiles for the card
aproman set-default-card CARD        Save default card and signal the daemon
aproman set-default-profile PROFILE  Save default profile and signal the daemon
aproman uninstall-service            Disable and remove the service (systemd or OpenRC)
```

### Daemon options

These flags apply to the daemon and to `cycle`:

```text
--card CARD            PipeWire/PulseAudio card name (default: config file, then auto-detect HDMI)
--profile PROFILE      Desired audio profile (default: config file, then active profile)
--wake-delay SECONDS   Seconds to wait after wake before cycling (default: 3.0)
```

### Configuration File

`aproman` reads defaults from `~/.config/aproman.conf` (or
`$XDG_CONFIG_HOME/aproman.conf`). The file uses one flag per line:

```text
--card=alsa_card.pci-0000_01_00.1
--profile=pro-audio
```

Only `--card` and `--profile` are supported. Unrecognized flags cause an error
at startup. Command-line arguments always take precedence over the config file.

When the daemon receives a reload signal (sent automatically by
`set-default-card` and `set-default-profile` via the Unix socket, or manually
via `kill -HUP`), it reloads the config file and updates the card and profile
for future suspend/resume cycles.

### One-Shot Fix

If audio breaks and the daemon missed the resume event (for example, after a
service restart), you can manually trigger a single profile cycle:

```bash
aproman cycle
```

This sends a cycle request to the running daemon via its Unix socket. If the
daemon is unavailable, it falls back to running the cycle directly.

When the card is stuck in the `off` state, `cycle` automatically selects the
highest-priority available profile. You can override with `--profile`:

```bash
aproman --profile pro-audio cycle
```

### Example: Custom Card and Profile

```bash
aproman set-default-card alsa_card.pci-0000_01_00.1
aproman set-default-profile output:hdmi-stereo
```

## Uninstall

```bash
aproman uninstall-service
uv tool uninstall aproman  # or: rm ~/.local/bin/aproman
rm -f ~/.config/aproman.conf
```

## Testing

```bash
bun run test                # unit tests
bun run test:integration    # Docker-based integration tests
bun run test:all            # both
```

## Hooks

```bash
bun run hooks:check         # run checks against working tree
lefthook install            # install git hooks
```

The pre-commit hook runs `uvx ruff check`, `uvx ty check`, and the unit test
suite.

## License

MIT
