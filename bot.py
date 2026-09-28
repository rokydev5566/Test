import random
import subprocess
import os
import discord
from discord.ext import commands, tasks
import asyncio
from discord import app_commands
import psutil
from datetime import datetime
import re
import time

# Configuration
TOKEN = 'MTU1NDE0OTU3OTM0MTk1NTExMw.GwOi3j.VUXBm8Eurt8G7dErKaNSmKI61NMoEt-dfeKrRs'  # REPLACE WITH YOUR BOT'S TOKEN
RAM_LIMIT = '2g'
SERVER_LIMIT = 12
LOGS_CHANNEL_ID = 123456789    # CHANGE TO YOUR LOGS CHANNEL ID

# Admin User IDs (comma-separated for multiple admins)
ADMIN_USER_IDS = [1155148045231591539]  # CHANGE TO YOUR USER ID(S)

database_file = 'database.txt'

intents = discord.Intents.default()
intents.messages = False
intents.message_content = False
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)

EMBED_COLOR = 0x9B59B6

OS_OPTIONS = {
    "ubuntu": {"image": "ubuntu:22.04", "name": "Ubuntu 22.04", "emoji": "🐧",
               "description": "Stable and widely-used Linux distribution"},
    "debian": {"image": "debian:12", "name": "Debian 12", "emoji": "🦕",
               "description": "Rock-solid stability with large software repository"},
    "alpine": {"image": "alpine:latest", "name": "Alpine Linux", "emoji": "⛰️",
               "description": "Lightweight and security-focused"},
    "arch": {"image": "archlinux:latest", "name": "Arch Linux", "emoji": "🎯",
             "description": "Rolling release with bleeding-edge software"},
    "kali": {"image": "kalilinux/kali-rolling", "name": "Kali Linux", "emoji": "💣",
             "description": "Penetration testing and security auditing"},
    "fedora": {"image": "fedora:latest", "name": "Fedora", "emoji": "🎩",
               "description": "Innovative features with Red Hat backing"}
}

LOADING_ANIMATION = ["🔄", "⚡", "✨", "🌀", "🌪️", "🌈"]
SUCCESS_ANIMATION = ["✅", "🎉", "✨", "🌟", "💫", "🔥"]
ERROR_ANIMATION = ["❌", "💥", "⚠️", "🚨", "🔴", "🛑"]
DEPLOY_ANIMATION = ["🚀", "🛰️", "🌌", "🔭", "👨‍🚀", "🪐"]

def generate_root_password():
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=12))

async def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return interaction.user.id in ADMIN_USER_IDS

async def is_admin_role_only(interaction: discord.Interaction) -> bool:
    return interaction.user.id in ADMIN_USER_IDS

def add_to_database(user, container_name, ssh_command, password):
    with open(database_file, 'a') as f:
        f.write(f"{user}|{container_name}|{ssh_command}|{password}\n")

def remove_from_database(ssh_command):
    if not os.path.exists(database_file):
        return
    with open(database_file, 'r') as f:
        lines = f.readlines()
    with open(database_file, 'w') as f:
        for line in lines:
            if ssh_command not in line:
                f.write(line)

def remove_container_from_database_by_id(container_id):
    if not os.path.exists(database_file):
        return
    with open(database_file, 'r') as f:
        lines = f.readlines()
    with open(database_file, 'w') as f:
        for line in lines:
            parts = line.strip().split('|')
            if len(parts) < 2 or parts[1] != container_id:
                f.write(line)

def get_container_info_by_id(container_id):
    if not os.path.exists(database_file):
        return None, None, None, None
    with open(database_file, 'r') as f:
        for line in f:
            parts = line.strip().split('|')
            if len(parts) >= 4 and parts[1].startswith(container_id):
                return parts[0], parts[1], parts[2], parts[3]
    return None, None, None, None

def get_user_servers(user):
    if not os.path.exists(database_file):
        return []
    servers = []
    with open(database_file, 'r') as f:
        for line in f:
            parts = line.strip().split('|')
            if len(parts) >= 3 and parts[0] == user:
                servers.append(line.strip())
    return servers

def get_all_servers():
    if not os.path.exists(database_file):
        return []
    with open(database_file, 'r') as f:
        return [line.strip() for line in f]

def count_user_servers(user):
    return len(get_user_servers(user))

def get_system_resources():
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        return {
            'cpu': cpu_percent,
            'memory': {'total': round(mem.total / (1024**3), 2),
                       'used': round(mem.used / (1024**3), 2),
                       'percent': mem.percent},
            'disk': {'total': round(disk.total / (1024**3), 2),
                     'used': round(disk.used / (1024**3), 2),
                     'percent': disk.percent}
        }
    except Exception:
        return {'cpu': 0,
                'memory': {'total': 0, 'used': 0, 'percent': 0},
                'disk': {'total': 0, 'used': 0, 'percent': 0}}

