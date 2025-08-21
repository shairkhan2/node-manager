#!/bin/bash

# Kill vpn_bot if it's running
pkill -f vpn_bot && echo "Killed vpn_bot process." || echo "vpn_bot not running."
sudo apt-get install at
sudo systemctl enable --now atd
# Disable and stop the bot.service
echo "Disabling and stopping bot.service..."
systemctl stop bot.service
systemctl disable bot.service

# Remove the existing bot.py if it exists
BOT_PATH="$HOME/gensyn-bot/bot.py"
if [ -f "$BOT_PATH" ]; then
    rm "$BOT_PATH"
    echo "Removed existing bot.py"
else
    echo "bot.py not found, skipping removal"
fi

# Change to the bot directory and download the new bot.py
cd "$HOME/gensyn-bot/" || { echo "Failed to cd into $HOME/gensyn-bot"; exit 1; }
wget https://raw.githubusercontent.com/shairkhan2/gensyn-bot/refs/heads/main/bot.py -O bot.py

# Enable and start the bot.service
echo "Enabling and starting bot.service..."
systemctl enable bot.service
systemctl start bot.service
echo "Done."
