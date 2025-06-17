import sys
import socket
import threading
import struct
import subprocess
import shutil
import os
import time
import json

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTextEdit, QFrame, QMessageBox, QStackedLayout,
    QDockWidget, QSizePolicy
)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt, QTimer, QPointF, QRectF, QEvent
from PyQt6.QtGui import QImage, QPixmap, QWindow

# --- Check for optional PyAudio library for legacy audio ---
try:
    import pyaudio
    PYAUDIO_SUPPORT = True
except ImportError:
    PYAUDIO_SUPPORT = False

# --- Remote Control Imports ---
try:
    from pynput import mouse, keyboard
    PYNPUT_SUPPORT = True
except ImportError:
    PYNPUT_SUPPORT = False

# --- Configuration ---
CHUNK = 1024
FORMAT = pyaudio.paInt16 if PYAUDIO_SUPPORT else None
CHANNELS = 2
RATE = 48000
FFPLAY_WINDOW_TITLE = "Remote Stream"
CONTROL_PORT = 9998


class Worker(QObject):
    """
    Handles all backend network communication and stream processing in a separate thread.
    """
    update_status_signal = pyqtSignal(str, bool)
    disconnected_signal = pyqtSignal()
    legacy_frame_signal = pyqtSignal(QImage)
    ffmpeg_ready_to_embed_signal = pyqtSignal()
    # Generic signal to send any type of control event
    send_control_event_signal = pyqtSignal(str, dict)

    def __init__(self, host, port):
        super().__init__()
        self.host = host
        self.port = port
        self.mode = None
        self.control_socket = None
        self.audio_socket = None
        self.control_socket_client = None
        self.ffplay_process = None
        self.stop_event = threading.Event()
        self._stop_control_send_event = threading.Event()

        # Connect the signal to the internal sending method
        self.send_control_event_signal.connect(self._send_control_event)

    def connect_and_run(self):
        """Main entry point for the worker thread."""
        if not self._connect_sockets():
            return

        if PYNPUT_SUPPORT:
            if not self._connect_control_socket():
                self.update_status_signal.emit("[!] Failed to establish remote control connection. Disconnecting video.", True)
                self.disconnect()
                return
        else:
            self.update_status_signal.emit("[!] pynput not available. Remote control will be disabled.", True)

        if self.mode == 'F':
            self._handle_ffmpeg_stream()
        elif self.mode == 'L':
            self._handle_legacy_stream()

    def _connect_sockets(self):
        """Establishes the initial connection to the server to determine the stream mode."""
        try:
            self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.control_socket.connect((self.host, self.port))
            self.update_status_signal.emit(f"[*] Connected to server at {self.host}:{self.port}", False)

            mode_byte = self.control_socket.recv(1)
            if not mode_byte:
                raise ConnectionAbortedError("Server did not send mode byte.")
            self.mode = mode_byte.decode()

            if self.mode == 'L':
                self.update_status_signal.emit("[*] Server is in Legacy mode. Connecting for audio...", False)
                self.audio_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.audio_socket.connect((self.host, self.port + 1))
                self.update_status_signal.emit("[*] Legacy audio socket connected.", False)
            elif self.mode == 'F':
                self.update_status_signal.emit("[*] Server is in FFmpeg mode.", False)
            return True
        except Exception as e:
            self.update_status_signal.emit(f"[!] Connection failed: {e}", True)
            self.disconnect()
            return False

    def _connect_control_socket(self):
        """Connects to the dedicated remote control port on the server."""
        try:
            self.control_socket_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.control_socket_client.connect((self.host, CONTROL_PORT))
            self.update_status_signal.emit(f"[*] Connected to remote control server at {self.host}:{CONTROL_PORT}", False)
            self._stop_control_send_event.clear()
            return True
        except Exception as e:
            self.update_status_signal.emit(f"[!] Remote control connection failed: {e}", True)
            return False

    def _send_control_event(self, event_type, data):
        """Sends a JSON-formatted control event (mouse, keyboard) to the server."""
        if self.control_socket_client and not self._stop_control_send_event.is_set():
            try:
                message = json.dumps({"type": event_type, "data": data}) + "\n"
                self.control_socket_client.sendall(message.encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                self.update_status_signal.emit(f"[*] Control socket send error: {e}. Remote control disconnected.", False)
                self._stop_control_send_event.set()
            except Exception as e:
                self.update_status_signal.emit(f"[!] Error sending control event: {e}", True)

    def _handle_ffmpeg_stream(self):
        """Starts an ffplay process and pipes video data from the server into it."""
        if not shutil.which('ffplay'):
            self.update_status_signal.emit("[!] CRITICAL: ffplay not found. Cannot display FFmpeg stream.", True)
            self.disconnect()
            return

        command = [
            'ffplay', '-loglevel', 'error', '-noborder', '-autoexit',
            '-window_title', FFPLAY_WINDOW_TITLE, '-fflags', 'nobuffer',
            '-flags', 'low_delay', '-framedrop', '-sync', 'ext',
            '-probesize', '32', '-analyzeduration', '0', '-i', 'pipe:0'
        ]
        self.update_status_signal.emit("[*] Starting ffplay...", False)
        try:
            # Use a dictionary for startupinfo on Windows to hide the console window
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.ffplay_process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=os.environ, startupinfo=startupinfo
            )
            self.ffmpeg_ready_to_embed_signal.emit()

            while not self.stop_event.is_set():
                data = self.control_socket.recv(CHUNK * 4)
                if not data:
                    self.update_status_signal.emit("[*] Stream ended.", False)
                    break
                if self.ffplay_process.poll() is not None:
                    if not self.stop_event.is_set():
                        self.update_status_signal.emit("[!] ffplay process exited unexpectedly.", True)
                    break
                try:
                    self.ffplay_process.stdin.write(data)
                except (BrokenPipeError, OSError):
                    if not self.stop_event.is_set():
                        self.update_status_signal.emit("[!] Broken pipe to ffplay.", True)
                    break
        finally:
            self.disconnect()

    def _handle_legacy_stream(self):
        """Handles the old-style streaming method (JPEG frames + raw audio)."""
        if PYAUDIO_SUPPORT:
            audio_thread = threading.Thread(target=self._play_legacy_audio, daemon=True)
            audio_thread.start()
        else:
            self.update_status_signal.emit("[!] PyAudio not found. Legacy audio disabled.", True)

        try:
            while not self.stop_event.is_set():
                img_size_data = self._recv_all(self.control_socket, 4)
                if not img_size_data: break
                img_size = struct.unpack('>I', img_size_data)[0]
                img_data = self._recv_all(self.control_socket, img_size)
                if not img_data: break
                q_image = QImage.fromData(img_data, "JPG")
                self.legacy_frame_signal.emit(q_image)
        finally:
            self.disconnect()

    def _play_legacy_audio(self):
        """Plays raw audio data received from the legacy server."""
        p = pyaudio.PyAudio()
        stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True, frames_per_buffer=CHUNK)
        try:
            while not self.stop_event.is_set():
                audio_data = self.audio_socket.recv(CHUNK)
                if not audio_data: break
                stream.write(audio_data)
        finally:
            stream.stop_stream()
            stream.close()
            p.terminate()

    def _recv_all(self, sock, n):
        """Helper function to ensure all n bytes are received from a socket."""
        data = bytearray()
        while len(data) < n:
            try:
                packet = sock.recv(n - len(data))
                if not packet: return None
                data.extend(packet)
            except OSError:
                return None
        return bytes(data)

    def toggle_mute(self):
        """Toggles mute on the ffplay process by writing 'm' to its stdin."""
        if self.ffplay_process and self.ffplay_process.poll() is None:
            try:
                # ffplay toggles mute by receiving 'm' on its standard input
                # Using os.write is more reliable for raw pipes than .write()
                os.write(self.ffplay_process.stdin.fileno(), b'm')
                self.update_status_signal.emit("[*] Mute toggled.", False)
            except (OSError, ValueError) as e:
                self.update_status_signal.emit(f"[!] Failed to toggle mute: {e}", True)

    def disconnect(self):
        """Shuts down all connections, processes, and signals the main window."""
        if not self.stop_event.is_set():
            self.stop_event.set()
            self._stop_control_send_event.set()
            self.disconnected_signal.emit()
            if self.ffplay_process and self.ffplay_process.poll() is None:
                try:
                    self.ffplay_process.terminate()
                    self.ffplay_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.ffplay_process.kill()
            # Safely close all sockets
            for sock in [self.control_socket, self.audio_socket, self.control_socket_client]:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass


