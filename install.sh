#!/bin/bash
# =============================================================================
# reefbeat⚡Backup — Installer
#
# Downloads the latest release from GitHub and runs the configuration wizard.
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | sudo bash
#   curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | sudo bash -s -- /custom/path
#
#   # Or download first then run:
#   wget https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh
#   sudo bash install.sh
# =============================================================================

set -e

REPO="Elwinmage/reefbeatEnergyBackup"
DEFAULT_DIR="${HOME}/scripts/reefbeatEnergyBackup"
INSTALL_DIR="${1:-${DEFAULT_DIR}}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

# When piped via curl|bash, stdin is the pipe.
# Redirect interactive reads from /dev/tty instead.
if [ ! -t 0 ]; then
    exec 3</dev/tty
else
    exec 3<&0
fi

# Helper: read from terminal (works both piped and direct)
ask_input() {
    local var_name="$1"
    local prompt="$2"
    local default="$3"
    local input

    if [ -n "$default" ]; then
        echo -en "${prompt} [${default}]: " >&2
    else
        echo -en "${prompt}: " >&2
    fi
    read -r input <&3
    if [ -z "$input" ] && [ -n "$default" ]; then
        input="$default"
    fi
    eval "${var_name}='${input}'"
}

# Detect language
LANG_CODE="${LANG%%_*}"
if [ "$LANG_CODE" = "fr" ]; then
    MSG_BANNER="⚡ reefbeat⚡Backup — Installation"
    MSG_ASK_DIR="Répertoire d'installation"
    MSG_CONFIRM="Confirmer ? (O/n)"
    MSG_DOWNLOADING="Téléchargement de la dernière version..."
    MSG_EXTRACTING="Extraction..."
    MSG_NO_RELEASE="Aucune release trouvée, clonage du dépôt..."
    MSG_DONE="Installation terminée !"
    MSG_CONFIGURE="Lancement de la configuration..."
    MSG_NEED_ROOT="Ce script nécessite les droits root pour installer les dépendances."
    MSG_RERUN="Relancez avec: sudo bash install.sh"
    MSG_DEPS="Installation des dépendances système..."
    MSG_EXISTING="Un répertoire existant a été détecté. Mise à jour..."
    MSG_BACKUP="Sauvegarde de la configuration existante..."
    MSG_PIP="Installation des dépendances Python..."
else
    MSG_BANNER="⚡ reefbeat⚡Backup — Installation"
    MSG_ASK_DIR="Installation directory"
    MSG_CONFIRM="Confirm? (Y/n)"
    MSG_DOWNLOADING="Downloading latest release..."
    MSG_EXTRACTING="Extracting..."
    MSG_NO_RELEASE="No release found, cloning repository..."
    MSG_DONE="Installation complete!"
    MSG_CONFIGURE="Launching configuration wizard..."
    MSG_NEED_ROOT="This script requires root privileges to install dependencies."
    MSG_RERUN="Rerun with: sudo bash install.sh"
    MSG_DEPS="Installing system dependencies..."
    MSG_EXISTING="Existing directory detected. Updating..."
    MSG_BACKUP="Backing up existing configuration..."
    MSG_PIP="Installing Python dependencies..."
fi

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "  ${BLUE}${MSG_BANNER}${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""

# Ask for install directory
ask_input INSTALL_DIR "${YELLOW}${MSG_ASK_DIR}${NC}" "${INSTALL_DIR}"

echo -e "  → ${INSTALL_DIR}"
ask_input confirm "${YELLOW}${MSG_CONFIRM}${NC}" "Y"

case "$confirm" in
    [Nn]*)
        echo "Aborted."
        exit 0
        ;;
esac

# Check root for system deps
SKIP_SYSTEM_DEPS=0
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${YELLOW}${MSG_NEED_ROOT}${NC}"
    echo ""
    echo "Continuing without system package installation..."
    SKIP_SYSTEM_DEPS=1
fi

# Install system dependencies
if [ "${SKIP_SYSTEM_DEPS}" -eq 0 ]; then
    echo -e "${BLUE}${MSG_DEPS}${NC}"
    apt-get update -qq 2>/dev/null || true
    apt-get install -y -qq \
        python3-pip python3-dev i2c-tools \
        wireless-tools curl jq git 2>/dev/null || true

    # Optional packages (don't fail if unavailable)
    apt-get install -y -qq hostapd dnsmasq bluetooth bluez 2>/dev/null || true
fi

