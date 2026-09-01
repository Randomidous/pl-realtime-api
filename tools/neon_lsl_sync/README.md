# Neon LSL Sync

A small GUI tool: pick a Neon (Pupil Labs) device from everything found on
the network, connect, and it starts pushing a synchronization marker once
a second, both to:

- an **LSL outlet** (`NeonSyncMarkers`), so any LSL-recording software
  (e.g. LabRecorder) can align its own timeline against Neon, and
- **Neon's own recording**, via `send_event`, timestamp-corrected using the
  measured clock offset between this computer and the Companion device.

## Why both?

LSL only knows about streams that publish to it. Neon's own recording only
knows about events sent to it directly. Sending the *same* labeled marker
down both paths at the same moment is what lets you later line up Neon's
recording against everything else you captured over LSL — not just "an
LSL stream exists somewhere."

## Important: events only save while Neon is recording

The Companion app **discards** any event sent via `send_event` unless a
recording is currently running on the device. The status screen shows a
best-effort "Neon recording: ..." indicator for this reason. The LSL side
of the sync is unaffected — that marker gets pushed regardless of Neon's
recording state.

## Running from source

```sh
pip install -r requirements.txt
python app.py
```

Requires the Neon Companion app open and the computer on the same network
as the glasses.

## Packaging as a Windows exe (PyInstaller)

`pylsl` ships a native `liblsl` shared library that PyInstaller's import
analysis does not pick up automatically, and device discovery pulls in
`zeroconf`, whose platform backends can also get missed. A onefile build
that at least accounts for both:

```sh
pyinstaller --onefile --windowed --name NeonLSLSync ^
  --collect-all pylsl ^
  --collect-all zeroconf ^
  --hidden-import=pupil_labs.realtime_api ^
  app.py
```

(`^` is the Windows cmd line-continuation character; use `\` in a POSIX
shell instead.)

After building, **test discovery specifically** in the frozen exe — it is
the step most likely to silently break under PyInstaller, even when the
exe launches and the window opens fine.

## Known limitations / things to revisit

- `is_recording()` reads a private (`_status`) attribute of the simple API
  because there is no public "is currently recording" property as of this
  writing — treat it as best-effort display info, not something else in
  the app should depend on.
- Reconnection is a fixed backoff retry; if the Companion device is gone
  for a long time (phone locked, app killed) this will keep retrying
  quietly rather than alerting loudly - consider a max-retry UI warning if
  that's the wrong tradeoff for how this gets used.
- The clock offset is re-estimated every 2 minutes
  (`TIME_OFFSET_REFRESH_SECONDS`) rather than continuously; for very long
  sessions or Companion devices with wonky NTP, tightening this may help.
