import ctypes
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QSlider,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:  # pragma: no cover - shown at runtime
    Qt = None
    QTimer = None
    QIcon = None
    QApplication = None
    QCheckBox = None
    QFrame = None
    QHBoxLayout = None
    QLabel = None
    QMainWindow = None
    QMessageBox = None
    QPushButton = None
    QSlider = None
    QVBoxLayout = None
    QWidget = None
    QT_IMPORT_ERROR = exc
else:
    QT_IMPORT_ERROR = None

QT_MAIN_WINDOW_BASE = QMainWindow if QMainWindow is not None else object

try:
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
except Exception as exc:  # pragma: no cover - shown in GUI at runtime
    comtypes = None
    AudioUtilities = None
    IAudioEndpointVolume = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


APP_NAME = "Hearing Boost"
APP_VERSION = "1.0.1"
GITHUB_URL = "https://github.com/CatGPT-Sys32"
APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "hearing-boost-icon.svg"
APO_DOWNLOAD_URL = "https://sourceforge.net/projects/equalizerapo/files/1.4.2/EqualizerAPO-x64-1.4.2.exe/download"
APO_INSTALLER_PATH = Path(__file__).resolve().parent / "installers" / "EqualizerAPO-latest.exe"
APO_CONFIG_CANDIDATES = [
    Path(r"C:\Program Files\EqualizerAPO\config\config.txt"),
    Path(r"C:\Program Files (x86)\EqualizerAPO\config\config.txt"),
]
BOOST_MARKER_BEGIN = "# Hearing Boost begin"
BOOST_MARKER_END = "# Hearing Boost end"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
UI_FONT = "Segoe UI" if IS_WINDOWS else "Cantarell"
UI_BG = "#000000"
UI_SURFACE = "#ffffff"
UI_TEXT = "#101010"
UI_MUTED = "#666660"
UI_LINE = "#deded8"
UI_ACCENT = "#000000"
UI_BUTTON_ACTIVE = "#eeeee8"
UI_BUTTON_PRESSED = "#e4e4dc"
UI_BORDER = "#9b9b94"


@dataclass
class ApoState:
    path: Path | None
    writable: bool
    message: str


@dataclass
class ApoSnapshot:
    path: Path
    original_text: str


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_apo_config() -> ApoState:
    if not IS_WINDOWS:
        return ApoState(
            path=None,
            writable=False,
            message="Using PulseAudio/PipeWire through pactl.",
        )

    for path in APO_CONFIG_CANDIDATES:
        if path.exists():
            try:
                with path.open("a", encoding="utf-8"):
                    pass
            except OSError:
                return ApoState(
                    path=path,
                    writable=False,
                    message=f"Found Equalizer APO, but {path} is not writable. Run as admin for >100%.",
                )
            return ApoState(
                path=path,
                writable=True,
                message=f"Equalizer APO boost enabled via {path}",
            )

    return ApoState(
        path=None,
        writable=False,
        message="Using Windows audio controls. Equalizer APO not found; boost is capped at 100%.",
    )


def db_for_boost(boost_percent: float) -> float:
    if boost_percent <= 100:
        return 0.0
    return 20.0 * math.log10(boost_percent / 100.0)


def update_apo_preamp(path: Path, boost_percent: float) -> None:
    preamp_db = db_for_boost(boost_percent)
    block = f"{BOOST_MARKER_BEGIN}\nPreamp: {preamp_db:.2f} dB\n{BOOST_MARKER_END}\n"

    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    start = text.find(BOOST_MARKER_BEGIN)
    end = text.find(BOOST_MARKER_END)
    if start != -1 and end != -1 and end > start:
        end += len(BOOST_MARKER_END)
        new_text = text[:start] + block.rstrip("\n") + text[end:]
    else:
        separator = "" if not text or text.endswith("\n") else "\n"
        new_text = f"{text}{separator}\n{block}"

    path.write_text(new_text, encoding="utf-8")


def restore_apo_snapshot(snapshot: ApoSnapshot) -> None:
    snapshot.path.write_text(snapshot.original_text, encoding="utf-8")


def download_apo_installer(destination: Path) -> None:
    import urllib.request

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(APO_DOWNLOAD_URL, headers={"User-Agent": "Mozilla/5.0"})
    page = urllib.request.urlopen(request, timeout=30).read()
    match = re.search(
        rb'https://downloads\.sourceforge\.net/project/equalizerapo/1\.4\.2/EqualizerAPO-x64-1\.4\.2\.exe[^"\']+',
        page,
    )
    download_url = html.unescape(match.group(0).decode("utf-8")) if match else APO_DOWNLOAD_URL
    request = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()

    if not data.startswith(b"MZ"):
        raise RuntimeError("SourceForge did not return a Windows installer.")
    destination.write_bytes(data)


