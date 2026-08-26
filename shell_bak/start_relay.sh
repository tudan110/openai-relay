#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
pm2 start pm2.config.js
