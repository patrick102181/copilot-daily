#!/usr/bin/env bash
# 改在自己的雲端主機（例如 DigitalOcean 印度機房）跑，而不是 GitHub Actions 時用這支。
# 好處：印度機房連得到幣安「合約」API，資料跟你下單的商品完全一樣。
#
# 安裝：
#   1. git clone 你的 repo 到主機，設定好能 git push（deploy key 或權杖）
#   2. cp .env.example .env，填入需要的金鑰
#   3. crontab -e 加這一行（UTC 時間，4H 收盤後 5 分鐘）：
#      5 0,4,8,12,16,20 * * * /path/to/copilot-daily/run_on_server.sh >> /path/to/copilot-daily/run.log 2>&1
#   4. 記得把 GitHub 上的 .github/workflows/update.yml 停用（Actions → 該流程 → Disable），避免兩邊同時跑
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
git pull --rebase --quiet
python3 scripts/update.py
python3 scripts/ai_summary.py
git add data
git diff --cached --quiet || git commit -qm "資料更新 $(date -u +'%Y-%m-%d %H:%M') UTC"
git push --quiet
