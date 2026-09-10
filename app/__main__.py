import argparse
import getpass
import subprocess
import sys

from app.config import Settings
from app.db import Database
from app.security.auth import ensure_key, set_owner_password


def main():
    parser = argparse.ArgumentParser(description="Private Opportunity Autopilot")
    parser.add_argument("command", choices=["setup", "start", "serve", "import-profile", "worker", "tick", "migrate"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "setup":
        settings.production = False
    settings.validate_runtime()
    ensure_key(settings)
    database = Database(settings.db_url)
    database.initialize()
    if args.command == "setup" or args.command == "start" and not settings.owner_path.exists():
        password = getpass.getpass("Choose the local owner password (12+ characters): ")
        if getpass.getpass("Repeat password: ") != password:
            parser.error("Passwords did not match")
        set_owner_password(settings, password)
        from app.models import AuthSession
        from sqlalchemy import delete
        with database.write() as session:
            session.execute(delete(AuthSession))
        print("Owner password saved privately. Live submissions and spending remain governed by disabled defaults.")
        if args.command == "setup":
            return
    if args.command in {"serve", "start"}:
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not settings.production:
            parser.error("Non-loopback binding requires OA_PRODUCTION=true and HTTPS configuration")
        import uvicorn
        from app.api.main import create_app
        worker = subprocess.Popen([sys.executable, "-m", "app", "worker"]) if args.command == "start" else None
        try:
            uvicorn.run(create_app(settings, database), host=args.host, port=args.port, access_log=False)
        finally:
            if worker:
                worker.terminate()
                try:
                    worker.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()
    elif args.command == "import-profile":
        from app.services import import_profile
        print(import_profile(database, settings.portfolio_root))
    elif args.command in {"worker", "tick"}:
        from app.workers.scheduler import run_loop, run_tick
        if args.command == "tick":
            print(run_tick(database, settings))
        else:
            run_loop(database, settings)
    elif args.command == "migrate":
        print("Database schema is current and initialized.")


if __name__ == "__main__":
    main()