def get_container_stats():
    try:
        stats_raw = subprocess.check_output(
            ["docker", "stats", "--no-stream", "--format", "{{.ID}}|{{.CPUPerc}}|{{.MemUsage}}"],
            text=True
        ).strip().split('\n')
        stats = {}
        for line in stats_raw:
            parts = line.split('|')
            if len(parts) >= 3:
                container_id = parts[0]
                cpu_percent = parts[1].strip()
                mem_usage_raw = parts[2].strip()
                mem_match = re.match(r"(\d+(\.\d+)?\w+)\s+/\s+(\d+(\.\d+)?\w+)", mem_usage_raw)
                mem_used = mem_match.group(1) if mem_match else '0B'
                mem_limit = mem_match.group(3) if mem_match else '0B'
                stats[container_id] = {'cpu': cpu_percent, 'mem_used': mem_used, 'mem_limit': mem_limit}
        return stats
    except Exception as e:
        print(f"Error getting container stats: {e}")
        return {}

async def animate_message(msg, base_embed, animation_frames, duration=5, text="Processing"):
    start_time = time.time()
    frame_index = 0
    while time.time() - start_time < duration:
        embed = base_embed.copy()
        embed.set_author(name=f"{animation_frames[frame_index]} {text}")
        try:
            await msg.edit(embed=embed)
        except Exception:
            pass
        frame_index = (frame_index + 1) % len(animation_frames)
        await asyncio.sleep(0.5)

