#!/usr/bin/env bash
# =============================================================================
# install.sh – Deploy the Tempest logger as a systemd service
# =============================================================================
# Usage:
#   sudo ./install.sh
#
# What it does:
#   1. Creates a Python virtual environment at .venv/ and installs dependencies.
#   2. Copies settings_sample.conf → settings.conf if settings.conf is absent.
#   3. Generates /etc/systemd/system/tempest-logger.service from the template.
#   4. Reloads systemd and enables the service to start on boot.
#
# The service is NOT started automatically so you can review/edit settings.conf
# first.  Start it manually when ready:
#   sudo systemctl start tempest-logger
# =============================================================================

set -euo pipefail

SERVICE_NAME="tempest-logger"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="${INSTALL_DIR}/tempest-logger.service.template"
SETTINGS_SAMPLE="${INSTALL_DIR}/settings_sample.conf"
SETTINGS="${INSTALL_DIR}/settings.conf"

# ---------------------------------------------------------------------------
# Require root
# ---------------------------------------------------------------------------
if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: This script must be run as root."
    echo "       Re-run with: sudo ./install.sh"
    exit 1
fi

# ---------------------------------------------------------------------------
# Determine the user to run the service as
# ---------------------------------------------------------------------------
# When invoked via sudo, SUDO_USER is set to the invoking user.
# Fall back to the current user if not available.
SERVICE_USER="${SUDO_USER:-$(whoami)}"

# ---------------------------------------------------------------------------
# Ensure python3-full is installed (provides venv + ensurepip on Debian/RPi OS)
# ---------------------------------------------------------------------------
if ! dpkg -s python3-full &>/dev/null; then
    echo "Installing python3-full via apt ..."
    apt-get update
    apt-get install -y python3-full
fi

# ---------------------------------------------------------------------------
# Ensure build dependencies for psycopg2 are available.
# Some ARM/Raspberry Pi environments do not have prebuilt wheels, so pip falls
# back to building from source and needs pg_config and compiler toolchain.
# ---------------------------------------------------------------------------
MISSING_PKGS=()
for pkg in libpq-dev gcc python3-dev; do
    if ! dpkg -s "${pkg}" &>/dev/null; then
        MISSING_PKGS+=("${pkg}")
    fi
done
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    echo "Installing build dependencies: ${MISSING_PKGS[*]}"
    apt-get update
    apt-get install -y "${MISSING_PKGS[@]}"
fi

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "ERROR: python3 not found in PATH."
    exit 1
fi
echo "Using Python: ${PYTHON_BIN}"

# ---------------------------------------------------------------------------
# Create virtual environment and install dependencies
# ---------------------------------------------------------------------------
VENV_DIR="${INSTALL_DIR}/.venv"
# Remove a previously broken venv that was created without pip
if [[ -d "${VENV_DIR}" && ! -f "${VENV_DIR}/bin/pip" ]]; then
    echo "Removing incomplete virtual environment ..."
    rm -rf "${VENV_DIR}"
fi
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "Creating virtual environment at ${VENV_DIR} ..."
    sudo -u "${SERVICE_USER}" "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# Keep packaging tools current so pip can use modern wheels/build backends.
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/python3" -m pip install -q --upgrade pip setuptools wheel

echo "Installing dependencies into virtual environment ..."
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"
PYTHON_BIN="${VENV_DIR}/bin/python3"
echo "Using venv Python: ${PYTHON_BIN}"

# ---------------------------------------------------------------------------
# Copy sample config if settings.conf does not yet exist
# ---------------------------------------------------------------------------
if [[ ! -f "${SETTINGS}" ]]; then
    cp "${SETTINGS_SAMPLE}" "${SETTINGS}"
    chown "${SERVICE_USER}" "${SETTINGS}"
    echo "Created ${SETTINGS} from template."
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Edit ${SETTINGS} and set your PostgreSQL connection URL and"
    echo "  column names before starting the service."
    echo ""
else
    echo "Found existing ${SETTINGS} – leaving it untouched."
fi

# ---------------------------------------------------------------------------
# Generate the systemd service file from the template
# ---------------------------------------------------------------------------
if [[ ! -f "${TEMPLATE}" ]]; then
    echo "ERROR: Service template not found: ${TEMPLATE}"
    exit 1
fi

sed \
    -e "s|%%INSTALL_DIR%%|${INSTALL_DIR}|g" \
    -e "s|%%PYTHON_BIN%%|${PYTHON_BIN}|g" \
    -e "s|%%SERVICE_USER%%|${SERVICE_USER}|g" \
    "${TEMPLATE}" > "${SERVICE_DEST}"

chmod 644 "${SERVICE_DEST}"
echo "Installed service file: ${SERVICE_DEST}"

# ---------------------------------------------------------------------------
# Reload systemd and enable the service
# ---------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "Service '${SERVICE_NAME}' is installed and enabled for start on boot."
echo ""
echo "Next steps:"
echo "  1. Edit ${SETTINGS}"
echo "  2. Start the service:  sudo systemctl start ${SERVICE_NAME}"
echo "  3. Check logs:         sudo journalctl -u ${SERVICE_NAME} -f"
