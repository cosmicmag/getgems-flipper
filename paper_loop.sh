#!/bin/zsh
# Paper-trading loop: scan -> log candidates -> check outcomes, every 60 min. Portals env optional (sanity filter only).
cd "$(dirname "$0")"
while true; do
  echo "=== $(date '+%F %T') scan ===" >> paper_loop.log
  python3 scan.py 20 >> paper_loop.log 2>&1
  python3 paper.py log >> paper_loop.log 2>&1
  python3 paper.py check >> paper_loop.log 2>&1
  sleep 3600
done
