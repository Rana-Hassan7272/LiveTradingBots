# LiveTradingBots

_A homemade humble library to run automated python crypto trading bots_

\
🛠️ Setup commands (virtual environment included)


# Install dependencies
sudo yum install python3-pip
pip3 install ccxt pandas ta

# Run the bot persistently
tmux new-session -s scalping_bot
python3 scalping_bot.py
# (Detach with Ctrl+B, D)