# Using HAM Digi Robot

Quick reference for the main window's controls. For install/build
instructions, see [README.md](README.md).

## Modes

**Mode** menu: CW, RTTY or BPSK63. The panels keep their layout; in RTTY the
*Pitch* box becomes the **mark tone** (the space tone is mark + shift, the
waterfall marker sits between the two), the speed controls become a
**baud rate** selector, and the CW-only controls (automatic speed,
bandwidth, manual-keying emulation) are hidden. *Squelch sensitivity* works
like in CW: the signal has to stand out of the noise (measured beside the
tone pair) by that factor; below it the squelch LED goes grey, the
waterfall dims and nothing is decoded (0 = squelch off, which prints
garbage on noise). Shift, reverse and
unshift-on-space are on the RX panel, stop bits next to the baud rate on
the TX panel.
RTTY audio goes through the sound card (Settings → Audio Devices); PTT
follows Settings → TX: with *Hamlib CAT* the same rig is keyed over CAT
around each transmission, with *Audio (sound card)* the rig must be keyed
by VOX / the audio itself. You can't
change mode while transmitting.

In **BPSK63** the *Pitch* box becomes the **carrier** frequency (the waterfall
marker sits on it; the decoder follows a few Hz of mistuning); there is
nothing else to set — the speed is fixed (62.5 baud) and the CW/RTTY
controls are hidden. Squelch, audio devices and PTT work as in RTTY. Type
plain text (upper and lower case are both sent; Enter = new line); the
code table covers ASCII up to `z` (no `{ | } ~`).

## Receive (RX panel)

- Waterfall + decoded text pane at the top. Drag the marker (anywhere in
  its tinted area) with the mouse to retune the pitch / carrier; the value
  is applied when you let go.
- **Pitch (Hz)** — set to match your radio's own sidetone frequency.
- **Bandwidth** — how wide a slice around the pitch the decoder listens
  to; **Automatic** lets it track the signal instead of a fixed width.
- **Squelch** — how strong a signal has to be before it's
  decoded; the dot next to it lights up while the squelch is open.
- **Input level** — software gain on the received audio, 0–300 % (100 % =
  unchanged). Set the sound card / rig output level first; this is for fine
  adjustment (it also changes the waterfall brightness).
- **RX speed** shows the sender's live estimated WPM.
- Select any decoded text to pop up **Callsign / RSTs / RSTr / QTH /
  Locator / Name** buttons — one click sends the selection straight into
  the matching QSO Data field below.
- Right-click the decoded text for **Clear**.

## Transmit levels (TX panel)

- **Output level** — 0–100 % of the audio sent to the sound card in the
  audio modes (RTTY, BPSK63, CW with the audio sidetone backend); adjust it
  live while sending, so the rig's ALC barely moves. Greyed out for CW over
  Hamlib CAT, where no audio is sent.

## QSO Data

Callsign, RSTs/RSTr, QTH, Locator, Name, Frequency, Start/End time for the
contact in progress. Fill by typing, or via the RX selection popup above.
Locator is always forced to upper case.

- **Start** is captured automatically the moment Callsign is first filled
  in; **End** keeps ticking to "now" until you log the QSO. Both are UTC
  (per ADIF convention), not your local time zone.
- **Frequency** — when Hamlib CAT is connected, the current VFO frequency
  is polled once a second and the field becomes read-only; otherwise type
  it in by hand.
- **✓ Log QSO** writes the contact out (file or UDP — see Settings →
  Logging) and clears the panel for the next one. Missing required fields
  (Callsign, Frequency) are highlighted in red instead of silently
  failing.

## Transmit (TX panel)

- **Quick Buttons** — eight macro buttons in two sets of four, each
  following the QSO flow (CQ, reply, report, bye): **F1–F4** and
  **F5–F8**. They fill in `{MY_CALLSIGN}`, `{CALLSIGN}`,
  `{RST_SENT}`, etc. from your own data (Settings → My Data) and the
  current QSO Data fields. Right-click a button to rename it or edit its
  text (the editor also lists the variables).
- Shortcuts: **F12** Send, **Esc** Stop, **F9** Log QSO.
- Type or edit the text box directly, then **▶ Send**; **■ Stop** aborts
  mid-transmission.
- **Speed (WPM)** sets sending speed; **⇄ RX speed** copies the RX panel's
  currently measured speed over.
- **Manual keying emulation** adds hand-sent-style timing jitter (slider
  controls how much); only available when a Hamlib rig with a working CAT
  connection is configured (see Settings → TX) — the checkbox explains why
  it's disabled otherwise via its tooltip.
- Right-click the text box for **Clear**.
- TX goes out either as an audio sidetone or as Hamlib CAT keying,
  whichever is chosen in Settings → TX.

## Settings (Settings menu → Settings…)

- **My Data** — your own callsign/name/locator/QTH, used by the macro
  variables above.
- **Audio Devices** — input/output device selection.
- **TX** — audio vs. Hamlib CAT mode, and a connection test button. Hamlib
  CAT connects either over a local **serial port** (rig model / port /
  baud rate) or over the **network to a `rigctld` daemon** (host / port,
  default `localhost:4532`) — pick one with the *Connection* selector.
- **Logging** — how **Log QSO** delivers contacts: to a local ADIF file
  (one file per your callsign) or over UDP, either as raw ADIF (Log4OM-
  style) or the WSJT-X protocol (QLog, JTAlert, GridTracker, ...).
