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

check_not_root() {
    if [[ $EUID -eq 0 ]]; then
        log_error "This script should NOT be run with sudo."
        log_error "Please run it as a normal user: bash ./install_pyviewer_server.sh"
        exit 1
    fi
    # Get the current user's UID, which will be used for XDG_RUNTIME_DIR fallback if needed
    CURRENT_USER_UID=$(id -u "$USER")
}

install_python_deps() {
    log_info "Installing Python dependencies into virtual environment..."
    # Python dependencies from pyviewer.server.py and pyviewer.client.py
    PYTHON_DEPS="PyQt6 Pillow mss pynput"
    # This command now runs directly as the current user
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
    # This task requires root privileges, so use sudo
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
    USER_HOME="$HOME" # This is now the actual user's home directory
    USER_SERVICE_CONF_DIR="$USER_HOME/.config/systemd/user"
    SERVICE_FILE="$USER_SERVICE_CONF_DIR/$SERVICE_FILE_NAME"

    # Create the directory as the current user (no sudo needed)
    mkdir -p "$USER_SERVICE_CONF_DIR"
    if [ $? -ne 0 ]; then log_error "Failed to create systemd user service directory '$USER_SERVICE_CONF_DIR'."; exit 1; fi

    # Create the service file as the current user (no sudo needed)
    cat <<EOF > "$SERVICE_FILE"
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
        log_error "Failed to create systemd user service file '$SERVICE_FILE'."
        exit 1
    fi
}

enable_and_start_service() {
    log_info "Reloading systemd user daemon and enabling/starting service..."

    # XDG_RUNTIME_DIR should be correctly set in the current user's environment
    # Fallback if not, though less likely now
    XDG_RUNTIME_DIR_USER="$XDG_RUNTIME_DIR"
    if [ -z "$XDG_RUNTIME_DIR_USER" ]; then
        XDG_RUNTIME_DIR_USER="/run/user/$CURRENT_USER_UID"
        log_warn "XDG_RUNTIME_DIR not set in current environment. Assuming fallback: $XDG_RUNTIME_DIR_USER"
        # Ensure the fallback directory exists and has correct permissions
        mkdir -p "$XDG_RUNTIME_DIR_USER"
        chmod 0700 "$XDG_RUNTIME_DIR_USER"
    fi

    # Explicitly pass XDG_RUNTIME_DIR for systemctl commands, though it should be inherited
    ENV_PREFIX="env XDG_RUNTIME_DIR=\"$XDG_RUNTIME_DIR_USER\""

    log_info "Attempting to reload systemd user daemon..."
    # These commands run directly as the current user, no sudo needed
    $ENV_PREFIX systemctl --user daemon-reload
    if [ $? -ne 0 ]; then
        log_error "Failed to reload systemd user daemon."
        log_error "Please try running 'systemctl --user daemon-reload' manually if the service doesn't start."
        return 1
    fi

    log_info "Attempting to enable PyViewer server service..."
    $ENV_PREFIX systemctl --user enable "$SERVICE_FILE_NAME"
    if [ $? -ne 0 ]; then
        log_error "Failed to enable PyViewer server service."
        log_error "Please try running 'systemctl --user enable $SERVICE_FILE_NAME' manually."
        return 1
    fi

    log_info "Attempting to start PyViewer server service..."
    $ENV_PREFIX systemctl --user start "$SERVICE_FILE_NAME"
    if [ $? -ne 0 ]; then
        log_error "Failed to start PyViewer server service automatically."
        log_error "The server has been installed, but you might need to start it manually for the first time by running:"
        log_error "  systemctl --user start $SERVICE_FILE_NAME"
        log_info "It should then start automatically on subsequent logins."
        return 1
    fi

    log_info "PyViewer server service enabled and started successfully."
    log_info "It will now start automatically after your graphical session is ready."
    return 0
}

# --- Main Installation Logic ---
check_not_root # Ensure the script is NOT run with sudo

log_info "Starting PyViewer server installation for user: $USER..."

# Create installation directory - Requires sudo
log_info "Creating installation directory: $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
if [ $? -ne 0 ]; then log_error "Failed to create installation directory."; exit 1; fi

# Set ownership of installation directory - Requires sudo
sudo chown -R "$USER":"$USER" "$INSTALL_DIR"
if [ $? -ne 0 ]; then log_error "Failed to set ownership of installation directory."; exit 1; fi

# Copy server files from current directory to installation directory (no sudo needed for copy, as user owns current dir)
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

# Create Python virtual environment (no sudo needed, as user owns INSTALL_DIR)
log_info "Creating Python virtual environment in $INSTALL_DIR/$VENV_DIR..."
python3 -m venv "$INSTALL_DIR/$VENV_DIR"
if [ $? -ne 0 ]; then
    log_error "Failed to create virtual environment. Ensure 'python3-venv' is installed (e.g., 'sudo apt install python3-venv')."
    exit 1
fi
log_info "Virtual environment created."

# Install Python dependencies into the venv
install_python_deps

# Check and warn about system-level dependencies
check_system_deps

# Create the wrapper script in system binaries (requires sudo)
create_wrapper_script

# Create and enable the systemd user service (no sudo needed for file creation, but commands below)
create_systemd_user_service

# Enable and start the service (commands run as user)
enable_and_start_service

log_info "PyViewer server installation complete."
log_info "--------------------------------------------------------------------------------"
log_info "To manage the PyViewer server service:"
log_info "  - Check status: systemctl --user status pyviewer-server.service"
log_info "  - Start:        systemctl --user start pyviewer-server.service"
log_info "  - Stop:         systemctl --user stop pyviewer-server.service"
log_info "  - Restart:      systemctl --user restart pyviewer-server.service"
log_info ""
log_info "To view the server's logs:"
log_info "  journalctl --user -u pyviewer-server.service"
log_info ""
log_info "If you want the PyViewer server to run automatically even after you log out of your graphical session, enable 'linger' for your user (requires one-time password prompt):"
log_info "  sudo loginctl enable-linger $USER"
log_info "--------------------------------------------------------------------------------"
