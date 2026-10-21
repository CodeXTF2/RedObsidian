"""Encrypted backups of the whole instance: database, uploads and the report template.

  python manage_backup.py backup --dir /backups --keep 14     # timestamped file, keeps the newest 14
  python manage_backup.py backup redobsidian.zip
  python manage_backup.py restore redobsidian.zip [--force]

The password comes from REDOBSIDIAN_BACKUP_PASSWORD or a prompt. PostgreSQL backups need pg_dump /
pg_restore on the PATH (the Docker image includes them). Stop the app before restoring.
"""
import argparse
import getpass
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import pyzipper
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from app import create_app, db

FORMAT_VERSION = 1


def backup_password(confirm):
    password = os.environ.get("REDOBSIDIAN_BACKUP_PASSWORD")
    if not password:
        password = getpass.getpass("Backup password: ")
        if confirm and password != getpass.getpass("Repeat password: "):
            sys.exit("Error: passwords don't match.")
    if len(password) < 12:
        sys.exit("Error: use a backup password of at least 12 characters.")
    return password


def pg_env(url):
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def pg_args(url):
    args = ["--host", url.host or "localhost", "--username", url.username or "postgres", "--dbname", url.database]
    if url.port:
        args += ["--port", str(url.port)]
    return args


def dump_database(app, workdir):
    url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if url.get_backend_name() == "sqlite":
        target = os.path.join(workdir, "database.sqlite")
        source = sqlite3.connect(url.database)
        destination = sqlite3.connect(target)
        with destination:
            source.backup(destination)  # consistent copy even while the app is writing
        source.close()
        destination.close()
        return "sqlite", target
    if url.get_backend_name() == "postgresql":
        target = os.path.join(workdir, "database.pgdump")
        subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--file", target, *pg_args(url)],
                       check=True, env=pg_env(url))
        return "postgresql", target
    sys.exit(f"Error: unsupported database backend {url.get_backend_name()}.")


def cmd_backup(args):
    app = create_app()
    if args.dir:
        os.makedirs(args.dir, exist_ok=True)
        output = os.path.join(args.dir, f"redobsidian-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.zip")
    elif args.file:
        output = args.file
    else:
        sys.exit("Error: give an output file or --dir.")
    password = backup_password(confirm=not os.environ.get("REDOBSIDIAN_BACKUP_PASSWORD"))

    with app.app_context(), tempfile.TemporaryDirectory() as workdir:
        kind, dump_path = dump_database(app, workdir)
        revision = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar()
        manifest = {"format_version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "database": kind, "schema_revision": revision}
        partial = output + ".partial"
        with pyzipper.AESZipFile(partial, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.write(dump_path, os.path.basename(dump_path))
            upload_root = app.config["UPLOAD_ROOT"]
            for root, _dirs, files in os.walk(upload_root):
                for name in files:
                    path = os.path.join(root, name)
                    zf.write(path, "uploads/" + os.path.relpath(path, upload_root).replace(os.sep, "/"))
            template = os.path.join(app.instance_path, "report_template.docx")
            if os.path.isfile(template):
                zf.write(template, "report_template.docx")
        os.replace(partial, output)
    print(f"Backup written to {output}")

    if args.dir and args.keep:
        backups = sorted(glob.glob(os.path.join(args.dir, "redobsidian-*.zip")))
        for old in backups[:-args.keep]:
            os.remove(old)
            print(f"Removed old backup {old}")


def safe_member_path(root, member):
    """Where an archive member should land under root, or None if it would escape it."""
    relative = member[len("uploads/"):]
    target = os.path.realpath(os.path.join(root, relative))
    if not target.startswith(os.path.realpath(root) + os.sep):
        return None
    return target


def cmd_restore(args):
    os.environ["REDOBSIDIAN_AUTO_MIGRATE"] = "0"
    app = create_app()
    password = backup_password(confirm=False)
    with app.app_context():
        has_data = "user" in inspect(db.engine).get_table_names() and db.session.execute(
            text('SELECT COUNT(*) FROM "user"')).scalar()
        db.session.remove()
        if has_data and not args.force:
            sys.exit("Error: the current database already has data. Re-run with --force to overwrite it.")
        url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
        db.engine.dispose()

    with pyzipper.AESZipFile(args.file, "r") as zf, tempfile.TemporaryDirectory() as workdir:
        zf.setpassword(password.encode("utf-8"))
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except (RuntimeError, KeyError) as exc:
            sys.exit(f"Error: wrong password or not a RedObsidian backup ({exc}).")
        if manifest.get("database") != url.get_backend_name():
            sys.exit(f"Error: backup is from {manifest.get('database')} but this server uses {url.get_backend_name()}.")

        if url.get_backend_name() == "sqlite":
            dump = zf.extract("database.sqlite", workdir)
            if os.path.exists(url.database):
                shutil.copy2(url.database, url.database + f".before-restore-{datetime.now():%Y%m%d%H%M%S}")
            for suffix in ("-wal", "-shm"):
                if os.path.exists(url.database + suffix):
                    os.remove(url.database + suffix)
            shutil.copy2(dump, url.database)
        else:
            dump = zf.extract("database.pgdump", workdir)
            subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", *pg_args(url), dump],
                           check=True, env=pg_env(url))

        upload_root = app.config["UPLOAD_ROOT"]
        if os.path.isdir(upload_root) and os.listdir(upload_root):
            moved = upload_root.rstrip("/\\") + f".before-restore-{datetime.now():%Y%m%d%H%M%S}"
            shutil.move(upload_root, moved)
            print(f"Existing uploads moved to {moved}")
        os.makedirs(upload_root, exist_ok=True)
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("uploads/"):
                target = safe_member_path(upload_root, info.filename)
                if not target:
                    print(f"Skipped unsafe path {info.filename}")
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)
            elif info.filename == "report_template.docx":
                with zf.open(info) as source, open(os.path.join(app.instance_path, "report_template.docx"), "wb") as destination:
                    shutil.copyfileobj(source, destination)

    # Bring the restored schema up to date with this version of the code
    os.environ["REDOBSIDIAN_AUTO_MIGRATE"] = "1"
    create_app()
    print(f"Restored backup from {manifest.get('created_at')}. Start the app again.")


def main():
    parser = argparse.ArgumentParser(description="Back up or restore a RedObsidian instance.")
    subparsers = parser.add_subparsers(dest="command")
    backup = subparsers.add_parser("backup", help="Write an encrypted backup")
    backup.add_argument("file", nargs="?", help="Output ZIP path")
    backup.add_argument("--dir", help="Write a timestamped backup into this directory instead")
    backup.add_argument("--keep", type=int, default=0, help="With --dir, keep only the newest N backups")
    backup.set_defaults(func=cmd_backup)
    restore = subparsers.add_parser("restore", help="Restore a backup (stop the app first)")
    restore.add_argument("file")
    restore.add_argument("--force", action="store_true", help="Overwrite a database that already has data")
    restore.set_defaults(func=cmd_restore)
    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
