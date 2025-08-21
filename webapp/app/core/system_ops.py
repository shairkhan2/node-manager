import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

import requests
import shutil


SWARM_PEM_PATH = "/root/rl-swarm/swarm.pem"
USER_DATA_PATH = "/root/rl-swarm/modal-login/temp-data/userData.json"
USER_APIKEY_PATH = "/root/rl-swarm/modal-login/temp-data/userApiKey.json"
SYNC_BACKUP_DIR = "/root/node-manager/sync-backup"
BACKUP_USERDATA_DIR = "/root/node-manager/backup-userdata"
GENSYN_LOG_PATH = "/root/rl-swarm/logs/swarm_launcher.log"


def start_vpn() -> Tuple[bool, str]:
    try:
        subprocess.run(["wg-quick", "up", "wg0"], check=True)
        return True, "✅ VPN enabled"
    except subprocess.CalledProcessError as e:
        if "already exists" in str(e):
            return True, "⚠️ VPN already enabled"
        return False, f"❌ VPN failed to start: {str(e)}"


def stop_vpn() -> Tuple[bool, str]:
    try:
        subprocess.run(["wg-quick", "down", "wg0"], check=True)
        return True, "❌ VPN disabled"
    except subprocess.CalledProcessError as e:
        if "is not a WireGuard interface" in str(e):
            return True, "⚠️ VPN already disabled"
        return False, f"❌ VPN failed to stop: {str(e)}"


def check_gensyn_api() -> bool:
    try:
        response = requests.get("http://localhost:3000", timeout=5)
        if response.status_code == 200:
            response_text = response.text.lower()
            gensyn_indicators = [
                "sign in to gensyn",
                "gensyn",
                "__next_error__",
                "<!doctype html>",
                "<html",
            ]
            return any(ind in response_text for ind in gensyn_indicators)
        return False
    except Exception:
        return False


def get_gensyn_log_status(log_path: str = GENSYN_LOG_PATH) -> Optional[Dict[str, Optional[str]]]:
    try:
        if not os.path.exists(log_path):
            return None
        with open(log_path, "r") as f:
            lines = f.readlines()[-50:]

        latest_ts: Optional[datetime] = None
        joining_round: Optional[str] = None
        starting_round: Optional[str] = None

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
                except Exception:
                    continue

        return {
            "timestamp": latest_ts,
            "joining": joining_round,
            "starting": starting_round,
        }
    except Exception:
        return None


def check_gensyn_screen_running() -> bool:
    try:
        result = subprocess.run("screen -ls", shell=True, capture_output=True, text=True)
        return "gensyn" in result.stdout
    except Exception:
        return False


def start_gensyn_session(use_sync_backup: bool = True, fresh_start: bool = False) -> None:
    if check_gensyn_screen_running():
        return

    os.makedirs(SYNC_BACKUP_DIR, exist_ok=True)

    if fresh_start:
        # Fresh start: do not require swarm.pem
        if use_sync_backup:
            for file in ["userData.json", "userApiKey.json"]:
                backup_path = os.path.join(SYNC_BACKUP_DIR, file)
                target_path = USER_DATA_PATH if file == "userData.json" else USER_APIKEY_PATH
                if os.path.exists(backup_path):
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    shutil.copy(backup_path, target_path)
        cmd = (
            "cd /root/rl-swarm && "
            "screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'"
        )
        subprocess.run(cmd, shell=True, check=True)
        return

    # Non-fresh start: if sync backup is enabled, restore login data
    if use_sync_backup:
        for file in ["userData.json", "userApiKey.json"]:
            backup_path = os.path.join(SYNC_BACKUP_DIR, file)
            target_path = USER_DATA_PATH if file == "userData.json" else USER_APIKEY_PATH
            if os.path.exists(backup_path):
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                shutil.copy(backup_path, target_path)

    cmd = (
        "cd /root/rl-swarm && "
        "screen -dmS gensyn bash -c 'python3 -m venv .venv && source .venv/bin/activate && ./run_rl_swarm.sh'"
    )
    subprocess.run(cmd, shell=True, check=True)


