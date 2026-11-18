"""Wait briefly for an explicitly started local fixture server."""
import sys
import time
from urllib.request import urlopen

if __name__ == "__main__":
    for attempt in range(50):
        try:
            with urlopen(sys.argv[1], timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("Fixture server did not become ready")
