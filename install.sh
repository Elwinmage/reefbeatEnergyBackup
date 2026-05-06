#!/bin/bash
# =============================================================================
# reefbeat⚡Backup — Installer
#
# Downloads the latest release from GitHub and runs the configuration wizard.
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | bash
#   curl -sL https://raw.githubusercontent.com/Elwinmage/reefbeatEnergyBackup/main/install.sh | bash -s -- /custom/path
# =============================================================================

set -e

REPO="Elwinmage/reefbeatEnergyBackup"
DEFAULT_DIR="$HOME/scripts/reefbeatEnergyBackup"
INSTALL_DIR="${1:-$DEFAULT_DIR}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

# Detect language
LANG_CODE="${LANG%%_*}"
if [[ "$LANG_CODE" == "fr" ]]; then
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
fi

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "  ${BLUE}${MSG_BANNER}${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""

# Ask for install directory
echo -e "${YELLOW}${MSG_ASK_DIR}${NC} [${INSTALL_DIR}]: "
read -r user_dir
if [[ -n "$user_dir" ]]; then
    INSTALL_DIR="$user_dir"
fi

echo -e "  → ${INSTALL_DIR}"
echo -en "${YELLOW}${MSG_CONFIRM}${NC} "
read -r confirm
if [[ "$confirm" =~ ^[Nn] ]]; then
    echo "Aborted."
    exit 0
fi

# Check root for system deps
if [[ $EUID -ne 0 ]]; then
    echo -e "${YELLOW}${MSG_NEED_ROOT}${NC}"
    echo -e "${YELLOW}${MSG_RERUN}${NC}"
    echo ""
    echo "Continuing without system package installation..."
    SKIP_SYSTEM_DEPS=1
else
    SKIP_SYSTEM_DEPS=0
fi

# Install system dependencies
if [[ $SKIP_SYSTEM_DEPS -eq 0 ]]; then
    echo -e "${BLUE}${MSG_DEPS}${NC}"
    apt-get update -qq 2>/dev/null || true
    apt-get install -y -qq python3-pip python3-dev i2c-tools \
        wireless-tools curl jq 2>/dev/null || true

    # Optional packages (don't fail if unavailable)
    apt-get install -y -qq hostapd dnsmasq bluetooth bluez 2>/dev/null || true
fi

# Backup existing config if directory exists
if [[ -d "$INSTALL_DIR" ]]; then
    echo -e "${YELLOW}${MSG_EXISTING}${NC}"
    if [[ -f "$INSTALL_DIR/config.json" ]]; then
        BACKUP_NAME="config.json.save.$(date +%Y%m%d.%H.%M.%S)"
        echo -e "${BLUE}${MSG_BACKUP}${NC} → ${BACKUP_NAME}"
        cp "$INSTALL_DIR/config.json" "$INSTALL_DIR/${BACKUP_NAME}"
    fi
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Try to download latest release
echo -e "${BLUE}${MSG_DOWNLOADING}${NC}"
RELEASE_URL=$(curl -sL "https://api.github.com/repos/${REPO}/releases/latest" \
    | python3 -c "import sys,json; r=json.load(sys.stdin); print(r.get('tarball_url',''))" 2>/dev/null || echo "")

if [[ -n "$RELEASE_URL" && "$RELEASE_URL" != "None" ]]; then
    echo -e "${BLUE}${MSG_EXTRACTING}${NC}"
    curl -sL "$RELEASE_URL" | tar xz --strip-components=1 -C "$INSTALL_DIR"
else
    echo -e "${YELLOW}${MSG_NO_RELEASE}${NC}"
    # Clone main branch
    TMP_DIR=$(mktemp -d)
    git clone --depth 1 "https://github.com/${REPO}.git" "$TMP_DIR" 2>/dev/null
    cp -r "$TMP_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
    rm -rf "$TMP_DIR"
fi

# Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install --break-system-packages smbus2 paho-mqtt requests RPi.GPIO 2>/dev/null || \
    pip3 install smbus2 paho-mqtt requests RPi.GPIO 2>/dev/null || true

# Optional: BLE dependencies
pip3 install --break-system-packages bleak victron-ble 2>/dev/null || \
    pip3 install bleak victron-ble 2>/dev/null || true

echo ""
echo -e "${GREEN}${MSG_DONE}${NC}"
echo ""

# Launch configuration wizard
echo -e "${BLUE}${MSG_CONFIGURE}${NC}"
echo ""

if [[ -f "$INSTALL_DIR/configure.py" ]]; then
    if [[ $EUID -eq 0 ]]; then
        python3 "$INSTALL_DIR/configure.py" --install-dir "$INSTALL_DIR"
    else
        echo "Running configuration (some features may require root)..."
        python3 "$INSTALL_DIR/configure.py" --install-dir "$INSTALL_DIR"
    fi
else
    echo "configure.py not found — skipping configuration wizard."
    echo "You can configure manually by editing config.json"
fi
