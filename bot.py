monitor_active = False
monitor_thread = None

def reward_win_monitor(chat_id):
    import time
    import json
    from urllib.parse import quote_plus
    last_reward = None
    last_win = None
    peer_name = None
    log_dir = "/root/rl-swarm/logs"
    while True:
        global monitor_active
        if not monitor_active:
            break
        try:
            # Discover peer name
            import glob
            log_files = glob.glob(f"{log_dir}/training_*.log")
            if log_files:
                log_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                fname = os.path.basename(log_files[0])
                if fname.startswith("training_") and fname.endswith(".log"):
                    peer_name_raw = fname[len("training_"):-len(".log")]
                    peer_name = peer_name_raw.replace("_", " ")
            if not peer_name:
                time.sleep(10)
                continue
            api_url = f"https://dashboard.gensyn.ai/api/v1/peer?name={quote_plus(peer_name)}"
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                reward = data.get("reward", 0)
                score = data.get("score", 0)
                # Alert if reward or win increased
                reward_diff = None
                win_diff = None
                if last_reward is not None and reward > last_reward:
                    reward_diff = reward - last_reward
                if last_win is not None and score > last_win:
                    win_diff = score - last_win
                last_reward = reward
                last_win = score
                msg = []
                if reward_diff:
                    msg.append(f"🎁 reward {reward}+{reward_diff}")
                if win_diff:
                    msg.append(f"🏆 win {score}+{win_diff}")
                if msg:
                    bot.send_message(chat_id, " ".join(msg))
            time.sleep(600)  # 10 min
        except Exception as e:
            logging.error(f"Monitor error: {str(e)}")
            time.sleep(30)
import os
import time
import threading
import subprocess
import logging
import requests
import shutil
from datetime import datetime, timedelta
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_CONFIG = "/root/bot_config.env"
WG_CONFIG_PATH = "/etc/wireguard/wg0.conf"
SWARM_PEM_PATH = "/root/rl-swarm/swarm.pem"
USER_DATA_PATH = "/root/rl-swarm/modal-login/temp-data/userData.json"
USER_APIKEY_PATH = "/root/rl-swarm/modal-login/temp-data/userApiKey.json"
BACKUP_USERDATA_DIR = "/root/node-manager/backup-userdata"
SYNC_BACKUP_DIR = "/root/node-manager/sync-backup"
GENSYN_LOG_PATH = "/root/rl-swarm/logs/swarm_launcher.log"
WANDB_LOG_DIR = "/root/rl-swarm/logs/wandb"

logging.basicConfig(
    filename='/root/bot_error.log',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

with open(BOT_CONFIG) as f:
    lines = f.read().strip().split("\n")
    config = dict(line.split("=", 1) for line in lines if "=" in line)

BOT_TOKEN = config["BOT_TOKEN"]
USER_ID = int(config["USER_ID"])

bot = TeleBot(BOT_TOKEN)
waiting_for_pem = False
login_in_progress = False
login_lock = threading.Lock()
tmate_running = False
last_action_time = {}
COOLDOWN_SECONDS = 2

os.makedirs(BACKUP_USERDATA_DIR, exist_ok=True)
os.makedirs(SYNC_BACKUP_DIR, exist_ok=True)

def get_menu():
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🌐 Check IP", callback_data="check_ip"),
        InlineKeyboardButton("📶 VPN ON", callback_data="vpn_on"),
        InlineKeyboardButton("📴 VPN OFF", callback_data="vpn_off")
    )
    markup.row(
        InlineKeyboardButton("📊 Gensyn Status", callback_data="gensyn_status"),
        InlineKeyboardButton("🔑 Gensyn Login", callback_data="gensyn_login")
    )
    markup.row(
        InlineKeyboardButton("▶️ Start Gensyn", callback_data="start_gensyn"),
        InlineKeyboardButton("🔁 Set Auto-Start", callback_data="set_autostart")
    )
    markup.row(
        InlineKeyboardButton("🛑 Kill Gensyn", callback_data="kill_gensyn")
    )
    terminal_label = "🖥️ Terminal: ON" if tmate_running else "🖥️ Terminal: OFF"
    markup.row(
        InlineKeyboardButton(terminal_label, callback_data="toggle_tmate")
    )
    markup.row(
        InlineKeyboardButton("🗂️ Get Backup", callback_data="get_backup")
    )
    markup.row(
        InlineKeyboardButton("🔄 Update", callback_data="update_menu")
    )
    markup.row(
        InlineKeyboardButton("▶️ Start Monitor", callback_data="start_monitor"),
        InlineKeyboardButton("⏹️ Stop Monitor", callback_data="stop_monitor")
    )
    return markup