class ClientWindow(QMainWindow):
    """The main application window."""

    # Signal to tell the worker to toggle mute
    toggle_mute_in_worker = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.worker = None
        self.thread = None
        self.embed_attempts = 0

        # --- State Attributes ---
        self.is_window_active = False
        self.is_muted = False
        self.video_aspect_ratio = 16.0 / 9.0  # Default, updated on first frame

        # --- Remote Control Listeners ---
        self.mouse_listener = None
        self.keyboard_listener = None

        self._setup_ui()
        self._setup_connections()
        self._check_dependencies()

    def _setup_ui(self):
        """Initializes the entire user interface."""
        self.setWindowTitle("PyQt6 Remote Desktop Client")
        self.setGeometry(100, 100, 1024, 768)
        self.setStyleSheet(self.get_modern_stylesheet())

        # --- Main Layout ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # --- Top Control Bar ---
        top_bar = QWidget()
        top_bar.setObjectName("topBar")
        top_bar_layout = QHBoxLayout(top_bar)
        self.main_layout.addWidget(top_bar)

        self.ip_entry = QLineEdit("127.0.0.1")
        self.ip_entry.setPlaceholderText("Server IP")
        self.port_entry = QLineEdit("9999")
        self.port_entry.setPlaceholderText("Port")
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)

        top_bar_layout.addWidget(QLabel("Server IP:"))
        top_bar_layout.addWidget(self.ip_entry)
        top_bar_layout.addWidget(QLabel("Port:"))
        top_bar_layout.addWidget(self.port_entry)
        top_bar_layout.addWidget(self.connect_button)
        top_bar_layout.addWidget(self.disconnect_button)
        top_bar_layout.addStretch()

        # --- Utility Bar ---
        self._create_utility_buttons(top_bar_layout)

        # --- Video Container ---
        self.video_container = QWidget()
        self.video_container.setStyleSheet("background-color: #000000;")
        self.video_layout = QStackedLayout(self.video_container)
        self.video_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.main_layout.addWidget(self.video_container, 1)
        self._setup_video_widgets()

        # --- Log Window (as a Dock Widget) ---
        self._create_log_dock()

    def _create_utility_buttons(self, layout):
        """Creates and adds the utility buttons to the provided layout."""
        self.fullscreen_button = QPushButton("⛶ Fullscreen")
        self.mute_button = QPushButton("🔇 Mute")
        self.logs_button = QPushButton("📖 Show Logs")
        self.clipboard_button = QPushButton("📋 Paste as Keys")
        self.exit_button = QPushButton("✕ Exit")

        self.mute_button.setEnabled(False)
        self.clipboard_button.setEnabled(False)

        # Style the exit button differently to indicate its function
        self.exit_button.setObjectName("exitButton")

        utility_buttons = [
            self.fullscreen_button, self.mute_button, self.clipboard_button,
            self.logs_button, self.exit_button
        ]
        for btn in utility_buttons:
            layout.addWidget(btn)

    def _create_log_dock(self):
        """Creates the dockable window for status logs."""
        self.log_dock = QDockWidget("Logs", self)
        self.log_dock.setObjectName("logDock")
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)

        self.status_log = QTextEdit()
        self.status_log.setReadOnly(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5,5,5,5)
        layout.addWidget(self.status_log)
        self.log_dock.setWidget(container)

        self.log_dock.setVisible(False) # Hidden by default

    def _setup_video_widgets(self):
        """Sets up the initial placeholder widgets for video display."""
        self.video_frame = QFrame() # Placeholder for FFmpeg
        self.video_frame.setStyleSheet("background-color: black;")
        self.video_layout.addWidget(self.video_frame)

        self.legacy_video_label = QLabel() # Placeholder for Legacy JPG stream
        self.legacy_video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.legacy_video_label.setStyleSheet("background-color: black;")

        # *** FIX: Prevent the label from forcing the window to resize ***
        # This tells the layout to ignore the label's size hint and just give it
        # all available space. This stops the infinite resize loop.
        size_policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.legacy_video_label.setSizePolicy(size_policy)

        self.video_layout.addWidget(self.legacy_video_label)

        self.video_layout.setCurrentWidget(self.video_frame)

    def _setup_connections(self):
        """Connects all signals and slots for the application."""
        self.connect_button.clicked.connect(self.start_connection)
        self.disconnect_button.clicked.connect(self.stop_connection)

        # Utility button connections
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        self.mute_button.clicked.connect(self.toggle_mute)
        self.logs_button.clicked.connect(self.toggle_logs)
        self.clipboard_button.clicked.connect(self.send_clipboard)
        self.exit_button.clicked.connect(self.confirm_exit)

    def _check_dependencies(self):
        """Checks for required external command-line tools and Python libraries."""
        warnings = []
        if sys.platform == "linux":
            if os.environ.get("XDG_SESSION_TYPE") == "wayland":
                warnings.append("You appear to be running a Wayland session. Video embedding may fail as it typically requires X11.")
            if not shutil.which('wmctrl'):
                warnings.append("'wmctrl' is not installed, but it is required for embedding the video in FFmpeg mode on Linux.")
        if not shutil.which('ffplay'):
            warnings.append("ffplay was not found in your system's PATH. FFmpeg mode will be unavailable.")
        if not PYAUDIO_SUPPORT:
            warnings.append("PyAudio not found. Audio in Legacy mode will be disabled.")
        if not PYNPUT_SUPPORT:
            warnings.append("'pynput' library not found. Remote control features will be disabled.")

        if warnings:
            QMessageBox.warning(self, "Dependency Warning", "\n\n".join(warnings))

    # --- Action Slots ---

    def start_connection(self):
        host = self.ip_entry.text()
        try:
            port = int(self.port_entry.text())
        except ValueError:
            self.update_status("[!] Invalid port number.", True)
            return

        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.mute_button.setEnabled(True)
        self.clipboard_button.setEnabled(True)
        self.status_log.clear()
        self.video_layout.setCurrentWidget(self.video_frame)

        self.thread = QThread()
        self.worker = Worker(host, port)
        self.worker.moveToThread(self.thread)

        # Connect worker signals to main thread slots
        self.worker.update_status_signal.connect(self.update_status)
        self.worker.disconnected_signal.connect(self.on_disconnect)
        self.worker.legacy_frame_signal.connect(self.update_legacy_frame)
        self.worker.ffmpeg_ready_to_embed_signal.connect(self.embed_ffplay_window)

        # Connect main thread signals to worker slots
        self.toggle_mute_in_worker.connect(self.worker.toggle_mute)

        self.thread.started.connect(self.worker.connect_and_run)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

        self.start_control_listeners()

    def stop_connection(self):
        if self.worker:
            self.worker.disconnect()

    def on_disconnect(self):
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.mute_button.setEnabled(False)
        self.clipboard_button.setEnabled(False)
        self.stop_control_listeners()

        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None

        # Clean up video container by removing embedded widget
        # The two placeholders (video_frame, legacy_video_label) are at index 0 and 1.
        # Any embedded ffplay widget will be at index 2.
        if self.video_layout.count() > 2:
            widget_to_remove = self.video_layout.takeAt(2).widget()
            if widget_to_remove:
                widget_to_remove.setParent(None)
                widget_to_remove.deleteLater()
        self.video_layout.setCurrentWidget(self.video_frame)

        # Reset mute button state
        self.is_muted = False
        self.mute_button.setText("🔇 Mute")

        self.update_status("[*] Connection closed.", False)

    def confirm_exit(self):
        """Shows a confirmation dialog before closing the application."""
        reply = QMessageBox.question(self, 'Exit Confirmation',
                                     "Are you sure you want to exit?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.close()

    def toggle_fullscreen(self):
        """Toggles the main window between fullscreen and normal mode."""
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("⛶ Fullscreen")
        else:
            self.showFullScreen()
            self.fullscreen_button.setText("Exit Fullscreen")

    def toggle_mute(self):
        """Sends a mute toggle command and updates the button text."""
        if self.worker:
            self.toggle_mute_in_worker.emit()
            self.is_muted = not self.is_muted
            self.mute_button.setText("🔊 Unmute" if self.is_muted else "🔇 Mute")

    def toggle_logs(self):
        """Shows or hides the log dock."""
        is_visible = self.log_dock.isVisible()
        self.log_dock.setVisible(not is_visible)
        self.logs_button.setText("📖 Hide Logs" if not is_visible else "📖 Show Logs")

    def send_clipboard(self):
        """Reads clipboard text and tells the worker to send it as keystrokes."""
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if text and self.worker:
            # Directly emit the worker's signal with the correct arguments
            self.worker.send_control_event_signal.emit('keyboard_type', {'text': text})
            self.update_status(f"[*] Sent {len(text)} characters from clipboard.", False)
        elif not text:
            self.update_status("[!] Clipboard is empty.", True)

    # --- UI Update and Event Handling ---

    def update_status(self, message, is_error):
        color = "#ff4c4c" if is_error else "#25be40" # Red for error, Green for success
        self.status_log.append(f'<span style="color:{color};">{message}</span>')

    def update_legacy_frame(self, q_image):
        try:
            if self.video_layout.currentWidget() != self.legacy_video_label:
                self.video_layout.setCurrentWidget(self.legacy_video_label)

            if q_image.height() > 0:
                self.video_aspect_ratio = q_image.width() / q_image.height()

            # Scale pixmap to fit the label while maintaining aspect ratio
            pixmap = QPixmap.fromImage(q_image)
            self.legacy_video_label.setPixmap(pixmap.scaled(
                self.legacy_video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        except RuntimeError:
            pass # Avoids errors if widget is deleted during update

    def embed_ffplay_window(self):
        if sys.platform != "linux":
            self.update_status("[*] Window embedding is only supported on Linux/X11.", False)
            return
        if not shutil.which('wmctrl'):
            return

        self.embed_attempts = 0
        def _try_embed():
            if self.worker is None or self.worker.stop_event.is_set(): return
            self.embed_attempts += 1
            if self.embed_attempts > 12:
                self.update_status("[!] Could not find ffplay window to embed. Giving up.", True)
                return

            ffplay_win_id = None
            try:
                wmctrl_output = subprocess.check_output(['wmctrl', '-l'], text=True)
                for line in wmctrl_output.splitlines():
                    if FFPLAY_WINDOW_TITLE in line:
                        ffplay_win_id = int(line.split()[0], 16)
                        break
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                self.update_status(f"[!] Error running wmctrl: {e}", True)
                return

            if ffplay_win_id:
                try:
                    ffplay_window = QWindow.fromWinId(ffplay_win_id)
                    if ffplay_window:
                        embedded_widget = QWidget.createWindowContainer(ffplay_window, self.video_container)
                        self.video_layout.addWidget(embedded_widget)
                        self.video_layout.setCurrentWidget(embedded_widget)
                        self.update_status("[*] Successfully embedded ffplay window.", False)
                    else:
                        self.update_status("[!] Failed to wrap ffplay window with QWindow.", True)
                except Exception as e:
                    self.update_status(f"[!] Error during window embedding: {e}", True)
            else:
                QTimer.singleShot(500, _try_embed)
        _try_embed()

    # --- Global Remote Control Methods ---

    def start_control_listeners(self):
        if not PYNPUT_SUPPORT or self.mouse_listener:
            return
        try:
            self.mouse_listener = mouse.Listener(on_move=self.on_move, on_click=self.on_click, on_scroll=self.on_scroll)
            self.keyboard_listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
            self.mouse_listener.start()
            self.keyboard_listener.start()
            self.update_status("[*] Remote control listeners started.", False)
        except Exception as e:
            self.update_status(f"[!] Failed to start control listeners: {e}", True)

    def stop_control_listeners(self):
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        if self.keyboard_listener:
            self.keyboard_listener.stop()
            self.keyboard_listener = None

    def event(self, event):
        # Track if the main window is active to enable/disable remote control
        if event.type() == QEvent.Type.WindowActivate:
            self.is_window_active = True
        elif event.type() == QEvent.Type.WindowDeactivate:
            self.is_window_active = False
        return super().event(event)

    def on_move(self, x, y):
        if not self.is_window_active or self.worker is None: return

        video_widget = self.video_layout.currentWidget()
        if not video_widget: return

        widget_pos = video_widget.mapFromGlobal(QPointF(x, y))
        video_rect = self.get_video_rect(video_widget.size(), self.video_aspect_ratio)

        if not video_rect.contains(widget_pos): return

        relative_pos = widget_pos - video_rect.topLeft()
        video_width, video_height = video_rect.width(), video_rect.height()
        if video_width < 1 or video_height < 1: return

        rel_x = max(0.0, min(1.0, relative_pos.x() / video_width))
        rel_y = max(0.0, min(1.0, relative_pos.y() / video_height))

        data = {'rel_x': rel_x, 'rel_y': rel_y}
        if self.worker and self.worker.send_control_event_signal:
             self.worker.send_control_event_signal.emit('mouse_move', data)

    def on_click(self, x, y, button, pressed):
        if not self.is_window_active or self.worker is None: return
        video_widget = self.video_layout.currentWidget()
        if not video_widget or not video_widget.rect().contains(video_widget.mapFromGlobal(QPointF(x, y)).toPoint()):
            return
        data = {'button': str(button), 'pressed': pressed}
        if self.worker and self.worker.send_control_event_signal:
            self.worker.send_control_event_signal.emit('mouse_click', data)

    def on_scroll(self, x, y, dx, dy):
        if not self.is_window_active or self.worker is None: return
        video_widget = self.video_layout.currentWidget()
        if not video_widget or not video_widget.rect().contains(video_widget.mapFromGlobal(QPointF(x, y)).toPoint()):
            return
        data = {'dx': dx, 'dy': dy}
        if self.worker and self.worker.send_control_event_signal:
            self.worker.send_control_event_signal.emit('mouse_scroll', data)

    def on_press(self, key):
        if not self.is_window_active or self.worker is None: return
        data = self._pynput_key_to_dict(key)
        if data and self.worker and self.worker.send_control_event_signal:
            self.worker.send_control_event_signal.emit('keyboard_press', data)

    def on_release(self, key):
        if not self.is_window_active or self.worker is None: return
        data = self._pynput_key_to_dict(key)
        if data and self.worker and self.worker.send_control_event_signal:
            self.worker.send_control_event_signal.emit('keyboard_release', data)

    def _pynput_key_to_dict(self, key):
        """Converts a pynput key object to a serializable dictionary."""
        if isinstance(key, keyboard.Key):
            return {'name': str(key)}
        elif isinstance(key, keyboard.KeyCode) and hasattr(key, 'char'):
            return {'char': key.char}
        return {}

    def get_video_rect(self, widget_size, video_aspect_ratio):
        """Calculates the actual video area inside a widget, accounting for letter/pillarboxing."""
        if widget_size.height() == 0: return QRectF()
        widget_aspect_ratio = widget_size.width() / widget_size.height()

        if widget_aspect_ratio > video_aspect_ratio: # Pillarboxing
            video_height = widget_size.height()
            video_width = video_height * video_aspect_ratio
            x_offset = (widget_size.width() - video_width) / 2
            y_offset = 0
        else: # Letterboxing
            video_width = widget_size.width()
            video_height = video_width / video_aspect_ratio
            x_offset = 0
            y_offset = (widget_size.height() - video_height) / 2
        return QRectF(x_offset, y_offset, video_width, video_height)


    def closeEvent(self, event):
        """Ensures connections are closed when the window is shut."""
        self.stop_connection()
        self.stop_control_listeners()
        event.accept()

    def get_modern_stylesheet(self):
        """Returns a QSS string for a modern dark theme."""
        return """
        QMainWindow {
            background-color: #2c3e50;
        }
        QWidget#topBar {
            background-color: #34495e;
            border-bottom: 1px solid #2c3e50;
        }
        QLabel {
            color: #ecf0f1;
            padding: 5px;
        }
        QLineEdit {
            background-color: #2c3e50;
            color: #ecf0f1;
            border: 1px solid #34495e;
            border-radius: 4px;
            padding: 5px;
        }
        QLineEdit:focus {
            border: 1px solid #3498db;
        }
        QPushButton {
            background-color: #3498db;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            font-weight: bold;
        }
        QPushButton:hover {
            background-color: #2980b9;
        }
        QPushButton:pressed {
            background-color: #1f618d;
        }
        QPushButton:disabled {
            background-color: #566573;
            color: #95a5a6;
        }
        QPushButton#exitButton {
            background-color: #e74c3c;
        }
        QPushButton#exitButton:hover {
            background-color: #c0392b;
        }
        QDockWidget {
            titlebar-close-icon: url(none);
            titlebar-normal-icon: url(none);
        }
        QDockWidget::title {
            text-align: left;
            background: #34495e;
            padding: 5px;
            padding-left: 10px;
            color: white;
            font-weight: bold;
        }
        QTextEdit {
            background-color: #1e2b37;
            color: #ecf0f1;
            border: 1px solid #34495e;
            font-family: Consolas, 'Courier New', monospace;
        }
        QMessageBox QPushButton {
            min-width: 80px;
        }
        """

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ClientWindow()
    window.show()
    sys.exit(app.exec())