# Backup existing config if directory exists
if [ -d "${INSTALL_DIR}" ]; then
    echo -e "${YELLOW}${MSG_EXISTING}${NC}"
    if [ -f "${INSTALL_DIR}/config.json" ]; then
        BACKUP_NAME="config.json.save.$(date +%Y%m%d.%H.%M.%S)"
        echo -e "${BLUE}${MSG_BACKUP}${NC} → ${BACKUP_NAME}"
        cp "${INSTALL_DIR}/config.json" "${INSTALL_DIR}/${BACKUP_NAME}"
    fi
fi

mkdir -p "${INSTALL_DIR}"

# Try to download latest release tarball
echo -e "${BLUE}${MSG_DOWNLOADING}${NC}"

RELEASE_URL=""
if command -v python3 >/dev/null 2>&1; then
    RELEASE_URL=$(curl -sL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
        | python3 -c "
import sys, json
try:
    r = json.load(sys.stdin)
    url = r.get('tarball_url', '')
    print(url if url else '')
except:
    print('')
" 2>/dev/null) || true
fi

if [ -n "${RELEASE_URL}" ] && [ "${RELEASE_URL}" != "None" ] && [ "${RELEASE_URL}" != "" ]; then
    echo -e "${BLUE}${MSG_EXTRACTING}${NC}"
    curl -sL "${RELEASE_URL}" | tar xz --strip-components=1 -C "${INSTALL_DIR}"
else
    echo -e "${YELLOW}${MSG_NO_RELEASE}${NC}"
    # Clone main branch
    TMP_DIR=$(mktemp -d)
    if git clone --depth 1 "https://github.com/${REPO}.git" "${TMP_DIR}" 2>/dev/null; then
        # Copy all files except .git
        find "${TMP_DIR}" -maxdepth 1 -not -name '.git' -not -path "${TMP_DIR}" \
            -exec cp -r {} "${INSTALL_DIR}/" \;
    else
        echo -e "${RED}Failed to download. Check your internet connection.${NC}"
        rm -rf "${TMP_DIR}"
        exit 1
    fi
    rm -rf "${TMP_DIR}"
fi

# Install Python dependencies
echo ""
echo -e "${BLUE}${MSG_PIP}${NC}"
pip3 install --break-system-packages \
    smbus2 paho-mqtt requests RPi.GPIO netifaces 2>/dev/null \
    || pip3 install smbus2 paho-mqtt requests RPi.GPIO netifaces 2>/dev/null \
    || true

# Optional: BLE dependencies
pip3 install --break-system-packages bleak victron-ble 2>/dev/null \
    || pip3 install bleak victron-ble 2>/dev/null \
    || true

echo ""
echo -e "${GREEN}✓ ${MSG_DONE}${NC}"
echo ""

# Launch configuration wizard
echo -e "${BLUE}${MSG_CONFIGURE}${NC}"
echo ""

if [ -f "${INSTALL_DIR}/configure.py" ]; then
    python3 "${INSTALL_DIR}/configure.py" --install-dir "${INSTALL_DIR}" <&3
else
    echo "configure.py not found in ${INSTALL_DIR}"
    echo "You can configure manually by copying config.example.json to config.json"
fi

# ==========================================================================
# Install and enable systemd service
# ==========================================================================

SERVICE_NAME="reefbeat-energy-backup"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -eq 0 ]; then
    echo ""
    if [ "$LANG_CODE" = "fr" ]; then
        echo -e "${BLUE}── Installation du service systemd ──${NC}"
    else
        echo -e "${BLUE}── Installing systemd service ──${NC}"
    fi

    # Generate service file pointing to the actual install directory
    cat > "${SERVICE_FILE}" <<SERVICEEOF
