#!/bin/zsh
# start the deck server and open it
cd "$(dirname "$0")"
python3 server.py "${DECK_PORT:-47823}" &
sleep 0.5; open "http://${DECK_HOST:-deck.localhost}:${DECK_PORT:-47823}"
wait
