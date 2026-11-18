# HAM Digi Robot

A desktop multi-mode amateur radio digital transceiver application,
forked from [CW Robot](https://github.com/dacrhu/cwrobot): decode what you
receive over your radio's audio, and send back — straight from your
computer. The window layout is the same in every mode; the mode-specific
settings (speed, tones, …) change with the **Mode** menu.

Modes so far: **CW** (Morse; audio sidetone or Hamlib CAT keying),
**RTTY** (Baudot/ITA2 AFSK through the sound card, optional Hamlib CAT PTT)
and **BPSK63** (PSK Varicode through the sound card, optional Hamlib CAT PTT).

![HAM Digi Robot main window](assets/main_window.png)

## Usage

See [USAGE.md](USAGE.md) for a quick rundown of the controls (RX, TX,
macros, QSO logging).

## Features

### Modes (Mode menu)

The window layout is the same in every mode; only the mode-specific
controls change. Switching mode (and starting the app) also puts the rig
into the matching mode over CAT, when a Hamlib connection is configured.

**CW**
- **Receive** — waterfall display, an adaptive Goertzel-based decoder that
  tracks variable/hand-keyed sending speed, adjustable squelch and
  detection bandwidth (automatic or manual), a tunable pitch reference to
  match your radio's sidetone. The waterfall marks the side of the pitch
  that the radio's sideband puts the signal on (LSB/USB selector next to
  the pitch; it follows the rig automatically over CAT).
- **Transmit** — audio-tone sidetone output or Hamlib CAT keying,
  adjustable speed with a one-click "match RX speed" shortcut, manual-
  keying jitter emulation.

**RTTY** (Baudot/ITA2, AFSK through the sound card)
- **Receive** — two-tone (mark/space) decoder with a one-bit matched
  filter and UART framing (a level squelch like the CW one: the signal
  must stand out of the noise measured beside the tone pair); the waterfall shows labelled MARK and SPACE markers with
  tolerance bands, to tune the signal in by.
- **Transmit** — phase-continuous AFSK, ITA2/US-TTY Baudot with automatic
  LTRS/FIGS shifts, optional CAT PTT (follows the TX tab: the same rig).
  The rig is put into data-USB (Hamlib PKTUSB, e.g. Yaesu DATA-USB),
  falling back to USB.
- **Settings, all on the main window** — baud rate (45.45 / 50 / 75 /
  100), mark tone (default 2125 Hz), shift (default 170 Hz), stop bits
  (1 / 1.5 / 2), reverse, unshift-on-space.

**BPSK63** (PSK31 Varicode at 62.5 baud, through the sound card)
- **Receive** — differential BPSK detector (one-symbol matched filter,
  automatic symbol timing and a few Hz of frequency tracking) feeding a
  Varicode decoder, with the same kind of level squelch as RTTY. The
  waterfall marker shows the carrier to tune the signal in by.
- **Transmit** — raised-cosine-shaped phase reversals on a single audio
  carrier (the *Carrier* box, default 1000 Hz), optional CAT PTT (same rig
  as the TX tab; the rig goes to data-USB like in RTTY). Logged to ADIF as
  `MODE=PSK`, `SUBMODE=BPSK63`.

### Common to all modes

- **Macros** — eight customizable quick-text buttons (CQ, reply, report,
  sign-off × 2, renamable, on F1–F8; F12 sends, Esc stops, F9 logs).
- **QSO logging** — one click logs the current contact (ADIF `MODE` follows the active mode) to an ADIF file
  (one growing file per operator callsign) or sends it live over UDP,
  either as a bare ADIF record (for Log4OM-style listeners) or framed as a
  WSJT-X Network Message ("Logged ADIF"), which QLog, JTAlert, GridTracker
  and others also understand.
- **Hamlib CAT** — connect over a local serial port (rig model picker
  searchable across the ~300 models Hamlib supports, serial port
  autodetection) or over TCP to a running `rigctld` daemon; live VFO
  frequency polling on its own background thread (so it won't freeze the
  UI even if the radio is off or disconnected), and a one-click
  connection test.
- Picks up any input/output PipeWire or PulseAudio exposes, not just plain
  ALSA hardware devices — including another application's monitor/sink as
  an RX source.

## Download

