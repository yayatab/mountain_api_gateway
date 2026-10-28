#!/usr/bin/env python3
"""CLI utility to generate test JWT tokens for Mountain API Gateway."""

import argparse
import os
from datetime import UTC, datetime, timedelta

import jwt

DEFAULT_SECRET = os.getenv("GATEWAY_JWT_SECRET", "gateway-insecure-dev-secret-change-in-production")
DEFAULT_ALGO = os.getenv("GATEWAY_JWT_ALGORITHM", "HS256")


def generate_token(
    sub: str,
    roles: list[str],
    scopes: list[str],
    exp_minutes: int,
    secret: str,
    algorithm: str,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": sub,
        "roles": roles,
        "scopes": scopes,
        "iat": now,
        "exp": now + timedelta(minutes=exp_minutes),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate JWT tokens for testing Mountain API Gateway"
    )
    parser.add_argument("--sub", default="alice", help="Subject / User ID (default: alice)")
    parser.add_argument(
        "--roles",
        default="admin",
        help="Comma-separated roles (e.g. admin,viewer,superadmin) (default: admin)",
    )
    parser.add_argument(
        "--scopes",
        default="read:reports,export:data",
        help="Comma-separated scopes (e.g. read:reports,export:data)",
    )
    parser.add_argument(
        "--exp-minutes",
        type=int,
        default=60,
        help="Token validity in minutes (negative values produce expired tokens) (default: 60)",
    )
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="JWT secret key")
    parser.add_argument("--algo", default=DEFAULT_ALGO, help="JWT algorithm (default: HS256)")

    args = parser.parse_args()
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]

    token = generate_token(
        sub=args.sub,
        roles=roles,
        scopes=scopes,
        exp_minutes=args.exp_minutes,
        secret=args.secret,
        algorithm=args.algo,
    )
    print(token)


if __name__ == "__main__":
    main()
