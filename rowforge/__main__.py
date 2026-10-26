import argparse
import logging
import signal
import subprocess
import sys
import threading

import uvicorn

from .api import create_app
from .config import Settings
from .db import connect
from .worker import work


def main():
    parser = argparse.ArgumentParser(description="RowForge API and durable workers")
    parser.add_argument("command", choices=["serve", "worker", "demo", "init-db"])
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--batch-delay", type=float, default=0, help="Worker-only demo delay per batch")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    if args.command == "worker":
        engine = connect(settings)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            work(engine, settings, stop, args.batch_delay)
        finally:
            engine.dispose()
    elif args.command == "init-db":
        connect(settings).dispose()
        print("Database schema ready.")
    else:
        child = None
        if args.command == "demo":
            # Initialize schema before spawning a separate worker process.
            connect(settings).dispose()
            child = subprocess.Popen([sys.executable, "-m", "rowforge", "worker", "--batch-delay", "0.08"])
        try:
            uvicorn.run(create_app(settings), host=args.host, port=args.port)
        finally:
            if child:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
