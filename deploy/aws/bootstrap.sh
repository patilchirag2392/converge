#!/usr/bin/env bash
#
# Converge -- prepare a fresh Ubuntu 24.04 EC2 instance to run the stack.
#
# Runs ON the box, as root, either as EC2 user-data at first boot or by hand:
#
#   sudo bash bootstrap.sh
#
# Deliberately contains NO SECRETS. When passed as user-data this script is
# readable forever from inside the instance (the metadata endpoint) and via
# `aws ec2 describe-instance-attribute --attribute userData` from outside. So it
# installs and clones only; the .env is written by hand over SSH afterwards.
#
# Idempotent: safe to re-run. Every step checks before acting.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/patilchirag2392/converge.git}"
REPO_REF="${REPO_REF:-stage-5-aws}"
APP_DIR="${APP_DIR:-/opt/converge}"
SWAP_SIZE="${SWAP_SIZE:-2G}"
# The unprivileged login user on Ubuntu AMIs. Given docker group membership so
# the stack can be driven without sudo after `newgrp docker` or a reconnect.
LOGIN_USER="${LOGIN_USER:-ubuntu}"

log() { printf '\n=== %s\n' "$*"; }

# -----------------------------------------------------------------------------
log "1/6  apt update + base packages"
# noninteractive so a grub/needrestart prompt cannot hang an unattended boot.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git gnupg jq >/dev/null

# -----------------------------------------------------------------------------
log "2/6  swap (${SWAP_SIZE})"
# This is not optional on a 1 GiB instance.
#
# `npm ci` plus `vite build` in the client image build is the memory spike --
# comfortably over 1 GiB at peak. Without swap the build is OOM-killed by the
# kernel, and the failure is confusing: the container just dies with exit 137
# and no error message pointing at memory.
#
# Swap on EBS is slow, and that is fine: it is absorbing a build-time spike, not
# serving requests. A t3.small would avoid it, but costs roughly twice as much
# and is not free-tier eligible.
if swapon --show | grep -q '/swapfile'; then
    echo "swap already active"
else
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile          # root-only: swap holds whatever was in RAM
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
    # Prefer RAM; only reach for swap under real pressure.
    sysctl -q vm.swappiness=10
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >>/etc/sysctl.conf
fi
free -h

# -----------------------------------------------------------------------------
log "3/6  Docker from Docker's own apt repository"
# Docker's repo rather than Ubuntu's `docker.io` package, because we need
# Compose v2 as a CLI plugin with `!reset`/`!override` support (v2.24+), and
# Ubuntu's packaging lags. `docker-compose-plugin` is what provides
# `docker compose` as a subcommand.
if command -v docker >/dev/null 2>&1; then
    echo "docker already installed: $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck source=/dev/null
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi

# Start on boot, so the stack comes back after an instance stop/start without
# anyone SSHing in. Combined with `restart: unless-stopped` on the containers,
# that is the whole "survives a reboot" story on a single box.
systemctl enable --now docker

# Run compose without sudo. Takes effect on the next login, hence the note at
# the end rather than a promise that it works right now.
usermod -aG docker "$LOGIN_USER" || true

docker --version
docker compose version

# Guard the version assumption rather than hoping.
compose_version="$(docker compose version --short 2>/dev/null || echo 0)"
if [ "$(printf '%s\n2.24.0\n' "$compose_version" | sort -V | head -1)" != "2.24.0" ]; then
    echo "!! Compose $compose_version is older than 2.24; docker-compose.prod.yml" >&2
    echo "!! uses the !reset / !override tags and WILL NOT close the database" >&2
    echo "!! ports correctly. Upgrade before deploying." >&2
    exit 1
fi

# -----------------------------------------------------------------------------
log "4/6  clone $REPO_URL @ $REPO_REF into $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --depth 1 origin "$REPO_REF"
    git -C "$APP_DIR" checkout -q FETCH_HEAD
    echo "updated to $(git -C "$APP_DIR" rev-parse --short HEAD)"
else
    # --depth 1: the box needs the tree, not the history.
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"
fi
chown -R "$LOGIN_USER:$LOGIN_USER" "$APP_DIR"

# -----------------------------------------------------------------------------
log "5/6  seed the .env template (no values)"
# Copied, not filled. The whole point is that the values are typed on the box.
if [ -f "$APP_DIR/.env" ]; then
    echo ".env already exists -- leaving it alone"
else
    cp "$APP_DIR/deploy/aws/.env.production.example" "$APP_DIR/.env"
    chown "$LOGIN_USER:$LOGIN_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"    # contains a password once filled in
    echo "wrote $APP_DIR/.env from the template -- it still needs values"
fi

# -----------------------------------------------------------------------------
log "6/6  done"
cat <<EOF

Bootstrap complete. Nothing is running yet, on purpose -- the stack needs
secrets that are deliberately not in this script.

Next, as $LOGIN_USER:

  cd $APP_DIR
  # PUBLIC_HOST is what you type in a browser; get it from the instance metadata:
  TOKEN=\$(curl -sX PUT http://169.254.169.254/latest/api/token \\
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -H "X-aws-ec2-metadata-token: \$TOKEN" \\
    http://169.254.169.254/latest/meta-data/public-hostname; echo

  openssl rand -base64 24        # the POSTGRES_PASSWORD
  nano .env                      # fill in PUBLIC_HOST and POSTGRES_PASSWORD

  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

If docker says "permission denied" on that last command, your shell predates
the docker group membership: run \`newgrp docker\`, or just log out and back in.

EOF