def start_vpn():
    try:
        subprocess.run(['wg-quick', 'up', 'wg0'], check=True)
        return True, "✅ VPN enabled"
    except subprocess.CalledProcessError as e:
        if "already exists" in str(e):
            return True, "⚠️ VPN already enabled"
        return False, f"❌ VPN failed to start: {str(e)}"

def stop_vpn():
    try:
        subprocess.run(['wg-quick', 'down', 'wg0'], check=True)
        return True, "❌ VPN disabled"
    except subprocess.CalledProcessError as e:
        if "is not a WireGuard interface" in str(e):
            return True, "⚠️ VPN already disabled"
        return False, f"❌ VPN failed to stop: {str(e)}"

def backup_user_data_sync():
    try:
        for src, name in [(USER_DATA_PATH, "userData.json"), (USER_APIKEY_PATH, "userApiKey.json")]:
            dst = os.path.join(SYNC_BACKUP_DIR, name)
            if os.path.exists(src):
                shutil.copy(src, dst)
        return True
    except Exception as e:
        logging.error(f"Sync backup error: {str(e)}")
        return False

def periodic_sync_backup():
    while True:
        try:
            backup_user_data_sync()
            time.sleep(60)
        except Exception as e:
            logging.error(f"Periodic sync backup thread error: {str(e)}")
            time.sleep(10)

threading.Thread(target=periodic_sync_backup, daemon=True).start()

def backup_user_data():
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for path, name in [(USER_DATA_PATH, "userData.json"), (USER_APIKEY_PATH, "userApiKey.json")]:
            if os.path.exists(path):
                backup_file = f"{name.split('.')[0]}_{timestamp}.json"
                shutil.copy(path, os.path.join(BACKUP_USERDATA_DIR, backup_file))
                latest_file = f"{name.split('.')[0]}_latest.json"
                shutil.copy(path, os.path.join(BACKUP_USERDATA_DIR, latest_file))
        return True
    except Exception as e:
        logging.error(f"Backup error: {str(e)}")
        return False

