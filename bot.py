import discord
from discord.ext import commands
import asyncio
import subprocess
import json
from datetime import datetime, timezone
import shlex
import logging
import shutil
import os
from typing import Optional, List, Dict, Any
import threading
import time
import sqlite3
import random
import requests
import secrets
import string
import re

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
BOT_NAME = os.getenv('BOT_NAME', 'BBYTOP-VPS-V1')
PREFIX = os.getenv('PREFIX', '!')
YOUR_SERVER_IP = os.getenv('YOUR_SERVER_IP', '127.0.0.1')

_cached_public_ip = None

def get_public_ip() -> str:
    global _cached_public_ip
    if _cached_public_ip:
        return _cached_public_ip
    try:
        resp = requests.get("https://ifconfig.me/ip", timeout=5)
        ip = resp.text.strip()
        if ip:
            _cached_public_ip = ip
            return ip
    except Exception as e:
        logger.warning(f"Failed to fetch public IP: {e}")
    return YOUR_SERVER_IP

_raw_main_admin_ids = os.getenv('MAIN_ADMIN_ID', '1155148045231591539')
MAIN_ADMIN_IDS_ENV = [uid.strip() for uid in _raw_main_admin_ids.split(',') if uid.strip()]
MAIN_ADMIN_ID = int(MAIN_ADMIN_IDS_ENV[0]) if MAIN_ADMIN_IDS_ENV else 0
VPS_USER_ROLE_ID = int(os.getenv('VPS_USER_ROLE_ID', '1210291131301101618'))
DEFAULT_STORAGE_POOL = os.getenv('DEFAULT_STORAGE_POOL', 'overlay2')
BOT_VERSION = os.getenv('BOT_VERSION', '10.0-DOCKER')
BOT_DEVELOPER = os.getenv('BOT_DEVELOPER', 'BBYTOP')

# OS options mapped to real Docker Hub images.
# `value` is stored in DB (what shows in menus); `docker` is the real image tag.
OS_OPTIONS = [
    {"label": "Ubuntu 20.04 LTS",     "value": "ubuntu:20.04", "docker": "ubuntu:20.04"},
    {"label": "Ubuntu 22.04 LTS",     "value": "ubuntu:22.04", "docker": "ubuntu:22.04"},
    {"label": "Ubuntu 24.04 LTS",     "value": "ubuntu:24.04", "docker": "ubuntu:24.04"},
    {"label": "Debian 10 (Buster)",   "value": "debian:10",    "docker": "debian:buster"},
    {"label": "Debian 11 (Bullseye)", "value": "debian:11",    "docker": "debian:bullseye"},
    {"label": "Debian 12 (Bookworm)", "value": "debian:12",    "docker": "debian:bookworm"},
    {"label": "Debian 13 (Trixie)",   "value": "debian:13",    "docker": "debian:trixie"},
]

def resolve_docker_image(value: str) -> str:
    for o in OS_OPTIONS:
        if o["value"] == value:
            return o["docker"]
    return "ubuntu:22.04"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()]
)
logger = logging.getLogger(f'{BOT_NAME.lower()}_vps_bot')

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect('vps.db')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id TEXT PRIMARY KEY
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS main_admins (
        user_id TEXT PRIMARY KEY
    )''')
    for uid in MAIN_ADMIN_IDS_ENV:
        cur.execute('INSERT OR IGNORE INTO main_admins (user_id) VALUES (?)', (uid,))
    cur.execute('''CREATE TABLE IF NOT EXISTS nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        location TEXT,
        total_vps INTEGER,
        tags TEXT DEFAULT '[]',
        api_key TEXT,
        url TEXT,
        is_local INTEGER DEFAULT 0
    )''')
    cur.execute('SELECT COUNT(*) FROM nodes WHERE is_local = 1')
    if cur.fetchone()[0] == 0:
        cur.execute(
            'INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?, ?, ?, ?, ?, ?, ?)',
            ('Local Node', 'Local', 100, '[]', None, None, 1))
    cur.execute('''CREATE TABLE IF NOT EXISTS vps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        node_id INTEGER NOT NULL DEFAULT 1,
        container_name TEXT UNIQUE NOT NULL,
        ram TEXT NOT NULL,
        cpu TEXT NOT NULL,
        storage TEXT NOT NULL,
        config TEXT NOT NULL,
        os_version TEXT DEFAULT 'ubuntu:22.04',
        status TEXT DEFAULT 'stopped',
        suspended INTEGER DEFAULT 0,
        whitelisted INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        shared_with TEXT DEFAULT '[]',
        suspension_history TEXT DEFAULT '[]',
        root_password TEXT DEFAULT '',
        pinggy_address TEXT DEFAULT ''
    )''')
    cur.execute('PRAGMA table_info(vps)')
    columns = [col[1] for col in cur.fetchall()]
    for col, ddl in [
        ('os_version', "ALTER TABLE vps ADD COLUMN os_version TEXT DEFAULT 'ubuntu:22.04'"),
        ('node_id', "ALTER TABLE vps ADD COLUMN node_id INTEGER DEFAULT 1"),
        ('root_password', "ALTER TABLE vps ADD COLUMN root_password TEXT DEFAULT ''"),
        ('pinggy_address', "ALTER TABLE vps ADD COLUMN pinggy_address TEXT DEFAULT ''"),
    ]:
        if col not in columns:
            cur.execute(ddl)
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')
    for k, v in [('cpu_threshold', '90'), ('ram_threshold', '90')]:
        cur.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
    cur.execute('''CREATE TABLE IF NOT EXISTS port_allocations (
        user_id TEXT PRIMARY KEY,
        allocated_ports INTEGER DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS port_forwards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        vps_container TEXT NOT NULL,
        vps_port INTEGER NOT NULL,
        host_port INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        proxy_name TEXT DEFAULT ''
    )''')
    cur.execute('PRAGMA table_info(port_forwards)')
    pf_cols = [col[1] for col in cur.fetchall()]
    if 'proxy_name' not in pf_cols:
        cur.execute("ALTER TABLE port_forwards ADD COLUMN proxy_name TEXT DEFAULT ''")
    conn.commit()
    conn.close()

def get_setting(key: str, default: Any = None):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cur.fetchone(); conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit(); conn.close()

def get_nodes() -> List[Dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM nodes')
    rows = cur.fetchall(); conn.close()
    nodes = [dict(r) for r in rows]
    for n in nodes:
        n['tags'] = json.loads(n['tags'])
    return nodes

def get_node(node_id: int) -> Optional[Dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
    row = cur.fetchone(); conn.close()
    if row:
        n = dict(row); n['tags'] = json.loads(n['tags']); return n
    return None

def get_current_vps_count(node_id: int) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM vps WHERE node_id = ?', (node_id,))
    c = cur.fetchone()[0]; conn.close(); return c

def get_vps_data() -> Dict[str, List[Dict[str, Any]]]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM vps')
    rows = cur.fetchall(); conn.close()
    data: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        uid = row['user_id']
        data.setdefault(uid, [])
        vps = dict(row)
        vps['shared_with'] = json.loads(vps.get('shared_with') or '[]')
        vps['suspension_history'] = json.loads(vps.get('suspension_history') or '[]')
        vps['suspended'] = bool(vps['suspended'])
        vps['whitelisted'] = bool(vps['whitelisted'])
        vps['os_version'] = vps.get('os_version') or 'ubuntu:22.04'
        vps['pinggy_address'] = vps.get('pinggy_address') or None
        data[uid].append(vps)
    return data

def get_admins() -> List[str]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT user_id FROM admins')
    rows = cur.fetchall(); conn.close()
    return [r['user_id'] for r in rows]

def get_main_admins() -> List[str]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT user_id FROM main_admins')
    rows = cur.fetchall(); conn.close()
    ids = [r['user_id'] for r in rows]
    return ids if ids else ([str(MAIN_ADMIN_ID)] if MAIN_ADMIN_ID else [])

def save_main_admins():
    conn = get_db(); cur = conn.cursor()
    cur.execute('DELETE FROM main_admins')
    for uid in main_admin_ids:
        cur.execute('INSERT INTO main_admins (user_id) VALUES (?)', (uid,))
    conn.commit(); conn.close()

def save_vps_data():
    conn = get_db(); cur = conn.cursor()
    try:
        for user_id, vps_list in vps_data.items():
            for vps in vps_list:
                shared_json = json.dumps(vps['shared_with'])
                history_json = json.dumps(vps.get('suspension_history', []))
                suspended_int = 1 if vps['suspended'] else 0
                whitelisted_int = 1 if vps.get('whitelisted', False) else 0
                os_ver = vps.get('os_version', 'ubuntu:22.04')
                created_at = vps.get('created_at', datetime.now().isoformat())
                node_id = vps.get('node_id', 1)
                root_password = vps.get('root_password', '')
                pinggy = vps.get('pinggy_address') or ''
                if 'id' not in vps or vps['id'] is None:
                    cur.execute('''INSERT INTO vps (user_id, node_id, container_name, ram, cpu, storage, config, os_version, status, suspended, whitelisted, created_at, shared_with, suspension_history, root_password, pinggy_address)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'],
                                 vps['storage'], vps['config'], os_ver, vps['status'],
                                 suspended_int, whitelisted_int, created_at,
                                 shared_json, history_json, root_password, pinggy))
                    vps['id'] = cur.lastrowid
                else:
                    cur.execute('''UPDATE vps SET user_id=?, node_id=?, container_name=?, ram=?, cpu=?, storage=?, config=?, os_version=?, status=?, suspended=?, whitelisted=?, shared_with=?, suspension_history=?, root_password=?, pinggy_address=?
                                   WHERE id = ?''',
                                (user_id, node_id, vps['container_name'], vps['ram'], vps['cpu'],
                                 vps['storage'], vps['config'], os_ver, vps['status'],
                                 suspended_int, whitelisted_int, shared_json, history_json,
                                 root_password, pinggy, vps['id']))
        conn.commit()
    finally:
        conn.close()

def save_admin_data():
    conn = get_db(); cur = conn.cursor()
    with conn:
        cur.execute('DELETE FROM admins')
        for admin_id in admin_data['admins']:
            cur.execute('INSERT INTO admins (user_id) VALUES (?)', (admin_id,))
    conn.close()

# ---------------------------------------------------------------------------
# Port allocations
# ---------------------------------------------------------------------------
def get_user_allocation(user_id: str) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT allocated_ports FROM port_allocations WHERE user_id = ?', (user_id,))
    row = cur.fetchone(); conn.close()
    return row[0] if row else 0

def get_user_used_ports(user_id: str) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM port_forwards WHERE user_id = ?', (user_id,))
    row = cur.fetchone(); conn.close()
    return row[0]

def allocate_ports(user_id: str, amount: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute(
        'INSERT INTO port_allocations (user_id, allocated_ports) VALUES (?, ?) '
        'ON CONFLICT(user_id) DO UPDATE SET allocated_ports = allocated_ports + excluded.allocated_ports',
        (user_id, amount))
    conn.commit(); conn.close()

def deallocate_ports(user_id: str, amount: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute('UPDATE port_allocations SET allocated_ports = MAX(0, allocated_ports - ?) WHERE user_id = ?',
                (amount, user_id))
    conn.commit(); conn.close()

def get_available_host_port(node_id: int) -> Optional[int]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT host_port FROM port_forwards WHERE vps_container IN '
                '(SELECT container_name FROM vps WHERE node_id = ?)', (node_id,))
    used = set(r[0] for r in cur.fetchall()); conn.close()
    for _ in range(200):
        port = random.randint(20000, 50000)
        if port not in used:
            # Check host if local
            node = get_node(node_id)
            if node and node['is_local']:
                try:
                    r = subprocess.run(['ss', '-tln'], capture_output=True, text=True, timeout=3)
                    if f":{port} " in r.stdout:
                        continue
                except Exception:
                    pass
            return port
    return None

def get_user_forwards(user_id: str) -> List[Dict]:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM port_forwards WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    rows = cur.fetchall(); conn.close()
    return [dict(r) for r in rows]

def find_node_id_for_container(container_name: str) -> int:
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT node_id FROM vps WHERE container_name = ?', (container_name,))
    row = cur.fetchone(); conn.close()
    return row[0] if row else 1

# ---------------------------------------------------------------------------
# Docker execution layer
# ---------------------------------------------------------------------------
async def execute_docker(container_name: str, command: str, timeout=120,
                         node_id: Optional[int] = None,
                         docker_args: Optional[List[str]] = None):
    """
    Execute a docker command on the target node.
    - If docker_args is provided, it is used as the raw argv after `docker`
      (safer for strings with spaces / special chars).
    - Else `command` is shlex-split and appended after `docker`.
    """
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if not node:
        raise Exception(f"Node {node_id} not found")

    if docker_args is not None:
        argv = ["docker"] + docker_args
        display = "docker " + " ".join(docker_args)
    else:
        argv = ["docker"] + shlex.split(command)
        display = "docker " + command

    if node['is_local']:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise asyncio.TimeoutError(f"Docker command timed out after {timeout}s: {display}")
            if proc.returncode != 0:
                err = stderr.decode(errors='replace').strip() or "Command failed"
                raise Exception(f"Docker command failed: {err}\nCommand: {display}")
            return stdout.decode(errors='replace').strip() if stdout else True
        except asyncio.TimeoutError:
            logger.error(f"Timeout: {display}")
            raise
        except Exception as e:
            logger.error(f"Docker error: {display} - {e}")
            raise
    else:
        url = f"{node['url']}/api/execute"
        data = {"command": display}
        params = {"api_key": node["api_key"]}
        try:
            response = requests.post(url, json=data, params=params, timeout=timeout)
            try:
                detail = response.json()
                err_msg = detail.get('detail') or detail.get('error') or response.text
            except Exception:
                err_msg = response.text
            response.raise_for_status()
            res = response.json()
            if res.get("returncode", 1) != 0:
                raise Exception(f"Remote Docker failed on {node['name']}: {res.get('stderr','')}\nCommand: {display}")
            return res.get("stdout", True)
        except requests.exceptions.RequestException as e:
            logger.error(f"Remote Docker error on {node['name']}: {e}")
            raise Exception(f"Remote execution failed on {node['name']}: {e}")

# Backwards-compat alias for the rest of the code
execute_lxc = execute_docker

# ---------------------------------------------------------------------------
# Docker container lifecycle helpers
# ---------------------------------------------------------------------------
async def docker_pull(image: str, node_id: int, timeout=900):
    try:
        await execute_docker("", f"pull {image}", node_id=node_id, timeout=timeout)
    except Exception as e:
        logger.warning(f"docker pull {image} failed (will try to run anyway): {e}")

async def docker_create_container(container_name: str,
                                  docker_image: str,
                                  ram_mb: int,
                                  cpu: float,
                                  disk_gb: int,
                                  node_id: int,
                                  host_port: Optional[int] = None,
                                  vps_port: int = 22) -> str:
    """
    Create and start a Docker container that behaves like a VPS.
    Returns the container ID (string).
    """
    # Pull image first (best-effort)
    await docker_pull(docker_image, node_id)

    args = [
        "run", "-d",
        "--name", container_name,
        "--hostname", container_name,
        "--restart", "unless-stopped",
        "--privileged",
        "--cap-add", "ALL",
        "--security-opt", "apparmor=unconfined",
        "--security-opt", "seccomp=unconfined",
        "--cgroupns", "host",
        "--memory", f"{ram_mb}m",
        "--memory-swap", f"{ram_mb}m",
        "--cpus", str(cpu),
        "--tmpfs", "/run",
        "--tmpfs", "/run/lock",
    ]

    if host_port:
        args += ["-p", f"{host_port}:{vps_port}"]

    # Entrypoint: install sshd + keep alive
    entry = (
        "set -e; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -y >/dev/null 2>&1 || true; "
        "apt-get install -y openssh-server sudo iproute2 iputils-ping procps >/dev/null 2>&1 || true; "
        "mkdir -p /run/sshd; "
        "sed -i 's/#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config || true; "
        "sed -i 's/#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config || true; "
        "service ssh start || /usr/sbin/sshd || true; "
        "tail -f /dev/null"
    )
    args += [docker_image, "bash", "-c", entry]

    return await execute_docker(container_name, "", node_id=node_id,
                                docker_args=args, timeout=900)

async def safe_start_container(container_name: str, node_id: int):
    try:
        await execute_docker(container_name, f"start {container_name}", node_id=node_id)
    except Exception as e:
        err = str(e).lower()
        if "already" in err and "running" in err:
            logger.info(f"{container_name} already running")
        else:
            raise

async def apply_docker_config(container_name: str, node_id: int):
    """Post-create config that doesn't require recreation."""
    try:
        await execute_docker(container_name, f"update --restart unless-stopped {container_name}", node_id=node_id)
        logger.info(f"Docker config applied to {container_name} on node {node_id}")
    except Exception as e:
        logger.error(f"apply_docker_config failed for {container_name}: {e}")

# Keep old name as alias so untouched code still works
apply_lxc_config = apply_docker_config

async def apply_internal_permissions(container_name: str, node_id: int):
    try:
        await asyncio.sleep(6)
        cmds = [
            "mkdir -p /etc/sysctl.d/",
            "echo 'net.ipv4.ip_unprivileged_port_start=0' > /etc/sysctl.d/99-custom.conf",
            "echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.d/99-custom.conf",
            "sysctl -p /etc/sysctl.d/99-custom.conf || true",
            "mkdir -p /run/sshd",
        ]
        for c in cmds:
            try:
                await execute_docker(container_name,
                                     f'exec {container_name} bash -c "{c}"',
                                     node_id=node_id)
            except Exception as e:
                logger.warning(f"Internal perm cmd failed in {container_name}: {c} - {e}")
        logger.info(f"Internal permissions applied to {container_name}")
    except Exception as e:
        logger.error(f"apply_internal_permissions failed for {container_name}: {e}")

# ---------------------------------------------------------------------------
# Passwords / SSH
# ---------------------------------------------------------------------------
def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

async def setup_ssh_access(container_name: str, node_id: int) -> str:
    """Set a random root password inside the container and ensure sshd is up."""
    password = generate_password()
    commands = [
        f"echo 'root:{password}' | chpasswd",
        "sed -i 's/#\\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config || true",
        "sed -i 's/#\\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config || true",
        "mkdir -p /run/sshd",
        "service ssh restart || /usr/sbin/sshd || true",
    ]
    for cmd in commands:
        try:
            await execute_docker(container_name,
                                 f'exec {container_name} bash -c "{cmd}"',
                                 node_id=node_id, timeout=120)
        except Exception as e:
            logger.warning(f"SSH setup step failed in {container_name}: {cmd} - {e}")
    return password

# ---------------------------------------------------------------------------
# Pinggy tunnel
# ---------------------------------------------------------------------------
PINGGY_LOG_PATH = "/root/.pinggy_tunnel.log"

def parse_pinggy_address(log_text: str) -> Optional[str]:
    if not log_text:
        return None
    m = re.search(r'tcp://([\w\.\-]+):(\d+)', log_text, re.IGNORECASE)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None

async def establish_pinggy_tunnel(container_name: str, node_id: int,
                                  retries: int = 4, wait_seconds: int = 5) -> Optional[str]:
    # Ensure ssh client
    try:
        await execute_docker(
            container_name,
            f'exec {container_name} bash -c "command -v ssh >/dev/null || (apt-get update -y && apt-get install -y openssh-client)"',
            node_id=node_id, timeout=300)
    except Exception as e:
        logger.warning(f"Pinggy: ssh client install failed in {container_name}: {e}")

    # Kill old tunnel and clear log
    try:
        await execute_docker(
            container_name,
            f'exec {container_name} bash -c "pkill -f free.pinggy.io >/dev/null 2>&1; rm -f {PINGGY_LOG_PATH}"',
            node_id=node_id)
    except Exception:
        pass

    tunnel_cmd = (
        "setsid nohup ssh -p 443 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=30 -R0:localhost:22 qr+tcp@free.pinggy.io "
        f"> {PINGGY_LOG_PATH} 2>&1 < /dev/null & disown"
    )
    try:
        await execute_docker(
            container_name,
            f'exec {container_name} bash -c "{tunnel_cmd}"',
            node_id=node_id)
    except Exception as e:
        logger.error(f"Pinggy start failed in {container_name}: {e}")
        return None

    for _ in range(retries):
        await asyncio.sleep(wait_seconds)
        try:
            out = await execute_docker(
                container_name,
                f"exec {container_name} cat {PINGGY_LOG_PATH}",
                node_id=node_id)
        except Exception:
            out = ""
        addr = parse_pinggy_address(out if isinstance(out, str) else "")
        if addr:
            return addr
    logger.warning(f"Pinggy: no address for {container_name} after {retries} attempts")
    return None

# ---------------------------------------------------------------------------
# Port forwarding via socat sidecar containers (TCP)
# ---------------------------------------------------------------------------
async def create_port_forward(user_id: str, container: str, vps_port: int,
                              node_id: int, protocol: str = "tcp") -> Optional[int]:
    host_port = get_available_host_port(node_id)
    if not host_port:
        return None

    # Find container bridge IP
    try:
        ip = await execute_docker(
            container,
            f"inspect -f '{{{{range .NetworkSettings.Networks}}}}{{{{.IPAddress}}}} {{{{end}}}}' {container}",
            node_id=node_id)
        ip = (ip or "").strip().split()[0] if ip else ""
    except Exception as e:
        logger.error(f"Failed to get IP of {container}: {e}")
        return None
    if not ip:
        return None

    proxy_name = f"proxy-{container}-{host_port}-{protocol}"
    socat_proto = "TCP" if protocol.lower() == "tcp" else "UDP"
    args = [
        "run", "-d",
        "--name", proxy_name,
        "--restart", "unless-stopped",
        "-p", f"{host_port}:{vps_port}/{protocol}",
        "alpine/socat",
        f"{socat_proto}-LISTEN:{vps_port},fork,reuseaddr",
        f"{socat_proto}:{ip}:{vps_port}",
    ]
    try:
        await execute_docker(container, "", node_id=node_id, docker_args=args)
    except Exception as e:
        logger.error(f"Failed to start socat proxy: {e}")
        # Cleanup half-created
        try:
            await execute_docker(container, f"rm -f {proxy_name}", node_id=node_id)
        except Exception:
            pass
        return None

    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO port_forwards (user_id, vps_container, vps_port, host_port, created_at, proxy_name) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, container, vps_port, host_port, datetime.now().isoformat(), proxy_name))
    conn.commit(); conn.close()
    return host_port

