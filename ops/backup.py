import argparse
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

LIMIT = 500_000_000


def cipher(key_file):
    path = Path(key_file)
    if path.stat().st_mode & 0o077:
        raise ValueError("Backup key must be private (0600)")
    return Fernet(path.read_bytes().strip())


def _pg(command, url, dump):
    parsed = make_url(url)
    env = {**os.environ, "PGPASSWORD": parsed.password or ""}
    args = [command, "--host", parsed.host or "localhost", "--port", str(parsed.port or 5432),
            "--username", parsed.username or "postgres", "--dbname", parsed.database]
    args += ["--format=custom", "--file", str(dump)] if command == "pg_dump" else ["--exit-on-error", str(dump)]
    result = subprocess.run(args, env=env, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{command} failed; verify database connectivity and server/client versions")


def backup(data_dir, database_url, destination, key_file):
    data_dir, destination = Path(data_dir).resolve(), Path(destination)
    encryptor = cipher(key_file)
    if destination.exists():
        raise ValueError("Backup destination already exists")
    if Path(key_file).resolve().is_relative_to(data_dir / "artifacts"):
        raise ValueError("Backup key cannot be inside artifact storage")
    with tempfile.TemporaryDirectory(prefix="oa-backup-") as temporary:
        dump = Path(temporary) / "database.dump"
        kind = "sqlite" if database_url.startswith("sqlite") else "postgresql"
        if kind == "sqlite":
            with sqlite3.connect(make_url(database_url).database) as source, sqlite3.connect(dump) as target:
                source.backup(target)
        else:
            _pg("pg_dump", database_url, dump)
        buffer = io.BytesIO()
        manifest = json.dumps({"version": 1, "database_kind": kind, "artifact_root": str(data_dir / "artifacts")}).encode()
        total = dump.stat().st_size
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("manifest.json")
            entry.size, entry.mode = len(manifest), 0o600
            archive.addfile(entry, io.BytesIO(manifest))
            archive.add(dump, arcname="database.dump", recursive=False)
            for path in (data_dir / "artifacts").rglob("*"):
                if path.is_symlink():
                    raise ValueError("Artifact symlinks cannot be backed up")
                if path.is_file():
                    total += path.stat().st_size
                    if total > LIMIT:
                        raise ValueError("Backup exceeds 500 MB; configure a streaming backup service")
                    archive.add(path, arcname=str(path.relative_to(data_dir)), recursive=False)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = encryptor.encrypt(buffer.getvalue())
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
        return {"bytes": len(payload), "database_kind": kind}


def restore(backup_file, key_file, target_data_dir, database_url):
    backup_file, target = Path(backup_file), Path(target_data_dir).resolve()
    if backup_file.stat().st_size > LIMIT * 2:
        raise ValueError("Encrypted archive exceeds limit")
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must be empty; stop services and preserve the previous data separately")
    payload = cipher(key_file).decrypt(backup_file.read_bytes())
    if len(payload) > LIMIT:
        raise ValueError("Archive exceeds limit")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        entries = archive.getmembers()
        if sum(x.size for x in entries) > LIMIT:
            raise ValueError("Expanded archive exceeds limit")
        seen = set()
        for entry in entries:
            name = PurePosixPath(entry.name)
            if not entry.isfile() or name.is_absolute() or ".." in name.parts or str(name) in seen or \
                    not (entry.name in {"manifest.json", "database.dump"} or name.parts[0] == "artifacts"):
                raise ValueError("Unsafe backup member")
            seen.add(str(name))
        manifest = json.load(archive.extractfile("manifest.json"))
        if manifest.get("version") != 1 or manifest.get("artifact_root") != str(target / "artifacts"):
            raise ValueError("Restore must use the original logical artifact path (same deployment mount)")
        kind = "sqlite" if database_url.startswith("sqlite") else "postgresql"
        if kind != manifest["database_kind"]:
            raise ValueError("Database kind mismatch")
        engine = create_engine(database_url)
        try:
            if kind == "postgresql" and inspect(engine).get_table_names():
                raise ValueError("PostgreSQL restore database must be empty")
        finally:
            engine.dispose()
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="oa-restore-") as temporary:
            dump = Path(temporary) / "database.dump"
            dump.write_bytes(archive.extractfile("database.dump").read())
            if kind == "sqlite":
                db_path = Path(make_url(database_url).database).resolve()
                if not db_path.is_relative_to(target):
                    raise ValueError("SQLite restore path must lie within target data directory")
                with sqlite3.connect(dump) as source, sqlite3.connect(db_path) as database:
                    source.backup(database)
                db_path.chmod(0o600)
            else:
                _pg("pg_restore", database_url, dump)
            for entry in entries:
                if not entry.name.startswith("artifacts/"):
                    continue
                destination = target / entry.name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(entry).read())
                destination.chmod(0o600)
    return {"status": "restored", "secrets": "Restore encryption/owner credentials separately before startup"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["keygen", "backup", "restore"])
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "keygen":
        fd = os.open(args.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(Fernet.generate_key())
    else:
        from app.config import Settings
        settings = Settings()
        if not args.archive:
            parser.error("--archive is required")
        if args.command == "backup":
            print(backup(settings.data_dir, settings.db_url, args.archive, args.key))
        else:
            print(restore(args.archive, args.key, settings.data_dir, settings.db_url))


if __name__ == "__main__":
    main()
