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
}

install_python_deps() {
    log_info "Installing Python dependencies into virtual environment..."
    PYTHON_DEPS="PyQt6 Pillow mss pynput"
    "$INSTALL_DIR/$VENV_DIR/bin/pip" install $PYTHON_DEPS
    if [ $? -ne 0 ]; then
        log_warn "Failed to install some Python dependencies. Please check the output above."
        log_warn "If you encounter issues, you might need additional system libraries (e.g., 'sudo apt install python3-pyqt6')."
    fi
}

check_system_deps() {
    log_info "Checking for critical system dependencies..."
    MISSING_DEPS=()
    if ! command -v ffmpeg &> /dev/null; then
        MISSING_DEPS+=("ffmpeg")
    fi
    if ! command -v parec &> /dev/null || ! command -v pactl &> /dev/null; then
        MISSING_DEPS+=("pulseaudio-utils (parec/pactl)")
    fi

    if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
        log_warn "The following system dependencies appear to be missing:"
        for dep in "${MISSING_DEPS[@]}"; do
            log_warn "- $dep"
        done
        log_warn "Please install them using your system's package manager, e.g., 'sudo apt install ffmpeg pulseaudio-utils'"
    else
        log_info "System dependencies check passed."
    fi
}

create_wrapper_script() {
    log_info "Creating wrapper script at $WRAPPER_SCRIPT_PATH..."
    # This task requires root privileges, so use sudo
    cat <<EOF | sudo tee "$WRAPPER_SCRIPT_PATH" > /dev/null
#!/bin/bash
# This script activates the PyViewer server's virtual environment and runs the server.
INSTALL_DIR="$INSTALL_DIR"
SERVER_SCRIPT_NAME="$SERVER_SCRIPT"
source "\$INSTALL_DIR/$VENV_DIR/bin/activate"
cd "\$INSTALL_DIR" || { echo "Failed to change directory to \$INSTALL_DIR" >&2; exit 1; }
exec python "\$SERVER_SCRIPT_NAME" "\$@"
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
    USER_SERVICE_CONF_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$USER_SERVICE_CONF_DIR/$SERVICE_FILE_NAME"

    mkdir -p "$USER_SERVICE_CONF_DIR"
    if [ $? -ne 0 ]; then log_error "Failed to create systemd user service directory '$USER_SERVICE_CONF_DIR'."; exit 1; fi

    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=PyViewer Remote Desktop Server
After=graphical-session.target network-online.target
Wants=network-online.target

[Service]
ExecStart=$WRAPPER_SCRIPT_PATH
Restart=on-failure
WorkingDirectory=$INSTALL_DIR
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF
    if [ $? -eq 0 ]; then
        log_info "Systemd user service file created at $SERVICE_FILE."
    else
        log_error "Failed to create systemd user service file '$SERVICE_FILE'."
        exit 1
    fi
}

# --- ROBUST SERVICE ENABLING FUNCTION ---
enable_and_start_service() {
    log_info "Configuring systemd user service..."

    # PRE-FLIGHT CHECK: First, verify we can communicate with the user's systemd instance.
    # This is the most common point of failure. It fails if not in a graphical session or a linger session.
    if ! systemctl --user is-system-running --quiet &> /dev/null; then
        log_error "Could not connect to the systemd user instance."
        log_warn "This is expected if you are running this script via SSH without a graphical session."
        log_warn "The service file has been created successfully. To enable it, please do one of the following:"
        log_warn "  1. After logging into your desktop, run this command:"
        log_warn "     systemctl --user enable --now $SERVICE_FILE_NAME"
        log_warn "  2. To allow the service to run without you being logged in, enable lingering for your user:"
        log_warn "     sudo loginctl enable-linger $USER"
        log_warn "     Then, reboot or run the 'systemctl --user enable --now' command above."
        # We return a special status code to indicate installation was successful but activation is manual.
        return 2
    fi
    log_info "Connection to systemd user instance is active."

    log_info "Reloading systemd daemon, enabling and starting the service..."
    # These commands run as the current user.
    # 1. Reload the daemon to make systemd aware of the new service file.
    systemctl --user daemon-reload
    if [ $? -ne 0 ]; then
        log_error "Failed to reload systemd user daemon. Please run 'systemctl --user daemon-reload' manually."
        return 1
    fi

    # 2. Enable the service to start on boot and start it right now.
    # Using 'enable --now' is an atomic operation that is more reliable than separate enable and start.
    systemctl --user enable --now "$SERVICE_FILE_NAME"
    if [ $? -ne 0 ]; then
        log_error "Failed to enable or start the PyViewer server service."
        log_error "Please try running the following commands manually:"
        log_error "  systemctl --user enable $SERVICE_FILE_NAME"
        log_error "  systemctl --user start $SERVICE_FILE_NAME"
        return 1
    fi

    log_info "PyViewer server service has been enabled and started successfully."
    log_info "It will now launch automatically when you log in."
    return 0
}

# --- Main Installation Logic ---
check_not_root

log_info "Starting PyViewer server installation for user: $USER..."

log_info "Creating installation directory: $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR" || { log_error "Failed to create installation directory."; exit 1; }

log_info "Setting ownership of $INSTALL_DIR to $USER..."
sudo chown -R "$USER":"$USER" "$INSTALL_DIR" || { log_error "Failed to set ownership."; exit 1; }

log_info "Copying PyViewer server files..."
# Use an array to handle file copy and provide a clear error if they don't exist
FILES_TO_COPY=("$SERVER_SCRIPT" "$CLIENT_SCRIPT" "$README_FILE")
for file in "${FILES_TO_COPY[@]}"; do
    if [ ! -f "$file" ]; then
        log_error "Source file not found: $file. Aborting."; exit 1;
    fi
    cp "$file" "$INSTALL_DIR/"
done
if [ -f "server.ini" ]; then
    cp "server.ini" "$INSTALL_DIR/"
    log_info "Copied existing server.ini."
fi
log_info "Server files copied."

log_info "Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/$VENV_DIR"
if [ $? -ne 0 ]; then
    log_error "Failed to create virtual environment. Is 'python3-venv' installed?"
    log_error "Try: sudo apt install python3-venv"
    exit 1
fi

install_python_deps
check_system_deps
create_wrapper_script
create_systemd_user_service

# Final step: Enable and start the service
enable_and_start_service
SERVICE_EXIT_CODE=$?

# Final messages based on outcome
log_info "--------------------------------------------------------------------------------"
if [ $SERVICE_EXIT_CODE -eq 0 ]; then
    log_info "PyViewer server installation complete and service is running."
    log_info "To manage the service:"
    log_info "  - Check status: systemctl --user status $SERVICE_FILE_NAME"
    log_info "  - View logs:    journalctl --user -u $SERVICE_FILE_NAME"
elif [ $SERVICE_EXIT_CODE -eq 2 ]; then
    log_info "PyViewer server installation is complete, but the service could not be auto-started."
    log_info "Please follow the manual activation steps printed in the warnings above."
else
    log_error "PyViewer server installation complete, but there were errors enabling the service."
    log_error "Please review the errors above and attempt to enable the service manually."
fi
log_info "--------------------------------------------------------------------------------"