def kill_gensyn() -> None:
    subprocess.run("screen -S gensyn -X quit", shell=True, check=True)


def get_public_ip() -> str:
    try:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()
    except Exception:
        return "Unknown"


# ---------- Backup & Sync ----------

def backup_user_data_sync() -> bool:
    try:
        for src, name in [
            (USER_DATA_PATH, "userData.json"),
            (USER_APIKEY_PATH, "userApiKey.json"),
        ]:
            dst = os.path.join(SYNC_BACKUP_DIR, name)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy(src, dst)
        return True
    except Exception:
        return False


def backup_user_data_timestamped() -> bool:
    try:
        os.makedirs(BACKUP_USERDATA_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for path, name in [
            (USER_DATA_PATH, "userData.json"),
            (USER_APIKEY_PATH, "userApiKey.json"),
        ]:
            if os.path.exists(path):
                backup_file = f"{name.split('.')[0]}_{timestamp}.json"
                shutil.copy(path, os.path.join(BACKUP_USERDATA_DIR, backup_file))
                latest_file = f"{name.split('.')[0]}_latest.json"
                shutil.copy(path, os.path.join(BACKUP_USERDATA_DIR, latest_file))
        return True
    except Exception:
        return False


_backup_thread: Optional[threading.Thread] = None
_backup_active = False


def _periodic_sync_backup():
    global _backup_active
    _backup_active = True
    while _backup_active:
        try:
            backup_user_data_sync()
            time.sleep(60)
        except Exception:
            time.sleep(10)


def start_periodic_sync_backup():
    global _backup_thread
    if _backup_thread and _backup_thread.is_alive():
        return
    _backup_thread = threading.Thread(target=_periodic_sync_backup, daemon=True)
    _backup_thread.start()


# ---------- Peer Info (reward/win) ----------

def discover_peer_name() -> Optional[str]:
    try:
        import glob
        log_dir = "/root/rl-swarm/logs"
        log_files = glob.glob(f"{log_dir}/training_*.log")
        if not log_files:
            return None
        log_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        fname = os.path.basename(log_files[0])
        if fname.startswith("training_") and fname.endswith(".log"):
            return fname[len("training_"):-len(".log")].replace("_", " ")
    except Exception:
        return None
    return None


def fetch_peer_info() -> Optional[Dict[str, object]]:
    from urllib.parse import quote_plus

    peer_name = discover_peer_name()
    if not peer_name:
        return None
    api_url = f"https://dashboard.gensyn.ai/api/v1/peer?name={quote_plus(peer_name)}"
    try:
        r = requests.get(api_url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            "peer_name": peer_name,
            "peerId": data.get("peerId"),
            "reward": data.get("reward"),
            "score": data.get("score"),
            "online": data.get("online", False),
            "rewardTimestamp": data.get("rewardTimestamp"),
            "scoreTimestamp": data.get("scoreTimestamp"),
        }
    except Exception:
        return None


# ---------- Tmate Terminal ----------

def is_tmate_running() -> bool:
    try:
        result = subprocess.run(
            "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'",
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception:
        return False


def start_tmate() -> Optional[str]:
    try:
        subprocess.run("tmate -S /tmp/tmate.sock new-session -d", shell=True, check=True)
        subprocess.run("tmate -S /tmp/tmate.sock wait tmate-ready", shell=True, check=True)
        result = subprocess.run(
            "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'",
            shell=True,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return None


def stop_tmate() -> bool:
    try:
        subprocess.run("tmate -S /tmp/tmate.sock kill-server", shell=True, check=True)
        return True
    except Exception:
        return False


def get_tmate_ssh() -> Optional[str]:
    try:
        result = subprocess.run(
            "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'",
            shell=True,
            capture_output=True,
            text=True,
            check=True,
        )
        ssh_line = result.stdout.strip()
        return ssh_line if ssh_line else None
    except Exception:
        return None


