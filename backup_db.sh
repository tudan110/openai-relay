#!/usr/bin/env bash
# 每日备份 relay.db(SQLite 在线 .backup,WAL 安全),压缩并保留最近 14 份。
# 由 cron 调用,见 README「运维要点」。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$DIR/relay.db"
BK="$DIR/backups"
mkdir -p "$BK"

STAMP="$(date +%Y%m%d-%H%M)"
OUT="$BK/relay-$STAMP.db"

# 用 Python 的 sqlite3 在线 .backup(一致性快照,正确处理 WAL,不影响运行中的中继)。
# 不依赖 sqlite3 命令行——服务器可能没装。
python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$OUT"

# 只保留最近 14 份,旧的删掉。
ls -1t "$BK"/relay-*.db.gz 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "[$(date '+%F %T')] backup -> ${OUT}.gz ($(du -h "${OUT}.gz" | cut -f1))"
