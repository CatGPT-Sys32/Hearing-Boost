# Hearing Boost

Small desktop utility for temporary hearing balance, up to 500% boost, and the ability to choose output balance between left and right balance.

Current version: `1.1.0`

## Features

- Native Qt UI for Windows and Linux
- Background audio apply worker so slider changes do not block the interface
- Output device selector
- Session restore on exit
- System tray support
- Quick presets for normal, left ear, right ear, and loud environments

## Linux

Linux uses `pactl`, so it works with PulseAudio and most PipeWire desktops through `pipewire-pulse`.

```bash
sudo apt install python3-venv pulseaudio-utils
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run_hearing_boost.sh
```

Install a desktop launcher:

```bash
./scripts/install_linux_desktop.sh
```

Start hidden in the tray:

```bash
./run_hearing_boost.sh --minimized
```

## Windows

Windows uses `pycaw` for regular volume and balance control. Boost above 100% uses Equalizer APO.

```powershell
python -m pip install -r requirements.txt pycaw comtypes
python hearing_boost.py
```

Build a Windows executable:

```powershell
.\scripts\build_windows.ps1
```