async def remove_port_forward(forward_id: int, is_admin: bool = False):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT user_id, vps_container, host_port, proxy_name FROM port_forwards WHERE id = ?', (forward_id,))
    row = cur.fetchone()
    if not row:
        conn.close(); return False, None
    user_id, container, host_port, proxy_name = row['user_id'], row['vps_container'], row['host_port'], row['proxy_name']
    node_id = find_node_id_for_container(container)
    if not proxy_name:
        proxy_name = f"proxy-{container}-{host_port}-tcp"
    try:
        await execute_docker(container, f"rm -f {proxy_name}", node_id=node_id)
        cur.execute('DELETE FROM port_forwards WHERE id = ?', (forward_id,))
        conn.commit(); conn.close()
        return True, user_id
    except Exception as e:
        logger.error(f"Failed to remove port forward {forward_id}: {e}")
        conn.close()
        return False, None

async def recreate_port_forwards(container_name: str) -> int:
    """After a container restart, sidecars are usually fine (unless container IP changed).
    We just re-verify the socat proxies exist. If a proxy is missing, recreate it."""
    node_id = find_node_id_for_container(container_name)
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT id, user_id, vps_port, host_port, proxy_name FROM port_forwards WHERE vps_container = ?',
                (container_name,))
    rows = cur.fetchall()
    conn.close()
    readded = 0
    for row in rows:
        proxy_name = row['proxy_name'] or f"proxy-{container_name}-{row['host_port']}-tcp"
        try:
            await execute_docker(container_name, f"inspect {proxy_name}", node_id=node_id)
            # exists — but container IP may have changed; recreate anyway to be safe
            try:
                await execute_docker(container_name, f"rm -f {proxy_name}", node_id=node_id)
            except Exception:
                pass
        except Exception:
            pass
        # Recreate
        new_host = await create_port_forward(row['user_id'], container_name,
                                             row['vps_port'], node_id)
        if new_host:
            # Delete old DB row (we just made a fresh one)
            conn = get_db(); cur = conn.cursor()
            cur.execute('DELETE FROM port_forwards WHERE id = ?', (row['id'],))
            conn.commit(); conn.close()
            readded += 1
    return readded

# ---------------------------------------------------------------------------
# Init DB + globals
# ---------------------------------------------------------------------------
init_db()
vps_data = get_vps_data()
admin_data = {'admins': get_admins()}
main_admin_ids = set(get_main_admins())
CPU_THRESHOLD = int(get_setting('cpu_threshold', 90))
RAM_THRESHOLD = int(get_setting('ram_threshold', 90))

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
resource_monitor_active = True

def truncate_text(text, max_length=1024):
    if not text:
        return text
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

