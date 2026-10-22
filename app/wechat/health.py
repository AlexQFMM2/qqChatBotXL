"""Container liveness. Full readiness/degradation details remain in health.json."""
import json
import sys
import time
from pathlib import Path


def main():
    try:
        state = json.loads(Path(sys.argv[1] if len(sys.argv)>1 else '/app/data/health.json').read_text())
        alive = state['alive'] and 0 <= time.time()-state['updated_at'] < 60
    except (OSError, ValueError, KeyError):
        alive = False
    raise SystemExit(0 if alive else 1)


if __name__ == '__main__':
    main()
