from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from podhub.config import load_config
from podhub.core.services import NotFound, Services
from podhub.domain.enums import Category, SourceKind
from podhub.domain.policy import SourcePolicy
from podhub.main import create_app
from podhub.version import app_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pod-hub")
    parser.add_argument("--version", action="version", version=f"%(prog)s {app_version()}")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        return sub

    for name in ("serve", "doctor", "pause", "resume"):
        command(name)
    sources = command("sources").add_subparsers(dest="action", required=True)
    sources.add_parser("list")
    add = sources.add_parser("add")
    add.add_argument("source")
    add.add_argument("--kind", choices=list(SourceKind))
    add.add_argument("--cookie-profile-id", type=int)
    add.add_argument("--initial-backfill-count", type=int)
    add.add_argument("--title", help="custom podcast display title")
    add.add_argument("--slug", help="custom source folder name")
    sources.add_parser("sync").add_argument("id", type=int)
    add_video = sources.add_parser("add-video")
    add_video.add_argument("id", type=int)
    add_video.add_argument("url")
    videos = command("videos").add_subparsers(dest="action", required=True)
    choice = videos.add_parser("retry").add_mutually_exclusive_group(required=True)
    choice.add_argument("--category", choices=list(Category))
    choice.add_argument("--video-id")
    breakers = command("breakers").add_subparsers(dest="action", required=True)
    breakers.add_parser("list")
    breakers.add_parser("reset").add_argument("key")
    return parser


async def execute(args: argparse.Namespace, services: Services) -> int:
    match args.command:
        case "doctor":
            report = await services.doctor()
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        case "pause" | "resume":
            services.pause(args.command == "pause")
        case "sources":
            match args.action:
                case "list":
                    for source in services.sources():
                        print(f"{source['id']}\t{source['kind']}\t{source['slug']}\t{source['title']}")
                case "add":
                    kind = SourceKind(args.kind) if args.kind else None
                    policy = services.config.defaults.model_dump()
                    if args.initial_backfill_count is not None:
                        policy["initial_backfill_count"] = args.initial_backfill_count
                    validated = SourcePolicy.model_validate(policy)
                    preview = await services.preview(args.source, kind, args.cookie_profile_id)
                    print(await services.add_source(
                        preview.identity, args.source, validated, args.cookie_profile_id,
                        title=args.title, slug=args.slug,
                    ))
                case "sync":
                    services.source_command(args.id, "sync")
                case "add-video":
                    print(services.add_video(args.id, args.url))
        case "videos":
            print(services.retry(video_id=args.video_id, category=args.category))
        case "breakers":
            if args.action == "list":
                print(json.dumps(services.list_breakers(), indent=2))
            else:
                await services.reset_breaker(args.key)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "serve":
            uvicorn.run(create_app(config), host=config.web.listen_host, port=config.web.listen_port,
                        access_log=False)
            return 0
        services = Services(config)
        services.initialize()
        return asyncio.run(execute(args, services))
    except (ValueError, OSError, NotFound, sqlite3.Error) as exc:
        print(f"pod-hub: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