def setup_ssh_and_pinggy(container_id, password, timeout=180):
    setup_cmd = (
        f"apt-get update -qq >/dev/null 2>&1; "
        f"(apt-get install -y -qq openssh-server >/dev/null 2>&1 || "
        f"apk add --no-cache openssh >/dev/null 2>&1 || "
        f"pacman -Sy --noconfirm openssh >/dev/null 2>&1 || "
        f"dnf install -y openssh-server >/dev/null 2>&1); "
        f"echo 'root:{password}' | chpasswd; "
        f"mkdir -p /var/run/sshd; "
        f"sed -i 's/#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config; "
        f"sed -i 's/#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        f"sed -i 's/#*UsePAM.*/UsePAM yes/' /etc/ssh/sshd_config; "
        f"pgrep sshd >/dev/null || nohup /usr/sbin/sshd -D >/tmp/sshd.log 2>&1 & "
        f"pkill -f pinggy 2>/dev/null; "
        f"nohup sh -c 'ssh -p 443 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 "
        f"-R0:localhost:22 tcp@free.pinggy.io' >/tmp/pinggy.log 2>&1 &"
    )
    try:
        subprocess.run(["docker", "exec", container_id, "sh", "-c", setup_cmd],
                       capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        print(f"setup_ssh_and_pinggy error: {e}")
        return None
    return None

async def wait_for_pinggy(container_id, max_wait=60):
    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(2)
        elapsed += 2
        try:
            log = subprocess.run(
                ["docker", "exec", container_id, "cat", "/tmp/pinggy.log"],
                capture_output=True, text=True, timeout=10
            )
            match = re.search(r'tcp://([a-zA-Z0-9\-\.]+):(\d+)', log.stdout)
            if match:
                host, port = match.group(1), match.group(2)
                return f"ssh root@{host} -p {port}"
        except Exception as e:
            print(f"wait_for_pinggy error: {e}")
    return None

@bot.event
async def on_ready():
    change_status.start()
    print(f'✨ Bot is ready. Logged in as {bot.user} ✨')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Error syncing commands: {e}")

@tasks.loop(seconds=5)
async def change_status():
    try:
        instance_count = len(open(database_file).readlines()) if os.path.exists(database_file) else 0
        statuses = [
            f"🌠 Managing {instance_count} Cloud Instances",
            f"⚡ Powering {instance_count} Servers",
            f"🔮 Watching over {instance_count} VMs",
            f"🚀 Hosting {instance_count} Dreams",
            f"💻 Serving {instance_count} Terminals",
            f"🌐 Running {instance_count} Nodes"
        ]
        await bot.change_presence(activity=discord.Game(name=random.choice(statuses)))
    except Exception as e:
        print(f"💥 Failed to update status: {e}")

async def send_to_logs(message):
    try:
        channel = bot.get_channel(LOGS_CHANNEL_ID)
        if channel:
            perms = channel.permissions_for(channel.guild.me)
            if perms.send_messages:
                timestamp = datetime.now().strftime("%H:%M:%S")
                await channel.send(f"`[{timestamp}]` {message}")
    except Exception as e:
        print(f"Failed to send logs: {e}")

@bot.tree.command(name="deploy", description="🚀 [ADMIN] Create a new cloud instance for a user")
@app_commands.describe(user="The user to deploy for", os="The OS to deploy (ubuntu, debian, alpine, arch, kali, fedora)")
async def deploy(interaction: discord.Interaction, user: discord.User, os: str):
    try:
        if not await is_admin_role_only(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="🚫 Permission Denied",
                description="This command is restricted to administrators only.",
                color=0xFF0000), ephemeral=True)
            return

        os = os.lower()
        if os not in OS_OPTIONS:
            valid = "\n".join([f"{OS_OPTIONS[o]['emoji']} **{o}** - {OS_OPTIONS[o]['description']}" for o in OS_OPTIONS])
            await interaction.response.send_message(embed=discord.Embed(
                title="❌ Invalid OS Selection",
                description=f"**Available OS options:**\n{valid}",
                color=EMBED_COLOR), ephemeral=True)
            return

        if count_user_servers(str(user)) >= SERVER_LIMIT:
            await interaction.response.send_message(embed=discord.Embed(
                title="🚫 Server Limit Reached",
                description=f"{user.mention} already has {SERVER_LIMIT} instances.",
                color=0xFF0000), ephemeral=True)
            return

        os_data = OS_OPTIONS[os]
        root_password = generate_root_password()

        embed = discord.Embed(
            title=f"🚀 Launching {os_data['emoji']} {os_data['name']} Instance",
            description=f"```diff\n+ Preparing {os_data['name']} for {user.display_name}...\n```",
            color=EMBED_COLOR)
        embed.add_field(name="🛠️ System Info",
                        value=f"```RAM: {RAM_LIMIT}\nTunnel: Pinggy```",
                        inline=False)
        embed.set_footer(text="This may take 1-2 minutes...")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await animate_message(msg, embed, DEPLOY_ANIMATION, 2, "Initializing Deployment")

        try:
            container_id = subprocess.check_output(
                ["docker", "run", "-itd", "--privileged", "--memory", RAM_LIMIT, os_data["image"]]
            ).strip().decode('utf-8')
            await send_to_logs(f"🔧 {interaction.user.mention} deployed {os_data['emoji']} {os_data['name']} for {user.mention} (ID: `{container_id[:12]}`)")

            embed.description = "```diff\n+ Installing SSH and configuring root password...\n```"
            await msg.edit(embed=embed)
            setup_ssh_and_pinggy(container_id, root_password)

            embed.description = "```diff\n+ Establishing Pinggy tunnel...\n```"
            await msg.edit(embed=embed)
            await animate_message(msg, embed, LOADING_ANIMATION, 2, "Starting Pinggy tunnel")

            ssh_command = await wait_for_pinggy(container_id, max_wait=60)

            if ssh_command:
                admin_embed = discord.Embed(
                    title=f"🎉 {os_data['emoji']} {os_data['name']} Instance Ready!",
                    description=f"**Deployed for {user.mention}**\n\n**🔑 SSH Command:**\n```{ssh_command}```",
                    color=0x00FF00)
                admin_embed.add_field(name="🔐 Root Password", value=f"```{root_password}```", inline=False)
                admin_embed.add_field(name="📦 Container Info",
                                      value=f"```ID: {container_id[:12]}\nOS: {os_data['name']}\nRAM: {RAM_LIMIT}\nStatus: Running```",
                                      inline=False)
                await interaction.followup.send(embed=admin_embed, ephemeral=True)

                try:
                    user_embed = discord.Embed(
                        title=f"✨ Your {os_data['name']} Instance is Ready!",
                        description=f"**SSH Access:**\n```{ssh_command}```\n\nDeployed by: {interaction.user.mention}",
                        color=EMBED_COLOR)
                    user_embed.add_field(name="🔐 Root Password", value=f"```{root_password}```", inline=False)
                    user_embed.add_field(name="💡 Getting Started",
                                         value="```ssh root@<host> -p <port>\nPassword: (see above)```",
                                         inline=False)
                    await user.send(embed=user_embed)
                except discord.Forbidden:
                    pass

                add_to_database(str(user), container_id, ssh_command, root_password)

                final = discord.Embed(
                    title=f"✅ Deployment Complete! {random.choice(SUCCESS_ANIMATION)}",
                    description=f"**{os_data['emoji']} {os_data['name']}** created for {user.mention}!\n\n**SSH:**\n```{ssh_command}```\n**Password:** `{root_password}`",
                    color=0x00FF00)
                await msg.edit(embed=final)
            else:
                await msg.edit(embed=discord.Embed(
                    title=f"⚠️ Tunnel Failed {random.choice(ERROR_ANIMATION)}",
                    description="```diff\n- Pinggy tunnel did not return a URL\n- Rolling back deployment\n```",
                    color=0xFF0000))
                subprocess.run(["docker", "kill", container_id], stderr=subprocess.DEVNULL)
                subprocess.run(["docker", "rm", "-f", container_id], stderr=subprocess.DEVNULL)

        except subprocess.CalledProcessError as e:
            await msg.edit(embed=discord.Embed(
                title=f"❌ Deployment Failed {random.choice(ERROR_ANIMATION)}",
                description=f"```diff\n- Error:\n{e}\n```", color=0xFF0000))
            await send_to_logs(f"💥 Deployment failed for {user.mention}: {e}")

    except Exception as e:
        print(f"Error in deploy: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Critical Error",
                description="```diff\n- Unexpected error\n- Try again later\n```",
                color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="start", description="🟢 Start your cloud instance")
