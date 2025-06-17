#!/bin/bash

# --- Configuration ---
INSTALL_DIR="/opt/pyviewer-server"
SERVER_SCRIPT="pyviewer.server.py"
CLIENT_SCRIPT="pyviewer.client.py" # Included for completeness of the PyViewer package
README_FILE="README.md"
VENV_DIR="venv"
WRAPPER_SCRIPT_NAME="pyviewer-server"
WRAPPER_SCRIPT_PATH="/usr/local/bin/$WRAPPER_SCRIPT_NAME"
SERVICE_FILE_NAME="pyviewer-server.service"
USER_SERVICE_DIR="$HOME/.config/systemd/user"

# --- Functions ---
log_info() {
    echo -e "\e[32mINFO:\e[0m $1"
}

log_warn() {
    echo -e "\e[33mWARN:\e[0m $1"
}

log_error() {
    echo -e "\e[31mERROR:\e[0m $1"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run with sudo or as root for system-wide installation."
        log_error "Usage: sudo ./install_pyviewer_server.sh"
        exit 1
    fi
}

install_python_deps() {
    log_info "Installing Python dependencies into virtual environment..."
    # Core dependencies from pyviewer.server.py and pyviewer.client.py
    PYTHON_DEPS="PyQt6 Pillow mss pynput"
    "$INSTALL_DIR/$VENV_DIR/bin/pip" install $PYTHON_DEPS
    if [ $? -ne 0 ]; then
        log_warn "Failed to install some Python dependencies. Please check the output above."
        log_warn "If you encounter issues with PyQt6 or other packages, you might need additional system libraries (e.g., 'sudo apt install python3-pyqt6' or 'sudo apt install libasound-dev portaudio19-dev' for PyAudio if you add it)."
    fi
}

check_system_deps() {
    log_info "Checking for critical system dependencies..."
    MISSING_DEPS=()
    # Check for FFmpeg (required for FFmpeg mode)
    if ! command -v ffmpeg &> /dev/null; then
        MISSING_DEPS+=("ffmpeg (required for FFmpeg streaming mode)")
    fi
    # Check for PulseAudio utilities (parec/pactl) (required for Legacy audio streaming)
    if ! command -v parec &> /dev/null || ! command -v pactl &> /dev/null; then
        MISSING_DEPS+=("pulseaudio-utils (parec/pactl) (required for Legacy audio streaming)")
    fi

    if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
        log_warn "The following system dependencies are missing or not in your PATH:"
        for dep in "${MISSING_DEPS[@]}"; do
            log_warn "- $dep"
        done
        log_warn "Please install them manually using your system's package manager. For Debian/Ubuntu-based systems, you might use:"
        log_warn "  sudo apt update"
        log_warn "  sudo apt install ffmpeg pulseaudio-utils"
    else
        log_info "All detected system dependencies are present."
    fi
}

create_wrapper_script() {
    log_info "Creating wrapper script at $WRAPPER_SCRIPT_PATH..."
    cat <<EOF | sudo tee "$WRAPPER_SCRIPT_PATH" > /dev/null
#!/bin/bash
# This script activates the PyViewer server's virtual environment and runs the server.

# Path to the PyViewer server installation directory
INSTALL_DIR="$INSTALL_DIR"
SERVER_SCRIPT_NAME="$SERVER_SCRIPT" # Name of the server script within INSTALL_DIR

# Activate the virtual environment
source "\$INSTALL_DIR/$VENV_DIR/bin/activate"

# Change to the installation directory to ensure relative paths work (e.g., server.ini)
cd "\$INSTALL_DIR" || { echo "Failed to change directory to \$INSTALL_DIR" >&2; exit 1; }

# Run the PyViewer server script
python "\$SERVER_SCRIPT_NAME"

# Deactivate the virtual environment (optional, script exits anyway)
deactivate
EOF
    sudo chmod +x "$WRAPPER_SCRIPT_PATH"
    if [ $? -eq 0 ]; then
        log_info "Wrapper script created successfully."
    else
        log_error "Failed to create wrapper script."
        exit 1
    fi
}

