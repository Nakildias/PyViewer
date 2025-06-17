import socket
import threading
import struct
import time
import io
import os
import shutil
import subprocess
import sys
import configparser
import platform
import json # Added for remote control events

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTextEdit, QFrame, QMessageBox,
    QSlider, QCheckBox, QRadioButton, QGroupBox, QSystemTrayIcon, QMenu,
    QButtonGroup
)
from PyQt6.QtCore import (
    QObject, QThread, pyqtSignal, Qt, QTimer, QCoreApplication, QSize
)
from PyQt6.QtGui import QIcon, QPixmap, QFont, QAction

# --- Check for required libraries ---
try:
    from PIL import Image
    PILLOW_SUPPORT = True
except ImportError:
    PILLOW_SUPPORT = False

try:
    import mss
    import mss.exception
    MSS_SUPPORT = True
except ImportError:
    MSS_SUPPORT = False

# --- Remote Control Imports ---
try:
    from pynput import mouse, keyboard
    PYNPUT_SUPPORT = True
except ImportError:
    PYNPUT_SUPPORT = False

# --- Configuration ---
HOST = '0.0.0.0'  # Listen on all available network interfaces
PORT = 9999
AUDIO_PORT = PORT + 1
CONTROL_PORT = 9998 # New port for remote control
CONFIG_FILE = "server.ini" # Configuration file for server settings

# Audio settings for parec and client
CHUNK = 1024
FORMAT_STR = 's16le' # Signed 16-bit Little Endian (lowercase for parec)
CHANNELS = 2 # Stereo
RATE = 48000 # 48kHz is a common desktop audio standard