@app_commands.describe(container_id="Your instance ID (first 4+ characters)")
async def start_server(interaction: discord.Interaction, container_id: str):
    try:
        user = str(interaction.user)
        container_info = None
        old_ssh = None
        password = None

        if not os.path.exists(database_file):
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances!",
                color=EMBED_COLOR), ephemeral=True)
            return

        with open(database_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4 and user == parts[0] and container_id in parts[1]:
                    container_info = parts[1]
                    old_ssh = parts[2]
                    password = parts[3]
                    break

        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔍 Instance Not Found",
                description="No instance found with that ID owned by you!",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(title=f"🔌 Starting Instance {container_info[:12]}",
                              description="```diff\n+ Powering up...\n```",
                              color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await animate_message(msg, embed, LOADING_ANIMATION, 2, "Booting System")

        try:
            check = subprocess.run(["docker", "inspect", "--format={{.State.Status}}", container_info],
                                   capture_output=True, text=True)
            if check.returncode != 0:
                await msg.edit(embed=discord.Embed(
                    title="❌ Container Not Found",
                    description=f"Container `{container_info[:12]}` no longer exists!",
                    color=0xFF0000))
                remove_from_database(old_ssh)
                return

            subprocess.run(["docker", "start", container_info], check=True)

            embed.description = "```diff\n+ Restarting SSH and Pinggy tunnel...\n```"
            await msg.edit(embed=embed)
            setup_ssh_and_pinggy(container_info, password)
            new_ssh = await wait_for_pinggy(container_info, max_wait=60)

            if new_ssh:
                remove_from_database(old_ssh)
                add_to_database(user, container_info, new_ssh, password)
                try:
                    await interaction.user.send(embed=discord.Embed(
                        title=f"🟢 Instance Started {random.choice(SUCCESS_ANIMATION)}",
                        description=f"**New SSH Command:**\n```{new_ssh}```\n**Password:** `{password}`",
                        color=0x00FF00))
                except discord.Forbidden:
                    pass
                await msg.edit(embed=discord.Embed(
                    title=f"✅ Instance Online {random.choice(SUCCESS_ANIMATION)}",
                    description=f"**SSH:**\n```{new_ssh}```\n**Password:** `{password}`",
                    color=0x00FF00))
            else:
                await msg.edit(embed=discord.Embed(
                    title=f"⚠️ Tunnel Failed {random.choice(ERROR_ANIMATION)}",
                    description="Pinggy did not return a URL. Try again.",
                    color=0xFF0000))

        except subprocess.CalledProcessError as e:
            await msg.edit(embed=discord.Embed(
                title=f"❌ Start Failed {random.choice(ERROR_ANIMATION)}",
                description=f"```{e}```", color=0xFF0000))

    except Exception as e:
        print(f"Error in start: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="stop", description="🛑 Stop your cloud instance")
@app_commands.describe(container_id="Your instance ID (first 4+ characters)")
async def stop_server(interaction: discord.Interaction, container_id: str):
    try:
        user = str(interaction.user)
        container_info = None
        ssh_command = None

        if not os.path.exists(database_file):
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances!",
                color=EMBED_COLOR), ephemeral=True)
            return

        with open(database_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4 and user == parts[0] and container_id in parts[1]:
                    container_info = parts[1]
                    ssh_command = parts[2]
                    break

        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔍 Instance Not Found",
                description="No instance found with that ID owned by you!",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(title=f"⏳ Stopping Instance {container_info[:12]}",
                              description="```diff\n+ Shutting down...\n```",
                              color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await animate_message(msg, embed, LOADING_ANIMATION, 2, "Stopping Services")

        try:
            check = subprocess.run(["docker", "inspect", container_info], capture_output=True, text=True)
            if check.returncode != 0:
                await msg.edit(embed=discord.Embed(
                    title="❌ Container Not Found",
                    description=f"Container `{container_info[:12]}` doesn't exist!",
                    color=0xFF0000))
                remove_from_database(ssh_command)
                return

            subprocess.run(["docker", "stop", container_info], check=True)
            await msg.edit(embed=discord.Embed(
                title=f"🛑 Instance Stopped {random.choice(SUCCESS_ANIMATION)}",
                description=f"Instance `{container_info[:12]}` stopped successfully!",
                color=0x00FF00))
            await send_to_logs(f"🛑 {interaction.user.mention} stopped instance `{container_info[:12]}`")

        except subprocess.CalledProcessError as e:
            await msg.edit(embed=discord.Embed(
                title=f"❌ Stop Failed {random.choice(ERROR_ANIMATION)}",
                description=f"```{e.stderr if e.stderr else e.stdout}```",
                color=0xFF0000))

    except Exception as e:
        print(f"Error in stop: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="restart", description="🔄 Restart your cloud instance")
@app_commands.describe(container_id="Your instance ID (first 4+ characters)")
async def restart_server(interaction: discord.Interaction, container_id: str):
    try:
        user = str(interaction.user)
        container_info = None
        old_ssh = None
        password = None

        if not os.path.exists(database_file):
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances!",
                color=EMBED_COLOR), ephemeral=True)
            return

        with open(database_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4 and user == parts[0] and container_id in parts[1]:
                    container_info = parts[1]
                    old_ssh = parts[2]
                    password = parts[3]
                    break

        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔍 Instance Not Found",
                description="No instance found with that ID owned by you!",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(title=f"🔄 Restarting Instance {container_info[:12]}",
                              description="```diff\n+ Rebooting...\n```",
                              color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await animate_message(msg, embed, LOADING_ANIMATION, 2, "Restarting")

        try:
            check = subprocess.run(["docker", "inspect", container_info], capture_output=True, text=True)
            if check.returncode != 0:
                await msg.edit(embed=discord.Embed(
                    title="❌ Container Not Found",
                    description=f"Container `{container_info[:12]}` doesn't exist!",
                    color=0xFF0000))
                remove_from_database(old_ssh)
                return

            subprocess.run(["docker", "restart", container_info], check=True)

            embed.description = "```diff\n+ Refreshing SSH tunnel...\n```"
            await msg.edit(embed=embed)
            setup_ssh_and_pinggy(container_info, password)
            new_ssh = await wait_for_pinggy(container_info, max_wait=60)

            if new_ssh:
                remove_from_database(old_ssh)
                add_to_database(user, container_info, new_ssh, password)
                try:
                    await interaction.user.send(embed=discord.Embed(
                        title=f"🔄 Instance Restarted {random.choice(SUCCESS_ANIMATION)}",
                        description=f"**New SSH:**\n```{new_ssh}```\n**Password:** `{password}`",
                        color=0x00FF00))
                except discord.Forbidden:
                    pass
                await msg.edit(embed=discord.Embed(
                    title=f"✅ Restart Complete {random.choice(SUCCESS_ANIMATION)}",
                    description=f"**SSH:**\n```{new_ssh}```\n**Password:** `{password}`",
                    color=0x00FF00))
            else:
                await msg.edit(embed=discord.Embed(
                    title=f"⚠️ SSH Refresh Failed {random.choice(ERROR_ANIMATION)}",
                    description="Container restarted but Pinggy did not respond.",
                    color=0xFFA500))

            await send_to_logs(f"🔄 {interaction.user.mention} restarted `{container_info[:12]}`")

        except subprocess.CalledProcessError as e:
            await msg.edit(embed=discord.Embed(
                title=f"❌ Restart Failed {random.choice(ERROR_ANIMATION)}",
                description=f"```{e.stderr if e.stderr else e.stdout}```",
                color=0xFF0000))

    except Exception as e:
        print(f"Error in restart: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="regen-ssh", description="🔄 Regenerate SSH for your instance")
@app_commands.describe(container_id="Your instance ID (first 4+ characters)")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    try:
        user = str(interaction.user)
        container_info = None
        old_ssh = None
        password = None

        if not os.path.exists(database_file):
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances!",
                color=EMBED_COLOR), ephemeral=True)
            return

        with open(database_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4 and user == parts[0] and container_id in parts[1]:
                    container_info = parts[1]
                    old_ssh = parts[2]
                    password = parts[3]
                    break

        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔍 Instance Not Found",
                description="No instance found with that ID owned by you!",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(title="⚙️ Regenerating SSH",
                              description=f"```diff\n+ Refreshing tunnel for {container_info[:12]}...\n```",
                              color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await animate_message(msg, embed, LOADING_ANIMATION, 2, "Creating New Session")

        setup_ssh_and_pinggy(container_info, password)
        new_ssh = await wait_for_pinggy(container_info, max_wait=60)

        if new_ssh:
            remove_from_database(old_ssh)
            add_to_database(user, container_info, new_ssh, password)
            try:
                await interaction.user.send(embed=discord.Embed(
                    title=f"🔄 SSH Regenerated {random.choice(SUCCESS_ANIMATION)}",
                    description=f"**New SSH:**\n```{new_ssh}```\n**Password:** `{password}`",
                    color=0x00FF00))
            except discord.Forbidden:
                pass
            await msg.edit(embed=discord.Embed(
                title=f"✅ SSH Regenerated {random.choice(SUCCESS_ANIMATION)}",
                description=f"**SSH:**\n```{new_ssh}```\n**Password:** `{password}`",
                color=0x00FF00))
            await send_to_logs(f"🔄 {interaction.user.mention} regenerated SSH for `{container_info[:12]}`")
        else:
            await msg.edit(embed=discord.Embed(
                title=f"⚠️ SSH Regeneration Failed {random.choice(ERROR_ANIMATION)}",
                description="Pinggy did not respond. Try again later.",
                color=0xFFA500))

    except Exception as e:
        print(f"Error in regen-ssh: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="remove", description="❌ Permanently delete your cloud instance")
@app_commands.describe(container_id="Your instance ID (first 4+ characters)")
async def remove_server(interaction: discord.Interaction, container_id: str):
    try:
        user = str(interaction.user)
        container_info = None
        ssh_command = None

        if not os.path.exists(database_file):
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances!",
                color=EMBED_COLOR), ephemeral=True)
            return

        with open(database_file, 'r') as f:
            for line in f:
                parts = line.strip().split('|')
                if len(parts) >= 4 and user == parts[0] and container_id in parts[1]:
                    container_info = parts[1]
                    ssh_command = parts[2]
                    break

        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="🔍 Instance Not Found",
                description="No instance found with that ID owned by you!",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ Confirm Deletion",
            description=f"Permanently delete instance `{container_info[:12]}`?",
            color=0xFFA500)
        embed.set_footer(text="This action cannot be undone!")

        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)

            @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
            async def confirm(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.defer()
                try:
                    subprocess.run(["docker", "stop", container_info], stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", "-f", container_info], stderr=subprocess.DEVNULL)
                    remove_from_database(ssh_command)
                    await i.followup.send(embed=discord.Embed(
                        title=f"🗑️ Deleted {random.choice(SUCCESS_ANIMATION)}",
                        description=f"Instance `{container_info[:12]}` removed.",
                        color=0x00FF00), ephemeral=True)
                    await send_to_logs(f"❌ {i.user.mention} deleted `{container_info[:12]}`")
                except Exception as e:
                    await i.followup.send(f"Error: {e}", ephemeral=True)

            @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
            async def cancel(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.edit_message(embed=discord.Embed(
                    title="Cancelled",
                    description=f"`{container_info[:12]}` not deleted.",
                    color=0x00FF00), view=None)

        await interaction.response.send_message(embed=embed, view=ConfirmView(), ephemeral=True)

    except Exception as e:
        print(f"Error in remove: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="list", description="📜 List your cloud instances")
async def list_servers(interaction: discord.Interaction):
    try:
        user = str(interaction.user)
        servers = get_user_servers(user)

        if not servers:
            await interaction.response.send_message(embed=discord.Embed(
                title="📭 No Instances Found",
                description="You don't have any active instances.",
                color=EMBED_COLOR), ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 Your Instances ({len(servers)}/{SERVER_LIMIT})",
            color=EMBED_COLOR)

        for server in servers:
            parts = server.split('|')
            if len(parts) < 4:
                continue
            cid, ssh_cmd, pwd = parts[1], parts[2], parts[3]
            os_type = "Unknown"
            for os_id, os_data in OS_OPTIONS.items():
                if os_id in cid.lower():
                    os_type = f"{os_data['emoji']} {os_data['name']}"
                    break
            try:
                status = subprocess.check_output(
                    ["docker", "inspect", "--format={{.State.Status}}", cid],
                    stderr=subprocess.DEVNULL
                ).decode().strip()
                status_emoji = "🟢" if status == "running" else "🔴"
                status_text = f"{status_emoji} {status.capitalize()}"
            except Exception:
                status_text = "🔴 Unknown"

            embed.add_field(
                name=f"🖥️ `{cid[:12]}`",
                value=f"▫️ **OS**: {os_type}\n▫️ **Status**: {status_text}\n▫️ **SSH**: `{ssh_cmd}`\n▫️ **Password**: `{pwd}`",
                inline=False)

        embed.set_footer(text="Use /start, /stop, /restart or /remove with the instance ID")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        print(f"Error in list: {e}")
        try:
            await interaction.response.send_message(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000), ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="list-all", description="📜 [ADMIN] List all instances with usage")
async def list_all_servers(interaction: discord.Interaction):
    try:
        if not await is_admin(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="🚫 Permission Denied",
                description="Admins only.",
                color=0xFF0000), ephemeral=True)
            return

        await interaction.response.defer()
        servers = get_all_servers()
        container_stats = get_container_stats()
        host_stats = get_system_resources()

        embed = discord.Embed(title=f"📊 All Instances ({len(servers)})", color=EMBED_COLOR)

        cpu_e = "🟢" if host_stats['cpu'] < 70 else "🟡" if host_stats['cpu'] < 90 else "🔴"
        mem_e = "🟢" if host_stats['memory']['percent'] < 70 else "🟡" if host_stats['memory']['percent'] < 90 else "🔴"
        disk_e = "🟢" if host_stats['disk']['percent'] < 70 else "🟡" if host_stats['disk']['percent'] < 90 else "🔴"
        embed.add_field(
            name="🖥️ Host Resources",
            value=(f"{cpu_e} **CPU**: {host_stats['cpu']}%\n"
                   f"{mem_e} **RAM**: {host_stats['memory']['used']}GB / {host_stats['memory']['total']}GB ({host_stats['memory']['percent']}%)\n"
                   f"{disk_e} **Disk**: {host_stats['disk']['used']}GB / {host_stats['disk']['total']}GB ({host_stats['disk']['percent']}%)"),
            inline=False)

        if not servers:
            embed.add_field(name="📭 Empty", value="No instances.", inline=False)
            await interaction.followup.send(embed=embed)
            return

        for server in servers:
            parts = server.split('|')
            if len(parts) < 4:
                continue
            owner, cid, ssh_cmd, pwd = parts
            stats = container_stats.get(cid, {'cpu': '0.00%', 'mem_used': '0B', 'mem_limit': '0B'})
            try:
                status = subprocess.check_output(
                    ["docker", "inspect", "--format={{.State.Status}}", cid],
                    stderr=subprocess.DEVNULL
                ).decode().strip()
                s_emoji = "🟢" if status == "running" else "🔴"
                s_text = f"{s_emoji} {status.capitalize()}"
            except Exception:
                s_text = "🔴 Unknown"

            embed.add_field(
                name=f"🖥️ `{cid[:12]}`",
                value=(f"▫️ **Owner**: `{owner}`\n"
                       f"▫️ **Status**: {s_text}\n"
                       f"▫️ **CPU**: {stats['cpu']}\n"
                       f"▫️ **RAM**: {stats['mem_used']} / {stats['mem_limit']}\n"
                       f"▫️ **SSH**: `{ssh_cmd}`"),
                inline=False)

        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"Error in list-all: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="delete-user-container", description="❌ [ADMIN] Force-delete any container")
@app_commands.describe(container_id="The container ID to delete")
async def delete_user_container(interaction: discord.Interaction, container_id: str):
    try:
        if not await is_admin_role_only(interaction):
            await interaction.response.send_message(embed=discord.Embed(
                title="🚫 Permission Denied",
                description="Admins only.",
                color=0xFF0000), ephemeral=True)
            return

        owner, container_info, ssh_command, pwd = get_container_info_by_id(container_id)
        if not container_info:
            await interaction.response.send_message(embed=discord.Embed(
                title="❌ Not Found",
                description=f"No container with ID `{container_id[:12]}`.",
                color=0xFF0000), ephemeral=True)
            return

        embed = discord.Embed(
            title="⚠️ Confirm Force Deletion",
            description=f"Force delete `{container_info[:12]}`?\n**Owner**: {owner}",
            color=0xFFA500)

        class AdminConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)

            @discord.ui.button(label="☠️ Force Delete", style=discord.ButtonStyle.red)
            async def confirm(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.defer()
                try:
                    subprocess.run(["docker", "stop", container_info], stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", "-f", container_info], stderr=subprocess.DEVNULL)
                    remove_from_database(ssh_command)
                    await i.followup.send(embed=discord.Embed(
                        title=f"☠️ Deleted {random.choice(SUCCESS_ANIMATION)}",
                        description=f"Container `{container_info[:12]}` removed.",
                        color=0x00FF00), ephemeral=True)
                    await send_to_logs(f"💥 {i.user.mention} force-deleted `{container_info[:12]}` (owner: {owner})")
                except Exception as e:
                    await i.followup.send(f"Error: {e}", ephemeral=True)

            @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.grey)
            async def cancel(self, i: discord.Interaction, button: discord.ui.Button):
                await i.response.edit_message(embed=discord.Embed(
                    title="Cancelled",
                    description=f"`{container_info[:12]}` not deleted.",
                    color=0x00FF00), view=None)

        await interaction.response.send_message(embed=embed, view=AdminConfirmView(), ephemeral=True)

    except Exception as e:
        print(f"Error in delete-user-container: {e}")
        try:
            await interaction.followup.send(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000))
        except Exception:
            pass

@bot.tree.command(name="resources", description="📊 Show host system resources")
async def resources_command(interaction: discord.Interaction):
    try:
        r = get_system_resources()
        cpu_e = "🟢" if r['cpu'] < 70 else "🟡" if r['cpu'] < 90 else "🔴"
        mem_e = "🟢" if r['memory']['percent'] < 70 else "🟡" if r['memory']['percent'] < 90 else "🔴"
        disk_e = "🟢" if r['disk']['percent'] < 70 else "🟡" if r['disk']['percent'] < 90 else "🔴"

        embed = discord.Embed(title="📊 Host System Resources", color=EMBED_COLOR)
        embed.add_field(name=f"{cpu_e} CPU", value=f"```{r['cpu']}%```", inline=True)
        embed.add_field(name=f"{mem_e} RAM",
                        value=f"```{r['memory']['used']}GB / {r['memory']['total']}GB ({r['memory']['percent']}%)```",
                        inline=True)
        embed.add_field(name=f"{disk_e} Disk",
                        value=f"```{r['disk']['used']}GB / {r['disk']['total']}GB ({r['disk']['percent']}%)```",
                        inline=True)

        score = (100 - r['cpu']) * 0.3 + (100 - r['memory']['percent']) * 0.4 + (100 - r['disk']['percent']) * 0.3
        health = "🌟 Excellent" if score > 80 else "👍 Good" if score > 60 else "⚠️ Moderate" if score > 40 else "🚨 Critical"
        embed.add_field(name="Health", value=health, inline=False)

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        print(f"Error in resources: {e}")
        try:
            await interaction.response.send_message(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000), ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="help", description="ℹ️ Show help message")
async def help_command(interaction: discord.Interaction):
    try:
        embed = discord.Embed(title="✨ Cloud Instance Bot Help",
                              description="All available commands:",
                              color=EMBED_COLOR)

        user_cmds = [
            ("📜 `/list`", "List your instances"),
            ("🟢 `/start <id>`", "Start your instance"),
            ("🛑 `/stop <id>`", "Stop your instance"),
            ("🔄 `/restart <id>`", "Restart your instance"),
            ("🔄 `/regen-ssh <id>`", "Regenerate SSH connection"),
            ("🗑️ `/remove <id>`", "Delete an instance"),
            ("📊 `/resources`", "Show host resources"),
            ("🏓 `/ping`", "Check bot latency"),
            ("ℹ️ `/help`", "Show this help")
        ]
        admin_cmds = [
            ("🚀 `/deploy user: @user os: <os>`", "[ADMIN] Create instance"),
            ("📜 `/list-all`", "[ADMIN] List all instances"),
            ("❌ `/delete-user-container <id>`", "[ADMIN] Force-delete container")
        ]

        if await is_admin(interaction):
            for c, d in admin_cmds:
                embed.add_field(name=c, value=d, inline=False)
            embed.add_field(name="\u200b", value="**─── User Commands ───**", inline=False)

        for c, d in user_cmds:
            embed.add_field(name=c, value=d, inline=False)

        os_info = "\n".join([f"{OS_OPTIONS[o]['emoji']} **{o}** - {OS_OPTIONS[o]['description']}" for o in OS_OPTIONS])
        embed.add_field(name="🖥️ Available OS", value=os_info, inline=False)
        embed.set_footer(text="💜 Need help? Contact staff!")

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        print(f"Error in help: {e}")
        try:
            await interaction.response.send_message(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000), ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="ping", description="🏓 Check bot latency")
async def ping_command(interaction: discord.Interaction):
    try:
        latency = round(bot.latency * 1000)
        if latency < 100:
            emoji, status = "⚡", "Excellent"
        elif latency < 300:
            emoji, status = "🏓", "Good"
        elif latency < 500:
            emoji, status = "🐢", "Slow"
        else:
            emoji, status = "🐌", "Laggy"

        embed = discord.Embed(title=f"{emoji} Pong!",
                              description=f"**Latency**: {latency}ms\n**Status**: {status}",
                              color=EMBED_COLOR)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        print(f"Error in ping: {e}")
        try:
            await interaction.response.send_message(embed=discord.Embed(
                title="💥 Error", description="```Something went wrong.```", color=0xFF0000), ephemeral=True)
        except Exception:
            pass

bot.run(TOKEN)