class WindowsAudio:
    backend_name = "Windows"

    def __init__(self) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("Windows audio controls are only available on Windows.")
        if IMPORT_ERROR:
            raise RuntimeError(f"Missing audio dependency: {IMPORT_ERROR}")
        comtypes.CoInitialize()
        self.device = AudioUtilities.GetSpeakers()
        self.volume = getattr(self.device, "EndpointVolume", None)
        if self.volume is None:
            endpoint = self.device._dev.Activate(IAudioEndpointVolume._iid_, 23, None)
            self.volume = endpoint.QueryInterface(IAudioEndpointVolume)
        self._last_channels: tuple[float, ...] | None = None
        self.original_channels = self.read_channel_scalars()

    @property
    def device_name(self) -> str:
        return getattr(self.device, "FriendlyName", "Default playback device")

    @property
    def channel_count(self) -> int:
        return int(self.volume.GetChannelCount())

    def read_channel_scalars(self) -> tuple[float, ...]:
        count = self.channel_count
        if count >= 2:
            return tuple(
                max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(channel))))
                for channel in range(count)
            )
        return (max(0.0, min(1.0, float(self.volume.GetMasterVolumeLevelScalar()))),)

    def restore_original(self) -> None:
        count = self.channel_count
        self.restore_channels(self.original_channels)
        self._last_channels = self.original_channels

    def restore_channels(self, channels: tuple[float, ...]) -> None:
        count = self.channel_count
        if count >= 2 and len(channels) >= 2:
            for channel, scalar in enumerate(channels[:count]):
                self.volume.SetChannelVolumeLevelScalar(channel, scalar, None)
        else:
            self.volume.SetMasterVolumeLevelScalar(channels[0], None)

    def read_state(self) -> tuple[int, int]:
        master = max(0.0, min(1.0, float(self.volume.GetMasterVolumeLevelScalar())))
        count = self.channel_count
        if count < 2:
            return round(master * 100), 0

        left = max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(0))))
        right = max(0.0, min(1.0, float(self.volume.GetChannelVolumeLevelScalar(1))))
        loudest = max(left, right, 0.01)
        if left >= right:
            balance = -round((1.0 - (right / loudest)) * 100)
        else:
            balance = round((1.0 - (left / loudest)) * 100)
        return round(loudest * 100), balance

    def apply(self, boost_percent: float, balance_percent: float, apo_active: bool) -> tuple[float, float, float]:
        windows_percent = min(boost_percent, 100.0)
        base = max(0.0, min(1.0, windows_percent / 100.0))
        balance = max(-100.0, min(100.0, balance_percent)) / 100.0

        left_gain = 1.0
        right_gain = 1.0
        if balance > 0:
            left_gain = 1.0 - balance
        elif balance < 0:
            right_gain = 1.0 + balance

        left = max(0.0, min(1.0, base * left_gain))
        right = max(0.0, min(1.0, base * right_gain))

        count = self.channel_count
        channels = (left, right, *([base] * max(0, count - 2))) if count >= 2 else (base,)
        if self._last_channels and len(self._last_channels) == len(channels):
            if all(abs(old - new) < 0.002 for old, new in zip(self._last_channels, channels)):
                return base * 100.0, left * 100.0, right * 100.0

        previous_channels = self.read_channel_scalars()
        try:
            if count >= 2:
                self.volume.SetChannelVolumeLevelScalar(0, left, None)
                self.volume.SetChannelVolumeLevelScalar(1, right, None)
                for channel in range(2, count):
                    self.volume.SetChannelVolumeLevelScalar(channel, base, None)
            else:
                self.volume.SetMasterVolumeLevelScalar(base, None)
        except Exception:
            self.restore_channels(previous_channels)
            self._last_channels = previous_channels
            raise

        self._last_channels = channels
        return base * 100.0, left * 100.0, right * 100.0