def get_local_ip():
    """Helper function to find the local IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1' # Fallback to 127.0.0.1 as a standard localhost
    finally:
        s.close()
    return IP

def recv_all(sock, n):
    """Helper function to reliably receive n bytes from a socket."""
    data = bytearray()
    while len(data) < n:
        try:
            packet = sock.recv(n - len(data))
            if not packet:
                # Connection closed or error
                return None
            data.extend(packet)
        except OSError as e:
            # Handle socket errors during recv
            print(f"Socket receive error: {e}")
            return None
    return bytes(data)

def detect_video_encoder(gui_updater):
    """Detects the best available hardware video encoder for FFmpeg."""
    if not shutil.which('ffmpeg'):
        gui_updater("[!] CRITICAL: ffmpeg is not installed! FFmpeg modes are unavailable.", error=True)
        return None, "FFmpeg (Unavailable)"

    # Platform-specific checks
    if sys.platform == "win32":
        try:
            # Check for NVIDIA GPU on Windows
            output = subprocess.check_output(['ffmpeg', '-encoders'], text=True, stderr=subprocess.STDOUT)
            if 'hevc_nvenc' in output:
                gui_updater("[*] NVIDIA GPU detected. Using 'hevc_nvenc'.")
                return 'hevc_nvenc', "FFmpeg HEVC (NVIDIA)"
        except (subprocess.CalledProcessError, FileNotFoundError): pass

    elif sys.platform == "linux":
        try:
            subprocess.check_output(['which', 'nvidia-smi'])
            gui_updater("[*] NVIDIA GPU detected. Using 'hevc_nvenc'.", False)
            return 'hevc_nvenc', "FFmpeg HEVC (NVIDIA)"
        except (subprocess.CalledProcessError, FileNotFoundError): pass
        try:
            lspci_output = subprocess.check_output(['lspci'], text=True)
            if 'AMD' in lspci_output and ('VGA' in lspci_output or 'Display' in lspci_output):
                gui_updater("[*] AMD GPU detected. Using 'hevc_amf'.", False)
                return 'hevc_amf', "FFmpeg HEVC (AMD)"
        except (subprocess.CalledProcessError, FileNotFoundError): pass
        try:
            lspci_output = subprocess.check_output(['lspci'], text=True)
            if 'Intel' in lspci_output and ('VGA' in lspci_output or 'Display' in lspci_output):
                gui_updater("[*] Intel GPU detected. Using 'hevc_qsv'.", False)
                return 'hevc_qsv', "FFmpeg HEVC (Intel)"
        except (subprocess.CalledProcessError, FileNotFoundError): pass

    # Fallback for all platforms
    gui_updater("[*] No dedicated hardware encoder found. Using CPU encoder 'libx264'.", False)
    return 'libx264', "FFmpeg x264 (CPU)"


# In viewer_server.py, replace the whole RemoteDesktopServer class

class RemoteDesktopServer(QObject):
    """
    Handles all server-side logic: networking, screen capture, audio streaming,
    and remote input processing. Runs in a QThread.
    """
    update_status_signal = pyqtSignal(str, bool)
    server_stopped_signal = pyqtSignal()
    client_connected_signal = pyqtSignal()
    client_disconnected_signal = pyqtSignal()
    server_startup_failed_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.host = HOST
        self.port = PORT
        self.server_socket = None
        self.client_conn = None
        self.audio_socket = None
        self.audio_client_conn = None
        self.is_running = False
        self.session_type = os.environ.get('XDG_SESSION_TYPE', 'x11').lower() # Default to x11
        self.wayland_screencap_tool = None # For Wayland legacy screenshot tool
        self.settings_lock = threading.Lock() # Protects settings access
        self.media_process = None # Holds the Popen object for FFmpeg/parec
        self._media_thread = None # To hold the streaming thread object
        self.stream_management_lock = threading.Lock()
        self._stop_stream_event = threading.Event()
        self._stop_heartbeat_event = threading.Event() # New event for heartbeat thread

        # --- Remote Control Init ---
        self.control_socket_listener = None
        self.control_client_conn = None
        self._stop_control_event = threading.Event()
        self.mouse_controller = mouse.Controller() if PYNPUT_SUPPORT else None
        self.keyboard_controller = keyboard.Controller() if PYNPUT_SUPPORT else None
        self.remote_client_video_width = 1920
        self.remote_client_video_height = 1080

        self.monitor_dims = None
        if MSS_SUPPORT:
            try:
                with mss.mss() as sct:
                    # sct.monitors[0] is the full bounding box of all monitors combined.
                    # This is the most likely candidate for what ffmpeg's "desktop" capture records.
                    # sct.monitors[1] is just the primary. Using [0] is more robust.
                    if len(sct.monitors) > 0:
                        self.monitor_dims = sct.monitors[0]
                    else:
                        raise mss.exception.ScreenShotError("No monitors found by MSS.")

                    self.update_status_signal.emit(f"[*] Using full screen dimensions for scaling: {self.monitor_dims['width']}x{self.monitor_dims['height']} at ({self.monitor_dims['left']}, {self.monitor_dims['top']})", False)
            except mss.exception.ScreenShotError as e:
                self.update_status_signal.emit(f"[!] WARNING: Could not get screen dimensions via mss: {e}", True)

        if not self.monitor_dims:
            self.update_status_signal.emit("[!] Using fallback resolution 1920x1080 for mouse scaling.", True)
            self.monitor_dims = {'left': 0, 'top': 0, 'width': 1920, 'height': 1080}

        self.config = configparser.ConfigParser()
        self._load_settings()

    def _restart_media_streams(self):
        """
        Stops the current media stream gracefully and starts a new one
        based on the current server.encoder_mode. Used for in-session mode switching.
        """
        with self.stream_management_lock:
            self.update_status_signal.emit(f"[*] Switching mode to {self.encoder_mode}...", False)

            self._stop_stream_event.set() # Signal the old streaming thread to stop its loop

            if self._media_thread and self._media_thread.is_alive():
                self._media_thread.join(timeout=3) # Wait for it to finish

            if self.media_process and self.media_process.poll() is None:
                self.media_process.terminate()
                try:
                    self.media_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.media_process.kill()

            self._media_thread = None # Reset reference
            self.media_process = None # Reset reference
            self._stop_stream_event.clear() # Clear for new stream

            if self.client_conn: # Only restart if a client is still connected
                # Now, re-create and start the new media thread
                if self.encoder_mode.startswith("FFmpeg"):
                    self._media_thread = threading.Thread(target=self.stream_ffmpeg, daemon=True)
                else: # Legacy Mode
                    self._media_thread = threading.Thread(target=self.stream_screen, daemon=True)
                self._media_thread.start()
                self.update_status_signal.emit(f"[*] Stream restarted in {self.encoder_mode} mode.", False)
            else:
                self.update_status_signal.emit("[*] No client connected, not restarting media stream.", False)


    # Properties for thread-safe access to settings
    @property
    def jpeg_quality(self):
        with self.settings_lock: return self._jpeg_quality
    @jpeg_quality.setter
    def jpeg_quality(self, value):
        with self.settings_lock: self._jpeg_quality = value

    @property
    def screen_capture_rate(self):
        with self.settings_lock: return self._screen_capture_rate
    @screen_capture_rate.setter
    def screen_capture_rate(self, value):
        with self.settings_lock: self._screen_capture_rate = value

    @property
    def is_muted(self):
        with self.settings_lock: return self._is_muted
    @is_muted.setter
    def is_muted(self, value):
        with self.settings_lock: self._is_muted = value

    @property
    def encoder_mode(self):
        with self.settings_lock: return self._encoder_mode
    @encoder_mode.setter
    def encoder_mode(self, value):
        with self.settings_lock: self._encoder_mode = value

    @property
    def ffmpeg_encoder(self):
        with self.settings_lock: return self._ffmpeg_encoder
    @ffmpeg_encoder.setter
    def ffmpeg_encoder(self, value):
        with self.settings_lock: self._ffmpeg_encoder = value

    def _load_settings(self):
        """Loads settings from the configuration file."""
        self.config.read(CONFIG_FILE)
        section = 'ServerSettings'
        if not self.config.has_section(section):
            self.config.add_section(section)

        with self.settings_lock:
            self._jpeg_quality = self.config.getint(section, 'jpeg_quality', fallback=75)
            self._screen_capture_rate = self.config.getint(section, 'screen_capture_rate', fallback=30)
            self._is_muted = self.config.getboolean(section, 'is_muted', fallback=False)
            self._encoder_mode = self.config.get(section, 'encoder_mode', fallback="Legacy (Slow)")
            self._ffmpeg_encoder = self.config.get(section, 'ffmpeg_encoder', fallback="libx264")

    def _save_settings(self):
        """Saves current settings to the configuration file."""
        section = 'ServerSettings'
        if not self.config.has_section(section):
            self.config.add_section(section)

        with self.settings_lock:
            self.config.set(section, 'jpeg_quality', str(self._jpeg_quality))
            self.config.set(section, 'screen_capture_rate', str(self._screen_capture_rate))
            self.config.set(section, 'is_muted', str(self._is_muted))
            self.config.set(section, 'encoder_mode', str(self._encoder_mode))
            self.config.set(section, 'ffmpeg_encoder', str(self._ffmpeg_encoder))

        try:
            with open(CONFIG_FILE, 'w') as f:
                self.config.write(f)
            self.update_status_signal.emit(f"[*] Settings saved to {CONFIG_FILE}", False)
        except IOError as e:
            self.update_status_signal.emit(f"[!] Error saving settings to {CONFIG_FILE}: {e}", True)

    def _connection_heartbeat(self):
        """
        A simple thread to keep the client_conn open and block the main loop.
        It waits until signalled to stop, or if client_conn is lost (e.g. from stream_ffmpeg).
        """
        self.update_status_signal.emit("[*] Video connection heartbeat thread started.", False)
        while not self._stop_heartbeat_event.is_set():
            time.sleep(0.5) # Periodically sleep to prevent busy-waiting
        self.update_status_signal.emit("[*] Video connection heartbeat thread stopped.", False)

    def run_server(self):
        """Starts the main server listening loop. This method runs in a QThread."""
        self.update_status_signal.emit(f"[*] Starting server on {self.host}:{self.port}...", False)

        # Initialize current_audio_thread at the top-level scope of this method
        current_audio_thread = None

        # Pre-flight Checks
        if not PILLOW_SUPPORT:
            self.update_status_signal.emit("[!] ERROR: 'Pillow' library not installed. Legacy mode requires it.", True)
            self.server_startup_failed_signal.emit("Pillow library missing.")
            return
        if not shutil.which('ffmpeg'):
            self.update_status_signal.emit("[!] ERROR: 'ffmpeg' not found in PATH. FFmpeg mode is disabled.", True)
        if sys.platform == "linux":
            if not shutil.which('parec') or not shutil.which('pactl'):
                self.update_status_signal.emit("[!] WARN: 'parec' or 'pactl' not found. Legacy audio will be disabled.", True)
        if self.encoder_mode.startswith("Legacy"):
            if self.session_type == 'wayland':
                tools = ['flameshot', 'gnome-screenshot', 'wayshot', 'grim']
                for tool in tools:
                    if shutil.which(tool):
                        self.wayland_screencap_tool = tool
                        self.update_status_signal.emit(f"[*] Wayland detected. Using '{tool}' for Legacy Mode.", False)
                        break
                if not self.wayland_screencap_tool:
                    self.update_status_signal.emit("[!] ERROR: Wayland detected, but no compatible screenshot tool found for Legacy Mode.", True)
                    self.server_startup_failed_signal.emit("Wayland screenshot tool missing.")
                    return
            elif not MSS_SUPPORT: # X11 session
                self.update_status_signal.emit("[!] ERROR: X11 detected, but 'mss' is not installed for Legacy Mode.", True)
                self.server_startup_failed_signal.emit("MSS library missing for X11.")
                return
            else:
                self.update_status_signal.emit("[*] X11 session detected. Using 'mss' for Legacy Mode.", False)
        if not PYNPUT_SUPPORT:
            self.update_status_signal.emit("[!] WARNING: 'pynput' library not installed. Remote control features will be disabled.", True)
            self.mouse_controller = None
            self.keyboard_controller = None

        try:
            self.is_running = True
            # --- Video/Audio Sockets ---
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.settimeout(1.0) # Non-blocking accept with timeout
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(1)
            self.update_status_signal.emit(f"[*] Video stream listener on {self.host}:{self.port}", False)

            if self.encoder_mode.startswith("Legacy"):
                self.audio_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.audio_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.audio_socket.settimeout(1.0)
                self.audio_socket.bind((self.host, AUDIO_PORT))
                self.audio_socket.listen(1)
                self.update_status_signal.emit(f"[*] Audio stream listener on {self.host}:{AUDIO_PORT}", False)

            # --- Control Socket ---
            if PYNPUT_SUPPORT:
                self.control_socket_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.control_socket_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.control_socket_listener.settimeout(1.0)
                self.control_socket_listener.bind((self.host, CONTROL_PORT))
                self.control_socket_listener.listen(1)
                self.update_status_signal.emit(f"[*] Remote control listener on {self.host}:{CONTROL_PORT}", False)
                # Start a thread to handle incoming control connections
                threading.Thread(target=self._control_listener_loop, daemon=True).start()

            self.update_status_signal.emit("[*] Waiting for a client connection...", False)

            while self.is_running:
                # This outer try-except handles general server errors and loops for new connections
                try:
                    self.client_conn, addr = self.server_socket.accept()
                    mode_byte = 'F' if self.encoder_mode.startswith("FFmpeg") else 'L'
                    self.client_conn.sendall(mode_byte.encode())
                    self.update_status_signal.emit(f"[*] Connected from {addr}. Server is in '{self.encoder_mode}' mode.", False)
                    self.client_connected_signal.emit()

                    # Initialize heartbeat event for this session
                    self._stop_heartbeat_event.clear()

                    # Handle Legacy audio connection and thread start if applicable
                    if mode_byte == 'L' and self.audio_socket:
                        try:
                            self.audio_client_conn, _ = self.audio_socket.accept()
                            self.update_status_signal.emit(f"[*] Legacy audio client connected from {addr}.", False)
                            # Start audio thread only if connection successful and it's Legacy mode
                            if sys.platform == "linux": # Check platform for parec/pactl dependency
                                current_audio_thread = threading.Thread(target=self.stream_audio, daemon=True)
                                current_audio_thread.start()
                            else:
                                self.update_status_signal.emit("[!] Audio streaming not supported on this OS for Legacy mode.", True)
                                self.audio_client_conn = None # Ensure client doesn't expect audio
                        except socket.timeout:
                            self.update_status_signal.emit("[*] Client connected for video but not audio within timeout.", False)
                            self.audio_client_conn = None
                        except Exception as e:
                            self.update_status_signal.emit(f"[*] Unexpected error during audio connection: {e}", True)
                            self.audio_client_conn = None

                    # --- Start client video session management ---
                    self._stop_stream_event.clear() # Clear for new stream instance
                    self._media_thread = None # Ensure no old thread reference before starting new one

                    if self.encoder_mode.startswith("FFmpeg"):
                        self._media_thread = threading.Thread(target=self.stream_ffmpeg, daemon=True)
                    else: # Legacy Mode
                        self._media_thread = threading.Thread(target=self.stream_screen, daemon=True)

                    if self._media_thread:
                        self._media_thread.start()

                    # Start the heartbeat thread to keep the session alive
                    heartbeat_thread = threading.Thread(target=self._connection_heartbeat, daemon=True)
                    heartbeat_thread.start()

                    # Wait for the heartbeat thread to indicate the session is over
                    heartbeat_thread.join()

                    self.update_status_signal.emit("[*] Client disconnected. Server ready for a new connection.", False)
                    self.client_disconnected_signal.emit()

                except socket.timeout:
                    continue # No client connected within timeout, continue waiting
                except Exception as e: # This handles errors before the main accept loop starts or if the loop breaks critically
                    if self.is_running:
                        self.update_status_signal.emit(f"[!] A critical error occurred in main loop: {e}", True)
                    break # Break out of while loop if a critical error occurs
                finally:
                    # Cleanup specific to this client session
                    self._stop_stream_event.set() # Ensure streaming threads stop
                    self._stop_heartbeat_event.set() # Ensure heartbeat thread stops
                    self._stop_control_event.set() # Ensure control thread stops for this session

                    if self._media_thread and self._media_thread.is_alive():
                        self._media_thread.join(timeout=2)
                        self._media_thread = None

                    if current_audio_thread and current_audio_thread.is_alive():
                        current_audio_thread.join(timeout=2)

                    if self.media_process:
                        self.media_process.terminate()
                        try:
                            self.media_process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.media_process.kill()
                        self.media_process = None

                    if self.client_conn:
                        try: self.client_conn.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                        try: self.client_conn.close()
                        except OSError: pass
                    self.client_conn = None

                    if self.audio_client_conn:
                        try: self.audio_client_conn.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                        try: self.audio_client_conn.close()
                        except OSError: pass
                    self.audio_client_conn = None

                    if self.control_client_conn: # Close control client for this session
                        try: self.control_client_conn.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                        try: self.control_client_conn.close()
                        except OSError: pass
                    self.control_client_conn = None

        except Exception as e:
            self.update_status_signal.emit(f"[!] An error occurred during server startup: {e}", True)
            self.server_startup_failed_signal.emit(str(e))
        finally:
            self.stop_server()

    def _control_listener_loop(self):
        """Listens for incoming remote control client connections."""
        self.update_status_signal.emit(f"[*] Control listener thread started on {self.host}:{CONTROL_PORT}.", False)
        while self.is_running:
            try:
                if self.control_client_conn is None:
                    conn, addr = self.control_socket_listener.accept()
                    self.update_status_signal.emit(f"[*] Control client connected from {addr}.", False)
                    self.control_client_conn = conn
                    self._stop_control_event.clear()
                    threading.Thread(target=self._handle_control_client, args=(conn, addr), daemon=True).start()
                else:
                    time.sleep(1)
            except socket.timeout:
                continue
            except Exception as e:
                if self.is_running:
                    self.update_status_signal.emit(f"[!] Error in control listener loop: {e}", True)
                break

        self.update_status_signal.emit("[*] Control listener thread stopped.", False)


    def _handle_control_client(self, conn, addr):
        """Handles incoming remote control events from a connected client."""
        buffer = ""
        try:
            while self.is_running and not self._stop_control_event.is_set():
                data = conn.recv(4096).decode('utf-8')
                if not data:
                    self.update_status_signal.emit("[*] Control client disconnected.", False)
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        event_data = json.loads(line)
                        self.process_control_event(event_data)
                    except json.JSONDecodeError as e:
                        self.update_status_signal.emit(f"[*] Control JSON decode error: {e}, Data: {line[:50]}...", True)
                    except Exception as e:
                        self.update_status_signal.emit(f"[*] Error processing control event: {e}", True)
        except (socket.error, ConnectionResetError, BrokenPipeError) as e:
            self.update_status_signal.emit(f"[*] Control client {addr} connection lost: {e}", False)
        except Exception as e:
            if self.is_running:
                self.update_status_signal.emit(f"[!] Error handling control client {addr}: {e}", True)
        finally:
            if conn == self.control_client_conn:
                self.control_client_conn = None
            self._stop_control_event.set()
            conn.close()


    def process_control_event(self, event_data):
        """Processes and simulates a received remote control event."""
        if not PYNPUT_SUPPORT or self.mouse_controller is None or self.keyboard_controller is None:
            return

        event_type = event_data['type']
        data = event_data['data']
        server_width = self.monitor_dims['width']
        server_height = self.monitor_dims['height']

        try:
            if event_type == 'mouse_move':
                client_rel_x = data['rel_x']
                client_rel_y = data['rel_y']
                client_width = data.get('client_video_width')
                client_height = data.get('client_video_height')

                if not client_width or not client_height:
                    target_x = int(client_rel_x * server_width) + self.monitor_dims['left']
                    target_y = int(client_rel_y * server_height) + self.monitor_dims['top']
                else:
                    server_aspect = server_width / server_height
                    client_aspect = client_width / client_height
                    corrected_rel_x = client_rel_x
                    corrected_rel_y = client_rel_y

                    if client_aspect > server_aspect:
                        scale = server_aspect / client_aspect
                        offset = (1.0 - scale) / 2.0
                        if client_rel_y < offset or client_rel_y > (1.0 - offset): return
                        corrected_rel_y = (client_rel_y - offset) / scale
                    elif client_aspect < server_aspect:
                        scale = client_aspect / server_aspect
                        offset = (1.0 - scale) / 2.0
                        if client_rel_x < offset or client_rel_x > (1.0 - offset): return
                        corrected_rel_x = (client_rel_x - offset) / scale

                    target_x = int(corrected_rel_x * server_width) + self.monitor_dims['left']
                    target_y = int(corrected_rel_y * server_height) + self.monitor_dims['top']

                bounds_right = self.monitor_dims['left'] + server_width
                bounds_bottom = self.monitor_dims['top'] + server_height
                target_x = max(self.monitor_dims['left'], min(target_x, bounds_right - 1))
                target_y = max(self.monitor_dims['top'], min(target_y, bounds_bottom - 1))
                self.mouse_controller.position = (target_x, target_y)

            elif event_type == 'mouse_click':
                button = mouse.Button[data['button'].split('.')[-1]]
                if data['pressed']: self.mouse_controller.press(button)
                else: self.mouse_controller.release(button)

            elif event_type == 'mouse_scroll':
                self.mouse_controller.scroll(data['dx'], data['dy'])

            elif event_type == 'keyboard_press':
                try:
                    key = getattr(keyboard.Key, data['name'].split('.')[-1]) if 'name' in data else keyboard.KeyCode(char=data['char'])
                    self.keyboard_controller.press(key)
                except (AttributeError, ValueError): pass

            elif event_type == 'keyboard_release':
                try:
                    key = getattr(keyboard.Key, data['name'].split('.')[-1]) if 'name' in data else keyboard.KeyCode(char=data['char'])
                    self.keyboard_controller.release(key)
                except (AttributeError, ValueError): pass

        except Exception as e:
            self.update_status_signal.emit(f"Error simulating control event {event_type}: {e}", True)


    def restart_ffmpeg_stream(self):
        """Safely restarts the FFmpeg stream to apply new settings (e.g., mute, quality)."""
        with self.stream_management_lock:
            if not self.is_running or not self.encoder_mode.startswith("FFmpeg") or not self.client_conn:
                return

            self.update_status_signal.emit(f"[*] Applying new FFmpeg settings (framerate: {self.screen_capture_rate} FPS, mute: {self.is_muted})...", False)

            self._stop_stream_event.set()
            if self._media_thread and self._media_thread.is_alive():
                self._media_thread.join(timeout=2)

            if self.media_process:
                self.media_process.terminate()
                try:
                    self.media_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.media_process.kill()
                self.media_process = None

            self._media_thread = None
            self._stop_stream_event.clear()

            self._media_thread = threading.Thread(target=self.stream_ffmpeg, daemon=True)
            self._media_thread.start()
            self.update_status_signal.emit("[*] FFmpeg stream restarted with new settings.", False)


    def _log_stderr(self, pipe):
        """Reads from a pipe and logs to the GUI status."""
        try:
            for line in iter(pipe.readline, b''):
                self.update_status_signal.emit(f"[FFmpeg]: {line.decode('utf-8', errors='ignore').strip()}", True)
        except Exception as e:
            self.update_status_signal.emit(f"[!] Stderr logging thread error: {e}", True)
        finally:
            pipe.close()

    def stream_ffmpeg(self):
        """Captures screen and audio with FFmpeg and streams it as a single MPEG-TS stream."""
        if not shutil.which('ffmpeg'):
            self.update_status_signal.emit("[!] Cannot start stream: ffmpeg executable not found.", True)
            return

        rate = self.screen_capture_rate
        encoder = self.ffmpeg_encoder
        is_muted = self.is_muted
        self.update_status_signal.emit(f"[*] Starting FFmpeg stream at {rate} FPS using '{encoder}'...", False)

        command = ['ffmpeg', '-y', '-loglevel', 'error']

        if sys.platform == "win32":
            command.extend(['-f', 'gdigrab', '-framerate', str(rate), '-i', 'desktop'])
        elif sys.platform == "darwin":
            command.extend(['-f', 'avfoundation', '-framerate', str(rate), '-i', '1:0'])
        else: # Linux
            display = os.environ.get('DISPLAY', ':0')
            # Capture the specific monitor area defined by monitor_dims
            command.extend(['-f', 'x11grab', '-video_size', f"{self.monitor_dims['width']}x{self.monitor_dims['height']}",
                            '-framerate', str(rate), '-i', f"{display}+{self.monitor_dims['left']},{self.monitor_dims['top']}"])

        audio_input_configured = False
        if not is_muted:
            if sys.platform == "win32":
                command.extend(['-f', 'dshow', '-i', 'audio=Stereo Mix'])
                audio_input_configured = True
            elif sys.platform == "darwin":
                command.extend(['-f', 'avfoundation', '-i', 'none:0'])
                audio_input_configured = True
            else: # Linux
                try:
                    pactl_output = subprocess.check_output(['pactl', 'get-default-sink'], text=True).strip()
                    monitor_source = f"{pactl_output}.monitor"
                    command.extend(['-f', 'pulse', '-i', monitor_source])
                    audio_input_configured = True
                    self.update_status_signal.emit(f"[*] FFmpeg using PulseAudio monitor: {monitor_source}", False)
                except (subprocess.CalledProcessError, FileNotFoundError):
                    self.update_status_signal.emit("[!] Could not find PulseAudio default sink. FFmpeg will run without audio.", True)

        command.extend(['-c:v', encoder])
        command.extend(['-g', str(rate * 2)])
        if encoder == 'libx264':
            command.extend(['-preset', 'ultrafast', '-tune', 'zerolatency', '-crf', '28'])
        else:
            command.extend(['-preset', 'p1', '-tune', 'll', '-b:v', '8M'])

        if audio_input_configured:
            command.extend(['-c:a', 'libopus', '-b:a', '128k', '-ar', str(RATE), '-ac', str(CHANNELS)])
        else:
            command.extend(['-an'])

        command.extend(['-f', 'mpegts', 'pipe:1'])

        try:
            self.media_process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0
            )
            stderr_thread = threading.Thread(target=self._log_stderr, args=(self.media_process.stderr,), daemon=True)
            stderr_thread.start()

            while self.is_running and not self._stop_stream_event.is_set():
                if self.client_conn is None or self.media_process.poll() is not None: break
                chunk = self.media_process.stdout.read(CHUNK * 4)
                if not chunk:
                    if self.media_process.poll() is not None: break
                    time.sleep(0.01)
                    continue
                try:
                    self.client_conn.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    self.update_status_signal.emit(f"[*] Client (FFmpeg mode) disconnected during send: {e}", False)
                    self._stop_heartbeat_event.set()
                    break
        except FileNotFoundError:
            self.update_status_signal.emit("[!] CRITICAL: 'ffmpeg' command failed.", True)
        except Exception as e:
            if self.is_running: self.update_status_signal.emit(f"[!] FFmpeg streaming error: {e}", True)
        finally:
            if self.media_process and self.media_process.poll() is None:
                self.media_process.terminate()
            self.media_process = None
            self.update_status_signal.emit("[*] FFmpeg stream stopped.", False)

    def stream_audio(self):
        """Captures desktop audio using parec and streams it. (LEGACY MODE)"""
        if not self.audio_client_conn: return
        try:
            if not shutil.which('parec'):
                self.update_status_signal.emit("[!] ERROR: 'parec' not found.", True)
                return
            pactl_output = subprocess.check_output(['pactl', 'get-default-sink'], text=True).strip()
            monitor_source = f"{pactl_output}.monitor"
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            self.update_status_signal.emit(f"[!] Could not determine audio source for parec: {e}.", True)
            return

        command = ['parec','--device', monitor_source, f'--format={FORMAT_STR}', f'--rate={RATE}', f'--channels={CHANNELS}']
        parec_process = None
        try:
            parec_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, bufsize=0)
            self.update_status_signal.emit("[*] Legacy audio stream started.", False)
            while self.is_running and not self._stop_stream_event.is_set():
                audio_data = parec_process.stdout.read(CHUNK)
                if not audio_data:
                    if parec_process.poll() is not None: self.update_status_signal.emit("[*] parec process exited.", False)
                    break
                if not self.is_muted:
                    try:
                        self.audio_client_conn.sendall(audio_data)
                    except (BrokenPipeError, ConnectionResetError, OSError) as e:
                        self.update_status_signal.emit(f"[*] Client audio disconnected: {e}", False)
                        self._stop_heartbeat_event.set()
                        break
        except Exception as e:
            if self.is_running: self.update_status_signal.emit(f"[!] Audio streaming error: {e}", True)
        finally:
            if parec_process and parec_process.poll() is None:
                parec_process.terminate()
            self.update_status_signal.emit("[*] Legacy audio stream stopped.", False)

    def stream_screen(self):
        """Dispatches to the correct screen streaming method based on session type. (LEGACY MODE)"""
        if self.session_type == 'wayland': self.stream_screen_wayland()
        else: self.stream_screen_x11()

    def stream_screen_wayland(self):
        """Captures screen on Wayland using an external tool and streams as JPEG."""
        if not self.client_conn: return
        while self.is_running and not self._stop_stream_event.is_set():
            try:
                rate, quality = self.screen_capture_rate, self.jpeg_quality
                command_map = {
                    'flameshot': ['flameshot', 'full', '--raw'],
                    'gnome-screenshot': ['gnome-screenshot', '-f', '/tmp/rd_screenshot.png'],
                    'wayshot': ['wayshot', '--stdout'],
                    'grim': ['grim', '-t', 'ppm', '-']
                }
                command = command_map.get(self.wayland_screencap_tool)
                if not command:
                    self.update_status_signal.emit(f"[!] No valid Wayland tool '{self.wayland_screencap_tool}'.", True)
                    break

                result = subprocess.run(command, capture_output=True, check=True, timeout=5)
                raw_image_data = None
                if self.wayland_screencap_tool == 'gnome-screenshot':
                    with open('/tmp/rd_screenshot.png', 'rb') as f: raw_image_data = f.read()
                    os.remove('/tmp/rd_screenshot.png')
                else: raw_image_data = result.stdout

                pil_img = Image.open(io.BytesIO(raw_image_data)).convert('RGB')
                img_buffer_out = io.BytesIO()
                pil_img.save(img_buffer_out, format='jpeg', quality=quality)
                img_bytes = img_buffer_out.getvalue()

                self.client_conn.sendall(struct.pack('>I', len(img_bytes)))
                self.client_conn.sendall(img_bytes)
                time.sleep(1 / rate if rate > 0 else 1)
            except (subprocess.CalledProcessError, socket.error, ConnectionResetError, BrokenPipeError, subprocess.TimeoutExpired) as e:
                self.update_status_signal.emit(f"[*] Client (Wayland) disconnected or error: {e}", False)
                self._stop_heartbeat_event.set()
                break
            except Exception as e:
                if self.is_running: self.update_status_signal.emit(f"[!] Wayland streaming error: {e}", True)
                break
        self.update_status_signal.emit("[*] Wayland screen stream stopped.", False)

    def stream_screen_x11(self):
        """Captures screen on X11 using MSS and streams as JPEG."""
        if not self.client_conn: return
        display = os.environ.get('DISPLAY')
        try:
            with mss.mss(display=display) as sct:
                while self.is_running and not self._stop_stream_event.is_set():
                    try:
                        rate, quality = self.screen_capture_rate, self.jpeg_quality
                        sct_img = sct.grab(self.monitor_dims)
                        pil_img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
                        img_buffer = io.BytesIO()
                        pil_img.save(img_buffer, format='jpeg', quality=quality)
                        img_bytes = img_buffer.getvalue()
                        self.client_conn.sendall(struct.pack('>I', len(img_bytes)))
                        self.client_conn.sendall(img_bytes)
                        time.sleep(1 / rate if rate > 0 else 1)
                    except (mss.exception.ScreenShotError, socket.error, ConnectionResetError, BrokenPipeError) as e:
                        self.update_status_signal.emit(f"[*] Client (X11) disconnected: {e}", False)
                        self._stop_heartbeat_event.set()
                        break
                    except Exception as e:
                        if self.is_running: self.update_status_signal.emit(f"[!] X11 streaming error: {e}", True)
                        break
        except mss.exception.ScreenShotError as e:
            self.update_status_signal.emit(f"[!] MSS initialization error: {e}.", True)
        self.update_status_signal.emit("[*] X11 screen stream stopped.", False)

    def stop_server(self):
        """Stops the server and cleans up all resources."""
        if not self.is_running and self.server_socket is None: return
        self.update_status_signal.emit("[*] Server stopping...", False)
        self.is_running = False
        self._stop_stream_event.set()
        self._stop_heartbeat_event.set()
        self._stop_control_event.set()

        time.sleep(0.1)

        if self.media_process:
            self.media_process.terminate()
            try: self.media_process.wait(timeout=2)
            except subprocess.TimeoutExpired: self.media_process.kill()
            self.media_process = None

        def close_socket(sock):
            if sock:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: sock.close()
                except OSError: pass

        close_socket(self.client_conn)
        self.client_conn = None
        close_socket(self.audio_client_conn)
        self.audio_client_conn = None
        close_socket(self.control_client_conn)
        self.control_client_conn = None
        close_socket(self.server_socket)
        self.server_socket = None
        close_socket(self.audio_socket)
        self.audio_socket = None
        close_socket(self.control_socket_listener)
        self.control_socket_listener = None

        self._save_settings()
        self.update_status_signal.emit("[*] Server stopped.", False)
        self.server_stopped_signal.emit()


class ServerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PyQt6 Remote Desktop Server")
        self.setGeometry(100, 100, 600, 750)
        self.server = RemoteDesktopServer()
        self.server_thread = QThread()
        self.server.moveToThread(self.server_thread)
        self.tray_icon = None

        # Connect server signals to GUI slots
        self.server.update_status_signal.connect(self.update_status)
        self.server.server_stopped_signal.connect(self.on_server_stopped)
        self.server.client_connected_signal.connect(self.on_client_connected)
        self.server.client_disconnected_signal.connect(self.on_client_disconnected)
        self.server.server_startup_failed_signal.connect(self.on_server_startup_failed)

        # Connect thread start/finish signals
        self.server_thread.started.connect(self.server.run_server)
        self.server_thread.finished.connect(self.server_thread.deleteLater)

        self._setup_ui()
        self._setup_tray_icon()
        self._check_initial_dependencies()

        # Start server automatically after UI is set up
        QTimer.singleShot(100, self.start_server)


    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.status_log = QTextEdit()
        self.status_log.setReadOnly(True)
        self.status_log.setMaximumHeight(200) # Increased height
        main_layout.addWidget(self.status_log)

        # IP Address Display
        ip_group = QGroupBox("Server IP Address")
        ip_layout = QHBoxLayout()
        ip_group.setLayout(ip_layout)
        ip_label_text = QLabel("Connect client to:")
        ip_label_text.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.ip_label = QLabel(get_local_ip())
        self.ip_label.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        self.ip_label.setStyleSheet("color: blue;")
        ip_layout.addWidget(ip_label_text)
        ip_layout.addWidget(self.ip_label)
        ip_layout.addStretch(1)
        main_layout.addWidget(ip_group)

        # Encoder Mode selection
        encoder_group = QGroupBox("Encoder Mode")
        encoder_layout = QHBoxLayout()
        encoder_group.setLayout(encoder_layout)

        self.ffmpeg_encoder_name, ffmpeg_mode_label = detect_video_encoder(self.update_status)

        self.encoder_mode_group = QButtonGroup(self) # Use QButtonGroup to manage radio buttons
        self.legacy_rb = QRadioButton("Legacy (Slow)")
        self.ffmpeg_rb = QRadioButton(ffmpeg_mode_label)

        self.encoder_mode_group.addButton(self.legacy_rb, 0) # ID 0 for Legacy
        self.encoder_mode_group.addButton(self.ffmpeg_rb, 1) # ID 1 for FFmpeg

        encoder_layout.addWidget(self.legacy_rb)
        encoder_layout.addWidget(self.ffmpeg_rb)
        encoder_layout.addStretch(1)
        main_layout.addWidget(encoder_group)

        # Set initial selection based on loaded settings
        if self.server.encoder_mode.startswith("FFmpeg") and self.ffmpeg_encoder_name:
            self.ffmpeg_rb.setChecked(True)
        else:
            self.legacy_rb.setChecked(True)
            self.server.encoder_mode = "Legacy (Slow)" # Ensure internal state matches if FFmpeg unavailable

        if not self.ffmpeg_encoder_name: # Disable FFmpeg option if not available
            self.ffmpeg_rb.setEnabled(False)

        self.encoder_mode_group.idClicked.connect(self.update_encoder_mode)


        # Streaming Settings
        settings_group = QGroupBox("Streaming Settings")
        settings_layout = QVBoxLayout()
        settings_group.setLayout(settings_layout)

        quality_layout = QHBoxLayout()
        quality_layout.addWidget(QLabel("JPEG Quality (Legacy Only):"))
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(10, 100)
        self.quality_slider.setValue(self.server.jpeg_quality)
        self.quality_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.quality_slider.setTickInterval(10)
        self.quality_value_label = QLabel(str(self.server.jpeg_quality))
        quality_layout.addWidget(self.quality_slider)
        quality_layout.addWidget(self.quality_value_label)
        settings_layout.addLayout(quality_layout)
        self.quality_slider.valueChanged.connect(self.update_quality)

        rate_layout = QHBoxLayout()
        self.rate_label = QLabel("Capture Rate (FPS):")
        rate_layout.addWidget(self.rate_label)
        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(1, 60)
        self.rate_slider.setValue(self.server.screen_capture_rate)
        self.rate_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.rate_slider.setTickInterval(5)
        self.rate_value_label = QLabel(str(self.server.screen_capture_rate))
        rate_layout.addWidget(self.rate_slider)
        rate_layout.addWidget(self.rate_value_label)
        settings_layout.addLayout(rate_layout)
        self.rate_slider.valueChanged.connect(self.update_rate)
        main_layout.addWidget(settings_group)


        # Audio Settings
        audio_group = QGroupBox("Audio Settings")
        audio_layout = QVBoxLayout()
        audio_group.setLayout(audio_layout)
        self.mute_checkbox = QCheckBox("Mute Audio Stream")
        self.mute_checkbox.setChecked(self.server.is_muted)
        audio_layout.addWidget(self.mute_checkbox)
        self.mute_checkbox.toggled.connect(self.toggle_mute)
        main_layout.addWidget(audio_group)

        # Status Log
        status_group = QGroupBox("Status Log")
        status_layout = QVBoxLayout()
        status_group.setLayout(status_layout)
        status_layout.addWidget(self.status_log)
        main_layout.addWidget(status_group, 1) # Expand vertically

        self.update_encoder_mode(self.encoder_mode_group.checkedId()) # Initial call to set states


    def _setup_tray_icon(self):
        if platform.system() == "Linux":
            self.tray_icon = QSystemTrayIcon(self)
            self.tray_icon.setIcon(QIcon(":/icon.png")) # Placeholder for icon
            self.tray_icon.setToolTip("Remote Desktop Server")

            tray_menu = QMenu()
            show_action = tray_menu.addAction("Show Window")
            hide_action = tray_menu.addAction("Hide Window")
            quit_action = tray_menu.addAction("Quit")

            show_action.triggered.connect(self.showNormal)
            hide_action.triggered.connect(self.hide)
            quit_action.triggered.connect(QCoreApplication.quit)

            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self.on_tray_icon_activated)
            self.tray_icon.show()
        else:
            self.tray_icon = None

    def _check_initial_dependencies(self):
        # Initial checks, similar to client
        if sys.platform == "linux":
            if os.environ.get("XDG_SESSION_TYPE") == "wayland":
                QMessageBox.warning(self, "Environment Warning", "You appear to be running a Wayland session. Legacy mode may require a compatible screenshot tool.")
            if not shutil.which('parec') or not shutil.which('pactl'):
                QMessageBox.warning(self, "Dependency Warning", "'parec' or 'pactl' not found. Audio in Legacy mode will be disabled.")
        if not shutil.which('ffmpeg'):
            QMessageBox.warning(self, "Dependency Warning", "ffmpeg was not found in your system's PATH. FFmpeg mode will be unavailable.")
        if not PILLOW_SUPPORT:
            QMessageBox.critical(self, "Dependency Error", "Pillow library not found. Legacy mode is unavailable.")
            # Disable legacy options if Pillow is missing
            self.legacy_rb.setEnabled(False)
            self.quality_slider.setEnabled(False)
        if not PYNPUT_SUPPORT:
            QMessageBox.warning(self, "Dependency Warning", "'pynput' library not found. Remote control features will be disabled on the server side.")
            # No need to disable UI elements related to control as there are none on server.

    def start_server(self):
        """Starts the RemoteDesktopServer in its own QThread."""
        if not self.server_thread.isRunning():
            self.server_thread.start()
        else:
            self.update_status("[*] Server thread already running.", False)


    def stop_server(self):
        """Signals the server to stop and waits for its thread to finish."""
        if self.server.is_running:
            self.server.stop_server() # Call server's stop method
        if self.server_thread.isRunning():
            self.server_thread.quit()
            self.server_thread.wait(5000) # Wait up to 5 seconds for thread to finish
            if self.server_thread.isRunning():
                self.update_status("[!] Server thread did not terminate gracefully. Force quitting.", True)
                self.server_thread.terminate() # Force terminate if it hangs

    def update_status(self, message, is_error):
        """Thread-safe method to update the status log in the GUI."""
        # This slot is connected to a signal, so it runs in the main GUI thread.
        color = "red" if is_error else "white"
        self.status_log.append(f'<span style="color:{color};">{message}</span>')
        # self.status_log.ensureCursorVisible() # Scrolls to bottom automatically

    def update_encoder_mode(self, radio_id):
        """Updates the server's encoder mode based on GUI selection."""
        if radio_id == 0: # Legacy
            mode = "Legacy (Slow)"
            is_legacy = True
        else: # FFmpeg
            mode = self.ffmpeg_rb.text() # Use the detected label as the mode name
            is_legacy = False

        self.server.encoder_mode = mode

        # Enable/disable JPEG quality slider based on mode
        self.quality_slider.setEnabled(is_legacy)
        self.quality_value_label.setEnabled(is_legacy)
        # Update label for capture rate based on mode
        self.rate_label.setText("JPEG Capture Rate (FPS):" if is_legacy else "FFmpeg Refresh Rate (FPS):")

        # If server is running and mode changes between Legacy and FFmpeg, restart stream
        # for in-session mode switching.
        # Note: server._restart_media_streams is called from the server's thread
        # if a client is connected. We just set the mode here.
        pass

        self.update_rate(self.server.screen_capture_rate) # Update label for rate based on new mode

    def update_quality(self, value):
        """Updates JPEG quality setting on the server."""
        self.server.jpeg_quality = value
        self.quality_value_label.setText(str(value))
        # No restart needed for Legacy mode; it picks up changes on next frame.

    def update_rate(self, value):
        """Updates screen capture rate setting on the server."""
        self.server.screen_capture_rate = value
        self.rate_value_label.setText(str(value))
        # IMPORTANT: For FFmpeg, rate changes only apply on connection establishment,
        # or when explicitly restarting the stream (e.g., mute toggle).
        # We DO NOT call server.restart_ffmpeg_stream directly here for FFmpeg,
        # as per requirement "Make the framerate only apply when connection closes and goes back up for FFMPEG".
        # So, for FFmpeg, we don't call restart_ffmpeg_stream here based on rate slider change.
        pass # Do nothing, the next connection will pick up the new rate.


    def toggle_mute(self, checked):
        """Toggles audio mute setting on the server."""
        self.server.is_muted = checked
        status = "Muted" if checked else "Unmuted"
        self.update_status(f"[*] Audio stream {status}.", False)
        # If in FFmpeg mode and client is connected, restarting the stream applies the mute/unmute
        if self.server.encoder_mode.startswith("FFmpeg") and self.server.is_running and self.server.client_conn:
            # Restart ffmpeg stream to apply mute/unmute without full client disconnect
            # This is an exception to the framerate rule, as mute/unmute is a stream property.
            threading.Thread(target=self.server.restart_ffmpeg_stream, daemon=True).start()


    def on_client_connected(self):
        self.update_status("[*] Client is connected.", False)

    def on_client_disconnected(self):
        self.update_status("[*] Client disconnected.", False)

    def on_server_stopped(self):
        self.update_status("[*] Server has fully stopped.", False)

    def on_server_startup_failed(self, error_message):
        QMessageBox.critical(self, "Server Startup Error", f"Server failed to start: {error_message}")
        self.update_status(f"[!] Server failed to start: {error_message}", True)
        self.close() # Close window if server fails to start critically

    def closeEvent(self, event):
        """Handles graceful shutdown when the GUI window is closed."""
        self.stop_server() # Ensure server stops before application exits
        if self.tray_icon:
            self.tray_icon.hide()
        event.accept()

    def changeEvent(self, event):
        if platform.system() == "Linux" and hasattr(self, 'tray_icon') and self.tray_icon:
            if event.type() == event.Type.WindowStateChange:
                if self.isMinimized():
                    self.hide()
                    self.tray_icon.showMessage(
                        "Remote Desktop Server",
                        "Application minimized to tray.",
                        QSystemTrayIcon.MessageIcon.Information,
                        2000
                    )
        super().changeEvent(event)

    def on_tray_icon_activated(self, reason):
        if platform.system() == "Linux" and self.tray_icon:
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                # Toggle visibility on single click
                if self.isVisible():
                    self.hide()
                else:
                    self.showNormal()
                    self.raise_()
                    self.activateWindow()


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setStyle("Fusion") # Better looking style

    app = QApplication(sys.argv)
    # Register an icon for the application (optional, but good for tray icon)
    app.setWindowIcon(QIcon(QPixmap(QSize(32,32)))) # Empty QPixmap as placeholder

    window = ServerWindow()
    window.show()
    sys.exit(app.exec())
