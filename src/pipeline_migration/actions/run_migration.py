"""The run-migration subcommand: run one migration script in the sandbox, as ``migrate`` does.

It lets task repositories check that their migration scripts work in the container sandbox (see
``pipeline_migration.actions.migrate.sandbox``) before the scripts are released.
"""

import argparse
import logging
import os
import sys

from pipeline_migration.actions.migrate import sandbox
from pipeline_migration.actions.migrate.constants import MIGRATION_TIMEOUT_SECONDS

logger = logging.getLogger("run_migration")

# Exit statuses that never come from the migration script itself, as in timeout(1)
EXIT_TIMEOUT = 124
EXIT_SANDBOX_ERROR = 125
# A failing script exits with this status instead of one of the reserved statuses above
EXIT_SCRIPT_FAILED = 1


def _existing_file(value: str) -> str:
    if not os.path.isfile(value):
        raise argparse.ArgumentTypeError(f"{value} is not an existing file")
    return value


def register_cli(subparser) -> None:
    """Register the run-migration subcommand and its arguments."""
    parser: argparse.ArgumentParser = subparser.add_parser(
        "run-migration",
        help="Run a migration script on a pipeline file in the container sandbox.",
        description="Run a migration script on a Pipeline or PipelineRun file in the container "
        "sandbox, exactly as the migrate subcommand runs it, whatever PMT_SANDBOX is set to. "
        "The pipeline file is updated only when the script succeeds. The output of the script is "
        "written to stdout. "
        "Exit status: the exit status of the script, "
        f"{EXIT_SCRIPT_FAILED} when the script left no usable pipeline file, "
        f"{EXIT_TIMEOUT} when the script timed out and "
        f"{EXIT_SANDBOX_ERROR} when the sandbox itself cannot be used on this machine. "
        f"A script exiting with {EXIT_TIMEOUT} or {EXIT_SANDBOX_ERROR} is reported as "
        f"{EXIT_SCRIPT_FAILED}.",
    )
    parser.add_argument("migration_file", type=_existing_file, help="Path to the migration script.")
    parser.add_argument("pipeline_file", type=_existing_file, help="Path to the pipeline file.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=MIGRATION_TIMEOUT_SECONDS,
        help="Seconds after which the script is killed. Defaults to %(default)s, as in migrate.",
    )
    parser.set_defaults(action=action)


def action(args) -> int:
    """Execute the run-migration command and return the exit status."""
    try:
        sandbox.prepare()
    except sandbox.SandboxError as e:
        logger.error("The sandbox cannot be used on this machine: %s", e)
        return EXIT_SANDBOX_ERROR

    try:
        proc = sandbox.run_migration(args.migration_file, args.pipeline_file, timeout=args.timeout)
    except TimeoutError as e:
        logger.error("%s", e)
        return EXIT_TIMEOUT
    except RuntimeError as e:
        # The script succeeded, but left no usable pipeline file (removed, symlink, too big)
        logger.error("%s", e)
        return EXIT_SCRIPT_FAILED

    sys.stdout.write(proc.stdout.decode(errors="replace"))
    sys.stdout.flush()
    if proc.returncode in (EXIT_TIMEOUT, EXIT_SANDBOX_ERROR):
        logger.error(
            "Migration script exited with code %d, reported as %d",
            proc.returncode,
            EXIT_SCRIPT_FAILED,
        )
        return EXIT_SCRIPT_FAILED
    if proc.returncode != 0:
        logger.error("Migration script exited with code %d", proc.returncode)
    return proc.returncode