def setup_autostart(chat_id):
    try:
        os.makedirs(BACKUP_USERDATA_DIR, exist_ok=True)
        if os.path.exists(USER_DATA_PATH):
            shutil.copy(USER_DATA_PATH, os.path.join(BACKUP_USERDATA_DIR, "userData.json"))
        if os.path.exists(USER_APIKEY_PATH):
            shutil.copy(USER_APIKEY_PATH, os.path.join(BACKUP_USERDATA_DIR, "userApiKey.json"))
        service_content = f"""[Unit]
Description=Gensyn Swarm Service
After=network.target

[Service]
Type=forking
User=root
WorkingDirectory=/root/rl-swarm
ExecStartPre=/usr/bin/wg-quick up wg0
ExecStartPre=/bin/bash -c 'mkdir -p /root/rl-swarm/modal-login/temp-data && cp {BACKUP_USERDATA_DIR}/userData.json {USER_DATA_PATH} || true'
ExecStartPre=/bin/bash -c 'cp {BACKUP_USERDATA_DIR}/userApiKey.json {USER_APIKEY_PATH} || true'
ExecStart=/bin/bash -c 'screen -dmS gensyn bash -c "python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh"'
ExecStopPost=/usr/bin/wg-quick down wg0
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
        with open("/etc/systemd/system/gensyn.service", "w") as f:
            f.write(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "gensyn.service"], check=True)
        subprocess.run(["systemctl", "start", "gensyn.service"], check=True)
        bot.send_message(chat_id, "✅ Auto-start configured! Gensyn and VPN will now start on boot.")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error setting up auto-start: {str(e)}")

def gensyn_soft_update(chat_id):
    backup_paths = [
        USER_DATA_PATH,
        USER_APIKEY_PATH
    ]
    backup_dir = "/root/node-manager/soft-update-backup"
    os.makedirs(backup_dir, exist_ok=True)
    try:
        for path in backup_paths:
            if os.path.exists(path):
                shutil.copy(path, backup_dir)
        bot.send_message(chat_id, "Backup done. Killing Gensyn...")
        # Only kill gensyn screen if present
        if check_gensyn_screen_running():
            subprocess.run("screen -S gensyn -X quit", shell=True, check=True)
            bot.send_message(chat_id, "Gensyn killed.")
        else:
            bot.send_message(chat_id, "No gensyn screen found. Proceeding with update...")
        bot.send_message(chat_id, "Updating (git switch/reset/clean/pull)...")
        update_cmd = (
            "cd /root/rl-swarm && "
            "git switch main && "
            "git reset --hard && "
            "git clean -fd && "
            "git pull origin main"
        )
        result = subprocess.run(update_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            msg = "Update done. Restarting node..."
        else:
            msg = "Update failed. Restoring backup..."
        for filename in ["userData.json", "userApiKey.json"]:
            src = os.path.join(backup_dir, filename)
            dst = f"/root/rl-swarm/modal-login/temp-data/{filename}"
            if os.path.exists(src):
                shutil.copy(src, dst)
        subprocess.run("cd /root/rl-swarm && screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'", shell=True)
        bot.send_message(chat_id, f"{msg}\nGensyn started.")
    except Exception as e:
        bot.send_message(chat_id, f"Soft update failed: {str(e)}")

def gensyn_hard_update(chat_id):
    backup_paths = [
        SWARM_PEM_PATH,
        USER_DATA_PATH,
        USER_APIKEY_PATH
    ]
    backup_dir = "/root/node-manager/hard-update-backup"
    os.makedirs(backup_dir, exist_ok=True)
    try:
        for path in backup_paths:
            if os.path.exists(path):
                shutil.copy(path, backup_dir)
        bot.send_message(chat_id, "Backup done. Killing Gensyn...")
        # Only kill gensyn screen if present
        if check_gensyn_screen_running():
            subprocess.run("screen -S gensyn -X quit", shell=True, check=True)
            bot.send_message(chat_id, "Gensyn killed.")
        else:
            bot.send_message(chat_id, "No gensyn screen found. Proceeding with update...")
        bot.send_message(chat_id, "Cloning repo...")
        subprocess.run("rm -rf /root/rl-swarm", shell=True)
        result = subprocess.run("git clone https://github.com/shairkhan2/rl-swarm.git /root/rl-swarm", shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            msg = "Hard update done. Restoring backup..."
        else:
            msg = "Hard update failed. Restoring backup to last state."
        for filename in ["swarm.pem", "userData.json", "userApiKey.json"]:
            src = os.path.join(backup_dir, filename)
            dst = f"/root/rl-swarm/{filename}" if filename == "swarm.pem" else f"/root/rl-swarm/modal-login/temp-data/{filename}"
            if os.path.exists(src):
                shutil.copy(src, dst)
        subprocess.run("cd /root/rl-swarm && screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'", shell=True)
        bot.send_message(chat_id, f"{msg}\nGensyn started.")
    except Exception as e:
        bot.send_message(chat_id, f"Hard update failed: {str(e)}")

def send_backup_files(chat_id):
    files = [
        SWARM_PEM_PATH,
        USER_DATA_PATH,
        USER_APIKEY_PATH
    ]
    for fpath in files:
        if os.path.exists(fpath):
            with open(fpath, "rb") as f:
                bot.send_document(chat_id, f)
        else:
            bot.send_message(chat_id, f"{os.path.basename(fpath)} not found.")

def check_gensyn_screen_running():
    """
    Check if the 'gensyn' screen session is running
    Returns True if running, False otherwise
    """
    try:
        result = subprocess.run("screen -ls", shell=True, capture_output=True, text=True)
        return "gensyn" in result.stdout
    except Exception as e:
        logging.error(f"Error checking screen: {str(e)}")
        return False

def start_gensyn_session(chat_id, use_sync_backup=True, fresh_start=False):
    # Check if gensyn screen is already running
    if check_gensyn_screen_running():
        bot.send_message(chat_id, "⚠️ Gensyn already running!")
        return
    
    # If fresh_start is True, skip swarm.pem check and just start the node
    if fresh_start:
        bot.send_message(chat_id, "🚀 Starting fresh node. swarm.pem will be generated automatically...")
        backup_found = False
        if use_sync_backup:
            for file in ["userData.json", "userApiKey.json"]:
                backup_path = os.path.join(SYNC_BACKUP_DIR, file)
                target_path = USER_DATA_PATH if file == "userData.json" else USER_APIKEY_PATH
                if os.path.exists(backup_path):
                    shutil.copy(backup_path, target_path)
                    backup_found = True
        commands = [
            "cd /root/rl-swarm",
            "screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'"
        ]
        try:
            subprocess.run("; ".join(commands), shell=True, check=True)
            bot.send_message(chat_id, "✅ Fresh node started in screen session 'gensyn'. swarm.pem will be generated.")
        except subprocess.CalledProcessError as e:
            bot.send_message(chat_id, f"❌ Error starting fresh node: {str(e)}")
        return
    # Check if swarm.pem exists (for non-fresh start)
    elif not os.path.exists(SWARM_PEM_PATH):
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Upload old swarm.pem", callback_data="upload_pem"),
            InlineKeyboardButton("Start Fresh Node", callback_data="start_fresh")
        )
        bot.send_message(
            chat_id,
            "❗ swarm.pem not found! If you have a backup of your old Gensyn node, please upload it.\nOtherwise, start fresh (new node, new keys).",
            reply_markup=markup
        )
        return
    try:
        backup_found = False
        if use_sync_backup:
            for file in ["userData.json", "userApiKey.json"]:
                backup_path = os.path.join(SYNC_BACKUP_DIR, file)
                target_path = USER_DATA_PATH if file == "userData.json" else USER_APIKEY_PATH
                if os.path.exists(backup_path):
                    shutil.copy(backup_path, target_path)
                    backup_found = True
        commands = [
            "cd /root/rl-swarm",
            "screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'"
        ]
        subprocess.run("; ".join(commands), shell=True, check=True)
        if backup_found and use_sync_backup:
            bot.send_message(chat_id, "✅ Login backup restored. Gensyn started in screen session 'gensyn'")
        else:
            bot.send_message(chat_id, "✅ Gensyn started in screen session 'gensyn'")
    except subprocess.CalledProcessError as e:
        bot.send_message(chat_id, f"❌ Error starting Gensyn: {str(e)}")

def get_gensyn_log_status(log_path=GENSYN_LOG_PATH):
    """
    Parses the last 50 lines of the Gensyn log to find the latest activity.
    Returns a dictionary with timestamp, joining, and starting round info, or None.
    """
    try:
        if not os.path.exists(log_path):
            return None

        with open(log_path, "r") as f:
            lines = f.readlines()[-50:]

        latest_ts = None
        joining_round = None
        starting_round = None

        for line in reversed(lines):
            if "] - " in line:
                try:
                    ts_str = line.split("]")[0][1:]
                    ts = datetime.strptime(ts_str.split(",")[0], "%Y-%m-%d %H:%M:%S")
                    msg = line.split("] - ", 1)[-1].strip()

                    if not latest_ts:
                        latest_ts = ts
                    if "Joining round" in msg and not joining_round:
                        joining_round = msg
                    if "Starting round" in msg and not starting_round:
                        starting_round = msg
                    
                    if latest_ts and joining_round and starting_round:
                        break
                except (ValueError, IndexError):
                    continue
        
        return {
            "timestamp": latest_ts,
            "joining": joining_round,
            "starting": starting_round,
        }
    except Exception as e:
        logging.error(f"Error reading Gensyn log: {str(e)}")
        return None

def check_gensyn_api():
    """
    Checks if the Gensyn API is online by making a request to localhost:3000
    Returns True if online, False otherwise
    """
    try:
        response = requests.get("http://localhost:3000", timeout=5)
        if response.status_code == 200:
            # Check for various indicators that the Gensyn service is running
            response_text = response.text.lower()
            gensyn_indicators = [
                "sign in to gensyn",
                "gensyn",
                "__next_error__",
                "<!doctype html>",
                "<html"
            ]
            
            # If any of these indicators are found, the service is running
            for indicator in gensyn_indicators:
                if indicator in response_text:
                    return True
        return False
    except Exception as e:
        logging.error(f"Error checking Gensyn API: {str(e)}")
        return False

def format_gensyn_status():
    """
    Formats the complete Gensyn status message, including peer info and EQA address
    """
    import glob
    import json
    from urllib.parse import quote_plus
    from web3 import Web3
    from datetime import date

    EOA_CACHE_FILE = "/root/node-manager/eoa_cache.json"
    ALCHEMY_RPC = "https://gensyn-testnet.g.alchemy.com/v2/TD5tr7mo4VfXlSaolFlSr3tL70br2M9J"
    CONTRACT_ADDRESS = "0xFaD7C5e93f28257429569B854151A1B8DCD404c2"
    ABI = [
        {
            "name": "getEoa",
            "type": "function",
            "stateMutability": "view",
            "inputs": [{"name": "peerIds", "type": "string[]"}],
            "outputs": [{"name": "", "type": "address[]"}]
        }
    ]

    def fetch_eoa_mapping(w3, contract, peer_ids):
        today = str(date.today())
        if os.path.exists(EOA_CACHE_FILE):
            try:
                with open(EOA_CACHE_FILE) as f:
                    data = json.load(f)
                    if data.get("date") == today:
                        return data.get("mapping", {})
            except Exception:
                pass
        try:
            addresses = contract.functions.getEoa(peer_ids).call()
            mapping = {pid: eoa for pid, eoa in zip(peer_ids, addresses)}
            with open(EOA_CACHE_FILE, "w") as f:
                json.dump({"date": today, "mapping": mapping}, f, indent=4)
            return mapping
        except Exception as e:
            return {pid: f"Error: {str(e)}" for pid in peer_ids}

    # Check API status by making a request to localhost:3000
    try:
        response = requests.get("http://localhost:3000", timeout=3)
        if "Sign in to Gensyn" in response.text:
            api_status = "localhost:3000: ✅ Running"
        else:
            api_status = "localhost:3000: ❌ Stopped"
    except Exception:
        api_status = "localhost:3000: ❌ Stopped"

    # Check log status
    log_data = get_gensyn_log_status()
    log_status_lines = []
    if log_data and any(log_data.values()):
        if log_data.get("timestamp"):
            ts = log_data["timestamp"]
            delta_min = int((datetime.utcnow() - ts).total_seconds() / 60)
            log_status_lines.append(f"🕰️ Last Activity: {delta_min} mins ago")
        if log_data.get("joining"):
            joining_text = log_data["joining"]
            # Extract round number
            import re
            m = re.search(r"(\d+)", joining_text)
            round_num = m.group(1) if m else joining_text
            log_status_lines.append(f"🤝 Joining: 🐝 Round {round_num}")
        if log_data.get("starting"):
            starting_text = log_data["starting"]
            m = re.search(r"(\d+/\d+)", starting_text)
            round_str = m.group(1) if m else starting_text
            log_status_lines.append(f"▶️ Starting: Round {round_str}")
    log_status = "\n".join(log_status_lines) if log_status_lines else "Round: No data found"

    # Peer Name Discovery
    peer_name = None
    log_dir = "/root/rl-swarm/logs"
    try:
        log_files = glob.glob(f"{log_dir}/training_*.log")
        if log_files:
            log_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            fname = os.path.basename(log_files[0])
            if fname.startswith("training_") and fname.endswith(".log"):
                peer_name_raw = fname[len("training_"):-len(".log")]
                peer_name = peer_name_raw.replace("_", " ")
    except Exception:
        peer_name = None

    peer_info_lines = []
    if peer_name:
        api_url = f"https://dashboard.gensyn.ai/api/v1/peer?name={quote_plus(peer_name)}"
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                peerId = data.get("peerId", "?")
                reward = data.get("reward", "?")
                score = data.get("score", "?")
                online = data.get("online", False)
                online_status = "✅ Online" if online else "❌ Offline"

                # Get EQA address from smart contract (batch)
                w3 = Web3(Web3.HTTPProvider(ALCHEMY_RPC))
                contract = w3.eth.contract(address=Web3.to_checksum_address(CONTRACT_ADDRESS), abi=ABI)
                eoa_mapping = fetch_eoa_mapping(w3, contract, [peerId])
                eqa = eoa_mapping.get(peerId, "?")

                now = datetime.utcnow()
                reward_time = f"[{int((now-datetime.utcfromtimestamp(data.get('rewardTimestamp', time.time()))).total_seconds()/60)}m ago]" if data.get('rewardTimestamp') else ""
                score_time = f"[{int((now-datetime.utcfromtimestamp(data.get('scoreTimestamp', time.time()))).total_seconds()/60)}m ago]" if data.get('scoreTimestamp') else ""

                # Send pet name, peerId, EQA in plain copy-paste format
                peer_info_lines.append("")
                peer_info_lines.append(f"Pet Name: {peer_name}")
                peer_info_lines.append(f"Peer ID: {peerId}")
                peer_info_lines.append(f"EQA: {eqa}")
                peer_info_lines.append(f"Reward: {reward} {reward_time}")
                peer_info_lines.append(f"Win: {score}{score_time}")
            else:
                peer_info_lines.append(f"Peer info fetch failed: {r.status_code}")
        except Exception as e:
            peer_info_lines.append(f"Peer info error: {str(e)}")
    else:
        peer_info_lines.append("No peer name found.")

    return f"{api_status}\n\n{log_status}\n" + "\n".join(peer_info_lines)

@bot.message_handler(commands=['start'])
def start_handler(message):
    if message.from_user.id == USER_ID:
        bot.send_message(message.chat.id, f"🤖 Bot ready.", reply_markup=get_menu())

@bot.message_handler(commands=['who'])
def who_handler(message):
    if message.from_user.id == USER_ID:
        bot.send_message(message.chat.id, f"👤 This is your VPN Bot")

@bot.message_handler(func=lambda message: message.from_user.id == USER_ID)
def handle_credentials(message):
    global login_in_progress
    if not login_in_progress:
        return
    text = message.text.strip()
    if "@" in text and "." in text and len(text) > 5:
        with open("/root/email.txt", "w") as f:
            f.write(text)
        bot.send_message(message.chat.id, "✅ Email received. Check your email for OTP.")
        return
    if text.isdigit() and len(text) == 6:
        with open("/root/otp.txt", "w") as f:
            f.write(text)
        bot.send_message(message.chat.id, "✅ OTP received. Continuing login...")
        return
    bot.send_message(message.chat.id, "⚠️ Please send either:\n- Your email address\n- 6-digit OTP code")

@bot.message_handler(commands=['gensyn_status'])
def gensyn_status_handler(message):
    if message.from_user.id != USER_ID:
        return
    try:
        status_message = format_gensyn_status()
        bot.send_message(message.chat.id, status_message, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error in gensyn_status_handler: {str(e)}")
        bot.send_message(message.chat.id, "❌ Error getting status. Check logs.")

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    global waiting_for_pem
    global login_in_progress
    global tmate_running
    global last_action_time
    global monitor_active
    global monitor_thread
    # Ensure globals are initialized
    try:
        monitor_active
    except NameError:
        monitor_active = False
    try:
        monitor_thread
    except NameError:
        monitor_thread = None
    user_id = call.from_user.id
    now = time.time()
    
    if user_id != USER_ID:
        return
        
    if user_id in last_action_time and (now - last_action_time[user_id]) < COOLDOWN_SECONDS:
        return
    last_action_time[user_id] = now

    try:
        if call.data == 'check_ip':
            try:
                ip = requests.get('https://api.ipify.org', timeout=10).text.strip()
                bot.send_message(call.message.chat.id, f"🌐 Current Public IP: {ip}")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Error checking IP: {str(e)}")
                
        elif call.data == 'gensyn_login':
            global login_lock
            with login_lock:
                if login_in_progress:
                    bot.send_message(call.message.chat.id, "⚠️ Login already in progress. Please complete current login first.")
                    return
                try:
                    for path in ["/root/email.txt", "/root/otp.txt"]:
                        if os.path.exists(path):
                            os.remove(path)
                    login_in_progress = True
                    bot.send_message(call.message.chat.id, "🚀 Starting GENSYN login...")
                    bot.send_message(call.message.chat.id, "📧 Please send your email address")
                    bot.send_message(call.message.chat.id, "🔐 Later, just send the 6-digit OTP code when received")
                    venv_python = "/root/node-manager/.venv/bin/python3"
                    signup_script = "/root/node-manager/signup.py"
                    venv_site_packages = "/root/node-manager/.venv/lib/python3.12/site-packages"
                    with open("/root/signup.log", "w") as f:
                        subprocess.Popen(
                            [venv_python, signup_script],
                            stdout=f,
                            stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONPATH": venv_site_packages}
                        )
                    threading.Thread(target=check_login_timeout, args=(call.message.chat.id,)).start()
                except Exception as e:
                    login_in_progress = False
                    bot.send_message(call.message.chat.id, f"❌ Error starting login: {str(e)}")
                    
        elif call.data == 'vpn_on':
            success, message = start_vpn()
            bot.send_message(call.message.chat.id, message)
            
        elif call.data == 'vpn_off':
            success, message = stop_vpn()
            bot.send_message(call.message.chat.id, message)
            
        elif call.data == 'gensyn_status':
            try:
                status_message = format_gensyn_status()
                markup = get_menu()
                bot.send_message(call.message.chat.id, status_message, parse_mode="Markdown", reply_markup=markup)
            except Exception as e:
                logging.error(f"Error in gensyn_status callback: {str(e)}")
                bot.send_message(call.message.chat.id, "❌ Error getting status. Check logs.")

        elif call.data == 'start_monitor':
            try:
                if not monitor_active:
                    monitor_active = True
                    if monitor_thread is None or not monitor_thread.is_alive():
                        monitor_thread = threading.Thread(target=reward_win_monitor, args=(call.message.chat.id,), daemon=True)
                        monitor_thread.start()
                    bot.send_message(call.message.chat.id, "🎯 Monitor started.")
                else:
                    bot.send_message(call.message.chat.id, "Monitor already running.")
            except Exception as e:
                logging.error(f"Monitor start error: {str(e)}")
                bot.send_message(call.message.chat.id, f"❌ Failed to start monitor: {str(e)}")

        elif call.data == 'stop_monitor':
            try:
                monitor_active = False
                bot.send_message(call.message.chat.id, "⏹️ Monitor stopped.")
            except Exception as e:
                logging.error(f"Monitor stop error: {str(e)}")
                bot.send_message(call.message.chat.id, f"❌ Failed to stop monitor: {str(e)}")
                
        elif call.data == 'start_gensyn':
            backup_exists = (
                os.path.exists(os.path.join(SYNC_BACKUP_DIR, "userData.json")) and
                os.path.exists(os.path.join(SYNC_BACKUP_DIR, "userApiKey.json"))
            )
            if backup_exists:
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton("Run with Login Backup", callback_data="start_gensyn_with_backup"),
                    InlineKeyboardButton("Run Without Login Backup", callback_data="start_gensyn_no_backup")
                )
                bot.send_message(call.message.chat.id, "Login backup found. How do you want to start?", reply_markup=markup)
            else:
                start_gensyn_session(call.message.chat.id, use_sync_backup=False)
                
        elif call.data == 'start_gensyn_with_backup':
            start_gensyn_session(call.message.chat.id, use_sync_backup=True)
            
        elif call.data == 'start_gensyn_no_backup':
            start_gensyn_session(call.message.chat.id, use_sync_backup=False)
            
        elif call.data == 'start_fresh':
            start_gensyn_session(call.message.chat.id, use_sync_backup=False, fresh_start=True)
            
        elif call.data == 'upload_pem':
            waiting_for_pem = True
            bot.send_message(call.message.chat.id, "⬆️ Please send the swarm.pem file now...")
            
        elif call.data == 'set_autostart':
            setup_autostart(call.message.chat.id)
            
        elif call.data == 'kill_gensyn':
            try:
                subprocess.run("screen -S gensyn -X quit", shell=True, check=True)
                bot.send_message(call.message.chat.id, "🛑 gensyn screen killed (and all child processes).")
            except subprocess.CalledProcessError as e:
                bot.send_message(call.message.chat.id, f"❌ Failed to kill gensyn screen: {str(e)}")
                
        elif call.data == 'toggle_tmate':
            if not tmate_running:
                try:
                    subprocess.run("tmate -S /tmp/tmate.sock new-session -d", shell=True, check=True)
                    subprocess.run("tmate -S /tmp/tmate.sock wait tmate-ready", shell=True, check=True)
                    result = subprocess.run(
                        "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'",
                        shell=True, check=True, capture_output=True, text=True
                    )
                    ssh_line = result.stdout.strip()
                    tmate_running = True
                    bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_menu())
                    bot.send_message(
                        call.message.chat.id,
                        f"<code>{ssh_line}</code>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    tmate_running = False
                    bot.send_message(call.message.chat.id, f"❌ Failed to start tmate: {str(e)}")
            else:
                try:
                    subprocess.run("tmate -S /tmp/tmate.sock kill-server", shell=True, check=True)
                    tmate_running = False
                    bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=get_menu())
                    bot.send_message(call.message.chat.id, "🛑 Terminal session killed.")
                except Exception as e:
                    bot.send_message(call.message.chat.id, f"❌ Failed to kill tmate: {str(e)}")
                    
        elif call.data == "update_menu":
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Gensyn Update", callback_data="gensyn_update"),
                InlineKeyboardButton("Bot Update", callback_data="bot_update")
            )
            bot.send_message(call.message.chat.id, "What do you want to update?", reply_markup=markup)
            
        elif call.data == "gensyn_update":
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Soft Update", callback_data="gensyn_soft_update"),
                InlineKeyboardButton("Hard Update", callback_data="gensyn_hard_update")
            )
            bot.send_message(call.message.chat.id, "Choose update type:", reply_markup=markup)
            
        elif call.data == "gensyn_soft_update":
            threading.Thread(target=gensyn_soft_update, args=(call.message.chat.id,), daemon=True).start()
            
        elif call.data == "gensyn_hard_update":
            threading.Thread(target=gensyn_hard_update, args=(call.message.chat.id,), daemon=True).start()
            
        elif call.data == "bot_update":
            try:
                bot.send_message(call.message.chat.id, "Bot update started. Bot will be back in about 1 minute.")
                update_script_path = "/tmp/bot_update_run.sh"
                with open(update_script_path, "w") as f:
                    f.write("curl -s https://raw.githubusercontent.com/shairkhan2/gensyn-bot/refs/heads/main/update_bot.sh | bash\n")
                os.chmod(update_script_path, 0o700)
                subprocess.run(f"echo 'bash {update_script_path} >/tmp/bot_update.log 2>&1' | at now + 1 minute", shell=True)
            except Exception as e:
                bot.send_message(call.message.chat.id, f"❌ Failed to update bot: {str(e)}")
                
        elif call.data == "get_backup":
            send_backup_files(call.message.chat.id)
            
        elif call.data == "wandb_send_log":
            try:
                # Find the most recent log file
                latest_log = None
                if os.path.exists(WANDB_LOG_DIR):
                    for root, dirs, files in os.walk(WANDB_LOG_DIR):
                        for file in files:
                            if file.endswith('.log'):
                                file_path = os.path.join(root, file)
                                if not latest_log or os.path.getmtime(file_path) > os.path.getmtime(latest_log):
                                    latest_log = file_path
                
                if latest_log and os.path.exists(latest_log):
                    with open(latest_log, "rb") as f:
                        bot.send_document(call.message.chat.id, f)
                else:
                    bot.send_message(call.message.chat.id, "No log file found.")
            except Exception as e:
                bot.send_message(call.message.chat.id, f"Error sending log: {str(e)}")
                
        elif call.data == "wandb_skip_log":
            bot.send_message(call.message.chat.id, "Log skipped.")
            
    except Exception as e:
        logging.error(f"Error in callback_query: {str(e)}")
        bot.send_message(call.message.chat.id, "❌ An error occurred. Check logs.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    global waiting_for_pem
    if message.from_user.id != USER_ID or not waiting_for_pem:
        return
    try:
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        os.makedirs(os.path.dirname(SWARM_PEM_PATH), exist_ok=True)
        with open(SWARM_PEM_PATH, 'wb') as f:
            f.write(file_data)
        waiting_for_pem = False
        bot.send_message(message.chat.id, "✅ swarm.pem saved! Starting Gensyn...")
        start_gensyn_session(message.chat.id, use_sync_backup=False)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error saving file: {str(e)}")
        waiting_for_pem = False

def check_login_timeout(chat_id):
    global login_in_progress
    time.sleep(300)
    if login_in_progress:
        login_in_progress = False
        bot.send_message(chat_id, "⏰ Login timed out. Please try again.")

def monitor():
    previous_ip = ''
    previous_alive = None
    wandb_file_cache = set()
    wandb_folder_cache = set()
    last_stale_sent_ts = None
    previous_localhost_alive = None


    while True:
        try:
            # 1. API status (using check_gensyn_api)
            alive = check_gensyn_api()

            # 1a. Localhost:3000 status (direct monitoring)
            try:
                response = requests.get("http://localhost:3000", timeout=3)
                localhost_alive = "Sign in to Gensyn" in response.text
            except Exception:
                localhost_alive = False

            if previous_localhost_alive is not None and localhost_alive != previous_localhost_alive:
                status = '✅ Online' if localhost_alive else '❌ Offline'
                bot.send_message(USER_ID, f"⚠️ localhost:3000 status changed: {status}")
            previous_localhost_alive = localhost_alive

            # 2. IP change
            try:
                ip = requests.get('https://api.ipify.org', timeout=10).text.strip()
            except:
                ip = "Unknown"

            if ip and ip != previous_ip:
                bot.send_message(USER_ID, f"⚠️ IP changed: {ip}")
                previous_ip = ip

            if previous_alive is not None and alive != previous_alive:
                status = '✅ Online' if alive else '❌ Offline'
                bot.send_message(USER_ID, f"⚠️ API status changed: {status}")
            previous_alive = alive

            # 3. Log freshness
            log_data = get_gensyn_log_status()
            if log_data and log_data.get("timestamp"):
                latest_ts = log_data["timestamp"]
                if (datetime.utcnow() - latest_ts > timedelta(minutes=240)):
                    if not last_stale_sent_ts or last_stale_sent_ts != latest_ts:
                        bot.send_message(
                            USER_ID,
                            f"❗ No new Gensyn log entry since {latest_ts.strftime('%Y-%m-%d %H:%M:%S')} UTC (>4h ago)!"
                        )
                        last_stale_sent_ts = latest_ts
                else:
                    last_stale_sent_ts = None

            # 4. WANDB monitoring - simplified
            new_folders = []
            new_files = []
            if os.path.exists(WANDB_LOG_DIR):
                for root, dirs, files in os.walk(WANDB_LOG_DIR):
                    for d in dirs:
                        folder_path = os.path.join(root, d)
                        if folder_path not in wandb_folder_cache:
                            wandb_folder_cache.add(folder_path)
                            new_folders.append(folder_path)
                    for name in files:
                        path = os.path.join(root, name)
                        if path not in wandb_file_cache:
                            wandb_file_cache.add(path)
                            new_files.append(path)

                if new_folders or new_files:
                    markup = InlineKeyboardMarkup()
                    markup.add(
                        InlineKeyboardButton("Yes", callback_data="wandb_send_log"),
                        InlineKeyboardButton("No", callback_data="wandb_skip_log")
                    )
                    bot.send_message(USER_ID, "🪄 WANDB detected. Want log file?", reply_markup=markup)

            time.sleep(60)

        except Exception as e:
            logging.error("Monitor error: %s", str(e))
            time.sleep(10)

threading.Thread(target=monitor, daemon=True).start()

try:
    bot.infinity_polling()
except Exception as e:
    logging.error("Bot crashed: %s", str(e))