class LinuxAudio:
    backend_name = "Linux"
    sink_name = "@DEFAULT_SINK@"

    def __init__(self) -> None:
        if not IS_LINUX:
            raise RuntimeError("Linux audio controls are only available on Linux.")
        if not shutil.which("pactl"):
            raise RuntimeError("pactl was not found. Install PulseAudio utilities or PipeWire Pulse compatibility.")
        self.default_sink = self._run_pactl("get-default-sink").strip()
        if not self.default_sink:
            raise RuntimeError("Could not determine the default audio output sink.")
        self._last_channels: tuple[float, ...] | None = None
        self.original_channels = self.read_channel_scalars()

    def _run_pactl(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["pactl", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("pactl was not found.") from exc
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"pactl failed: {message}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pactl timed out.") from exc
        return completed.stdout

    def _default_sink_info(self) -> dict:
        output = self._run_pactl("--format=json", "list", "sinks")
        try:
            sinks = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Could not parse pactl sink information.") from exc

        for sink in sinks:
            if sink.get("name") == self.default_sink:
                return sink
        raise RuntimeError(f"Default sink was not found: {self.default_sink}")

    @property
    def device_name(self) -> str:
        info = self._default_sink_info()
        return str(info.get("description") or info.get("name") or "Default playback device")

    @property
    def channel_count(self) -> int:
        return len(self.read_channel_scalars())

    def read_channel_scalars(self) -> tuple[float, ...]:
        info = self._default_sink_info()
        volumes = info.get("volume")
        if not isinstance(volumes, dict) or not volumes:
            raise RuntimeError("Default sink does not expose channel volume information.")

        channels: list[float] = []
        for channel in volumes.values():
            if not isinstance(channel, dict):
                continue
            percent_text = str(channel.get("value_percent", "")).strip()
            match = re.search(r"(-?\d+(?:\.\d+)?)%", percent_text)
            if match:
                channels.append(max(0.0, float(match.group(1)) / 100.0))
                continue
            value = channel.get("value")
            if isinstance(value, (int, float)):
                channels.append(max(0.0, float(value) / 65536.0))

        if not channels:
            raise RuntimeError("Could not read default sink channel volumes.")
        return tuple(channels)

    def restore_original(self) -> None:
        self.restore_channels(self.original_channels)
        self._last_channels = self.original_channels

    def restore_channels(self, channels: tuple[float, ...]) -> None:
        self._set_channel_volumes(channels)

    def read_state(self) -> tuple[int, int]:
        channels = self.read_channel_scalars()
        if len(channels) < 2:
            return round(channels[0] * 100), 0

        left = max(0.0, channels[0])
        right = max(0.0, channels[1])
        loudest = max(left, right, 0.01)
        if left >= right:
            balance = -round((1.0 - (right / loudest)) * 100)
        else:
            balance = round((1.0 - (left / loudest)) * 100)
        return round(loudest * 100), balance

    def _set_channel_volumes(self, channels: tuple[float, ...]) -> None:
        volume_args = [f"{max(0.0, channel) * 100.0:.0f}%" for channel in channels]
        self._run_pactl("set-sink-volume", self.sink_name, *volume_args)

    def apply(self, boost_percent: float, balance_percent: float, apo_active: bool) -> tuple[float, float, float]:
        base = max(0.0, min(5.0, boost_percent / 100.0))
        balance = max(-100.0, min(100.0, balance_percent)) / 100.0

        left_gain = 1.0
        right_gain = 1.0
        if balance > 0:
            left_gain = 1.0 - balance
        elif balance < 0:
            right_gain = 1.0 + balance

        left = max(0.0, base * left_gain)
        right = max(0.0, base * right_gain)
        count = max(1, self.channel_count)
        channels = (left, right, *([base] * max(0, count - 2))) if count >= 2 else (base,)
        if self._last_channels and len(self._last_channels) == len(channels):
            if all(abs(old - new) < 0.002 for old, new in zip(self._last_channels, channels)):
                return base * 100.0, left * 100.0, right * 100.0

        previous_channels = self.read_channel_scalars()
        try:
            self._set_channel_volumes(channels)
        except Exception:
            self.restore_channels(previous_channels)
            self._last_channels = previous_channels
            raise

        self._last_channels = channels
        return base * 100.0, left * 100.0, right * 100.0


def create_audio_backend() -> WindowsAudio | LinuxAudio:
    if IS_WINDOWS:
        return WindowsAudio()
    if IS_LINUX:
        return LinuxAudio()
    raise RuntimeError(f"{sys.platform} is not supported yet.")


def make_separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def app_icon():
    if QIcon is None or not APP_ICON_PATH.exists():
        return None
    icon = QIcon(str(APP_ICON_PATH))
    return None if icon.isNull() else icon


def open_external_url(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


class HearingBoostApp(QT_MAIN_WINDOW_BASE):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        icon = app_icon()
        if icon:
            self.setWindowIcon(icon)
        self.resize(460, 560)
        self.setMinimumSize(420, 500)

        self.audio: WindowsAudio | LinuxAudio | None = None
        self.apo = find_apo_config()
        self.last_apo_boost: int | None = None
        self.apo_snapshot: ApoSnapshot | None = None
        self.closing = False
        self.loading = False

        self.apply_timer = QTimer(self)
        self.apply_timer.setSingleShot(True)
        self.apply_timer.timeout.connect(self.apply_settings)

        self._apply_style()
        self._build()
        self._connect_audio()
        self._capture_apo_snapshot()
        self._schedule_apply()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"""
            QWidget {{
                background: #000000;
                color: #f7f7f2;
                font-family: "{UI_FONT}";
                font-size: 10pt;
            }}
            QLabel#title {{
                color: #ffffff;
                font-size: 21pt;
                font-weight: 700;
            }}
            QLabel#caption, QLabel#sliderMark, QLabel#footer {{
                color: #8b8b84;
                font-size: 8.5pt;
            }}
            QLabel#credits {{
                color: #8b8b84;
                font-size: 8.5pt;
            }}
            QLabel#credits a {{
                color: #ffffff;
                text-decoration: none;
            }}
            QLabel#section {{
                color: #f4f4ef;
                font-size: 10pt;
            }}
            QLabel#metric {{
                color: #ffffff;
                font-size: 26pt;
                font-weight: 700;
            }}
            QLabel#balanceMetric {{
                color: #ffffff;
                font-size: 14pt;
                font-weight: 700;
            }}
            QLabel#status {{
                color: #ffffff;
                font-size: 10pt;
            }}
            QFrame#separator {{
                color: #222222;
                background: #222222;
                max-height: 1px;
                border: 0;
            }}
            QPushButton {{
                background: #050505;
                color: #ffffff;
                border: 1px solid #3a3a3a;
                padding: 8px 14px;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background: #151515;
                border-color: #686868;
            }}
            QPushButton:pressed {{
                background: #242424;
            }}
            QCheckBox {{
                color: #ffffff;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 13px;
                height: 13px;
                border: 1px solid #ffffff;
                background: #000000;
            }}
            QCheckBox::indicator:checked {{
                background: #ffffff;
                image: none;
            }}
            QSlider {{
                min-height: 30px;
                max-height: 30px;
            }}
            QSlider::groove:horizontal {{
                height: 2px;
                background: #333333;
            }}
            QSlider::sub-page:horizontal {{
                background: #ffffff;
            }}
            QSlider::add-page:horizontal {{
                background: #333333;
            }}
            QSlider::handle:horizontal {{
                background: #ffffff;
                border: 1px solid #ffffff;
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #cfcfc8;
                border-color: #cfcfc8;
            }}
            """
        )

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(34, 30, 34, 28)
        layout.setSpacing(0)

        self.title_label = QLabel(APP_NAME)
        self.title_label.setObjectName("title")
        layout.addWidget(self.title_label)

        self.device_label = QLabel("Default playback device")
        self.device_label.setObjectName("caption")
        self.device_label.setWordWrap(True)
        layout.addWidget(self.device_label)
        layout.addSpacing(30)

        boost_header = QHBoxLayout()
        boost_header.setContentsMargins(0, 0, 0, 0)
        boost_title = QLabel("Boost")
        boost_title.setObjectName("section")
        boost_header.addWidget(boost_title)
        boost_header.addStretch(1)
        self.boost_label = QLabel("100%")
        self.boost_label.setObjectName("metric")
        boost_header.addWidget(self.boost_label)
        layout.addLayout(boost_header)

        self.boost_slider = QSlider(Qt.Orientation.Horizontal)
        self.boost_slider.setRange(0, 500)
        self.boost_slider.setValue(100)
        self.boost_slider.valueChanged.connect(self._on_boost_changed)
        layout.addWidget(self.boost_slider)

        boost_marks = self._mark_row("0%", "500%")
        layout.addLayout(boost_marks)
        layout.addSpacing(26)
        layout.addWidget(make_separator())
        layout.addSpacing(26)

        balance_header = QHBoxLayout()
        balance_header.setContentsMargins(0, 0, 0, 0)
        balance_title = QLabel("Balance")
        balance_title.setObjectName("section")
        balance_header.addWidget(balance_title)
        balance_header.addStretch(1)
        self.left_label = QLabel("L --")
        self.left_label.setObjectName("balanceMetric")
        self.right_label = QLabel("R --")
        self.right_label.setObjectName("balanceMetric")
        balance_header.addWidget(self.left_label)
        balance_header.addSpacing(14)
        balance_header.addWidget(self.right_label)
        layout.addLayout(balance_header)

        self.balance_slider = QSlider(Qt.Orientation.Horizontal)
        self.balance_slider.setRange(-100, 100)
        self.balance_slider.setValue(0)
        self.balance_slider.valueChanged.connect(self._on_balance_changed)
        layout.addWidget(self.balance_slider)

        self.balance_value_label = QLabel("Center")
        self.balance_value_label.setObjectName("caption")
        self.balance_value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.balance_value_label)
        layout.addLayout(self._mark_row("Left", "Right"))
        layout.addSpacing(26)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        self.center_button = QPushButton("Center balance")
        self.center_button.clicked.connect(self.center_balance)
        self.reset_button = QPushButton("Reset session")
        self.reset_button.clicked.connect(self.reset)
        controls.addWidget(self.center_button)
        controls.addStretch(1)
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)
        layout.addSpacing(22)

        self.session_checkbox = QCheckBox("Restore original settings when closing")
        self.session_checkbox.setChecked(True)
        layout.addWidget(self.session_checkbox)
        layout.addSpacing(24)
        layout.addWidget(make_separator())
        layout.addSpacing(16)

        self.status_label = QLabel("Starting...")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.apo_label = QLabel(self.apo.message)
        self.apo_label.setObjectName("footer")
        self.apo_label.setWordWrap(True)
        layout.addWidget(self.apo_label)
        layout.addStretch(1)

        self.credits_label = QLabel(f'v{APP_VERSION}  |  by <a href="{GITHUB_URL}">CatGPT-Sys32</a>')
        self.credits_label.setObjectName("credits")
        self.credits_label.setOpenExternalLinks(False)
        self.credits_label.setTextFormat(Qt.TextFormat.RichText)
        self.credits_label.linkActivated.connect(open_external_url)
        layout.addWidget(self.credits_label)

        if IS_WINDOWS:
            layout.addSpacing(14)
            apo_controls = QHBoxLayout()
            self.install_button = QPushButton("Install APO")
            self.install_button.clicked.connect(self.install_apo)
            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh_apo)
            apo_controls.addWidget(self.install_button)
            apo_controls.addWidget(self.refresh_button)
            apo_controls.addStretch(1)
            layout.addLayout(apo_controls)

    def _mark_row(self, left: str, right: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        left_label = QLabel(left)
        left_label.setObjectName("sliderMark")
        right_label = QLabel(right)
        right_label.setObjectName("sliderMark")
        row.addWidget(left_label)
        row.addStretch(1)
        row.addWidget(right_label)
        return row

    def _connect_audio(self) -> None:
        try:
            self.audio = create_audio_backend()
            current_volume, current_balance = self.audio.read_state()
            self.loading = True
            self.boost_slider.setValue(current_volume)
            self.balance_slider.setValue(current_balance)
            self.loading = False
            self._update_labels()
            self.device_label.setText(self.audio.device_name)
            self.status_label.setText("Ready.")
        except Exception as exc:
            self.status_label.setText(f"Could not access audio controls: {exc}")
            QMessageBox.critical(self, APP_NAME, str(exc))

    def _capture_apo_snapshot(self) -> None:
        if self.apo.path and self.apo.path.exists() and self.apo.writable:
            try:
                self.apo_snapshot = ApoSnapshot(
                    path=self.apo.path,
                    original_text=self.apo.path.read_text(encoding="utf-8", errors="ignore"),
                )
            except OSError:
                self.apo_snapshot = None

    def _on_boost_changed(self) -> None:
        self._update_labels()
        if not self.loading:
            self._schedule_apply()

    def _on_balance_changed(self) -> None:
        self._update_labels()
        if not self.loading:
            self._schedule_apply()

    def _schedule_apply(self, delay_ms: int = 140) -> None:
        self.apply_timer.start(delay_ms)

    def _update_labels(self) -> None:
        boost = self.boost_slider.value()
        balance = self.balance_slider.value()
        self.boost_label.setText(f"{boost}%")
        if balance == 0:
            self.balance_value_label.setText("Center")
        else:
            self.balance_value_label.setText(f"{abs(balance)}% {'R' if balance > 0 else 'L'}")

    def apply_settings(self) -> None:
        if self.closing:
            return
        boost = self.boost_slider.value()
        balance = self.balance_slider.value()
        self.boost_label.setText(f"{boost}%")

        if not self.audio:
            return

        self.status_label.setText("Applying...")
        try:
            windows, left, right, apo_active = self._apply_settings_blocking(boost, balance)
        except Exception as exc:
            self.status_label.setText(f"Failed to apply audio settings: {exc}")
            return

        self.left_label.setText(f"L {left:.0f}%")
        self.right_label.setText(f"R {right:.0f}%")
        backend_name = self.audio.backend_name
        if apo_active:
            boost_note = f", APO +{db_for_boost(boost):.1f} dB"
        elif IS_LINUX and boost > 100:
            boost_note = ", software boost"
        else:
            boost_note = ""
        if IS_WINDOWS and boost > 100 and not apo_active:
            self.status_label.setText("Boost above 100% needs Equalizer APO. Applying 100% Windows volume instead.")
        else:
            self.status_label.setText(f"Applied {backend_name} {windows:.0f}%: L {left:.0f}% / R {right:.0f}%{boost_note}")

    def _apply_settings_blocking(self, boost: int, balance: int) -> tuple[float, float, float, bool]:
        if not self.audio:
            raise RuntimeError("Audio is not connected.")

        apo_active = False
        if IS_WINDOWS and boost > 100:
            if self.apo.path and self.apo.writable:
                try:
                    if self.last_apo_boost != boost:
                        update_apo_preamp(self.apo.path, boost)
                        self.last_apo_boost = boost
                    apo_active = True
                except OSError as exc:
                    raise RuntimeError(f"Could not write Equalizer APO config: {exc}") from exc
            else:
                pass
        elif IS_WINDOWS and self.apo.path and self.apo.writable:
            try:
                if self.last_apo_boost != boost:
                    update_apo_preamp(self.apo.path, boost)
                    self.last_apo_boost = boost
            except OSError:
                pass

        windows, left, right = self.audio.apply(boost, balance, apo_active)
        return windows, left, right, apo_active

    def center_balance(self) -> None:
        self.balance_slider.setValue(0)
        self._schedule_apply(0)

    def bump_balance(self, amount: int) -> None:
        self.balance_slider.setValue(max(-100, min(100, self.balance_slider.value() + amount)))
        self._schedule_apply(0)

    def reset(self) -> None:
        self.boost_slider.setValue(100)
        self.balance_slider.setValue(0)
        self._schedule_apply(0)

    def refresh_apo(self) -> None:
        self.apo = find_apo_config()
        self.apo_label.setText(self.apo.message)
        self.last_apo_boost = None
        self._capture_apo_snapshot()
        self._schedule_apply(0)

    def install_apo(self) -> None:
        try:
            APO_INSTALLER_PATH.parent.mkdir(parents=True, exist_ok=True)
            if not APO_INSTALLER_PATH.exists():
                self.status_label.setText("Downloading Equalizer APO installer...")
                download_apo_installer(APO_INSTALLER_PATH)

            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Start-Process -FilePath '{APO_INSTALLER_PATH}' -Verb RunAs",
                ],
                shell=False,
            )
            self.status_label.setText("Equalizer APO installer launched. Select your headset in Configurator, then reboot.")
        except Exception as exc:
            self.status_label.setText(f"Could not launch Equalizer APO installer: {exc}")

    def restore_session(self) -> None:
        if not self.session_checkbox.isChecked():
            return

        errors: list[str] = []
        self.apply_timer.stop()

        if self.apo_snapshot:
            try:
                restore_apo_snapshot(self.apo_snapshot)
            except OSError as exc:
                errors.append(f"APO: {exc}")
        elif self.apo.path and self.apo.writable:
            try:
                update_apo_preamp(self.apo.path, 100)
            except OSError as exc:
                errors.append(f"APO: {exc}")

        if self.audio:
            try:
                self.audio.restore_original()
            except Exception as exc:
                errors.append(f"Audio: {exc}")

        if errors:
            QMessageBox.warning(self, APP_NAME, "Some settings could not be restored:\n" + "\n".join(errors))

    def closeEvent(self, event) -> None:
        self.closing = True
        self.restore_session()
        event.accept()


def main() -> int:
    if QT_IMPORT_ERROR:
        print(
            f"{APP_NAME} needs PySide6 for its native UI. Install it with: python3 -m pip install -r requirements.txt\n"
            f"Import error: {QT_IMPORT_ERROR}",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = app_icon()
    if icon:
        app.setWindowIcon(icon)
    window = HearingBoostApp()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