def create_embed(title, description="", color=0x1a1a1a):
    embed = discord.Embed(
        title=truncate_text(f"🌟 {BOT_NAME} - {title}", 256),
        description=truncate_text(description, 4096),
        color=color)
    embed.set_footer(
        text=f"{BOT_NAME} VPS Manager v{BOT_VERSION} • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return embed

def add_field(embed, name, value, inline=False):
    embed.add_field(
        name=truncate_text(f"▸ {name}", 256),
        value=truncate_text(value, 1024),
        inline=inline)
    return embed

def create_success_embed(t, d=""): return create_embed(t, d, 0x00ff88)
def create_error_embed(t, d=""):   return create_embed(t, d, 0xff3366)
def create_info_embed(t, d=""):    return create_embed(t, d, 0x00ccff)
def create_warning_embed(t, d=""): return create_embed(t, d, 0xffaa00)

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def is_admin():
    async def predicate(ctx):
        uid = str(ctx.author.id)
        if uid in main_admin_ids or uid in admin_data.get("admins", []):
            return True
        raise commands.CheckFailure("You need admin permissions to use this command. Contact support.")
    return commands.check(predicate)

def is_main_admin():
    async def predicate(ctx):
        if str(ctx.author.id) in main_admin_ids:
            return True
        raise commands.CheckFailure("Only the main admin can use this command.")
    return commands.check(predicate)

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def _to_mb(s: str) -> float:
    s = s.strip()
    try:
        if s.endswith("GiB"): return float(s[:-3]) * 1024
        if s.endswith("MiB"): return float(s[:-3])
        if s.endswith("KiB"): return float(s[:-3]) / 1024
        if s.endswith("GB"):  return float(s[:-2]) * 1000
        if s.endswith("MB"):  return float(s[:-2])
        if s.endswith("kB"):  return float(s[:-2]) / 1000
        if s.endswith("B"):   return float(s[:-1]) / (1024 * 1024)
    except Exception:
        pass
    return 0.0

def _humanize_uptime(iso_started: str) -> str:
    try:
        started = datetime.fromisoformat(iso_started.replace("Z", "+00:00")).replace(tzinfo=None)
        delta = datetime.utcnow() - started
        d, rem = divmod(int(delta.total_seconds()), 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        return f"{d}d {h}h {m}m"
    except Exception:
        return "Unknown"

async def get_container_stats(container_name: str, node_id: Optional[int] = None) -> Dict:
    if node_id is None:
        node_id = find_node_id_for_container(container_name)
    node = get_node(node_id)
    if node and not node['is_local']:
        url = f"{node['url']}/api/get_container_stats"
        try:
            r = requests.post(url, json={"container": container_name},
                              params={"api_key": node["api_key"]}, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Remote stats failed from {node['name']}: {e}")
            return {"status": "unknown", "cpu": 0.0,
                    "ram": {"used": 0, "total": 0, "pct": 0.0},
                    "disk": "Unknown", "uptime": "Unknown"}
    try:
        stats_out = await execute_docker(
            container_name,
            f"stats --no-stream --format '{{{{.CPUPerc}}}}|{{{{.MemUsage}}}}|{{{{.MemPerc}}}}' {container_name}",
            node_id=node_id)
        cpu_s, mem_usage, mem_pct_s = stats_out.split("|")
        cpu = float(cpu_s.strip().rstrip("%") or 0)
        mem_used_str, mem_total_str = [x.strip() for x in mem_usage.split("/")]
        mem_pct = float(mem_pct_s.strip().rstrip("%") or 0)

        try:
            status = (await execute_docker(
                container_name,
                f"inspect -f '{{{{.State.Status}}}}' {container_name}",
                node_id=node_id)).strip()
        except Exception:
            status = "unknown"

        try:
            started = (await execute_docker(
                container_name,
                f"inspect -f '{{{{.State.StartedAt}}}}' {container_name}",
                node_id=node_id)).strip()
            uptime = _humanize_uptime(started)
        except Exception:
            uptime = "Unknown"

        try:
            df_out = await execute_docker(
                container_name,
                f"exec {container_name} df -h /",
                node_id=node_id)
            df_lines = [l for l in df_out.splitlines() if l.strip()]
            disk = df_lines[-1] if df_lines else "Unknown"
            parts = disk.split()
            if len(parts) >= 5:
                disk = f"{parts[2]}/{parts[1]} ({parts[4]})"
        except Exception:
            disk = "Unknown"

        return {"status": status, "cpu": cpu,
                "ram": {"used": _to_mb(mem_used_str), "total": _to_mb(mem_total_str), "pct": mem_pct},
                "disk": disk, "uptime": uptime}
    except Exception as e:
        logger.error(f"get_container_stats failed for {container_name}: {e}")
        return {"status": "unknown", "cpu": 0.0,
                "ram": {"used": 0, "total": 0, "pct": 0.0},
                "disk": "Unknown", "uptime": "Unknown"}

async def get_container_status(container_name, node_id=None):
    return (await get_container_stats(container_name, node_id))['status']

async def get_container_cpu_pct(container_name, node_id=None):
    return (await get_container_stats(container_name, node_id))['cpu']

async def get_container_uptime(container_name, node_id=None):
    return (await get_container_stats(container_name, node_id))['uptime']

async def get_container_ram_pct(container_name, node_id=None):
    return (await get_container_stats(container_name, node_id))['ram']['pct']

# ---------------------------------------------------------------------------
# Host resources
# ---------------------------------------------------------------------------
def get_host_cpu_usage():
    try:
        if shutil.which("mpstat"):
            r = subprocess.run(['mpstat', '1', '1'], capture_output=True, text=True)
            for line in r.stdout.split('\n'):
                if 'all' in line and '%' in line:
                    return 100.0 - float(line.split()[-1])
        r = subprocess.run(['top', '-bn1'], capture_output=True, text=True)
        for line in r.stdout.split('\n'):
            if '%Cpu(s):' in line:
                p = line.split()
                return float(p[1]) + float(p[3]) + float(p[5]) + float(p[9]) + float(p[11]) + float(p[13]) + float(p[15])
        return 0.0
    except Exception as e:
        logger.error(f"CPU usage error: {e}")
        return 0.0

def get_host_ram_usage():
    try:
        r = subprocess.run(['free', '-m'], capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) > 1:
            p = lines[1].split()
            return (int(p[2]) / int(p[1]) * 100) if int(p[1]) else 0.0
        return 0.0
    except Exception as e:
        logger.error(f"RAM usage error: {e}")
        return 0.0

def get_host_disk_usage():
    try:
        r = subprocess.run(['df', '-h', '/'], capture_output=True, text=True)
        lines = r.stdout.splitlines()
        if len(lines) > 1:
            p = lines[1].split()
            return f"{p[2]}/{p[1]} ({p[4]})"
        return "Unknown"
    except Exception:
        return "Unknown"

async def get_host_stats(node_id: int) -> Dict:
    node = get_node(node_id)
    if node['is_local']:
        return {"cpu": get_host_cpu_usage(), "ram": get_host_ram_usage(), "disk": get_host_disk_usage()}
    try:
        r = requests.get(f"{node['url']}/api/get_host_stats",
                         params={"api_key": node["api_key"]}, timeout=10)
        r.raise_for_status()
        s = r.json()
        s['disk'] = s.get('disk', 'Unknown')
        return s
    except Exception as e:
        logger.error(f"Remote host stats failed from {node['name']}: {e}")
        return {"cpu": 0.0, "ram": 0.0, "disk": "Unknown"}

def resource_monitor():
    global resource_monitor_active
    backup_interval = 3600
    last_backup = time.time()
    while resource_monitor_active:
        try:
            for node in get_nodes():
                stats = asyncio.run(get_host_stats(node['id']))
                logger.info(f"Node {node['name']}: CPU {stats['cpu']:.1f}%, RAM {stats['ram']:.1f}%")
                if stats['cpu'] > CPU_THRESHOLD or stats['ram'] > RAM_THRESHOLD:
                    logger.warning(f"Node {node['name']} exceeded thresholds.")
            if time.time() - last_backup > backup_interval:
                backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                try:
                    shutil.copy('vps.db', backup_name)
                    if os.path.exists('vps.db-wal'):
                        shutil.copy('vps.db-wal', f"{backup_name}-wal")
                    if os.path.exists('vps.db-shm'):
                        shutil.copy('vps.db-shm', f"{backup_name}-shm")
                    logger.info(f"DB backup: {backup_name}")
                    last_backup = time.time()
                except Exception as e:
                    logger.error(f"Backup failed: {e}")
            time.sleep(60)
        except Exception as e:
            logger.error(f"resource_monitor error: {e}")
            time.sleep(60)

threading.Thread(target=resource_monitor, daemon=True).start()

def get_uptime():
    try:
        return subprocess.run(['uptime'], capture_output=True, text=True).stdout.strip()
    except Exception:
        return "Unknown"

# ---------------------------------------------------------------------------
# Role helper
# ---------------------------------------------------------------------------
async def get_or_create_vps_role(guild):
    global VPS_USER_ROLE_ID
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return None
    role_name = f"{BOT_NAME} VPS User"
    if VPS_USER_ROLE_ID:
        role = guild.get_role(VPS_USER_ROLE_ID)
        if role and role < me.top_role:
            return role
        VPS_USER_ROLE_ID = None
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        if role >= me.top_role:
            try:
                await role.delete(reason="Role above bot, recreating")
            except discord.Forbidden:
                return None
            role = None
        else:
            VPS_USER_ROLE_ID = role.id
            return role
    try:
        role = await guild.create_role(
            name=role_name,
            color=discord.Color.dark_purple(),
            permissions=discord.Permissions.none(),
            reason=f"{BOT_NAME} VPS User role")
        await role.edit(position=me.top_role.position - 1)
        VPS_USER_ROLE_ID = role.id
        return role
    except Exception as e:
        logger.error(f"Role create failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    logger.info(f'{bot.user} connected!')
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name=f"{BOT_NAME} VPS Manager"))

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=create_error_embed("Missing Argument", "Check command usage with `!help`."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=create_error_embed("Invalid Argument", "Please check your input."))
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=create_error_embed("Access Denied", str(error) or "Admin only."))
    elif isinstance(error, discord.NotFound):
        await ctx.send(embed=create_error_embed("Error", "Resource not found."))
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(embed=create_error_embed("System Error", "Unexpected error occurred."))

# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------
@bot.command(name='ping')
async def ping(ctx):
    await ctx.send(embed=create_success_embed("Pong!", f"Latency: {round(bot.latency*1000)}ms"))

@bot.command(name='uptime')
async def uptime(ctx):
    await ctx.send(embed=create_info_embed("Host Uptime", get_uptime()))

@bot.command(name='thresholds')
@is_admin()
async def thresholds(ctx):
    await ctx.send(embed=create_info_embed("Resource Thresholds",
        f"**CPU:** {CPU_THRESHOLD}%\n**RAM:** {RAM_THRESHOLD}%"))

@bot.command(name='set-threshold')
@is_admin()
async def set_threshold(ctx, cpu: int, ram: int):
    global CPU_THRESHOLD, RAM_THRESHOLD
    if cpu < 0 or ram < 0:
        await ctx.send(embed=create_error_embed("Invalid", "Must be non-negative."))
        return
    CPU_THRESHOLD, RAM_THRESHOLD = cpu, ram
    set_setting('cpu_threshold', str(cpu))
    set_setting('ram_threshold', str(ram))
    await ctx.send(embed=create_success_embed("Thresholds Updated", f"CPU {cpu}% / RAM {ram}%"))

@bot.command(name='set-status')
@is_admin()
async def set_status(ctx, activity_type: str, *, name: str):
    types = {'playing': discord.ActivityType.playing,
             'watching': discord.ActivityType.watching,
             'listening': discord.ActivityType.listening,
             'streaming': discord.ActivityType.streaming}
    if activity_type.lower() not in types:
        await ctx.send(embed=create_error_embed("Invalid Type", "playing/watching/listening/streaming"))
        return
    await bot.change_presence(activity=discord.Activity(type=types[activity_type.lower()], name=name))
    await ctx.send(embed=create_success_embed("Status Updated", f"{activity_type}: {name}"))

# ---------------------------------------------------------------------------
# myvps
# ---------------------------------------------------------------------------
@bot.command(name="myvps")
async def my_vps(ctx):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        e = create_error_embed("❌ No VPS Found", f"You don't have any {BOT_NAME} VPS yet.")
        add_field(e, "🚀 Quick Actions", f"• `{PREFIX}manage` – Manage VPS\n• Contact admin for VPS")
        await ctx.send(embed=e)
        return
    embed = create_info_embed("🖥️ My VPS Dashboard", "Your personal VPS overview")
    total_vps = len(vps_list); running = suspended = whitelisted = 0
    cards = []
    for i, vps in enumerate(vps_list, start=1):
        node = get_node(vps.get("node_id"))
        node_name = node["name"] if node else "Unknown"
        cfg, ram, cpu, storage = vps.get("config", "Custom"), vps.get("ram", "0GB"), vps.get("cpu", "0"), vps.get("storage", "0GB")
        if vps.get("suspended"): status = "⛔ SUSPENDED"; suspended += 1
        elif vps.get("status") == "running": status = "🟢 RUNNING"; running += 1
        else: status = "🔴 STOPPED"
        if vps.get("whitelisted"): whitelisted += 1
        cards.append(f"**{i}.** `{vps['container_name']}`\n{status} • `{cfg}`\n⚙️ `{ram}` RAM • `{cpu}` CPU • `{storage}` Disk\n📍 Node: `{node_name}`")
    embed.add_field(name="📊 Summary",
                    value=f"🖥️ `{total_vps}` VPS\n🟢 `{running}` Running\n⛔ `{suspended}` Suspended\n✅ `{whitelisted}` Whitelisted",
                    inline=True)
    embed.add_field(name="⚡ Quick Actions",
                    value=f"`{PREFIX}manage`\n`{PREFIX}reinstall`\n`{PREFIX}status`", inline=True)
    embed.add_field(name="🧭 Tip", value="Use **manage** to control your VPS", inline=True)
    vps_text = "\n\n".join(cards)
    for i in range(0, len(vps_text), 1024):
        embed.add_field(name="🖥️ Your VPS", value=vps_text[i:i+1024], inline=False)
    await ctx.send(embed=embed)

@bot.command(name='lxc-list')
@is_admin()
async def lxc_list(ctx, node_id: int = 1):
    try:
        out = await execute_docker("", "ps -a --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}'", node_id=node_id)
        node = get_node(node_id)
        await ctx.send(embed=create_info_embed(f"Docker Containers on {node['name']}", out))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Error", str(e)))

# ---------------------------------------------------------------------------
# Node selector for create flow
# ---------------------------------------------------------------------------
class NodeSelectView(discord.ui.View):
    def __init__(self, ram, cpu, disk, user, ctx):
        super().__init__(timeout=300)
        self.ram, self.cpu, self.disk = ram, cpu, disk
        self.user, self.ctx = user, ctx
        options = []
        for n in get_nodes():
            current = get_current_vps_count(n['id'])
            if current < n['total_vps']:
                options.append(discord.SelectOption(
                    label=n['name'], value=str(n['id']),
                    description=f"{n['location']} - Available: {n['total_vps'] - current}"))
        if not options:
            self.add_item(discord.ui.Select(placeholder="No available nodes", disabled=True))
        else:
            self.select = discord.ui.Select(placeholder="Select a Node", options=options)
            self.select.callback = self.select_node
            self.add_item(self.select)

    async def select_node(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the author can select."), ephemeral=True)
            return
        node_id = int(self.select.values[0])
        self.select.disabled = True
        await interaction.response.edit_message(view=self)
        os_view = OSSelectView(self.ram, self.cpu, self.disk, self.user, self.ctx, node_id)
        await interaction.followup.send(embed=create_info_embed("Select OS", "Choose the OS for the VPS."), view=os_view)

class OSSelectView(discord.ui.View):
    def __init__(self, ram, cpu, disk, user, ctx, node_id):
        super().__init__(timeout=300)
        self.ram, self.cpu, self.disk = ram, cpu, disk
        self.user, self.ctx, self.node_id = user, ctx, node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the VPS",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS])
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        if str(interaction.user.id) != str(self.ctx.author.id):
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the author can select."), ephemeral=True)
            return
        os_version = self.select.values[0]
        self.select.disabled = True
        await interaction.response.edit_message(
            embed=create_info_embed("Creating VPS",
                f"Deploying {os_version} VPS for {self.user.mention} on node {self.node_id}..."),
            view=self)

        user_id = str(self.user.id)
        vps_data.setdefault(user_id, [])
        vps_count = len(vps_data[user_id]) + 1
        container_name = f"{BOT_NAME.lower()}-vps-{user_id}-{vps_count}"
        ram_mb = self.ram * 1024
        docker_image = resolve_docker_image(os_version)

        try:
            await docker_create_container(
                container_name=container_name,
                docker_image=docker_image,
                ram_mb=ram_mb,
                cpu=self.cpu,
                disk_gb=self.disk,
                node_id=self.node_id)

            await apply_internal_permissions(container_name, self.node_id)
            root_password = await setup_ssh_access(container_name, self.node_id)
            pinggy_address = await establish_pinggy_tunnel(container_name, self.node_id)

            config_str = f"{self.ram}GB RAM / {self.cpu} CPU / {self.disk}GB Disk"
            vps_info = {
                "container_name": container_name,
                "node_id": self.node_id,
                "ram": f"{self.ram}GB",
                "cpu": str(self.cpu),
                "storage": f"{self.disk}GB",
                "config": config_str,
                "os_version": os_version,
                "status": "running",
                "suspended": False,
                "whitelisted": False,
                "suspension_history": [],
                "created_at": datetime.now().isoformat(),
                "shared_with": [],
                "root_password": root_password,
                "pinggy_address": pinggy_address,
                "id": None,
            }
            vps_data[user_id].append(vps_info)
            save_vps_data()

            if self.ctx.guild:
                role = await get_or_create_vps_role(self.ctx.guild)
                if role:
                    try:
                        await self.user.add_roles(role, reason="VPS ownership granted")
                    except discord.Forbidden:
                        logger.warning(f"Failed to assign role to {self.user.name}")

            success_embed = create_success_embed("VPS Created Successfully")
            add_field(success_embed, "Owner", self.user.mention, True)
            add_field(success_embed, "VPS ID", f"#{vps_count}", True)
            add_field(success_embed, "Container", f"`{container_name}`", True)
            add_field(success_embed, "Node", get_node(self.node_id)['name'], True)
            add_field(success_embed, "Resources",
                      f"**RAM:** {self.ram}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.disk}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Features",
                      "Privileged container, cgroupns host, unprivileged ports from 0 (Docker-in-Docker ready)", False)
            add_field(success_embed, "Disk Note",
                      "Docker disk limits require XFS+prjquota on /var/lib/docker. If not enabled, the value shown is advisory.", False)
            await interaction.followup.send(embed=success_embed)

            dm_embed = create_success_embed("VPS Created!", f"Your VPS has been successfully deployed!")
            add_field(dm_embed, "VPS Details",
                      f"**VPS ID:** #{vps_count}\n**Container Name:** `{container_name}`\n"
                      f"**Configuration:** {config_str}\n**Status:** Running\n"
                      f"**OS:** {os_version}\n**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", False)
            if pinggy_address:
                th, tp = pinggy_address.split(":")
                add_field(dm_embed, "🔑 SSH Login",
                          f"**Host:** `{th}`\n**Port:** `{tp}`\n**User:** `root`\n**Password:** `{root_password}`\n\n"
                          f"```ssh root@{th} -p {tp}```", False)
            else:
                add_field(dm_embed, "🔑 Root Password",
                          f"`{root_password}`\n⚠️ SSH tunnel setup failed — use `{PREFIX}manage` → 🔌 Reconnect Tunnel", False)
            add_field(dm_embed, "Management",
                      f"• Use `{PREFIX}manage` to start/stop/reinstall\n• Use `{PREFIX}manage` → SSH for terminal\n• Contact admin for upgrades", False)
            add_field(dm_embed, "Important",
                      "• Full root access via SSH\n• Change your root password after first login\n• Back up your data regularly", False)
            try:
                await self.user.send(embed=dm_embed)
            except discord.Forbidden:
                await self.ctx.send(embed=create_info_embed("Notification Failed",
                    f"Couldn't DM {self.user.mention}. Please enable DMs."))
        except Exception as e:
            logger.exception("VPS creation failed")
            await interaction.followup.send(embed=create_error_embed("Creation Failed", f"Error: {e}"))

@bot.command(name='create')
@is_admin()
async def create_vps(ctx, ram: int, cpu: int, disk: int, user: discord.Member):
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=create_error_embed("Invalid Specs", "All must be positive integers."))
        return
    embed = create_info_embed("VPS Creation",
        f"Creating VPS for {user.mention} with {ram}GB RAM, {cpu} CPU cores, {disk}GB Disk.\nSelect node below.")
    view = NodeSelectView(ram, cpu, disk, user, ctx)
    await ctx.send(embed=embed, view=view)

# ---------------------------------------------------------------------------
# Reinstall OS
# ---------------------------------------------------------------------------
class ReinstallOSSelectView(discord.ui.View):
    def __init__(self, parent_view, container_name, owner_id, actual_idx,
                 ram_gb, cpu, storage_gb, node_id):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.container_name = container_name
        self.owner_id = owner_id
        self.actual_idx = actual_idx
        self.ram_gb, self.cpu, self.storage_gb = ram_gb, cpu, storage_gb
        self.node_id = node_id
        self.select = discord.ui.Select(
            placeholder="Select an OS for the reinstall",
            options=[discord.SelectOption(label=o["label"], value=o["value"]) for o in OS_OPTIONS])
        self.select.callback = self.select_os
        self.add_item(self.select)

    async def select_os(self, interaction: discord.Interaction):
        os_version = self.select.values[0]
        self.select.disabled = True
        await interaction.response.edit_message(
            embed=create_info_embed("Reinstalling VPS", f"Deploying {os_version} for `{self.container_name}`..."),
            view=self)
        docker_image = resolve_docker_image(os_version)
        try:
            # Remove old container
            try:
                await execute_docker(self.container_name, f"rm -f {self.container_name}", node_id=self.node_id)
            except Exception:
                pass

            await docker_create_container(
                container_name=self.container_name,
                docker_image=docker_image,
                ram_mb=self.ram_gb * 1024,
                cpu=self.cpu,
                disk_gb=self.storage_gb,
                node_id=self.node_id)

            await apply_internal_permissions(self.container_name, self.node_id)
            root_password = await setup_ssh_access(self.container_name, self.node_id)
            pinggy_address = await establish_pinggy_tunnel(self.container_name, self.node_id)

            target_vps = vps_data[self.owner_id][self.actual_idx]
            target_vps["os_version"] = os_version
            target_vps["status"] = "running"
            target_vps["suspended"] = False
            target_vps["created_at"] = datetime.now().isoformat()
            target_vps["root_password"] = root_password
            target_vps["pinggy_address"] = pinggy_address
            config_str = f"{self.ram_gb}GB RAM / {self.cpu} CPU / {self.storage_gb}GB Disk"
            target_vps["config"] = config_str
            save_vps_data()

            success_embed = create_success_embed("Reinstall Complete",
                f"VPS `{self.container_name}` has been successfully reinstalled!")
            add_field(success_embed, "Resources",
                      f"**RAM:** {self.ram_gb}GB\n**CPU:** {self.cpu} Cores\n**Storage:** {self.storage_gb}GB", False)
            add_field(success_embed, "OS", os_version, True)
            add_field(success_embed, "Disk Note",
                      "Run `resize2fs /` inside the VPS if using XFS prjquota and it didn't auto-grow.", False)
            await interaction.followup.send(embed=success_embed, ephemeral=True)

            try:
                owner_user = await bot.fetch_user(int(self.owner_id))
                dm = create_success_embed("VPS Reinstalled!",
                    f"Your VPS `{self.container_name}` was reinstalled. New root password below.")
                if pinggy_address:
                    th, tp = pinggy_address.split(":")
                    add_field(dm, "🔑 SSH Login",
                              f"**Host:** `{th}`\n**Port:** `{tp}`\n**User:** `root`\n**Password:** `{root_password}`\n\n"
                              f"```ssh root@{th} -p {tp}```", False)
                else:
                    add_field(dm, "🔑 Root Password",
                              f"`{root_password}`\n⚠️ SSH tunnel failed — use `{PREFIX}manage` → Reconnect.", False)
                await owner_user.send(embed=dm)
            except Exception:
                pass
            self.stop()
        except Exception as e:
            await interaction.followup.send(embed=create_error_embed("Reinstall Failed", f"Error: {e}"), ephemeral=True)
            self.stop()

# ---------------------------------------------------------------------------
# Manage view
# ---------------------------------------------------------------------------
class ManageView(discord.ui.View):
    def __init__(self, user_id, vps_list, is_shared=False, owner_id=None,
                 is_admin=False, actual_index=None):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.vps_list = vps_list[:]
        self.selected_index = None
        self.is_shared = is_shared
        self.owner_id = owner_id or user_id
        self.is_admin = is_admin
        self.actual_index = actual_index
        self.indices = list(range(len(vps_list)))
        if self.is_shared and self.actual_index is None:
            raise ValueError("actual_index required for shared views")
        if len(vps_list) > 1:
            options = [discord.SelectOption(
                label=f"VPS {i+1} ({v.get('config', 'Custom')})",
                description=f"Status: {v.get('status', 'unknown')}",
                value=str(i)) for i, v in enumerate(vps_list)]
            self.select = discord.ui.Select(placeholder="Select a VPS", options=options)
            self.select.callback = self.select_vps
            self.add_item(self.select)
            self.initial_embed = create_embed("VPS Management",
                "Select a VPS from the dropdown below.", 0x1a1a1a)
            add_field(self.initial_embed, "Available VPS",
                      "\n".join([f"**VPS {i+1}:** `{v['container_name']}` - Status: `{v.get('status','unknown').upper()}`"
                                 for i, v in enumerate(vps_list)]), False)
        else:
            self.selected_index = 0
            self.initial_embed = None
            self.add_action_buttons()

    async def get_initial_embed(self):
        if self.initial_embed is not None:
            return self.initial_embed
        self.initial_embed = await self.create_vps_embed(self.selected_index)
        return self.initial_embed

    async def create_vps_embed(self, index):
        vps = self.vps_list[index]
        node = get_node(vps['node_id'])
        node_name = node['name'] if node else "Unknown"
        status = vps.get('status', 'unknown')
        suspended = vps.get('suspended', False)
        whitelisted = vps.get('whitelisted', False)
        color = 0x00ff88 if status == 'running' and not suspended else 0xffaa00 if suspended else 0xff3366
        container_name = vps['container_name']
        stats = await get_container_stats(container_name, vps.get('node_id'))
        status_text = stats['status'].upper()
        if suspended: status_text += " (SUSPENDED)"
        if whitelisted: status_text += " (WHITELISTED)"
        owner_text = ""
        if self.is_admin and self.owner_id != self.user_id:
            try:
                ou = await bot.fetch_user(int(self.owner_id))
                owner_text = f"\n**Owner:** {ou.mention}"
            except Exception:
                owner_text = f"\n**Owner ID:** {self.owner_id}"
        embed = create_embed(f"VPS Management - VPS {index+1}",
            f"Managing `{container_name}` on node {node_name}{owner_text}", color)
        resource_info = (f"**Configuration:** {vps.get('config','Custom')}\n"
                         f"**Status:** `{status_text}`\n"
                         f"**RAM:** {vps['ram']}\n"
                         f"**CPU:** {vps['cpu']} Cores\n"
                         f"**Storage:** {vps['storage']}\n"
                         f"**OS:** {vps.get('os_version','ubuntu:22.04')}\n"
                         f"**Uptime:** {stats['uptime']}")
        add_field(embed, "📊 Allocated Resources", resource_info, False)
        if suspended:
            add_field(embed, "⚠️ Suspended", "Contact an admin to unsuspend.", False)
        if whitelisted:
            add_field(embed, "✅ Whitelisted", "Exempt from auto-suspension.", False)
        live = (f"**CPU Usage:** {stats['cpu']:.1f}%\n"
                f"**Memory:** {stats['ram']['used']:.0f}/{stats['ram']['total']:.0f} MB ({stats['ram']['pct']:.1f}%)\n"
                f"**Disk:** {stats['disk']}")
        add_field(embed, "📈 Live Usage", live, False)
        pinggy = vps.get('pinggy_address')
        if pinggy:
            th, tp = pinggy.split(":")
            add_field(embed, "🔌 SSH Tunnel",
                      f"**Host:** `{th}`\n**Port:** `{tp}`\n```ssh root@{th} -p {tp}```", False)
        else:
            add_field(embed, "🔌 SSH Tunnel", "No active tunnel. Use 🔌 Reconnect Tunnel.", False)
        add_field(embed, "🎮 Controls", "Use the buttons below", False)
        return embed    def add_action_buttons(self):
        if not self.is_shared and not self.is_admin:
            b = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            b.callback = lambda i: self.action_callback(i, 'reinstall')
            self.add_item(b)
        for label, style, action in [
            ("▶ Start", discord.ButtonStyle.success, 'start'),
            ("⏸ Stop", discord.ButtonStyle.secondary, 'stop'),
            ("🔑 SSH", discord.ButtonStyle.primary, 'tmate'),
            ("📊 Stats", discord.ButtonStyle.secondary, 'stats'),
        ]:
            b = discord.ui.Button(label=label, style=style)
            b.callback = lambda i, a=action: self.action_callback(i, a)
            self.add_item(b)
        if not self.is_shared:
            for label, style, action in [
                ("➕ Add Port", discord.ButtonStyle.primary, 'addport'),
                ("🔐 Change Password", discord.ButtonStyle.danger, 'changepass'),
                ("🔌 Reconnect Tunnel", discord.ButtonStyle.primary, 'reconnect_tunnel'),
            ]:
                b = discord.ui.Button(label=label, style=style)
                b.callback = lambda i, a=action: self.action_callback(i, a)
                self.add_item(b)

    async def select_vps(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS."), ephemeral=True)
            return
        self.selected_index = int(self.select.values[0])
        await interaction.response.defer()
        new_embed = await self.create_vps_embed(self.selected_index)
        self.clear_items()
        self.add_action_buttons()
        await interaction.edit_original_response(embed=new_embed, view=self)

    async def action_callback(self, interaction: discord.Interaction, action: str):
        if str(interaction.user.id) != self.user_id and not self.is_admin:
            await interaction.response.send_message(embed=create_error_embed("Access Denied", "Not your VPS."), ephemeral=True)
            return
        if self.selected_index is None:
            await interaction.response.send_message(embed=create_error_embed("No VPS Selected", "Select a VPS first."), ephemeral=True)
            return
        actual_idx = self.actual_index if self.is_shared else self.indices[self.selected_index]
        target_vps = vps_data[self.owner_id][actual_idx]
        suspended = target_vps.get('suspended', False)
        if suspended and not self.is_admin and action != 'stats':
            await interaction.response.send_message(embed=create_error_embed("Suspended", "VPS is suspended."), ephemeral=True)
            return
        container_name = target_vps["container_name"]
        node_id = target_vps['node_id']

        if action == 'stats':
            stats = await get_container_stats(container_name, node_id)
            e = create_info_embed("📈 Live Statistics", f"Real-time stats for `{container_name}`")
            add_field(e, "Status", f"`{stats['status'].upper()}`", True)
            add_field(e, "CPU", f"{stats['cpu']:.1f}%", True)
            add_field(e, "Memory", f"{stats['ram']['used']:.0f}/{stats['ram']['total']:.0f} MB ({stats['ram']['pct']:.1f}%)", True)
            add_field(e, "Disk", stats['disk'], True)
            add_field(e, "Uptime", stats['uptime'], True)
            await interaction.response.send_message(embed=e, ephemeral=True)
            return

        if action == 'reinstall':
            if self.is_shared or self.is_admin:
                await interaction.response.send_message(embed=create_error_embed("Access Denied", "Only the owner can reinstall."), ephemeral=True)
                return
            if suspended:
                await interaction.response.send_message(embed=create_error_embed("Suspended", "Unsuspend first."), ephemeral=True)
                return
            ram_gb = int(target_vps['ram'].replace('GB', ''))
            cpu = int(target_vps['cpu'])
            storage_gb = int(target_vps['storage'].replace('GB', ''))
            confirm = create_warning_embed("Reinstall Warning",
                f"⚠️ This will erase all data on `{container_name}` and reinstall a fresh OS.\n\nContinue?")

            class ConfirmView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=60)
                @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
                async def confirm(self, inter: discord.Interaction, item):
                    await inter.response.defer(ephemeral=True)
                    try:
                        await inter.followup.send(embed=create_info_embed("Deleting Container", f"Removing `{container_name}`..."), ephemeral=True)
                        await execute_docker(container_name, f"rm -f {container_name}", node_id=node_id)
                        os_view = ReinstallOSSelectView(self, container_name, self.owner_id, actual_idx,
                                                        ram_gb, cpu, storage_gb, node_id)
                        await inter.followup.send(embed=create_info_embed("Select OS", "Choose new OS."), view=os_view, ephemeral=True)
                    except Exception as e:
                        await inter.followup.send(embed=create_error_embed("Delete Failed", f"Error: {e}"), ephemeral=True)
                @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
                async def cancel(self, inter: discord.Interaction, item):
                    new_embed = await self.parent_view.create_vps_embed(self.parent_view.selected_index)
                    await inter.response.edit_message(embed=new_embed, view=self.parent_view)
            await interaction.response.send_message(embed=confirm, view=ConfirmView(), ephemeral=True)
            return

        if action == 'addport':
            class AddPortModal(discord.ui.Modal, title="Add Port Forward"):
                vps_port_input = discord.ui.TextInput(label="VPS Port (1-65535)", placeholder="e.g. 8080", max_length=5)
                async def on_submit(self, modal_inter: discord.Interaction):
                    await modal_inter.response.defer(ephemeral=True)
                    try:
                        vps_port = int(self.vps_port_input.value)
                        if not 1 <= vps_port <= 65535:
                            raise ValueError
                    except ValueError:
                        await modal_inter.followup.send(embed=create_error_embed("Invalid Port", "Port must be 1-65535."), ephemeral=True)
                        return
                    owner_id = self.owner_id
                    allocated = get_user_allocation(owner_id)
                    used = get_user_used_ports(owner_id)
                    if used >= allocated:
                        await modal_inter.followup.send(embed=create_error_embed("Quota Exceeded",
                            f"Allocated: {allocated}, Used: {used}."), ephemeral=True)
                        return
                    host_port = await create_port_forward(owner_id, container_name, vps_port, node_id)
                    if host_port:
                        public_ip = get_public_ip()
                        e = create_success_embed("Port Forward Created",
                            f"VPS port `{vps_port}` → host port `{host_port}` (TCP).")
                        add_field(e, "Access", f"`{public_ip}:{host_port}` → VPS:{vps_port}", False)
                        await modal_inter.followup.send(embed=e, ephemeral=True)
                        try:
                            dm_user = await bot.fetch_user(int(owner_id))
                            dm = create_success_embed("🔌 Port Forward Created",
                                f"New port forward for `{container_name}`.")
                            add_field(dm, "Details", f"**VPS Port:** {vps_port}\n**External Port:** {host_port}\n**Access:** `{public_ip}:{host_port}` (TCP)", False)
                            await dm_user.send(embed=dm)
                        except discord.Forbidden:
                            pass
                    else:
                        await modal_inter.followup.send(embed=create_error_embed("Failed", "Could not assign host port."), ephemeral=True)
            await interaction.response.send_modal(AddPortModal())
            return

        await interaction.response.defer(ephemeral=True)
        if suspended:
            target_vps['suspended'] = False
            save_vps_data()

        if action == 'start':
            try:
                await safe_start_container(container_name, node_id)
                target_vps["status"] = "running"
                save_vps_data()
                await apply_internal_permissions(container_name, node_id)
                readded = await recreate_port_forwards(container_name)
                await interaction.followup.send(embed=create_success_embed("VPS Started",
                    f"`{container_name}` is running. Re-added {readded} port forwards."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == 'stop':
            try:
                await execute_docker(container_name, f"stop {container_name}", timeout=120, node_id=node_id)
                target_vps["status"] = "stopped"
                save_vps_data()
                await interaction.followup.send(embed=create_success_embed("VPS Stopped", f"`{container_name}` stopped."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == 'tmate':
            if suspended:
                await interaction.followup.send(embed=create_error_embed("Access Denied", "VPS suspended."), ephemeral=True)
                return
            await interaction.followup.send(embed=create_info_embed("SSH Access", "Generating SSH connection..."), ephemeral=True)
            try:
                try:
                    await execute_docker(container_name, f"exec {container_name} which tmate", node_id=node_id)
                except Exception:
                    await interaction.followup.send(embed=create_info_embed("Installing", "Installing tmate..."), ephemeral=True)
                    await execute_docker(container_name, f"exec {container_name} apt-get update -y", node_id=node_id, timeout=180)
                    await execute_docker(container_name, f"exec {container_name} apt-get install tmate -y", node_id=node_id, timeout=180)
                session_name = f"{BOT_NAME.lower()}-session-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                await execute_docker(container_name, f"exec {container_name} tmate -S /tmp/{session_name}.sock new-session -d", node_id=node_id)
                await asyncio.sleep(3)
                ssh_output = await execute_docker(container_name,
                    f"exec {container_name} tmate -S /tmp/{session_name}.sock display -p '#{{tmate_ssh}}'",
                    node_id=node_id)
                ssh_url = ssh_output.strip() if isinstance(ssh_output, str) else ""
                if ssh_url:
                    try:
                        e = create_embed("🔑 SSH Access", f"SSH connection for `{container_name}`:", 0x00ff88)
                        add_field(e, "Command", f"```{ssh_url}```", False)
                        add_field(e, "⚠️ Security", "Temporary. Do not share.", False)
                        add_field(e, "📝 Session", f"Session ID: {session_name}", False)
                        await interaction.user.send(embed=e)
                        await interaction.followup.send(embed=create_success_embed("SSH Sent", f"Check DMs. Session: {session_name}"), ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=create_error_embed("DM Failed", "Enable DMs."), ephemeral=True)
                else:
                    await interaction.followup.send(embed=create_error_embed("SSH Failed", "No URL generated."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("SSH Error", str(e)), ephemeral=True)

        elif action == 'changepass':
            try:
                new_password = generate_password()
                await execute_docker(container_name,
                    f'exec {container_name} bash -c "echo \'root:{new_password}\' | chpasswd"',
                    node_id=node_id, timeout=60)
                target_vps['root_password'] = new_password
                save_vps_data()
                try:
                    dm_user = await bot.fetch_user(int(self.owner_id))
                    dm = create_success_embed("🔐 Root Password Changed",
                        f"Your root password for `{container_name}` has changed.")
                    add_field(dm, "New Password", f"`{new_password}`", False)
                    await dm_user.send(embed=dm)
                    await interaction.followup.send(embed=create_success_embed("Password Changed", "Check DMs."), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=create_success_embed("Password Changed",
                        f"New password: `{new_password}`\n(Enable DMs to receive future passwords privately)"), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Password Change Failed", str(e)), ephemeral=True)

        elif action == 'reconnect_tunnel':
            try:
                await interaction.followup.send(embed=create_info_embed("Reconnecting",
                    "Setting up fresh SSH tunnel (up to 30s)..."), ephemeral=True)
                new_addr = await establish_pinggy_tunnel(container_name, node_id)
                target_vps['pinggy_address'] = new_addr
                save_vps_data()
                if new_addr:
                    th, tp = new_addr.split(":")
                    try:
                        dm_user = await bot.fetch_user(int(self.owner_id))
                        dm = create_success_embed("🔌 SSH Tunnel Reconnected",
                            f"Fresh tunnel for `{container_name}` ready.")
                        add_field(dm, "🔑 SSH Login",
                                  f"**Host:** `{th}`\n**Port:** `{tp}`\n**User:** `root`\n\n```ssh root@{th} -p {tp}```", False)
                        await dm_user.send(embed=dm)
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected", "Check DMs."), ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(embed=create_success_embed("Tunnel Reconnected",
                            f"**Host:** `{th}`\n**Port:** `{tp}`"), ephemeral=True)
                else:
                    await interaction.followup.send(embed=create_error_embed("Reconnect Failed", "Could not establish tunnel."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=create_error_embed("Reconnect Failed", str(e)), ephemeral=True)

        new_embed = await self.create_vps_embed(self.selected_index)
        await interaction.edit_original_response(embed=new_embed, view=self)

# ---------------------------------------------------------------------------
# manage / shared / userinfo etc.
# ---------------------------------------------------------------------------
@bot.command(name='manage')
async def manage_vps(ctx, user: discord.Member = None):
    if user:
        if str(ctx.author.id) not in main_admin_ids and str(ctx.author.id) not in admin_data.get("admins", []):
            await ctx.send(embed=create_error_embed("Access Denied", "Admin only."))
            return
        user_id = str(user.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            await ctx.send(embed=create_error_embed("No VPS", f"{user.mention} has no VPS."))
            return
        view = ManageView(str(ctx.author.id), vps_list, is_admin=True, owner_id=user_id)
        await ctx.send(embed=create_info_embed(f"Managing {user.name}'s VPS",
                                               f"Managing VPS for {user.mention}"), view=view)
    else:
        user_id = str(ctx.author.id)
        vps_list = vps_data.get(user_id, [])
        if not vps_list:
            e = create_error_embed("No VPS Found", f"You don't have any {BOT_NAME} VPS.")
            add_field(e, "Quick Actions", f"• `{PREFIX}manage`\n• Contact admin", False)
            await ctx.send(embed=e)
            return
        view = ManageView(user_id, vps_list)
        embed = await view.get_initial_embed()
        await ctx.send(embed=embed, view=view)

@bot.command(name='manage-shared')
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    owner_id = str(owner.id); user_id = str(ctx.author.id)
    if owner_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[owner_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Bad VPS number or owner."))
        return
    vps = vps_data[owner_id][vps_number - 1]
    if user_id not in vps.get("shared_with", []):
        await ctx.send(embed=create_error_embed("Access Denied", "No access."))
        return
    view = ManageView(user_id, [vps], is_shared=True, owner_id=owner_id, actual_index=vps_number - 1)
    embed = await view.get_initial_embed()
    await ctx.send(embed=embed, view=view)

@bot.command(name='share-user')
async def share_user(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id); shared_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Bad VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    vps.setdefault("shared_with", [])
    if shared_id in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Already Shared", f"{shared_user.mention} already has access."))
        return
    vps["shared_with"].append(shared_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("VPS Shared", f"VPS #{vps_number} shared with {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Granted",
            f"You have access to VPS #{vps_number} from {ctx.author.mention}. Use `{PREFIX}manage-shared {ctx.author.mention} {vps_number}`", 0x00ff88))
    except discord.Forbidden:
        await ctx.send(embed=create_info_embed("Notification Failed", f"Could not DM {shared_user.mention}"))

@bot.command(name='share-ruser')
async def revoke_share(ctx, shared_user: discord.Member, vps_number: int):
    user_id = str(ctx.author.id); shared_id = str(shared_user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Bad VPS number."))
        return
    vps = vps_data[user_id][vps_number - 1]
    vps.setdefault("shared_with", [])
    if shared_id not in vps["shared_with"]:
        await ctx.send(embed=create_error_embed("Not Shared", f"{shared_user.mention} doesn't have access."))
        return
    vps["shared_with"].remove(shared_id)
    save_vps_data()
    await ctx.send(embed=create_success_embed("Access Revoked", f"Revoked from {shared_user.mention}!"))
    try:
        await shared_user.send(embed=create_embed("VPS Access Revoked",
            f"Your access to VPS #{vps_number} has been revoked.", 0xff3366))
    except discord.Forbidden:
        pass

# ---------------------------------------------------------------------------
# Ports commands
# ---------------------------------------------------------------------------
@bot.command(name='ports-add-user')
@is_admin()
async def ports_add_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid", "Positive integer."))
        return
    uid = str(user.id)
    allocate_ports(uid, amount)
    e = create_success_embed("Ports Allocated", f"Allocated {amount} slots to {user.mention}.")
    add_field(e, "Quota", f"Total: {get_user_allocation(uid)} slots", False)
    await ctx.send(embed=e)

@bot.command(name='ports-remove-user')
@is_admin()
async def ports_remove_user(ctx, amount: int, user: discord.Member):
    if amount <= 0:
        await ctx.send(embed=create_error_embed("Invalid", "Positive integer."))
        return
    uid = str(user.id)
    current = get_user_allocation(uid)
    amount = min(amount, current)
    deallocate_ports(uid, amount)
    await ctx.send(embed=create_success_embed("Ports Deallocated",
        f"Removed {amount} slots. Remaining: {get_user_allocation(uid)}"))

@bot.command(name='ports-revoke')
@is_admin()
async def ports_revoke(ctx, forward_id: int):
    success, user_id = await remove_port_forward(forward_id, is_admin=True)
    if success and user_id:
        try:
            u = await bot.fetch_user(int(user_id))
            await u.send(embed=create_warning_embed("Port Forward Revoked",
                f"One of your forwards (ID: {forward_id}) was revoked."))
        except Exception:
            pass
        await ctx.send(embed=create_success_embed("Revoked", f"Forward ID {forward_id} revoked."))
    else:
        await ctx.send(embed=create_error_embed("Failed", "Not found or removal failed."))

@bot.command(name='ports')
async def ports_command(ctx, subcmd: str = None, *args):
    user_id = str(ctx.author.id)
    allocated = get_user_allocation(user_id)
    used = get_user_used_ports(user_id)
    available = allocated - used
    if subcmd is None:
        e = create_info_embed("Port Forwarding Help",
            f"**Quota:** Allocated: {allocated}, Used: {used}, Available: {available}")
        add_field(e, "Commands", f"{PREFIX}ports add <vps_num> <port>\n{PREFIX}ports list\n{PREFIX}ports remove <id>", False)
        await ctx.send(embed=e); return
    if subcmd == 'add':
        if len(args) < 2:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}ports add <vps_number> <vps_port>")); return
        try:
            vps_num, vps_port = int(args[0]), int(args[1])
            if not 1 <= vps_port <= 65535: raise ValueError
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid Input", "Must be integers, port 1-65535.")); return
        vps_list = vps_data.get(user_id, [])
        if vps_num < 1 or vps_num > len(vps_list):
            await ctx.send(embed=create_error_embed("Invalid VPS", f"1-{len(vps_list)}")); return
        vps = vps_list[vps_num - 1]
        if used >= allocated:
            await ctx.send(embed=create_error_embed("Quota Exceeded", f"{used}/{allocated}.")); return
        host_port = await create_port_forward(user_id, vps['container_name'], vps_port, vps['node_id'])
        if host_port:
            e = create_success_embed("Port Forward Created",
                f"VPS #{vps_num} port {vps_port} (TCP) → host port {host_port}.")
            add_field(e, "Access", f"`{get_public_ip()}:{host_port}` → VPS:{vps_port} (TCP)", False)
            add_field(e, "Quota Update", f"Used: {used+1}/{allocated}", False)
            await ctx.send(embed=e)
        else:
            await ctx.send(embed=create_error_embed("Failed", "Could not assign host port."))
    elif subcmd == 'list':
        forwards = get_user_forwards(user_id)
        e = create_info_embed("Your Port Forwards", f"Allocated: {allocated}, Used: {used}, Available: {available}")
        if not forwards:
            add_field(e, "Forwards", "No active port forwards.", False)
        else:
            text = []
            for f in forwards:
                vps_num = next((i+1 for i, v in enumerate(vps_data.get(user_id, []))
                                if v['container_name'] == f['vps_container']), 'Unknown')
                created = datetime.fromisoformat(f['created_at']).strftime('%Y-%m-%d %H:%M')
                text.append(f"**ID {f['id']}** - VPS #{vps_num}: {f['vps_port']} (TCP) → {f['host_port']} (Created: {created})")
            add_field(e, "Active Forwards", "\n".join(text[:10]), False)
            if len(forwards) > 10:
                add_field(e, "Note", f"Showing 10 of {len(forwards)}.")
        await ctx.send(embed=e)
    elif subcmd == 'remove':
        if len(args) < 1:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}ports remove <forward_id>")); return
        try: fid = int(args[0])
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid ID", "Integer required.")); return
        success, _ = await remove_port_forward(fid)
        if success:
            await ctx.send(embed=create_success_embed("Removed", f"Port forward {fid} removed."))
        else:
            await ctx.send(embed=create_error_embed("Not Found", "Forward ID not found."))
    else:
        await ctx.send(embed=create_error_embed("Invalid Subcommand", "add/list/remove"))

# ---------------------------------------------------------------------------
# Admin VPS commands
# ---------------------------------------------------------------------------
@bot.command(name='delete-vps')
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason"):
    user_id = str(user.id)
    if user_id not in vps_data or vps_number < 1 or vps_number > len(vps_data[user_id]):
        await ctx.send(embed=create_error_embed("Invalid VPS", "Bad VPS number.")); return
    vps = vps_data[user_id][vps_number - 1]
    container_name = vps["container_name"]
    node_id = vps.get("node_id", 1)
    await ctx.send(embed=create_info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))
    node_result = "Not checked"
    try:
        # Remove any port-forward sidecars
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT proxy_name FROM port_forwards WHERE vps_container = ?', (container_name,))
        for row in cur.fetchall():
            if row['proxy_name']:
                try:
                    await execute_docker(container_name, f"rm -f {row['proxy_name']}", node_id=node_id)
                except Exception:
                    pass
        conn.close()

        await execute_docker(container_name, f"rm -f {container_name}", node_id=node_id)
        node_result = "Container deleted."
    except Exception as e:
        err = str(e).lower()
        if "no such" in err or "not found" in err:
            node_result = "Container not found (force DB cleanup)."
        else:
            node_result = f"Delete failed: {e}"

    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM vps WHERE container_name = ?", (container_name,))
    cur.execute("DELETE FROM port_forwards WHERE vps_container = ?", (container_name,))
    conn.commit(); conn.close()

    del vps_data[user_id][vps_number - 1]
    if not vps_data[user_id]:
        del vps_data[user_id]
        if ctx.guild:
            role = await get_or_create_vps_role(ctx.guild)
            if role and role in user.roles:
                try:
                    await user.remove_roles(role, reason="No VPS ownership")
                except discord.Forbidden:
                    logger.warning(f"Failed to remove role from {user.name}")
    save_vps_data()

    e = create_success_embed("VPS Deleted Successfully")
    add_field(e, "Owner", user.mention, True)
    add_field(e, "VPS Number", f"#{vps_number}", True)
    add_field(e, "Container", container_name, False)
    add_field(e, "Node Result", node_result, False)
    add_field(e, "Reason", reason, False)
    await ctx.send(embed=e)

@bot.command(name='add-resources')
@is_admin()
async def add_resources(ctx, vps_id: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Specify at least one of ram/cpu/disk."))
        return
    found = None; user_id = None; vps_index = None
    for uid, vps_list in vps_data.items():
        for i, vps in enumerate(vps_list):
            if vps['container_name'] == vps_id:
                found = vps; user_id = uid; vps_index = i; break
        if found: break
    if not found:
        await ctx.send(embed=create_error_embed("VPS Not Found", f"No VPS: `{vps_id}`")); return
    node_id = found['node_id']
    was_running = found.get('status') == 'running' and not found.get('suspended', False)
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping", f"Stopping `{vps_id}` to apply changes..."))
        try:
            await execute_docker(vps_id, f"stop {vps_id}", node_id=node_id)
            found['status'] = 'stopped'; save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", str(e))); return
    changes = []
    try:
        cur_ram = int(found['ram'].replace('GB',''))
        cur_cpu = int(found['cpu'])
        cur_disk = int(found['storage'].replace('GB',''))
        new_ram, new_cpu, new_disk = cur_ram, cur_cpu, cur_disk
        if ram is not None and ram > 0:
            new_ram += ram
            await execute_docker(vps_id, f"update --memory {new_ram*1024}m {vps_id}", node_id=node_id)
            changes.append(f"RAM: +{ram}GB (total {new_ram}GB)")
        if cpu is not None and cpu > 0:
            new_cpu += cpu
            await execute_docker(vps_id, f"update --cpus {new_cpu} {vps_id}", node_id=node_id)
            changes.append(f"CPU: +{cpu} (total {new_cpu})")
        if disk is not None and disk > 0:
            new_disk += disk
            # Docker disk growth requires storage-opt recreate; note only.
            changes.append(f"Disk: +{disk}GB (total {new_disk}GB) — recreate to apply")
        found['ram'] = f"{new_ram}GB"; found['cpu'] = str(new_cpu); found['storage'] = f"{new_disk}GB"
        found['config'] = f"{new_ram}GB RAM / {new_cpu} CPU / {new_disk}GB Disk"
        save_vps_data()
        if was_running:
            await safe_start_container(vps_id, node_id)
            found['status'] = 'running'; save_vps_data()
            await apply_internal_permissions(vps_id, node_id)
            await recreate_port_forwards(vps_id)
        e = create_success_embed("Resources Added", f"Applied to `{vps_id}`")
        add_field(e, "Changes", "\n".join(changes), False)
        if disk is not None:
            add_field(e, "Disk Note", "Docker disk growth requires recreate with a new size. RAM/CPU applied live.", False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Failed", f"Error: {e}"))

@bot.command(name='vps-list')
@is_admin()
async def vps_list(ctx, node_id: int = 1):
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"ID {node_id}.")); return
    status = await get_node_status(node_id)
    is_online = status.startswith("🟢")
    stats = await get_host_stats(node_id)
    cpu = stats.get('cpu', 0.0); ram = stats.get('ram', 0.0); disk = stats.get('disk', 'Unknown')
    if is_online:
        cpu_bar = '█'*int(cpu/5) + '░'*(20-int(cpu/5))
        ram_bar = '█'*int(ram/5) + '░'*(20-int(ram/5))
        resources_text = f"**CPU** {cpu:.0f}% {cpu_bar}\n**RAM** {ram:.0f}% {ram_bar}\n**Disk** {disk}"
    else:
        resources_text = "⚠️ Resources unavailable (Offline)"
    current_vps = get_current_vps_count(node_id)
    total_capacity = node['total_vps']
    capacity_pct = (current_vps / total_capacity * 100) if total_capacity else 0
    capacity_text = f"{current_vps}/{total_capacity} ({capacity_pct:.0f}%)"
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM vps WHERE node_id = ?', (node_id,))
    rows = cur.fetchall(); conn.close()
    total_vps = len(rows)
    running = stopped = suspended = other = 0
    vps_info = []
    for i, row in enumerate(rows, 1):
        vps = dict(row); uid = vps['user_id']
        try:
            u = await bot.fetch_user(int(uid)); username = u.name
        except Exception:
            username = f"Unknown ({uid})"
        st = vps.get('status', 'unknown'); sus = vps.get('suspended', False)
        if sus: suspended += 1
        elif st == 'running': running += 1
        elif st == 'stopped': stopped += 1
        else: other += 1
        emoji = "🟢" if st == 'running' and not sus else "🟡" if sus else "🔴"
        label = st.upper()
        if sus: label += " (SUSPENDED)"
        if vps.get('whitelisted'): label += " (WHITELISTED)"
        cfg = vps.get('config', 'Custom')
        vps_info.append(f"{emoji} **{i}.** {username} • `{vps['container_name']}`\n _{label} | {cfg}_")
    color = 0x10b981 if is_online else 0xef4444
    embed = create_embed(f"🖥️ VPS Dashboard - {node['name']}",
        f"**ID:** `{node_id}` | **Region:** {node['location']}\n*Updated: <t:{int(datetime.now().timestamp())}:R>*",
        color)
    add_field(embed, "📡 Status", status, True)
    add_field(embed, "🗄️ Capacity", capacity_text, True)
    add_field(embed, "📊 Resources", resources_text, False)
    summary = (f"**Total:** {total_vps}\n**Running:** {running} 🟢\n"
               f"**Stopped:** {stopped} ⏸️\n**Suspended:** {suspended} 🟡")
    if other: summary += f"\n**Other:** {other} ⚠️"
    add_field(embed, "📈 Summary", summary, True)
    if vps_info:
        chunk_size = 6
        chunks = [vps_info[i:i+chunk_size] for i in range(0, len(vps_info), chunk_size)]
        add_field(embed, f"📋 Active VPS (1/{len(chunks)})", "```" + "\n".join(chunks[0]) + "```", False)
        for idx, chunk in enumerate(chunks[1:], 2):
            pe = create_embed(f"🖥️ VPS Dashboard - {node['name']} (Page {idx}/{len(chunks)})",
                              f"**ID:** `{node_id}` | **Region:** {node['location']}", color)
            add_field(pe, "📋 VPS List", "```" + "\n".join(chunk) + "```", False)
            await ctx.send(embed=pe)
    else:
        add_field(embed, "📋 VPS List", "No deployments yet. 🚀", False)
    await ctx.send(embed=embed)

@bot.command(name='list-all')
@is_admin()
async def list_all_vps(ctx):
    total_vps = 0; total_users = len(vps_data)
    running_vps = stopped_vps = suspended_vps = whitelisted_vps = 0
    vps_info = []; user_summary = []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            cnt = len(vps_list)
            r = sum(1 for v in vps_list if v.get('status') == 'running' and not v.get('suspended', False))
            s = sum(1 for v in vps_list if v.get('status') == 'stopped')
            su = sum(1 for v in vps_list if v.get('suspended', False))
            w = sum(1 for v in vps_list if v.get('whitelisted', False))
            total_vps += cnt; running_vps += r; stopped_vps += s
            suspended_vps += su; whitelisted_vps += w
            user_summary.append(f"**{user.name}** ({user.mention}) - {cnt} VPS ({r} running, {su} suspended, {w} whitelisted)")
            for i, vps in enumerate(vps_list):
                node = get_node(vps['node_id']); node_name = node['name'] if node else "Unknown"
                emoji = "🟢" if vps.get('status') == 'running' and not vps.get('suspended', False) else "🟡" if vps.get('suspended', False) else "🔴"
                label = vps.get('status', 'unknown').upper()
                if vps.get('suspended', False): label += " (SUSPENDED)"
                if vps.get('whitelisted', False): label += " (WHITELISTED)"
                vps_info.append(f"{emoji} **{user.name}** - VPS {i+1}: `{vps['container_name']}` - {vps.get('config','Custom')} - {label} (Node: {node_name})")
        except discord.NotFound:
            vps_info.append(f"❓ Unknown User ({user_id}) - {len(vps_list)} VPS")
    e = create_embed("All VPS Information", "Complete overview", 0x1a1a1a)
    add_field(e, "System Overview",
              f"**Total Users:** {total_users}\n**Total VPS:** {total_vps}\n**Running:** {running_vps}\n"
              f"**Stopped:** {stopped_vps}\n**Suspended:** {suspended_vps}\n**Whitelisted:** {whitelisted_vps}", False)
    await ctx.send(embed=e)
    if user_summary:
        e = create_embed("User Summary", "All users and VPS", 0x1a1a1a)
        text = "\n".join(user_summary)
        for i, chunk in enumerate([text[i:i+1024] for i in range(0, len(text), 1024)], 1):
            add_field(e, f"Users (Part {i})", chunk, False)
        await ctx.send(embed=e)
    if vps_info:
        text = "\n".join(vps_info)
        for i, chunk in enumerate([text[i:i+1024] for i in range(0, len(text), 1024)], 1):
            e = create_embed(f"VPS Details (Part {i})", "All VPS deployments", 0x1a1a1a)
            add_field(e, "VPS List", chunk, False)
            await ctx.send(embed=e)

# ---------------------------------------------------------------------------
# Admin info / status
# ---------------------------------------------------------------------------
@bot.command(name='status')
@is_admin()
async def system_status(ctx):
    start_time = time.time()
    nodes = get_nodes()
    total_nodes = len(nodes)
    running_nodes = stopped_nodes = local_nodes = remote_nodes = 0
    total_vps = 0; total_users = len(vps_data)
    running_vps = stopped_vps = suspended_vps = whitelisted_vps = 0
    total_admins = len(admin_data.get("admins", [])) + 1
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    total_ports_allocated = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM port_forwards")
    total_ports_used = cur.fetchone()[0] or 0
    conn.close()
    total_ram = total_cpu = total_disk = 0
    for uid, vps_list in vps_data.items():
        total_vps += len(vps_list)
        for vps in vps_list:
            if vps.get('suspended'): suspended_vps += 1
            elif vps.get('status') == 'running': running_vps += 1
            else: stopped_vps += 1
            if vps.get('whitelisted'): whitelisted_vps += 1
            try: total_ram += int(vps['ram'].replace('GB',''))
            except Exception: pass
            try: total_cpu += int(vps['cpu'])
            except Exception: pass
            try: total_disk += int(vps['storage'].replace('GB',''))
            except Exception: pass
    node_statuses = []
    for node in nodes:
        if node['is_local']: local_nodes += 1; node_type = "🖥️ Local"
        else: remote_nodes += 1; node_type = "🌐 Remote"
        if node['is_local']:
            status = "🟢 Online"; running_nodes += 1
        else:
            try:
                r = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                if r.status_code == 200: status = "🟢 Online"; running_nodes += 1
                else: status = "🔴 Offline"; stopped_nodes += 1
            except Exception:
                status = "🔴 Offline"; stopped_nodes += 1
        nvps = get_current_vps_count(node['id'])
        cap = node['total_vps']
        pct = (nvps / cap * 100) if cap else 0
        node_statuses.append(f"**{node['name']}** ({node_type})\n📍 {node['location']} • 📊 {nvps}/{cap} VPS ({pct:.0f}%)\nStatus: {status}")
    rt = (time.time() - start_time) * 1000
    e = create_embed("📊 System Status Dashboard",
        f"**{BOT_NAME}** - Complete System Overview\n*Generated in {rt:.0f}ms*", 0x1a1a1a)
    add_field(e, "🤖 Bot Status",
        f"**Latency:** {round(bot.latency*1000)}ms\n**Version:** {BOT_VERSION}\n**Developer:** {BOT_DEVELOPER}", True)
    add_field(e, "🌐 Nodes Overview",
        f"**Total:** {total_nodes}\n**Running:** {running_nodes} 🟢\n**Stopped:** {stopped_nodes} 🔴\n**Local/Remote:** {local_nodes}/{remote_nodes}", True)
    add_field(e, "👥 Users & VPS",
        f"**Users:** {total_users}\n**VPS:** {total_vps}\n**Running:** {running_vps} 🟢\n"
        f"**Stopped:** {stopped_vps} 🔴\n**Suspended:** {suspended_vps} 🟡\n**Whitelisted:** {whitelisted_vps} ✅", True)
    add_field(e, "💾 Resource Allocation",
        f"**RAM:** {total_ram} GB\n**CPU:** {total_cpu} Cores\n**Disk:** {total_disk} GB", True)
    add_field(e, "⚙️ System",
        f"**Admins:** {total_admins}\n**Ports Allocated:** {total_ports_allocated}\n"
        f"**Ports In Use:** {total_ports_used}\n**Available:** {total_ports_allocated - total_ports_used}", True)
    if node_statuses:
        nt = "\n\n".join(node_statuses)
        for i, chunk in enumerate([nt[i:i+1024] for i in range(0, len(nt), 1024)], 1):
            title = "📡 Node Details" if i == 1 else f"📡 Node Details (Part {i})"
            add_field(e, title, chunk, False)
    await ctx.send(embed=e)

@bot.command(name='status-summary')
@is_admin()
async def status_summary(ctx):
    nodes = get_nodes(); total_nodes = len(nodes); running_nodes = 0
    for node in nodes:
        if node['is_local']: running_nodes += 1
        else:
            try:
                r = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=3)
                if r.status_code == 200: running_nodes += 1
            except Exception: pass
    total_vps = sum(len(v) for v in vps_data.values())
    total_users = len(vps_data)
    running = stopped = suspended = 0
    for vlist in vps_data.values():
        for v in vlist:
            if v.get('suspended'): suspended += 1
            elif v.get('status') == 'running': running += 1
            else: stopped += 1
    await ctx.send(embed=create_success_embed("📈 Quick Status",
        f"**Nodes:** {running_nodes}/{total_nodes} 🟢\n"
        f"**VPS:** {total_vps} total\n• Running: {running} 🟢\n• Stopped: {stopped} 🔴\n• Suspended: {suspended} 🟡\n"
        f"**Users:** {total_users} 👥\n**Bot Latency:** {round(bot.latency*1000)}ms"))

@bot.command(name='admin-add')
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    uid = str(user.id)
    if uid in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already Admin", "This user is a main admin!")); return
    if uid in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Already Admin", f"{user.mention} is already an admin!")); return
    admin_data["admins"].append(uid)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Added", f"{user.mention} is now an admin!"))

@bot.command(name='admin-remove')
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    uid = str(user.id)
    if uid in main_admin_ids:
        await ctx.send(embed=create_error_embed("Cannot Remove", "Cannot remove main admin!")); return
    if uid not in admin_data.get("admins", []):
        await ctx.send(embed=create_error_embed("Not Admin", f"{user.mention} is not admin!")); return
    admin_data["admins"].remove(uid)
    save_admin_data()
    await ctx.send(embed=create_success_embed("Admin Removed", f"{user.mention} is no longer admin!"))

@bot.command(name='add-admin')
@is_main_admin()
async def add_admin_id(ctx, user_id: str):
    if not user_id.isdigit():
        await ctx.send(embed=create_error_embed("Invalid ID", "Numeric ID required.")); return
    if user_id in main_admin_ids:
        await ctx.send(embed=create_error_embed("Already", "Already a main admin!")); return
    main_admin_ids.add(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Added", f"`{user_id}` is now a main admin!"))

@bot.command(name='rm-admin')
@is_main_admin()
async def rm_admin_id(ctx, user_id: str):
    if user_id not in main_admin_ids:
        await ctx.send(embed=create_error_embed("Not Admin", f"`{user_id}` isn't a main admin!")); return
    if len(main_admin_ids) <= 1:
        await ctx.send(embed=create_error_embed("Cannot Remove", "At least one must remain.")); return
    main_admin_ids.discard(user_id)
    save_main_admins()
    await ctx.send(embed=create_success_embed("Main Admin Removed", f"`{user_id}` removed."))

@bot.command(name='admin-list')
@is_main_admin()
async def admin_list(ctx):
    admins = admin_data.get("admins", [])
    e = create_embed("👑 Admin Team", "Current admins", 0x1a1a1a)
    main_lines = []
    for mid in main_admin_ids:
        try:
            u = await bot.fetch_user(int(mid))
            main_lines.append(f"• {u.mention} (ID: {mid})")
        except Exception:
            main_lines.append(f"• Unknown (ID: {mid})")
    add_field(e, "🔰 Main Admin(s)", "\n".join(main_lines) or "None", False)
    if admins:
        lines = []
        for aid in admins:
            try:
                u = await bot.fetch_user(int(aid))
                lines.append(f"• {u.mention} (ID: {aid})")
            except Exception:
                lines.append(f"• Unknown (ID: {aid})")
        add_field(e, "🛡️ Admins", "\n".join(lines), False)
    else:
        add_field(e, "🛡️ Admins", "No additional admins", False)
    await ctx.send(embed=e)

@bot.command(name="userinfo")
@is_admin()
async def user_info(ctx, user: discord.Member):
    uid = str(user.id); vps_list = vps_data.get(uid, [])
    e = create_embed("👤 User Dashboard", f"Stats for {user.mention}", 0x1A1A1A)
    add_field(e, "👤 User",
        f"**Name:** `{user.name}`\n**ID:** `{user.id}`\n"
        f"**Joined:** `{user.joined_at.strftime('%Y-%m-%d') if user.joined_at else 'Unknown'}`", True)
    is_admin_user = uid in main_admin_ids or uid in admin_data.get("admins", [])
    add_field(e, "🛡️ Admin", "✅ Yes" if is_admin_user else "❌ No", True)
    add_field(e, "🖥️ VPS Count", f"`{len(vps_list)}`", True)
    if vps_list:
        total_ram = total_cpu = total_storage = 0
        running = suspended = whitelisted = 0
        lines = []
        for i, vps in enumerate(vps_list, 1):
            node = get_node(vps.get("node_id")); node_name = node["name"] if node else "Unknown"
            ram = int(vps.get("ram","0GB").replace("GB",""))
            storage = int(vps.get("storage","0GB").replace("GB",""))
            cpu = int(vps.get("cpu", 0))
            total_ram += ram; total_storage += storage; total_cpu += cpu
            if vps.get("suspended"): status = "⛔ SUSPENDED"; suspended += 1
            elif vps.get("status") == "running": status = "🟢 RUNNING"; running += 1
            else: status = "🔴 STOPPED"
            if vps.get("whitelisted"): whitelisted += 1
            lines.append(f"**{i}.** `{vps['container_name']}`\n{status} | `{ram}GB` RAM • `{cpu}` CPU • `{storage}GB` Disk\n📍 Node: `{node_name}`")
        add_field(e, "📊 VPS Summary",
            f"🖥️ `{len(vps_list)}` Total\n🟢 `{running}` Running\n⛔ `{suspended}` Suspended\n✅ `{whitelisted}` Whitelisted", True)
        add_field(e, "📈 Resources",
            f"**RAM:** `{total_ram} GB`\n**CPU:** `{total_cpu} Cores`\n**Disk:** `{total_storage} GB`", True)
        add_field(e, "🌐 Ports",
            f"`{get_user_used_ports(uid)}/{get_user_allocation(uid)}` Used", True)
        text = "\n\n".join(lines)
        for i in range(0, len(text), 1024):
            add_field(e, "📋 VPS List", text[i:i+1024], False)
    else:
        add_field(e, "🖥️ VPS", "❌ No VPS assigned", False)
    await ctx.send(embed=e)

@bot.command(name="serverstats")
@is_admin()
async def server_stats(ctx):
    total_users = len(vps_data)
    total_admins = len(admin_data.get("admins", [])) + 1
    total_vps = sum(len(v) for v in vps_data.values())
    total_ram = total_cpu = total_storage = 0
    running = suspended = stopped = whitelisted = 0
    for vlist in vps_data.values():
        for vps in vlist:
            total_ram += int(vps.get("ram","0GB").replace("GB",""))
            total_storage += int(vps.get("storage","0GB").replace("GB",""))
            total_cpu += int(vps.get("cpu", 0))
            if vps.get("status") == "running":
                if vps.get("suspended", False): suspended += 1
                else: running += 1
            else: stopped += 1
            if vps.get("whitelisted"): whitelisted += 1
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT SUM(allocated_ports) FROM port_allocations")
    pa = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM port_forwards")
    pu = cur.fetchone()[0] or 0
    conn.close()
    e = create_embed("📊 Server Statistics", "**Live Infrastructure Dashboard**", 0x1A1A1A)
    add_field(e, "👥 Users", f"`{total_users}` Users\n`{total_admins}` Admins", True)
    add_field(e, "🖥️ VPS", f"Total: `{total_vps}`\n🟢 `{running}` Running\n⛔ `{suspended}` Suspended", True)
    add_field(e, "📌 Status", f"🔴 `{stopped}` Stopped\n✅ `{whitelisted}` Whitelisted", True)
    add_field(e, "📈 RAM", f"`{total_ram} GB`", True)
    add_field(e, "⚙️ CPU", f"`{total_cpu} Cores`", True)
    add_field(e, "💾 Storage", f"`{total_storage} GB`", True)
    add_field(e, "🌐 Ports Allocated", f"`{pa}`", True)
    add_field(e, "🔌 Ports In Use", f"`{pu}`", True)
    add_field(e, "📊 Utilization", f"`{pu}/{pa}`" if pa else "`N/A`", True)
    await ctx.send(embed=e)

@bot.command(name='vpsinfo')
@is_admin()
async def vps_info(ctx, container_name: str = None):
    if not container_name:
        all_vps = []
        for uid, vlist in vps_data.items():
            try:
                u = await bot.fetch_user(int(uid))
                for i, vps in enumerate(vlist):
                    node = get_node(vps['node_id']); nn = node['name'] if node else "Unknown"
                    st = vps.get('status', 'unknown').upper()
                    if vps.get('suspended', False): st += " (SUSPENDED)"
                    if vps.get('whitelisted', False): st += " (WHITELISTED)"
                    all_vps.append(f"**{u.name}** - VPS {i+1}: `{vps['container_name']}` - {st} (Node: {nn})")
            except Exception:
                pass
        text = "\n".join(all_vps)
        for i, chunk in enumerate([text[i:i+1024] for i in range(0, len(text), 1024)], 1):
            e = create_embed(f"🖥️ All VPS (Part {i})", "List of all VPS", 0x1a1a1a)
            add_field(e, "VPS List", chunk, False)
            await ctx.send(embed=e)
    else:
        found = None; owner_user = None
        for uid, vlist in vps_data.items():
            for vps in vlist:
                if vps['container_name'] == container_name:
                    found = vps; owner_user = await bot.fetch_user(int(uid)); break
            if found: break
        if not found:
            await ctx.send(embed=create_error_embed("VPS Not Found", f"`{container_name}`")); return
        node = get_node(found['node_id']); nn = node['name'] if node else "Unknown"
        e = create_embed(f"🖥️ VPS Information - {container_name}",
            f"Owned by {owner_user.mention} on node {nn}", 0x1a1a1a)
        add_field(e, "👤 Owner", f"**Name:** {owner_user.name}\n**ID:** {owner_user.id}", False)
        add_field(e, "📊 Specs",
            f"**RAM:** {found['ram']}\n**CPU:** {found['cpu']} Cores\n**Storage:** {found['storage']}", False)
        add_field(e, "📈 Status",
            f"**Current:** {found.get('status','unknown').upper()}\n"
            f"**Suspended:** {found.get('suspended', False)}\n"
            f"**Whitelisted:** {found.get('whitelisted', False)}\n"
            f"**Created:** {found.get('created_at','Unknown')}", False)
        if 'config' in found:
            add_field(e, "⚙️ Config", found['config'], False)
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM port_forwards WHERE vps_container = ?', (container_name,))
        pc = cur.fetchone()[0]; conn.close()
        add_field(e, "🌐 Active Ports", f"{pc} forwarded (TCP)", False)
        await ctx.send(embed=e)

@bot.command(name='restart-vps')
@is_admin()
async def restart_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Restarting VPS", f"`{container_name}`..."))
    try:
        await execute_docker(container_name, f"restart {container_name}", node_id=node_id)
        for uid, vlist in vps_data.items():
            for vps in vlist:
                if vps['container_name'] == container_name:
                    vps['status'] = 'running'; save_vps_data(); break
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("VPS Restarted", f"`{container_name}` restarted!"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Restart Failed", str(e)))

@bot.command(name='exec')
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Executing", f"In `{container_name}`..."))
    try:
        out = await execute_docker(container_name, f'exec {container_name} bash -c "{command}"', node_id=node_id)
        e = create_embed(f"Command Output - {container_name}", f"Command: `{command}`", 0x1a1a1a)
        if isinstance(out, str) and out.strip():
            if len(out) > 1000: out = out[:1000] + "\n... (truncated)"
            add_field(e, "📤 Output", f"```\n{out}\n```", False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Execution Failed", str(e)))

@bot.command(name='stop-vps-all')
@is_admin()
async def stop_all_vps(ctx):
    e = create_warning_embed("Stopping All VPS",
        "⚠️ This will stop ALL running containers on all nodes.\n\nContinue?")
    class ConfirmView(discord.ui.View):
        def __init__(self): super().__init__(timeout=60)
        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, item):
            await inter.response.defer()
            try:
                stopped_count = 0
                for node in get_nodes():
                    if node['is_local']:
                        proc = await asyncio.create_subprocess_exec(
                            "bash", "-c", "docker ps -q | xargs -r docker stop",
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await proc.communicate()
                    else:
                        try:
                            requests.post(f"{node['url']}/api/execute",
                                json={"command": "bash -c 'docker ps -q | xargs -r docker stop'"},
                                params={"api_key": node["api_key"]}, timeout=600)
                        except Exception as ex:
                            logger.error(f"Remote stop-all failed on {node['name']}: {ex}")
                            continue
                    for uid, vlist in vps_data.items():
                        for vps in vlist:
                            if vps.get('node_id') == node['id'] and vps.get('status') == 'running':
                                vps['status'] = 'stopped'; vps['suspended'] = False
                                stopped_count += 1
                save_vps_data()
                await inter.followup.send(embed=create_success_embed("All VPS Stopped",
                    f"Stopped {stopped_count} VPS across all nodes."))
            except Exception as e:
                await inter.followup.send(embed=create_error_embed("Error", str(e)))
        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, item):
            await inter.response.edit_message(embed=create_info_embed("Cancelled", "No action taken."))
    await ctx.send(embed=e, view=ConfirmView())

@bot.command(name='cpu-monitor')
@is_admin()
async def resource_monitor_control(ctx, action: str = "status"):
    global resource_monitor_active
    if action.lower() == "status":
        status = "Active" if resource_monitor_active else "Inactive"
        e = create_embed("Resource Monitor Status",
            f"Monitoring is **{status}** (logs only; no auto-stop)", 0x00ccff if resource_monitor_active else 0xffaa00)
        add_field(e, "Thresholds", f"{CPU_THRESHOLD}% CPU / {RAM_THRESHOLD}% RAM", True)
        await ctx.send(embed=e)
    elif action.lower() == "enable":
        resource_monitor_active = True
        await ctx.send(embed=create_success_embed("Monitor Enabled"))
    elif action.lower() == "disable":
        resource_monitor_active = False
        await ctx.send(embed=create_warning_embed("Monitor Disabled"))
    else:
        await ctx.send(embed=create_error_embed("Invalid", f"Use `{PREFIX}cpu-monitor <status|enable|disable>`"))

@bot.command(name='resize-vps')
@is_admin()
async def resize_vps(ctx, container_name: str, ram: int = None, cpu: int = None, disk: int = None):
    if ram is None and cpu is None and disk is None:
        await ctx.send(embed=create_error_embed("Missing Parameters", "Specify at least one.")); return
    found = None; uid = None; idx = None
    for u, vlist in vps_data.items():
        for i, v in enumerate(vlist):
            if v['container_name'] == container_name:
                found = v; uid = u; idx = i; break
        if found: break
    if not found:
        await ctx.send(embed=create_error_embed("Not Found", f"`{container_name}`")); return
    node_id = found['node_id']
    was_running = found.get('status') == 'running' and not found.get('suspended', False)
    if was_running:
        await ctx.send(embed=create_info_embed("Stopping", f"`{container_name}`..."))
        try:
            await execute_docker(container_name, f"stop {container_name}", node_id=node_id)
            found['status'] = 'stopped'; save_vps_data()
        except Exception as e:
            await ctx.send(embed=create_error_embed("Stop Failed", str(e))); return
    changes = []
    try:
        new_ram = int(found['ram'].replace('GB',''))
        new_cpu = int(found['cpu'])
        new_disk = int(found['storage'].replace('GB',''))
        if ram is not None and ram > 0:
            new_ram = ram
            await execute_docker(container_name, f"update --memory {ram*1024}m {container_name}", node_id=node_id)
            changes.append(f"RAM: {ram}GB")
        if cpu is not None and cpu > 0:
            new_cpu = cpu
            await execute_docker(container_name, f"update --cpus {cpu} {container_name}", node_id=node_id)
            changes.append(f"CPU: {cpu}")
        if disk is not None and disk > 0:
            new_disk = disk
            changes.append(f"Disk: {disk}GB (recreate to apply)")
        found['ram'] = f"{new_ram}GB"; found['cpu'] = str(new_cpu); found['storage'] = f"{new_disk}GB"
        found['config'] = f"{new_ram}GB RAM / {new_cpu} CPU / {new_disk}GB Disk"
        save_vps_data()
        if was_running:
            await safe_start_container(container_name, node_id)
            found['status'] = 'running'; save_vps_data()
            await apply_internal_permissions(container_name, node_id)
            await recreate_port_forwards(container_name)
        e = create_success_embed("VPS Resized", f"`{container_name}` updated.")
        add_field(e, "Changes", "\n".join(changes), False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Resize Failed", str(e)))

@bot.command(name='clone-vps')
@is_admin()
async def clone_vps(ctx, container_name: str, new_name: str = None):
    if not new_name:
        new_name = f"{BOT_NAME.lower()}-{container_name}-clone-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Cloning VPS", f"`{container_name}` → `{new_name}`..."))
    try:
        found = None; uid = None
        for u, vlist in vps_data.items():
            for v in vlist:
                if v['container_name'] == container_name:
                    found = v; uid = u; break
            if found: break
        if not found:
            await ctx.send(embed=create_error_embed("Not Found", f"`{container_name}`")); return
        # Commit as new image
        img_tag = f"{BOT_NAME.lower()}-clone:{new_name}"
        await execute_docker(container_name, f"commit {container_name} {img_tag}", node_id=node_id)
        # Run new container from image
        args = [
            "run", "-d", "--name", new_name,
            "--privileged", "--cap-add", "ALL",
            "--cgroupns", "host",
            img_tag,
            "bash", "-c",
            "service ssh start || /usr/sbin/sshd || true; tail -f /dev/null"
        ]
        await execute_docker(new_name, "", node_id=node_id, docker_args=args, timeout=300)
        await apply_internal_permissions(new_name, node_id)
        await setup_ssh_access(new_name, node_id)
        vps_data.setdefault(uid, [])
        new_vps = dict(found)
        new_vps['container_name'] = new_name
        new_vps['status'] = 'running'
        new_vps['suspended'] = False
        new_vps['whitelisted'] = False
        new_vps['suspension_history'] = []
        new_vps['created_at'] = datetime.now().isoformat()
        new_vps['shared_with'] = []
        new_vps['id'] = None
        vps_data[uid].append(new_vps)
        save_vps_data()
        e = create_success_embed("VPS Cloned", f"`{container_name}` → `{new_name}`")
        add_field(e, "New VPS",
                  f"**RAM:** {new_vps['ram']}\n**CPU:** {new_vps['cpu']} Cores\n**Storage:** {new_vps['storage']}", False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Clone Failed", str(e)))

@bot.command(name='migrate-vps')
@is_admin()
async def migrate_vps(ctx, container_name: str, target_node_id: int):
    node_id = find_node_id_for_container(container_name)
    target = get_node(target_node_id)
    if not target:
        await ctx.send(embed=create_error_embed("Invalid Node", "Target node not found.")); return
    await ctx.send(embed=create_info_embed("Migrating VPS", f"`{container_name}` → node {target['name']}..."))
    try:
        # Commit image locally, then transfer. Without a registry this is nontrivial;
        # we document that a registry is recommended and only support same-host move here.
        await execute_docker(container_name, f"stop {container_name}", node_id=node_id)
        img_tag = f"{BOT_NAME.lower()}-migrate:{container_name}"
        await execute_docker(container_name, f"commit {container_name} {img_tag}", node_id=node_id)
        # In a real setup you'd `docker save | ssh node docker load`, then run on target.
        await ctx.send(embed=create_warning_embed("Migration Note",
            "Cross-node Docker migration needs a registry or image transfer. "
            "Image committed locally as `" + img_tag + "`. Complete migration manually or deploy a registry."))
        for uid, vlist in vps_data.items():
            for v in vlist:
                if v['container_name'] == container_name:
                    v['status'] = 'stopped'; save_vps_data(); break
    except Exception as e:
        await ctx.send(embed=create_error_embed("Migration Failed", str(e)))

@bot.command(name='vps-stats')
@is_admin()
async def vps_stats(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Gathering Stats", f"`{container_name}`..."))
    try:
        stats = await get_container_stats(container_name, node_id)
        e = create_embed(f"📊 VPS Statistics - {container_name}", "Resource usage", 0x1a1a1a)
        add_field(e, "📈 Status", f"**{stats['status'].upper()}**", False)
        add_field(e, "💻 CPU", f"**{stats['cpu']:.1f}%**", True)
        add_field(e, "🧠 Memory", f"**{stats['ram']['used']:.0f}/{stats['ram']['total']:.0f} MB ({stats['ram']['pct']:.1f}%)**", True)
        add_field(e, "💾 Disk", f"**{stats['disk']}**", True)
        add_field(e, "⏱️ Uptime", f"**{stats['uptime']}**", True)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Failed", str(e)))

@bot.command(name='node-check')
@is_admin()
async def node_check(ctx, node_id: int):
    node = get_node(node_id)
    if not node:
        await ctx.send(embed=create_error_embed("Node Not Found", f"ID {node_id}")); return
    e = create_info_embed(f"Node Check - {node['name']}",
        f"Checking node {node['name']}...")
    status = await get_node_status(node_id)
    add_field(e, "📡 Status", status, False)
    if status.startswith("🟢"):
        try:
            out = await execute_docker("", "info --format '{{.Driver}} | Containers: {{.Containers}} | Images: {{.Images}}'", node_id=node_id, timeout=30)
            add_field(e, "🐳 Docker Info", f"```{out}```", False)
        except Exception as ex:
            add_field(e, "🐳 Docker Info", f"Error: {str(ex)[:200]}", False)
        try:
            out = await execute_docker("", "ps -a --format '{{.Names}} ({{.Status}})'", node_id=node_id, timeout=30)
            add_field(e, "📋 Containers", f"```{(out or '')[:900]}```", False)
        except Exception:
            pass
        if not node['is_local']:
            try:
                r = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
                add_field(e, "🔌 API", f"✅ Reachable ({r.status_code})", False)
            except Exception as ex:
                add_field(e, "🔌 API", f"❌ {str(ex)[:200]}", False)
    else:
        add_field(e, "⚠️", "Node offline/unreachable", False)
    await ctx.send(embed=e)

@bot.command(name='vps-network')
@is_admin()
async def vps_network(ctx, container_name: str, action: str, value: str = None):
    node_id = find_node_id_for_container(container_name)
    if action.lower() not in ["list", "add", "remove", "limit"]:
        await ctx.send(embed=create_error_embed("Invalid Action", f"`{PREFIX}vps-network <container> <list|add|remove|limit> [value]`")); return
    try:
        if action.lower() == "list":
            out = await execute_docker(container_name, f"exec {container_name} ip addr", node_id=node_id)
            if isinstance(out, str) and len(out) > 1000: out = out[:1000] + "\n... (truncated)"
            e = create_embed(f"🌐 Network - {container_name}", "Network config", 0x1a1a1a)
            add_field(e, "Interfaces", f"```\n{out}\n```", False)
            await ctx.send(embed=e)
        elif action.lower() == "limit" and value:
            await execute_docker(container_name, f"exec {container_name} tc qdisc add dev eth0 root tbf rate {value} burst 32kbit latency 400ms || true", node_id=node_id)
            await ctx.send(embed=create_success_embed("Network Limited", f"Set {value} for `{container_name}`"))
        else:
            await ctx.send(embed=create_error_embed("Not Supported",
                "Docker doesn't support dynamic NIC add/remove. Use `docker network connect` on the host."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Network Failed", str(e)))

@bot.command(name='vps-processes')
@is_admin()
async def vps_processes(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Processes", f"Listing in `{container_name}`..."))
    try:
        out = await execute_docker(container_name, f"exec {container_name} ps aux", node_id=node_id)
        if isinstance(out, str) and len(out) > 1000: out = out[:1000] + "\n... (truncated)"
        e = create_embed(f"⚙️ Processes - {container_name}", "Running processes", 0x1a1a1a)
        add_field(e, "Process List", f"```\n{out}\n```", False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Failed", str(e)))

@bot.command(name='vps-logs')
@is_admin()
async def vps_logs(ctx, container_name: str, lines: int = 50):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Logs", f"Last {lines} lines from `{container_name}`..."))
    try:
        out = await execute_docker(container_name, f"logs --tail {lines} {container_name}", node_id=node_id)
        if isinstance(out, str) and len(out) > 1000: out = out[:1000] + "\n... (truncated)"
        e = create_embed(f"📋 Logs - {container_name}", f"Last {lines} lines", 0x1a1a1a)
        add_field(e, "Container Logs", f"```\n{out}\n```", False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=create_error_embed("Log Retrieval Failed", str(e)))

@bot.command(name='vps-uptime')
@is_admin()
async def vps_uptime(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    up = await get_container_uptime(container_name, node_id)
    await ctx.send(embed=create_info_embed("VPS Uptime", f"`{container_name}`: {up}"))

@bot.command(name='suspend-vps')
@is_admin()
async def suspend_vps(ctx, container_name: str, *, reason: str = "Admin action"):
    node_id = find_node_id_for_container(container_name)
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if vps.get('status') != 'running':
                    await ctx.send(embed=create_error_embed("Cannot Suspend", "Must be running.")); return
                try:
                    await execute_docker(container_name, f"stop {container_name}", node_id=node_id)
                    vps['status'] = 'stopped'; vps['suspended'] = True
                    vps.setdefault('suspension_history', []).append({
                        'time': datetime.now().isoformat(), 'reason': reason,
                        'by': f"{ctx.author.name} ({ctx.author.id})"})
                    save_vps_data()
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Suspend Failed", str(e))); return
                try:
                    owner = await bot.fetch_user(int(uid))
                    await owner.send(embed=create_warning_embed("🚨 VPS Suspended",
                        f"`{container_name}` suspended by admin.\n**Reason:** {reason}"))
                except Exception:
                    pass
                await ctx.send(embed=create_success_embed("VPS Suspended",
                    f"`{container_name}` suspended. Reason: {reason}"))
                return
    await ctx.send(embed=create_error_embed("Not Found", f"`{container_name}`"))

@bot.command(name='unsuspend-vps')
@is_admin()
async def unsuspend_vps(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    for uid, lst in vps_data.items():
        for vps in lst:
            if vps['container_name'] == container_name:
                if not vps.get('suspended'):
                    await ctx.send(embed=create_error_embed("Not Suspended")); return
                try:
                    vps['suspended'] = False; vps['status'] = 'running'
                    await safe_start_container(container_name, node_id)
                    await apply_internal_permissions(container_name, node_id)
                    await recreate_port_forwards(container_name)
                    save_vps_data()
                except Exception as e:
                    await ctx.send(embed=create_error_embed("Start Failed", str(e))); return
                await ctx.send(embed=create_success_embed("VPS Unsuspended",
                    f"`{container_name}` unsuspended and running."))
                try:
                    owner = await bot.fetch_user(int(uid))
                    await owner.send(embed=create_success_embed("🟢 VPS Unsuspended",
                        f"`{container_name}` has been unsuspended."))
                except Exception:
                    pass
                return
    await ctx.send(embed=create_error_embed("Not Found", f"`{container_name}`"))

@bot.command(name='suspension-logs')
@is_admin()
async def suspension_logs(ctx, container_name: str = None):
    if container_name:
        found = None
        for lst in vps_data.values():
            for vps in lst:
                if vps['container_name'] == container_name:
                    found = vps; break
            if found: break
        if not found:
            await ctx.send(embed=create_error_embed("Not Found")); return
        history = found.get('suspension_history', [])
        if not history:
            await ctx.send(embed=create_info_embed("No Suspensions", f"No history for `{container_name}`.")); return
        e = create_embed("Suspension History", f"For `{container_name}`")
        text = []
        for h in sorted(history, key=lambda x: x['time'], reverse=True)[:10]:
            t = datetime.fromisoformat(h['time']).strftime('%Y-%m-%d %H:%M:%S')
            text.append(f"**{t}** - {h['reason']} (by {h['by']})")
        add_field(e, "History", "\n".join(text), False)
        await ctx.send(embed=e)
    else:
        all_logs = []
        for uid, lst in vps_data.items():
            for vps in lst:
                for event in sorted(vps.get('suspension_history', []), key=lambda x: x['time'], reverse=True):
                    t = datetime.fromisoformat(event['time']).strftime('%Y-%m-%d %H:%M')
                    all_logs.append(f"**{t}** - `{vps['container_name']}` (Owner: <@{uid}>) - {event['reason']} (by {event['by']})")
        if not all_logs:
            await ctx.send(embed=create_info_embed("No Suspensions", "No events.")); return
        text = "\n".join(all_logs)
        for i, chunk in enumerate([text[i:i+1024] for i in range(0, len(text), 1024)], 1):
            e = create_embed(f"Suspension Logs (Part {i})", "Global events")
            add_field(e, "Events", chunk, False)
            await ctx.send(embed=e)

@bot.command(name='apply-permissions')
@is_admin()
async def apply_permissions(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Applying", f"`{container_name}`..."))
    try:
        was_running = (await get_container_status(container_name, node_id)) == 'running'
        if was_running:
            await execute_docker(container_name, f"stop {container_name}", node_id=node_id)
        # Privileged etc. can only be set at creation in Docker; we restart with update only.
        await execute_docker(container_name, f"update --restart unless-stopped {container_name}", node_id=node_id)
        await safe_start_container(container_name, node_id)
        await apply_internal_permissions(container_name, node_id)
        await recreate_port_forwards(container_name)
        for uid, vlist in vps_data.items():
            for v in vlist:
                if v['container_name'] == container_name:
                    v['status'] = 'running'; v['suspended'] = False; save_vps_data(); break
        await ctx.send(embed=create_success_embed("Applied",
            f"Permissions refreshed for `{container_name}`. (Privileged is set at creation in Docker.)"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Failed", str(e)))

@bot.command(name='resource-check')
@is_admin()
async def resource_check(ctx):
    suspended_count = 0
    msg = await ctx.send(embed=create_info_embed("Resource Check", "Scanning VPS..."))
    for uid, vps_list in vps_data.items():
        for vps in vps_list:
            if vps.get('status') == 'running' and not vps.get('suspended', False) and not vps.get('whitelisted', False):
                container = vps['container_name']
                node_id = vps['node_id']
                stats = await get_container_stats(container, node_id)
                cpu = stats['cpu']; ram = stats['ram']['pct']
                if cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD:
                    reason = f"High usage: CPU {cpu:.1f}%, RAM {ram:.1f}%"
                    try:
                        await execute_docker(container, f"stop {container}", node_id=node_id)
                        vps['status'] = 'stopped'; vps['suspended'] = True
                        vps.setdefault('suspension_history', []).append({
                            'time': datetime.now().isoformat(), 'reason': reason,
                            'by': 'Manual Resource Check'})
                        save_vps_data()
                        try:
                            owner = await bot.fetch_user(int(uid))
                            await owner.send(embed=create_warning_embed("🚨 VPS Auto-Suspended",
                                f"`{container}` suspended: {reason}"))
                        except Exception:
                            pass
                        suspended_count += 1
                    except Exception as e:
                        logger.error(f"Failed to suspend {container}: {e}")
    await msg.edit(embed=create_info_embed("Resource Check Complete",
        f"Suspended {suspended_count} high-usage VPS."))

@bot.command(name='whitelist-vps')
@is_admin()
async def whitelist_vps(ctx, container_name: str, action: str):
    if action.lower() not in ['add', 'remove']:
        await ctx.send(embed=create_error_embed("Invalid", f"`{PREFIX}whitelist-vps <container> <add|remove>`")); return
    for uid, vlist in vps_data.items():
        for vps in vlist:
            if vps['container_name'] == container_name:
                if action.lower() == 'add':
                    vps['whitelisted'] = True; msg = "added to whitelist"
                else:
                    vps['whitelisted'] = False; msg = "removed from whitelist"
                save_vps_data()
                await ctx.send(embed=create_success_embed("Whitelist Updated", f"`{container_name}` {msg}."))
                return
    await ctx.send(embed=create_error_embed("Not Found", f"`{container_name}`"))

@bot.command(name='snapshot')
@is_admin()
async def snapshot_vps(ctx, container_name: str, snap_name: str = "snap0"):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_info_embed("Creating Snapshot", f"`{snap_name}` for `{container_name}`..."))
    try:
        await execute_docker(container_name, f"commit {container_name} {container_name}:{snap_name}", node_id=node_id)
        await ctx.send(embed=create_success_embed("Snapshot Created",
            f"Snapshot `{snap_name}` committed for `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Snapshot Failed", str(e)))

@bot.command(name='list-snapshots')
@is_admin()
async def list_snapshots(ctx, container_name: str):
    node_id = find_node_id_for_container(container_name)
    try:
        out = await execute_docker(container_name,
            f"images {container_name} --format '{{{{.Repository}}}}:{{{{.Tag}}}} {{{{.CreatedSince}}}} {{{{.Size}}}}'",
            node_id=node_id)
        await ctx.send(embed=create_info_embed(f"Snapshots for {container_name}", out or "No snapshots"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("List Failed", str(e)))

@bot.command(name='restore-snapshot')
@is_admin()
async def restore_snapshot(ctx, container_name: str, snap_name: str):
    node_id = find_node_id_for_container(container_name)
    await ctx.send(embed=create_warning_embed("Restore Snapshot",
        f"Restoring `{snap_name}` for `{container_name}` will overwrite current state. Continue?"))
    class RestoreConfirm(discord.ui.View):
        def __init__(self): super().__init__(timeout=60)
        @discord.ui.button(label="Confirm Restore", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, item):
            await inter.response.defer()
            try:
                await execute_docker(container_name, f"stop {container_name}", node_id=node_id)
                await execute_docker(container_name, f"rm -f {container_name}", node_id=node_id)
                args = ["run", "-d", "--name", container_name, "--privileged", "--cap-add", "ALL",
                        "--cgroupns", "host", f"{container_name}:{snap_name}",
                        "bash", "-c", "service ssh start || /usr/sbin/sshd || true; tail -f /dev/null"]
                await execute_docker(container_name, "", node_id=node_id, docker_args=args, timeout=300)
                await apply_internal_permissions(container_name, node_id)
                await recreate_port_forwards(container_name)
                for uid, lst in vps_data.items():
                    for v in lst:
                        if v['container_name'] == container_name:
                            v['status'] = 'running'; v['suspended'] = False; save_vps_data(); break
                await inter.followup.send(embed=create_success_embed("Snapshot Restored",
                    f"Restored `{snap_name}` for `{container_name}`."))
            except Exception as e:
                await inter.followup.send(embed=create_error_embed("Restore Failed", str(e)))
        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, item):
            await inter.response.edit_message(embed=create_info_embed("Cancelled", "No action taken."))
    await ctx.send(view=RestoreConfirm())

@bot.command(name='backup-db')
@is_admin()
async def backup_db(ctx):
    backup_name = f"vps_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    try:
        shutil.copy('vps.db', backup_name)
        if os.path.exists('vps.db-wal'):
            shutil.copy('vps.db-wal', f"{backup_name}-wal")
        if os.path.exists('vps.db-shm'):
            shutil.copy('vps.db-shm', f"{backup_name}-shm")
        await ctx.send(embed=create_success_embed("DB Backup Created", f"Saved as `{backup_name}`"))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Backup Failed", str(e)))

@bot.command(name='repair-ports')
@is_admin()
async def repair_ports(ctx, container_name: str):
    await ctx.send(embed=create_info_embed("Repairing Ports", f"`{container_name}`..."))
    try:
        readded = await recreate_port_forwards(container_name)
        await ctx.send(embed=create_success_embed("Ports Repaired",
            f"Re-added {readded} port forwards for `{container_name}`."))
    except Exception as e:
        await ctx.send(embed=create_error_embed("Repair Failed", str(e)))

@bot.command(name='about')
async def about(ctx):
    total_users = len(vps_data)
    total_vps = sum(len(v) for v in vps_data.values())
    latency = round(bot.latency * 1000)
    e = create_info_embed(f"About {BOT_NAME}", "Bot information")
    add_field(e, "Bot Name", BOT_NAME, True)
    add_field(e, "Developer", BOT_DEVELOPER, True)
    add_field(e, "Ping", f"{latency}ms", True)
    add_field(e, "Version", BOT_VERSION, True)
    add_field(e, "Total VPS", str(total_vps), True)
    add_field(e, "Total Users", str(total_users), True)
    add_field(e, "Backend", "Docker (privileged containers, cgroupns=host)", False)
    await ctx.send(embed=e)

# ---------------------------------------------------------------------------
# Node management
# ---------------------------------------------------------------------------
@bot.command(name='node')
@is_admin()
async def node_cmd(ctx, sub: str, *args):
    if sub == 'create':
        await ctx.send("Enter node name:")
        def check(m): return m.author == ctx.author and m.channel == ctx.channel
        name = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter location:")
        location = (await bot.wait_for('message', check=check)).content.strip()
        await ctx.send("Enter total VPS capacity:")
        tv = (await bot.wait_for('message', check=check)).content.strip()
        try: total_vps = int(tv)
        except ValueError:
            await ctx.send(embed=create_error_embed("Invalid", "Integer required.")); return
        await ctx.send("Enter tags (comma separated):")
        tags = [t.strip() for t in (await bot.wait_for('message', check=check)).content.strip().split(',') if t.strip()]
        await ctx.send("Enter node URL (e.g. http://ip:port) or leave blank for local:")
        url_str = (await bot.wait_for('message', check=check)).content.strip()
        url = url_str if url_str else None
        is_local = 1 if not url else 0
        api_key = None if is_local else ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=32))
        conn = get_db(); cur = conn.cursor()
        try:
            cur.execute('INSERT INTO nodes (name, location, total_vps, tags, api_key, url, is_local) VALUES (?,?,?,?,?,?,?)',
                        (name, location, total_vps, json.dumps(tags), api_key, url, is_local))
            conn.commit()
            nid = cur.lastrowid
            e = create_success_embed("Node Created",
                f"ID: {nid}\nName: {name}\nLocation: {location}\nCapacity: {total_vps}\nTags: {', '.join(tags)}")
            if not is_local:
                add_field(e, "API Key", api_key, False)
                add_field(e, "URL", url, False)
            await ctx.send(embed=e)
        except sqlite3.IntegrityError:
            await ctx.send(embed=create_error_embed("Error", "Node name already exists."))
        conn.close()
    elif sub == 'list':
        nodes = get_nodes()
        e = create_info_embed("Nodes List", "")
        for n in nodes:
            status = "Local" if n['is_local'] else "Down"
            if not n['is_local']:
                try:
                    r = requests.get(f"{n['url']}/api/ping", params={'api_key': n['api_key']}, timeout=5)
                    status = "Up" if r.status_code == 200 else "Down"
                except Exception: pass
            field = f"ID: {n['id']}\nName: {n['name']}\nLocation: {n['location']}\nCapacity: {n['total_vps']}\nTags: {', '.join(n['tags'])}\nStatus: {status}"
            if not n['is_local']: field += f"\nURL: {n['url']}"
            add_field(e, f"Node {n['id']}", field, False)
        await ctx.send(embed=e)
    elif sub == 'status':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node status <id>")); return
        try: nid = int(args[0])
        except ValueError: await ctx.send(embed=create_error_embed("Invalid", "Integer ID.")); return
        node = get_node(nid)
        if not node: await ctx.send(embed=create_error_embed("Not Found")); return
        e = create_info_embed(f"Node Status - {node['name']}")
        status = await get_node_status(nid)
        add_field(e, "Status", status, True)
        vps_count = get_current_vps_count(nid)
        cap = node['total_vps']
        add_field(e, "Capacity", f"{vps_count}/{cap} ({(vps_count/cap*100 if cap else 0):.1f}%)", True)
        add_field(e, "Location", node['location'], True)
        add_field(e, "Tags", ", ".join(node['tags']), True)
        if not node['is_local']:
            add_field(e, "URL", node['url'], False)
        await ctx.send(embed=e)
    elif sub == 'delete':
        if not args:
            await ctx.send(embed=create_error_embed("Usage", f"{PREFIX}node delete <id> [force]")); return
        try: nid = int(args[0])
        except ValueError: await ctx.send(embed=create_error_embed("Invalid", "Integer ID.")); return
        force = len(args) > 1 and args[1].lower() == 'force'
        node = get_node(nid)
        if not node: await ctx.send(embed=create_error_embed("Not Found")); return
        if node['is_local']:
            await ctx.send(embed=create_error_embed("Cannot Delete", "Cannot delete the local node.")); return
        vps_count = get_current_vps_count(nid)
        if not force and vps_count > 0:
            await ctx.send(embed=create_error_embed("Cannot Delete",
                f"Node has {vps_count} VPS. Use 'force' to delete all.")); return
        e = create_warning_embed("⚠️ Delete Node",
            f"Delete node **{node['name']}** (ID {nid})?\nThis cannot be undone." + (f"\nForce will also delete {vps_count} VPS." if force else ""))
        class ConfirmDelete(discord.ui.View):
            def __init__(self): super().__init__(timeout=60)
            @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
            async def confirm(self, inter: discord.Interaction, item):
                await inter.response.defer()
                conn = get_db(); cur = conn.cursor()
                if force: cur.execute('DELETE FROM vps WHERE node_id = ?', (nid,))
                cur.execute('DELETE FROM nodes WHERE id = ?', (nid,))
                conn.commit(); conn.close()
                await inter.followup.send(embed=create_success_embed("Node Deleted", f"Node {nid} removed."))
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, item):
                await inter.response.edit_message(embed=create_info_embed("Cancelled"))
        await ctx.send(embed=e, view=ConfirmDelete())
    elif sub == 'edit':
        await ctx.send(embed=create_info_embed("Edit",
            "Node editing is done by direct DB manipulation in this build. Use node delete + create."))
    else:
        await ctx.send(embed=create_info_embed("Node Management",
            f"Subcommands: create, list, status <id>, delete <id> [force]"))

async def get_node_status(node_id: int) -> str:
    node = get_node(node_id)
    if not node: return "❓ Unknown"
    if node['is_local']: return "🟢 Online (Local)"
    try:
        r = requests.get(f"{node['url']}/api/ping", params={'api_key': node['api_key']}, timeout=5)
        return "🟢 Online" if r.status_code == 200 else "🔴 Offline"
    except Exception:
        return "🔴 Offline"

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
class HelpView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.current_category = "user"
        self.command_categories = {
            "user": {
                "name": "👤 User Commands",
                "commands": [
                    (f"{PREFIX}ping", "Check bot latency"),
                    (f"{PREFIX}uptime", "Show host uptime"),
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}manage [@user]", "Manage your VPS"),
                    (f"{PREFIX}share-user @user <n>", "Share VPS access"),
                    (f"{PREFIX}share-ruser @user <n>", "Revoke VPS access"),
                    (f"{PREFIX}manage-shared @owner <n>", "Manage shared VPS"),
                    (f"{PREFIX}ports", "Port forwards"),
                    (f"{PREFIX}about", "Bot information"),
                ]
            },
            "vps": {
                "name": "🖥️ VPS Management",
                "commands": [
                    (f"{PREFIX}myvps", "List your VPS"),
                    (f"{PREFIX}vpsinfo [container]", "VPS information"),
                    (f"{PREFIX}vps-stats <container>", "VPS stats"),
                    (f"{PREFIX}vps-uptime <container>", "VPS uptime"),
                    (f"{PREFIX}vps-processes <container>", "List processes"),
                    (f"{PREFIX}vps-logs <container> [n]", "Show logs"),
                    (f"{PREFIX}restart-vps <container>", "Restart VPS"),
                    (f"{PREFIX}clone-vps <container> [new]", "Clone VPS"),
                    (f"{PREFIX}snapshot <container> [name]", "Create snapshot"),
                    (f"{PREFIX}list-snapshots <container>", "List snapshots"),
                    (f"{PREFIX}restore-snapshot <c> <name>", "Restore snapshot"),
                ]
            },
            "ports": {
                "name": "🔌 Port Forwarding",
                "commands": [
                    (f"{PREFIX}ports [add <n> <port>|list|remove <id>]", "Manage port forwards"),
                    (f"{PREFIX}ports-add-user <n> @user", "Allocate port slots (Admin)"),
                    (f"{PREFIX}ports-remove-user <n> @user", "Deallocate (Admin)"),
                    (f"{PREFIX}ports-revoke <id>", "Revoke forward (Admin)"),
                ]
            },
            "system": {
                "name": "⚙️ System Commands",
                "commands": [
                    (f"{PREFIX}serverstats", "Server statistics"),
                    (f"{PREFIX}resource-check", "Check high-usage VPS (Admin)"),
                    (f"{PREFIX}cpu-monitor <status|enable|disable>", "Monitor control"),
                    (f"{PREFIX}thresholds", "View thresholds"),
                    (f"{PREFIX}set-threshold <cpu> <ram>", "Set thresholds (Admin)"),
                    (f"{PREFIX}set-status <type> <name>", "Set bot status (Admin)"),
                ]
            },
            "nodes": {
                "name": "🌐 Node Management",
                "commands": [
                    (f"{PREFIX}node create", "Create node (Admin)"),
                    (f"{PREFIX}node list", "List nodes (Admin)"),
                    (f"{PREFIX}node status <id>", "Check node (Admin)"),
                    (f"{PREFIX}node delete <id>", "Delete node (Admin)"),
                    (f"{PREFIX}lxc-list [node_id]", "List containers (Admin)"),
                    (f"{PREFIX}node-check <id>", "Detailed node check (Admin)"),
                ]
            },
            "admin": {
                "name": "🛡️ Admin Commands",
                "commands": [
                    (f"{PREFIX}lxc-list", "List all containers"),
                    (f"{PREFIX}create <ram> <cpu> <disk> @user", "Create VPS"),
                    (f"{PREFIX}delete-vps @user <n> [reason]", "Delete VPS"),
                    (f"{PREFIX}add-resources <container> [ram] [cpu] [disk]", "Add resources"),
                    (f"{PREFIX}resize-vps <container> [ram] [cpu] [disk]", "Resize"),
                    (f"{PREFIX}suspend-vps <container> [reason]", "Suspend"),
                    (f"{PREFIX}unsuspend-vps <container>", "Unsuspend"),
                    (f"{PREFIX}suspension-logs [container]", "Suspension logs"),
                    (f"{PREFIX}whitelist-vps <container> <add|remove>", "Whitelist"),
                    (f"{PREFIX}userinfo @user", "User info"),
                    (f"{PREFIX}list-all", "List all VPS"),
                    (f"{PREFIX}exec <container> <cmd>", "Execute command"),
                    (f"{PREFIX}stop-vps-all", "Stop all VPS"),
                    (f"{PREFIX}apply-permissions <container>", "Refresh perms"),
                    (f"{PREFIX}repair-ports <container>", "Repair ports"),
                ]
            },
            "main_admin": {
                "name": "👑 Main Admin Commands",
                "commands": [
                    (f"{PREFIX}admin-add @user", "Add admin"),
                    (f"{PREFIX}admin-remove @user", "Remove admin"),
                    (f"{PREFIX}add-admin <id>", "Add main admin by ID"),
                    (f"{PREFIX}rm-admin <id>", "Remove main admin by ID"),
                    (f"{PREFIX}admin-list", "List admins"),
                ]
            },
        }
        self.select = discord.ui.Select(placeholder="Select Category", options=[])
        self._build_select_options()
        self.select.callback = self.select_callback
        self.add_item(self.select)
        self.embed = None
        self.update_embed()

    def _build_select_options(self):
        uid = str(self.ctx.author.id)
        is_admin_user = uid in main_admin_ids or uid in admin_data.get("admins", [])
        is_main_admin_user = uid in main_admin_ids
        options = []
        for cat in ["user", "vps", "ports", "system", "nodes", "admin"]:
            if cat in ("nodes", "admin") and not is_admin_user: continue
            options.append(discord.SelectOption(label=self.command_categories[cat]["name"], value=cat))
        if is_main_admin_user:
            options.append(discord.SelectOption(label=self.command_categories["main_admin"]["name"], value="main_admin"))
        self.select.options = options

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            return
        self.current_category = interaction.data['values'][0]
        self.update_embed()
        await interaction.response.edit_message(embed=self.embed, view=self)

    def update_embed(self):
        cat = self.command_categories[self.current_category]
        colors = {"user": 0x3498db, "vps": 0x2ecc71, "ports": 0xe74c3c,
                  "system": 0xf39c12, "nodes": 0x1abc9c, "admin": 0xe67e22,
                  "main_admin": 0xf1c40f}
        color = colors.get(self.current_category, 0x1a1a1a)
        self.embed = create_embed(f"📚 {BOT_NAME} Command Help - {cat['name']}",
            f"**{cat['name']}**\nUse the dropdown to switch categories.", color)
        text = "\n".join(f"**{c}** - {d}" for c, d in cat["commands"])
        add_field(self.embed, "Commands", text, False)

@bot.command(name='help')
async def show_help(ctx):
    view = HelpView(ctx)
    await ctx.send(embed=view.embed, view=view)

@bot.command(name='commands')
async def commands_alias(ctx):
    await show_help(ctx)

@bot.command(name='quickhelp')
async def quick_help(ctx):
    uid = str(ctx.author.id)
    is_admin_user = uid in main_admin_ids or uid in admin_data.get("admins", [])
    e = create_info_embed("🚀 Quick Help Reference",
        "Quick reference. Use `!help` for complete command list.")
    add_field(e, "👤 For Users",
        "• `!myvps` - List your VPS\n"
        "• `!manage` - Start/stop/manage VPS\n"
        "• `!ports` - Port forwarding\n"
        "• `!share-user @user 1` - Share VPS #1\n"
        "• `!about` - Bot info", False)
    add_field(e, "🖥️ VPS Control",
        "• `!manage` → ▶ Start\n"
        "• `!manage` → ⏸ Stop\n"
        "• `!manage` → 🔑 SSH\n"
        "• `!manage` → 📊 Stats\n"
        "• `!manage` → 🔄 Reinstall", False)
    if is_admin_user:
        add_field(e, "🛡️ Admin Quick Actions",
            "• `!create 2 2 20 @user`\n"
            "• `!userinfo @user`\n"
            "• `!node list`\n"
            "• `!serverstats`\n"
            "• `!suspend-vps <container> <reason>`", False)
    await ctx.send(embed=e)

@bot.command(name='mangage')
async def manage_typo(ctx):
    await ctx.send(embed=create_info_embed("Command Correction",
        f"Did you mean `{PREFIX}manage`?"))

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        logger.error("No Discord token found in DISCORD_TOKEN environment variable.")
