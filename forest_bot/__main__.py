"""Entry point.

    python -m forest_bot            run the Telegram bot (default; Drive sync at startup and periodically)
    python -m forest_bot sync       run one Google Drive synchronisation and print the report
    python -m forest_bot ask "..."  answer one question from the command line (no Telegram)
    python -m forest_bot status     show document counts by status and the last sync run
    python -m forest_bot docs       list documents (id, source, status, scope, version time, title)
    python -m forest_bot pending    print exactly what /pending shows
    python -m forest_bot search "query" <mitten-code>   retrieval only (no LLM): which active docs are used
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import Counter

from .app import build_services, startup_checks
from .config import ConfigError, Settings


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "telegram.ext.Application"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_bot(settings: Settings) -> int:
    problems = settings.validate_for_bot()
    if problems:
        for p in problems:
            logging.error("Configuration error: %s (see .env.example)", p)
        return 2
    from .telegram_bot import TelegramBot

    services = build_services(settings)
    log = logging.getLogger("forest_bot")

    async def on_startup(application):
        await startup_checks(services)
        if services.drive_sync is not None:
            async def sync_job(context):
                await services.drive_sync.run()
                services.retriever.refresh(services.library.revision)
                sync = services.drive_sync
                if (sync.throttled or sync.retry_after) and not context.job_queue.get_jobs_by_name("drive-sync-retry"):
                    from .drive import throttle_retry_delay
                    delay = throttle_retry_delay(sync.throttle_streak, settings.drive_throttle_retry_seconds,
                                                 settings.drive_throttle_max_retry_seconds)
                    delay = max(delay, sync.retry_after)          # never earlier than Google asked
                    log.info("Drive downloads blocked by Google (%d run(s) in a row); extra sync in %d s",
                             services.drive_sync.throttle_streak, delay)
                    context.job_queue.run_once(sync_job, delay, name="drive-sync-retry")
            application.job_queue.run_repeating(sync_job, interval=settings.drive_sync_interval_seconds, first=1,
                                                name="drive-sync")
            log.info("Drive sync scheduled at startup and every %d s", settings.drive_sync_interval_seconds)
        log.info("Bot started (model=%s, router=%s, timezone=%s)", settings.llm_model, settings.llm_router_model,
                 settings.timezone_name)

    async def on_shutdown(application):
        await services.close()

    bot = TelegramBot(settings.telegram_bot_token, services.conversation, on_startup, on_shutdown)
    bot.run()
    return 0


async def run_sync(settings: Settings) -> int:
    services = build_services(settings)
    try:
        if services.drive_sync is None:
            print("GOOGLE_DRIVE_API_KEY is not set (or DRIVE_SYNC_ENABLED=false)")
            return 2
        await startup_checks(services)
        report = await services.drive_sync.run()
        print(f"ok={report.ok} message={report.message} listed={report.listed} added={report.added} "
              f"updated={report.updated} unchanged={report.unchanged} removed={report.removed} "
              f"pending={report.pending} errors={report.errors}")
        for o in report.outcomes:
            print(f"  [{o.kind}] #{o.doc.id} «{o.doc.title}» scope={o.doc.scope} {o.doc.status_reason or ''}")
        return 0 if report.ok else 1
    finally:
        await services.close()


async def run_ask(settings: Settings, question: str) -> int:
    services = build_services(settings)
    try:
        await startup_checks(services)
        replies = await services.conversation.on_text(0, question)
        for r in replies:
            print(r.text)
            for row in r.buttons:
                print("   buttons:", [b.label for b in row])
        return 0
    finally:
        await services.close()


async def run_status(settings: Settings) -> int:
    services = build_services(settings)
    try:
        docs = services.store.documents()
        print("documents by status:", dict(Counter(d.status for d in docs)))
        print("active by source:", dict(Counter(d.source for d in docs if d.status == "active")))
        print("last sync:", services.store.last_sync_run())
        return 0
    finally:
        await services.close()


async def run_docs(settings: Settings) -> int:
    services = build_services(settings)
    try:
        for d in services.store.documents():
            print(f"#{d.id:<4} {d.source:8} {d.status:19} {str(d.scope):34} {d.version_ts[:19]} «{d.title}»"
                  + (f"  [file {d.drive_file_id}]" if d.drive_file_id else "")
                  + (f"  — {d.status_reason}" if d.status_reason else ""))
        return 0
    finally:
        await services.close()


async def run_pending(settings: Settings) -> int:
    services = build_services(settings)
    try:
        import re
        reply = services.conversation._pending_page(0)
        print(re.sub(r"<[^>]+>", "", reply.text))
        return 0
    finally:
        await services.close()


async def run_search(settings: Settings, query: str, mitten: str) -> int:
    from .retrieval import QueryScope
    services = build_services(settings)
    try:
        services.retriever.refresh(force=True)
        result = services.retriever.retrieve(query, QueryScope("mittens", [mitten]))
        for d in result.docs:
            print(f"#{d.document.id} level={d.levels} «{d.document.title}» ({d.document.source}, {d.document.status})")
        if not result.docs:
            print("(no applicable active documents)")
        return 0
    finally:
        await services.close()


def main(argv: list[str]) -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(settings.log_level)
    command = argv[0] if argv else "run"
    if command == "run":
        return run_bot(settings)
    if command == "sync":
        return asyncio.run(run_sync(settings))
    if command == "ask":
        return asyncio.run(run_ask(settings, " ".join(argv[1:])))
    if command == "status":
        return asyncio.run(run_status(settings))
    if command == "docs":
        return asyncio.run(run_docs(settings))
    if command == "pending":
        return asyncio.run(run_pending(settings))
    if command == "search" and len(argv) >= 3:
        return asyncio.run(run_search(settings, argv[1], argv[2]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