[Unit]
Description=reefbeat⚡Backup — Battery Monitor & Pump Controller
After=network-online.target mosquitto.service bluetooth.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

    # Disable hostapd and dnsmasq (we manage them manually)
    systemctl disable hostapd 2>/dev/null || true
    systemctl stop hostapd 2>/dev/null || true
    systemctl disable dnsmasq 2>/dev/null || true
    systemctl stop dnsmasq 2>/dev/null || true

    # Reload systemd and enable service
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" 2>/dev/null

    echo -e "  ${GREEN}✓${NC} Service ${BOLD}${SERVICE_NAME}${NC} installed and enabled"
    echo ""

    # Ask to start now
    if [ "$LANG_CODE" = "fr" ]; then
        ask_input start_now "${YELLOW}Démarrer le service maintenant ? (O/n)${NC}" "O"
    else
        ask_input start_now "${YELLOW}Start the service now? (Y/n)${NC}" "Y"
    fi

    case "$start_now" in
        [Nn]*)
            echo ""
            if [ "$LANG_CODE" = "fr" ]; then
                echo -e "  ${BLUE}ℹ${NC} Pour démarrer plus tard :"
            else
                echo -e "  ${BLUE}ℹ${NC} To start later:"
            fi
            echo "    sudo systemctl start ${SERVICE_NAME}"
            echo "    sudo journalctl -u ${SERVICE_NAME} -f"
            ;;
        *)
            if [ -f "${INSTALL_DIR}/config.json" ]; then
                systemctl start "${SERVICE_NAME}"
                sleep 2
                if systemctl is-active --quiet "${SERVICE_NAME}"; then
                    echo -e "  ${GREEN}✓${NC} Service started successfully"
                    echo ""
                    if [ "$LANG_CODE" = "fr" ]; then
                        echo -e "  ${BLUE}ℹ${NC} Voir les logs :"
                    else
                        echo -e "  ${BLUE}ℹ${NC} View logs:"
                    fi
                    echo "    sudo journalctl -u ${SERVICE_NAME} -f"
                else
                    echo -e "  ${RED}✗${NC} Service failed to start"
                    echo ""
                    echo "    sudo journalctl -u ${SERVICE_NAME} --no-pager -n 20"
                fi
            else
                echo -e "  ${YELLOW}⚠${NC} config.json not found — cannot start service"
                if [ "$LANG_CODE" = "fr" ]; then
                    echo -e "  ${BLUE}ℹ${NC} Lancez d'abord la configuration :"
                else
                    echo -e "  ${BLUE}ℹ${NC} Run the configuration first:"
                fi
                echo "    python3 ${INSTALL_DIR}/configure.py --install-dir ${INSTALL_DIR}"
            fi
            ;;
    esac
else
    echo ""
    if [ "$LANG_CODE" = "fr" ]; then
        echo -e "  ${YELLOW}⚠${NC} Pas root — le service systemd n'a pas été installé."
        echo -e "  ${BLUE}ℹ${NC} Pour installer le service :"
        echo "    sudo cp ${INSTALL_DIR}/reef-battery-monitor.service /etc/systemd/system/${SERVICE_NAME}.service"
        echo "    sudo systemctl daemon-reload"
        echo "    sudo systemctl enable ${SERVICE_NAME}"
        echo "    sudo systemctl start ${SERVICE_NAME}"
    else
        echo -e "  ${YELLOW}⚠${NC} Not root — systemd service was not installed."
        echo -e "  ${BLUE}ℹ${NC} To install the service:"
        echo "    sudo cp ${INSTALL_DIR}/reef-battery-monitor.service /etc/systemd/system/${SERVICE_NAME}.service"
        echo "    sudo systemctl daemon-reload"
        echo "    sudo systemctl enable ${SERVICE_NAME}"
        echo "    sudo systemctl start ${SERVICE_NAME}"
    fi
fi

# ==========================================================================
# Final summary
# ==========================================================================
echo ""
echo -e "${BOLD}============================================${NC}"
if [ "$LANG_CODE" = "fr" ]; then
    echo -e "  ${GREEN}⚡ reefbeat⚡Backup — Prêt !${NC}"
else
    echo -e "  ${GREEN}⚡ reefbeat⚡Backup — Ready!${NC}"
fi
echo -e "${BOLD}============================================${NC}"
echo ""
if [ "$LANG_CODE" = "fr" ]; then
    echo -e "  ${BLUE}ℹ${NC} Répertoire : ${INSTALL_DIR}"
    echo -e "  ${BLUE}ℹ${NC} Service    : sudo systemctl status ${SERVICE_NAME}"
    echo -e "  ${BLUE}ℹ${NC} Logs       : sudo journalctl -u ${SERVICE_NAME} -f"
    echo -e "  ${BLUE}ℹ${NC} Reconfigurer : python3 ${INSTALL_DIR}/configure.py"
else
    echo -e "  ${BLUE}ℹ${NC} Directory  : ${INSTALL_DIR}"
    echo -e "  ${BLUE}ℹ${NC} Service    : sudo systemctl status ${SERVICE_NAME}"
    echo -e "  ${BLUE}ℹ${NC} Logs       : sudo journalctl -u ${SERVICE_NAME} -f"
    echo -e "  ${BLUE}ℹ${NC} Reconfigure: python3 ${INSTALL_DIR}/configure.py"
fi
echo ""

# Close fd 3
exec 3<&-
