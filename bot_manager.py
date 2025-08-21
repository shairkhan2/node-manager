import os
import time
import subprocess

BOT_CONFIG = "/root/bot_config.env"
WG_CONFIG_PATH = "/etc/wireguard/wg0.conf"
BOT_PATH = "/root/node-manager/bot.py"
VENV_PATH = "/root/node-manager/.venv"
PYTHON_BIN = f"{VENV_PATH}/bin/python3"
REQUIREMENTS = "/root/node-manager/requirements.txt"


def menu():
    while True:
        print("\n🛠️ VPN Bot Manager")
        print("1. Paste WireGuard config")
        print("2. Setup Telegram Bot")
        print("3. Enable Bot on Boot")
        print("4. Exit")
        print("5. Start Bot")
        print("6. Stop Bot")
        print("7. View Bot Logs")
        print("8. Rebuild Virtual Environment")
        print("9. Install requirements.txt")
        choice = input("\nSelect an option: ")

        if choice == "1":
            setup_vpn()
        elif choice == "2":
            setup_bot()
        elif choice == "3":
            setup_systemd()
        elif choice == "4":
            break
        elif choice == "5":
            start_bot()
        elif choice == "6":
            stop_bot()
        elif choice == "7":
            os.system("journalctl -u bot -f")
        elif choice == "8":
            rebuild_venv()
        elif choice == "9":
            install_requirements()
        else:
            print("❌ Invalid option.")


def setup_vpn():
    print("\n📋 Paste full WireGuard config. Type 'END' on a new line to finish:")
    config = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        config.append(line)

    os.makedirs("/etc/wireguard", exist_ok=True)
    with open(WG_CONFIG_PATH, "w") as f:
        f.write("\n".join(config))
    os.system("chmod 600 " + WG_CONFIG_PATH)
    print("✅ WireGuard config saved.")


def setup_bot():
    print("\n🤖 Telegram Bot Setup")
    token = input("Bot Token: ")
    user_id = input("Your Telegram User ID: ")

    with open(BOT_CONFIG, "w") as f:
        f.write(f"BOT_TOKEN={token}\n")
        f.write(f"USER_ID={user_id}\n")

    if not os.path.exists(BOT_PATH):
        os.system("cp ./default_bot.py /root/node-manager/bot.py")
        os.system(f"chmod +x {BOT_PATH}")

    print("✅ Bot config saved and default bot.py is ready.")


def start_bot():
    print("🚀 Starting bot and reward.py in a screen session with virtual environment...")

    if not os.path.exists(f"{VENV_PATH}/bin/activate"):
        print("❌ Virtual environment not found. Please run option 8 to rebuild it.")
        return

    REWARD_PATH = "/root/node-manager/reward.py"

    os.system("screen -S vpn_bot -X quit")
    os.system(
        f"screen -dmS vpn_bot bash -c 'source {VENV_PATH}/bin/activate && "
        f"python {BOT_PATH} & python {REWARD_PATH} && wait'"
    )
    print("✅ bot.py and reward.py started in screen session named 'vpn_bot'. Use: screen -r vpn_bot")


def stop_bot():
    print("🛑 Stopping bot...")
    if os.system(f"pgrep -f '{BOT_PATH}' > /dev/null") == 0:
        os.system(f"pkill -f '{BOT_PATH}'")
        print("✅ Bot stopped.")
    else:
        print("ℹ️ Bot is not running.")


def setup_systemd():
    print("\n⚙️ Enabling bot service...")

    service = f"""[Unit]
Description=VPN Telegram Bot
After=network.target

[Service]
Type=simple
ExecStart={PYTHON_BIN} {BOT_PATH}
EnvironmentFile={BOT_CONFIG}
Restart=always
User=root
WorkingDirectory=/root/node-manager
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    with open("/etc/systemd/system/bot.service", "w") as f:
        f.write(service)

    os.system("systemctl daemon-reexec")
    os.system("systemctl daemon-reload")
    os.system("systemctl enable bot")
    os.system("systemctl restart bot")
    print("✅ Bot service enabled and running via systemd.")


def rebuild_venv():
    print("♻️ Rebuilding virtual environment...")
    os.system(f"rm -rf {VENV_PATH}")
    os.system(f"python3 -m venv {VENV_PATH}")
    os.system(f"{PYTHON_BIN} -m pip install --upgrade pip")
    print("✅ Virtual environment rebuilt.")


def install_requirements():
    print("📦 Installing requirements.txt and Playwright dependencies...")
    if not os.path.exists(REQUIREMENTS):
        print("❌ requirements.txt not found.")
        return

    os.system(f"{PYTHON_BIN} -m pip install -r {REQUIREMENTS}")
    print("✅ Requirements and Playwright setup complete.")


if __name__ == "__main__":
    menu()

