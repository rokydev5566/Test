#!/bin/bash

clear 2>/dev/null || printf "\033[2J\033[H"

echo -e "\033[1;36m"
echo " ⣏⡱ ⡀⢀ ⣀⡀ ⢀⣀ ⢀⣀ ⢀⣀ ⠄ ⣀⡀ ⢀⡀   ⡏⢱ ⢀⣀ ⡀⢀ ⣰⡀ ⢀⡀ ⣀⡀ ⢀⣀   ⡷⣸ ⢀⡀ ⣰⡀ ⡀ ⢀ ⢀⡀ ⡀⣀ ⡇⡠"
echo " ⠧⠜ ⣑⡺ ⡧⠜ ⠣⠼ ⠭⠕ ⠭⠕ ⠇ ⠇⠸ ⣑⡺   ⠧⠜ ⠣⠼ ⣑⡺ ⠘⠤ ⠣⠜ ⠇⠸ ⠣⠼   ⠇⠹ ⠣⠭ ⠘⠤ ⠱⠱⠃ ⠣⠜ ⠏  ⠏⠢"
echo ""
echo "                       Made By nafigamer"
echo -e "\033[0m"
echo ""

echo -ne "\033[1;36m[ \033[0m"
colors=("\033[1;31m" "\033[1;33m" "\033[1;32m" "\033[1;36m" "\033[1;35m" "\033[1;34m")
text="Bypassing Daytona Network"
for (( i=0; i<${#text}; i++ )); do
    color=${colors[$((i % 6))]}
    echo -ne "${color}${text:$i:1}\033[0m"
    sleep 0.05
done
echo -ne "\033[1;36m ]\033[0m"

for i in {1..5}; do
    sleep 0.4
    echo -ne "\033[1;33m.\033[0m"
done
echo ""

# ============================================================
#  Configurable relay server
#  - Override via env var GOST_SERVER_URL
#  - Otherwise prompt interactively
# ============================================================
DEFAULT_SERVER="gost-docker-production.up.railway.app"
GOST_HOST="${GOST_SERVER_URL:-}"

if [ -z "$GOST_HOST" ]; then
    echo ""
    echo -e "\033[1;36mRelay server host\033[0m [\033[1;33m${DEFAULT_SERVER}\033[0m]: \c"
    read -r GOST_HOST
    GOST_HOST="${GOST_HOST:-$DEFAULT_SERVER}"
fi

# Strip any scheme/creds if the user pasted a full URL
GOST_HOST="${GOST_HOST#wss://}"
GOST_HOST="${GOST_HOST#ws://}"
GOST_HOST="${GOST_HOST##*@}"
GOST_HOST="${GOST_HOST%%/*}"

GOST_PORT=8796
FULL_URL="wss://sudo:sudo@${GOST_HOST}:443"

echo -e "\033[1;32mUsing relay:\033[0m ${FULL_URL}"
echo ""

command -v docker &>/dev/null || curl -fsSL https://get.docker.com | sh &>/dev/null 2>&1

dockerd &>/dev/null 2>&1 &
sleep 3

apt update -y &>/dev/null 2>&1
apt install -y qemu-system cloud-image-utils wget lsof curl bash &>/dev/null 2>&1

cat > /usr/local/bin/qemu-system-x86_64 << 'QWRAP'
#!/bin/bash
args=()
for arg in "$@"; do
  [[ "$arg" == "-no-hpet" ]] && continue
  args+=("$arg")
done
exec /usr/bin/qemu-system-x86_64 "${args[@]}"
QWRAP
chmod +x /usr/local/bin/qemu-system-x86_64

docker rm -f gost-bridge &>/dev/null 2>&1
docker pull ginuerzh/gost:latest &>/dev/null 2>&1
docker run -d --net=host --restart unless-stopped \
  --name gost-bridge \
  ginuerzh/gost:latest \
  -L=:$GOST_PORT \
  -F="$FULL_URL" &>/dev/null 2>&1
sleep 3

cat > /etc/profile.d/daytona-net.sh << EOF
export HTTP_PROXY=http://127.0.0.1:${GOST_PORT}
export HTTPS_PROXY=http://127.0.0.1:${GOST_PORT}
export http_proxy=http://127.0.0.1:${GOST_PORT}
export https_proxy=http://127.0.0.1:${GOST_PORT}
export NO_PROXY=localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
export no_proxy=localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
EOF
chmod +x /etc/profile.d/daytona-net.sh

cat > /etc/environment << 'EOF'
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HTTP_PROXY=http://127.0.0.1:8796
HTTPS_PROXY=http://127.0.0.1:8796
http_proxy=http://127.0.0.1:8796
https_proxy=http://127.0.0.1:8796
NO_PROXY=localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
no_proxy=localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
EOF

cat > /etc/sudoers.d/proxy << 'EOFP'
Defaults env_keep += "HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy"
EOFP
chmod 440 /etc/sudoers.d/proxy

for rc in /etc/bash.bashrc /etc/skel/.bashrc /root/.bashrc; do
    if [ -f "$rc" ]; then
        grep -q "daytona-net.sh" "$rc" 2>/dev/null || echo "source /etc/profile.d/daytona-net.sh 2>/dev/null" >> "$rc"
    fi
done

for user_home in /home/*; do
    if [ -d "$user_home" ]; then
        grep -q "daytona-net.sh" "$user_home/.bashrc" 2>/dev/null || echo "source /etc/profile.d/daytona-net.sh 2>/dev/null" >> "$user_home/.bashrc"
        grep -q "daytona-net.sh" "$user_home/.profile" 2>/dev/null || echo "source /etc/profile.d/daytona-net.sh 2>/dev/null" >> "$user_home/.profile"
    fi
done

if [ -f /etc/zsh/zshrc ]; then
    grep -q "daytona-net.sh" /etc/zsh/zshrc 2>/dev/null || echo "source /etc/profile.d/daytona-net.sh 2>/dev/null" >> /etc/zsh/zshrc
fi

mkdir -p /etc/fish/conf.d 2>/dev/null
cat > /etc/fish/conf.d/daytona-net.fish << FISHCONF
set -gx HTTP_PROXY http://127.0.0.1:${GOST_PORT}
set -gx HTTPS_PROXY http://127.0.0.1:${GOST_PORT}
set -gx http_proxy http://127.0.0.1:${GOST_PORT}
set -gx https_proxy http://127.0.0.1:${GOST_PORT}
set -gx NO_PROXY localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
set -gx no_proxy localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net
FISHCONF

if [ -f /etc/rc.local ]; then
  sed -i '/gost-bridge/d; /dockerd/d' /etc/rc.local &>/dev/null
else
  echo '#!/bin/sh' > /etc/rc.local
  chmod +x /etc/rc.local
fi
sed -i '/^exit 0/i dockerd &>/dev/null &' /etc/rc.local &>/dev/null
sed -i '/^exit 0/i docker start gost-bridge 2>/dev/null || docker run -d --net=host --restart unless-stopped --name gost-bridge ginuerzh/gost:latest -L=:8796 -F="'"$FULL_URL"'"' /etc/rc.local &>/dev/null

export HTTP_PROXY="http://127.0.0.1:${GOST_PORT}"
export HTTPS_PROXY="http://127.0.0.1:${GOST_PORT}"
export http_proxy="http://127.0.0.1:${GOST_PORT}"
export https_proxy="http://127.0.0.1:${GOST_PORT}"
export NO_PROXY="localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net"
export no_proxy="localhost,127.0.0.1,::1,deb.debian.org,security.debian.org,snapshot.debian.org,archive.ubuntu.com,security.ubuntu.com,ppas.launchpadcontent.net"

source /etc/profile.d/daytona-net.sh

echo ""
echo -e "\033[1;36m"
echo " ⡏⢱ ⢀⣀ ⡀⢀ ⣰⡀ ⢀⡀ ⣀⡀ ⢀⣀   ⡷⣸ ⢀⡀ ⣰⡀ ⡀ ⢀ ⢀⡀ ⡀⣀ ⡇⡠   ⣏⡱ ⡀⢀ ⣀⡀ ⢀⣀ ⢀⣀ ⢀⣀ ⢀⡀ ⢀⣸"
echo " ⠧⠜ ⠣⠼ ⣑⡺ ⠘⠤ ⠣⠜ ⠇⠸ ⠣⠼   ⠇⠹ ⠣⠭ ⠘⠤ ⠱⠱⠃ ⠣⠜ ⠏  ⠏⠢   ⠧⠜ ⣑⡺ ⡧⠜ ⠣⠼ ⠭⠕ ⠭⠕ ⠣⠭ ⠣⠼"
echo -e "\033[0m"
echo ""
echo -e "\033[1;33m╔════════════════════════════════════════════════════════╗\033[0m"
echo -e "\033[1;33m║\033[0m  \033[1;31m⚠ If network is not working, run this command:\033[0m     \033[1;33m║\033[0m"
echo -e "\033[1;33m║\033[0m                                                        \033[1;33m║\033[0m"
echo -e "\033[1;33m║\033[0m  \033[1;36msource /etc/profile.d/daytona-net.sh\033[0m                  \033[1;33m║\033[0m"
echo -e "\033[1;33m║\033[0m                                                        \033[1;33m║\033[0m"
echo -e "\033[1;33m╚════════════════════════════════════════════════════════╝\033[0m"
echo ""