create_systemd_user_service() {
    log_info "Creating systemd user service file..."
    # SUDO_USER is the original user who ran `sudo`
    mkdir -p "$(sudo -u "$SUDO_USER" eval echo "$USER_SERVICE_DIR")"
    SERVICE_FILE="$(sudo -u "$SUDO_USER" eval echo "$USER_SERVICE_DIR/$SERVICE_FILE_NAME")"

    # The systemd user service runs as the user who enables/starts it, so no explicit User= is needed.
    cat <<EOF | sudo -u "$SUDO_USER" tee "$SERVICE_FILE" > /dev/null
[Unit]
Description=PyViewer Remote Desktop Server
# Start after the graphical session is ready and network is online
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
ExecStart=$WRAPPER_SCRIPT_PATH
Restart=on-failure
WorkingDirectory=$INSTALL_DIR
StandardOutput=journal # Redirect stdout to systemd journal
StandardError=journal  # Redirect stderr to systemd journal

[Install]
WantedBy=graphical-session.target
EOF
    if [ $? -eq 0 ]; then
        log_info "Systemd user service file created successfully at $SERVICE_FILE."
    else
        log_error "Failed to create systemd user service file."
        exit 1
    fi
}

enable_and_start_service() {
    log_info "Reloading systemd user daemon and enabling/starting service..."
    # Run systemctl commands as the original user, not root
    sudo -u "$SUDO_USER" systemctl --user daemon-reload
    if [ $? -ne 0 ]; then log_error "Failed to reload systemd user daemon."; exit 1; fi

    sudo -u "$SUDO_USER" systemctl --user enable "$SERVICE_FILE_NAME"
    if [ $? -ne 0 ]; then log_error "Failed to enable PyViewer server service."; exit 1; fi

    sudo -u "$SUDO_USER" systemctl --user start "$SERVICE_FILE_NAME"
    if [ $? -ne 0 ]; then log_error "Failed to start PyViewer server service."; exit 1; fi

    log_info "PyViewer server service enabled and started successfully."
    log_info "It will now start automatically after your graphical session is ready."
}

# --- Main Installation Logic ---
check_root

# Ensure the original user (who ran sudo) is available for user-specific systemd commands
if [ -z "$SUDO_USER" ]; then
    log_error "SUDO_USER environment variable not set. This script expects to be run with 'sudo'."
    exit 1
fi

log_info "Starting PyViewer server installation for user: $SUDO_USER..."

# Create installation directory and set ownership
log_info "Creating installation directory: $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
if [ $? -ne 0 ]; then log_error "Failed to create installation directory."; exit 1; fi
sudo chown -R "$SUDO_USER":"$SUDO_USER" "$INSTALL_DIR" # Ensure user has ownership for venv and config

# Copy server files from current directory to installation directory
log_info "Copying PyViewer server files to $INSTALL_DIR..."
cp "$SERVER_SCRIPT" "$INSTALL_DIR/"
cp "$CLIENT_SCRIPT" "$INSTALL_DIR/"
cp "$README_FILE" "$INSTALL_DIR/"
# Check if server.ini exists in current directory, otherwise let the application create a default
if [ -f "server.ini" ]; then
    cp "server.ini" "$INSTALL_DIR/"
    log_info "Copied existing server.ini."
else
    log_info "No server.ini found in the current directory. The server will create a default one on first run."
fi
if [ $? -ne 0 ]; then log_error "Failed to copy server files. Make sure '$SERVER_SCRIPT', '$CLIENT_SCRIPT', and '$README_FILE' are in the current directory."; exit 1; fi
log_info "Server files copied."

# Create Python virtual environment
log_info "Creating Python virtual environment in $INSTALL_DIR/$VENV_DIR..."
sudo -u "$SUDO_USER" python3 -m venv "$INSTALL_DIR/$VENV_DIR"
if [ $? -ne 0 ]; then
    log_error "Failed to create virtual environment. Ensure 'python3-venv' is installed (e.g., 'sudo apt install python3-venv')."
    exit 1
fi
log_info "Virtual environment created."

# Install Python dependencies
install_python_deps

# Check and warn about system-level dependencies
check_system_deps

# Create the wrapper script in system binaries
create_wrapper_script

# Create and enable the systemd user service
create_systemd_user_service
enable_and_start_service

log_info "PyViewer server installation complete."
log_info "--------------------------------------------------------------------------------"
log_info "To manage the PyViewer server service (as your user, NOT root):"
log_info "  - Check status: systemctl --user status pyviewer-server.service"
log_info "  - Start:        systemctl --user start pyviewer-server.service"
log_info "  - Stop:         systemctl --user stop pyviewer-server.service"
log_info "  - Restart:      systemctl --user restart pyviewer-server.service"
log_info ""
log_info "To view the server's logs:"
log_info "  journalctl --user -u pyviewer-server.service"
log_info ""
log_info "If you want the PyViewer server to run automatically even after you log out of your graphical session, enable 'linger' for your user (requires one-time root privilege):"
log_info "  sudo loginctl enable-linger $SUDO_USER"
log_info "--------------------------------------------------------------------------------"
