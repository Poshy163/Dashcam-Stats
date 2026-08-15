"""The way back in.

Sign-in used to be two environment variables, which meant the answer to "I have locked
myself out" was "edit the compose file". It is a database row now, and without something
like this the answer would be "open the SQLite file by hand", which is not an answer.

    docker compose exec dashcam entrypoint.sh recover-login status
    docker compose exec dashcam entrypoint.sh recover-login set-password
    docker compose exec dashcam entrypoint.sh recover-login disable

``disable`` is the one that matters: it deletes the account and switches
``security.require_login`` off together, which is the only pair of changes that reliably
reopens the app.

``disable`` takes effect on a running container within about half a minute, without a
restart: the process answers "is there an account?" from memory on every request, but while
sign-in is switched on and that answer is "no account" it re-reads the row every thirty
seconds, which is exactly the state ``disable`` leaves behind. ``set-password`` is read from
the database on every attempt and so takes effect immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import delete, func, select

from app.auth.passwords import hash_password
from app.auth.service import CREDENTIAL_ID, MIN_PASSWORD_LENGTH
from app.config import get_config
from app.db.models import AppSetting, AuthCredential, AuthSession
from app.db.session import dispose_engine, session_scope


async def _status() -> int:
    async with session_scope() as session:
        credential = await session.get(AuthCredential, CREDENTIAL_ID)
        sessions = int((await session.execute(select(func.count(AuthSession.id)))).scalar() or 0)
    required = await _require_login_value()

    print(f"database:        {get_config().db_path}")
    print(f"sign-in required: {'yes' if required else 'no'}")
    if credential is None:
        print("account:          none configured")
    else:
        print(f"account:          {credential.username}")
    print(f"active sessions:  {sessions}")
    if required and credential is None:
        print()
        print("Sign-in is switched on with no account to sign in against. The app serves")
        print("without authentication in that state rather than locking you out. Run")
        print("'disable' to tidy it up, or 'set-password' to finish setting it up.")
    return 0


async def _disable() -> int:
    async with session_scope() as session:
        await session.execute(delete(AuthSession))
        await session.execute(delete(AuthCredential))
    await _set_require_login(False)
    print("Sign-in disabled and the account deleted. Every session was signed out.")
    print("A running container reopens within about thirty seconds; no restart needed.")
    return 0


async def _set_password(username: str | None) -> int:
    name = (username or "").strip()
    if not name:
        name = input("Username: ").strip()
    if not name:
        print("A username is required.", file=sys.stderr)
        return 2

    if sys.stdin.isatty():
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Those did not match.", file=sys.stderr)
            return 2
    else:
        # Piped input, so this can be scripted. Nothing is echoed either way.
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 2

    encoded = await asyncio.to_thread(hash_password, password)
    async with session_scope() as session:
        row = await session.get(AuthCredential, CREDENTIAL_ID)
        if row is None:
            session.add(AuthCredential(id=CREDENTIAL_ID, username=name, password_hash=encoded))
        else:
            row.username = name
            row.password_hash = encoded
        await session.execute(delete(AuthSession))

    print(f"Password set for {name}. Every existing session was signed out.")
    if not await _require_login_value():
        print("Sign-in is still switched off — turn it on in Settings > Access.")
    print("You can sign in with it straight away; no restart needed.")
    return 0


async def _require_login_value() -> bool:
    async with session_scope() as session:
        row = await session.get(AppSetting, "security.require_login")
    return bool(row.value) if row is not None else False


async def _set_require_login(value: bool) -> None:
    async with session_scope() as session:
        row = await session.get(AppSetting, "security.require_login")
        if row is None:
            session.add(AppSetting(key="security.require_login", value=value))
        else:
            row.value = value


async def _run(args: argparse.Namespace) -> int:
    if not get_config().db_path.exists() and not get_config().database_url:
        print(
            f"No database at {get_config().db_path}. Start the container once so it can be "
            "created, then run this again.",
            file=sys.stderr,
        )
        return 1
    try:
        if args.command == "status":
            return await _status()
        if args.command == "disable":
            return await _disable()
        return await _set_password(args.username)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recover-login",
        description="Inspect, reset or switch off this deployment's sign-in.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show whether sign-in is on and whether an account exists")
    sub.add_parser("disable", help="delete the account and switch sign-in off")
    setter = sub.add_parser("set-password", help="set the username and password")
    setter.add_argument("--username", help="account name; prompted for when omitted")

    args = parser.parse_args(argv)
    # Migrations are deliberately not run here. The container that owns this database is
    # very likely running, and this is a rescue tool -- it should read and write two rows,
    # not take a schema lock behind the app's back.
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