The easiest way to run HAM Digi Robot is a prebuilt package from the
[Releases page](https://github.com/dacrhu/hamdigirobot/releases) — each one
bundles its own Python, PySide6/Qt6, and Hamlib, so nothing needs to be
installed first.

**Linux — AppImage** (the primary, most-tested platform):

```sh
chmod +x HAM_Digi_Robot-x86_64.AppImage
./HAM_Digi_Robot-x86_64.AppImage
```

It's built on a recent Ubuntu/Fedora glibc, so it should run on most
current Linux distros; if it doesn't start on yours, please open an issue.
PipeWire device routing still needs `pactl` on the host (see "Audio
handling" below) — everything else is self-contained. See
`packaging/build_appimage.sh` if you'd rather build it yourself.

**Windows and macOS (Apple Silicon) — beta.** Built the same way in CI
(`.github/workflows/windows-macos-build.yml`, PyInstaller-based) and
verified there with an automated startup smoke test on real Windows/macOS
GitHub Actions runners, but nobody on this project has Windows/Mac
hardware to test on by hand yet — reports of what does or doesn't work are
very welcome. On macOS, the app is unsigned (ad-hoc signed only), so
Gatekeeper will block the first launch: right-click → Open, or run
`xattr -d com.apple.quarantine "HAM Digi Robot.app"`.

## Requirements

- Linux (the most-tested platform). Windows and macOS (Apple Silicon)
  builds are available too — see "Download" above — but are new and less
  battle-tested; `pyserial`, the one OS-facing dependency besides
  audio/Qt, is cross-platform, so this should keep working, but
  reports/contributions are welcome either way.
- Python 3.11+
- [Hamlib](https://hamlib.github.io/) (`libhamlib.so` / `.dll` / `.dylib`)
  — only needed for CAT keying; audio-tone TX and RX work fine without it.
  Bundled already in every prebuilt package above; only relevant if you're
  running from source.

## Installation (Fedora)

Just want to run the app? Grab the prebuilt AppImage from the
[Releases page](https://github.com/dacrhu/hamdigirobot/releases) instead — see
"Download" above. The rest of this section is for running from source or
contributing.

The project relies on system packages where it can (PySide6, numpy, scipy)
and only pulls the awkward-to-package dependencies (`sounddevice`) into the
virtual environment via pip:

```sh
sudo dnf install python3-pyside6 hamlib hamlib-devel python3-pyserial
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

(`hamlib-devel` is only needed at development time, as a header reference
for `<hamlib/rig.h>` — at runtime, the `hamlib` package's `libhamlib.so` is
enough; see below.)

`--system-site-packages` matters here because Fedora's `python3-pyside6`
package is tightly tied to the system's own Qt6 install (platform plugins,
themes) — a separate pip-installed PySide6 wheel would conflict with it.

### Audio handling (PipeWire/PulseAudio)

The PortAudio build shipped by every current distro has no PulseAudio
hostAPI, so PipeWire's own virtual/monitor sources and sinks don't show up
directly in `sd.query_devices()`. HAM Digi Robot doesn't work around this by
writing ALSA config (no `~/.config/alsa/asoundrc` or `conf.d/*.conf`
poking) — instead it uses `pactl`'s PulseAudio-compatible protocol: the
stream always opens on the safe ALSA "default" device, then at startup the
app runs `pactl move-sink-input` / `pactl move-source-output` to move its
own stream onto the selected PipeWire sink/source afterward (see
`hamdigirobot/audio/pipewire_route.py`). This needs the `pactl` binary to be
available (Fedora's `pipewire-pulseaudio` package provides it) — if it's
missing, or PipeWire isn't running, that specific selection just isn't
available; every other device still works normally.

### Hamlib CAT keying

CAT keying only needs the `hamlib` package's shared library
(`libhamlib.so`) at runtime — `ctypes` loads it directly, no compilation
or `hamlib-devel` headers required (those are only useful at development
time, as a `<hamlib/rig.h>` reference; see `hamdigirobot/hamlib/
ctypes_bindings.py`'s module docstring for why Hamlib's internal struct
layouts never need to be probed). If `libhamlib.so` isn't present on the
system, the CAT feature disables itself with a clear error message —
receive and audio-tone TX keep working normally regardless.

The Settings dialog's "TX" tab also offers a live-queried serial port
dropdown (via `pyserial`, see `hamdigirobot/hamlib/serial_ports.py`); the field
stays editable too, for when the radio isn't plugged in yet or is on an
unusual path. `pyserial` itself is cross-platform (Linux/Windows/macOS),
so port listing should work under Windows too, with no OS-specific code.

The same tab's **Connection** selector switches from "Serial port" to
"Network — rigctld (TCP)", which instead connects to a running `rigctld`
daemon at a given host/port (default `localhost:4532`) via Hamlib's own
NETRIGCTL backend. `rigctld` already owns the real rig, so no rig model or
baud rate needs picking in that mode — handy when another program (a
logger, WSJT-X, …) is already holding the serial port.

### Building the AppImage

```sh
./packaging/build_appimage.sh
```

Downloads a portable, prebuilt CPython AppImage
([niess/python-appimage](https://github.com/niess/python-appimage)), pip
installs HAM Digi Robot and its dependencies into it, copies this machine's
`libhamlib.so` (plus its own transitive shared-library dependencies) in
alongside it, and packs the result with `appimagetool` (must be on
`PATH`) into `dist/HAM_Digi_Robot-x86_64.AppImage`. See the script's own
comments for the exact steps and the current portability caveat (the
bundled Hamlib is linked against whatever glibc this build machine has).
A GitHub Actions workflow (`.github/workflows/appimage.yml`) runs the same
script and attaches the result to the GitHub Release on every tag push.

### Building the Windows/macOS packages

These aren't built with a single standalone script the way the AppImage
is — `.github/workflows/windows-macos-build.yml` runs
[PyInstaller](https://pyinstaller.org/) against `packaging/hamdigirobot.spec`
directly, bundling in a Hamlib runtime for each platform
(`packaging/bundle_hamlib_macos.sh` re-links a Homebrew-installed copy on
macOS, then the workflow ad-hoc re-signs the whole bundle; Windows
downloads Hamlib's own official prebuilt zip). See that workflow file's
comments for the full sequence, including the `--smoke-test`-based
verification (`hamdigirobot.app`/`hamdigirobot.hamlib.ctypes_bindings`) that runs on
every build.

## Running

```sh
hamdigirobot
```

(installed as a console script by `pip install -e .`) or, without
installing it as a script:

```sh
python -m hamdigirobot.app
```

## Configuration

Settings are stored as human-readable JSON at
`$XDG_CONFIG_HOME/hamdigirobot/config.json` (typically
`~/.config/hamdigirobot/config.json`) — deliberately not `QSettings`, so the
file stays easy to read or hand-edit, and carries an explicit schema
version so it can be migrated forward cleanly as settings are added (see
`hamdigirobot/config.py`).

## Tests

```sh
pytest
```

## License

MIT.
