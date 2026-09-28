#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: bash run.sh "/path/to/photos" [--data "/path/to/project"] [--port 8765]\n'
}
if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
  usage
  exit 0
fi
if [[ ! -d "$1" ]]; then
  printf 'Папка не найдена: %s\n' "$1" >&2
  exit 1
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'Приложение поддерживает только macOS.\n' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf 'Нужен Python 3.10 или новее (команда python3).\n' >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  printf 'Нужен Python 3.10 или новее.\n' >&2
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
media_dir="$1"
shift
exec python3 "$script_dir/src/server.py" --root "$media_dir" --open "$@"
