# Hearing Boost

Small desktop utility for temporary hearing balance, up to 500% boost, and the ability to choose output balance between left and right balance.

## Linux

Linux runs natively through `pactl`, so it works with PulseAudio and most PipeWire desktops through `pipewire-pulse`.

Requirements:

- Python 3
- PySide6
- `pactl`

On Linux Mint/Ubuntu:

```bash
sudo apt install python3-venv pulseaudio-utils
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run:

```bash
./run_hearing_boost.sh
```

or:

```bash
.venv/bin/python hearing_boost.py
```

## Windows

Windows uses pycaw for regular volume/balance control. Boost above 100% uses Equalizer APO.

```bash
python -m pip install -r requirements.txt pycaw comtypes
python hearing_boost.py
```
