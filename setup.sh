#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)

require_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
  fi
}

install_prereqs() {
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl screen jq
}

setup_local() {
  echo "[1] Local install (current webapp)"
  install_prereqs
  cd "$ROOT_DIR"
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --upgrade pip
  # Reuse webapp dependencies from current runtime
  pip install fastapi uvicorn jinja2 passlib[bcrypt] requests playwright
  python -m playwright install --with-deps chromium || true

  cat >/etc/systemd/system/gensyn-webapp.service <<EOF
[Unit]
Description=Gensyn WebApp
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR
Environment=SESSION_SECRET=${SESSION_SECRET:-change_me_please}
ExecStart=$ROOT_DIR/.venv/bin/uvicorn webapp.app.main:app --host 0.0.0.0 --port 3000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable gensyn-webapp
  systemctl restart gensyn-webapp
  echo "Local webapp installed and started on port 3000."
}

setup_backend() {
  echo "[2] Backend manager install"
  install_prereqs
  cd "$ROOT_DIR/manager"
  python3 -m venv venv
  . venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt

  read -rp "Manager listen port [8080]: " MPORT
  MPORT=${MPORT:-8080}
  read -rp "Agent registration key (keep secret): " AGENT_KEY
  AGENT_KEY=${AGENT_KEY:-changeme}
  read -rp "Admin username for manager [admin]: " MUSER
  MUSER=${MUSER:-admin}
  read -rp "Admin password for manager [admin]: " MPASS
  MPASS=${MPASS:-admin}

  cat >/etc/systemd/system/gensyn-manager.service <<EOF
[Unit]
Description=Gensyn Manager (backend)
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR/manager
Environment=AGENT_REGISTRATION_KEY=$AGENT_KEY
Environment=SESSION_SECRET=${SESSION_SECRET:-change_me_backend}
Environment=MANAGER_USER=$MUSER
Environment=MANAGER_PASS=$MPASS
ExecStart=$ROOT_DIR/manager/venv/bin/uvicorn main:app --host 0.0.0.0 --port $MPORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable gensyn-manager
  systemctl restart gensyn-manager
  echo "Manager running on port $MPORT. Keep AGENT_REGISTRATION_KEY safe."
}

setup_agent() {
  echo "[3] Multi-VPS agent install"
  install_prereqs
  cd "$ROOT_DIR/agent"
  python3 -m venv venv
  . venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt

  # Install Playwright browser and dependencies for remote login assistant
  # This will fetch Chromium and apt dependencies non-interactively
  python -m playwright install --with-deps chromium || true

  read -rp "Manager URL (e.g. http://MANAGER_HOST:8080): " MURL
  read -rp "Agent name (hostname if empty) [$(hostname)]: " ANAME
  ANAME=${ANAME:-$(hostname)}
  read -rp "Registration key (from manager): " AKEY

  # Write config
  mkdir -p /etc/gensyn-agent
  cat >/etc/gensyn-agent/config.env <<EOF
MANAGER_URL=$MURL
AGENT_NAME=$ANAME
AGENT_REG_KEY=$AKEY
EOF

  cat >/etc/systemd/system/gensyn-agent.service <<EOF
[Unit]
Description=Gensyn Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT_DIR/agent
EnvironmentFile=/etc/gensyn-agent/config.env
ExecStart=$ROOT_DIR/agent/venv/bin/python agent.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable gensyn-agent
  systemctl restart gensyn-agent
  echo "Agent installed and started. Check manager UI for connection."
}

main() {
  echo "Select setup mode:"
  echo "  1) Local (current single-node webapp)"
  echo "  2) Backend manager (multi-VPS control plane)"
  echo "  3) Multi-VPS agent (connect to manager)"
  read -rp "Enter choice [1-3]: " choice
  require_root
  case "$choice" in
    1) setup_local ;;
    2) setup_backend ;;
    3) setup_agent ;;
    *) echo "Invalid choice" >&2; exit 1 ;;
  esac
}

main "$@"

#!/usr/bin/env bash
set -euo pipefail

echo "🔧 Updating system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl screen tmate wireguard ufw || true

# Playwright runtime system libraries (Ubuntu 24.04 t64 variants)
apt-get install -y \
  libnss3 \
  libicu74 \
  libatk1.0-0t64 \
  libatk-bridge2.0-0t64 \
  libcups2t64 \
  libatspi2.0-0t64 \
  libx11-6 \
  libxcomposite1 \
  libxdamage1 \
  libxext6 \
  libxfixes3 \
  libxrandr2 \
  libgbm1 \
  libxcb1 \
  libxkbcommon0 \
  libpango-1.0-0 \
  libcairo2 \
  libasound2t64 || true

cd /root/node-manager

if [ ! -d .venv ]; then
  echo "🐍 Creating Python virtual environment..."
  python3 -m venv .venv
fi

echo "📦 Installing Python dependencies..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install \
  fastapi==0.111.0 \
  uvicorn[standard]==0.30.1 \
  jinja2==3.1.4 \
  python-multipart==0.0.9 \
  passlib==1.7.4 \
  itsdangerous==2.2.0 \
  requests==2.32.3

# Optional: install existing project deps if present
if [ -f requirements.txt ]; then
  ./.venv/bin/pip install -r requirements.txt || true
fi

echo "👤 Set up admin credentials for the web UI"
if [ -z "${ADMIN_USERNAME:-}" ]; then
  read -rp "Admin username: " ADMIN_USERNAME
fi
if [ -z "${ADMIN_PASSWORD:-}" ]; then
  while true; do
    read -srp "Admin password: " ADMIN_PASSWORD
    echo
    read -srp "Confirm password: " ADMIN_PASSWORD2
    echo
    if [ "$ADMIN_PASSWORD" = "$ADMIN_PASSWORD2" ]; then
      break
    else
      echo "Passwords do not match. Try again."
    fi
  done
fi

SESSION_SECRET=$(./.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')
PASSWORD_HASH=$(./.venv/bin/python - "$ADMIN_PASSWORD" <<'PY'
import sys
from passlib.hash import pbkdf2_sha256
print(pbkdf2_sha256.hash(sys.argv[1]))
PY
)

# Ensure Playwright Chromium browser is present
./.venv/bin/playwright install chromium || true

cat >/root/node-manager/.env.web <<ENV
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD_HASH=${PASSWORD_HASH}
SESSION_SECRET=${SESSION_SECRET}
ENV

echo "🧩 Creating systemd service for the web UI..."
cat >/etc/systemd/system/gensyn-web.service <<'SERVICE'
[Unit]
Description=Gensyn Web Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/node-manager
EnvironmentFile=/root/node-manager/.env.web
ExecStart=/root/node-manager/.venv/bin/uvicorn webapp.app.main:app --host 0.0.0.0 --port 3012
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable gensyn-web.service
systemctl restart gensyn-web.service

if command -v ufw >/dev/null 2>&1; then
  if ufw status | grep -q "Status: active"; then
    ufw allow 3012/tcp || true
  fi
fi

IP=$(curl -s https://api.ipify.org || echo "<your_server_ip>")
echo "✅ Setup complete. Open: http://${IP}:3012"
echo "Use the username and password you just set to sign in."


