#!/bin/bash
PID_FILE="/tmp/strongsport.pid"
BOT_DIR="$(dirname "$(readlink -f "$0")")"

start_bot() {
    cd "$BOT_DIR"
    nohup python3 bot.py start > /dev/null 2>&1 &
    notify-send "TrateGuides" "Бот запущен"
}

stop_bot() {
    cd "$BOT_DIR"
    python3 bot.py stop
    notify-send "TrateGuides" "Бот остановлен"
}

if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    stop_bot
else
    start_bot
fi
