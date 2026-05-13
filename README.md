# Hearing Boost

Small Windows utility for temporary hearing balance help.

## Run

Double-click:

```text
dist\HearingBoost.exe
```

For the Python version, double-click `run_hearing_boost.bat`, or run:

```powershell
python hearing_boost.py
```

If you install Equalizer APO and want boost above 100%, use `run_hearing_boost_admin.bat`.

## What works

- Controls the default Windows playback device.
- Lets you bias audio toward the left or right channel.
- Uses normal Windows endpoint volume up to 100%.

## About high boost

Windows does not expose a normal system-wide "500%" output setting for a headset. This app supports boosts above 100% through Equalizer APO, which is a low-latency Windows Audio Processing Object.

Use the app's `Install APO` button to download and launch Equalizer APO.

In the Equalizer APO Configurator, select your headset output device, finish installation, and reboot. After reboot, run this app as administrator if Windows blocks edits to:

```text
C:\Program Files\EqualizerAPO\config\config.txt
```

The app writes a small marked `Preamp` block so it can update only its own setting.

## Performance

The sliders are debounced and slow Windows audio writes run on a background worker, so dragging should stay responsive even if a headset driver takes several seconds to accept channel changes. The app writes Equalizer APO config only when the boost value actually changes, and it skips redundant Windows audio calls when the channel levels are unchanged.

## Reset

Use `Reset` in the app to return volume to 100%, center balance, and remove any active APO preamp boost by setting it back to 0 dB.

## Session safety

By default, `Restore original settings when closing` is enabled.

When enabled, closing the app restores:

- the original Windows output channel levels from when the app opened
- the original Equalizer APO config text from before the app wrote its boost block

Untick that option only if you deliberately want the current balance or boost to remain after closing the app.
