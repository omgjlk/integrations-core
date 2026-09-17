# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Renders every comment scenario to files, for eyeballing the real thing on GitHub.

Unit tests cannot tell you that GitHub failed to parse the Markdown inside a `<details>`, or that a
nested disclosure renders as a wall of brackets. Run as a module from the `ddev` directory, which is
what puts the test helpers on `sys.path`:

    cd ddev
    hatch run python -m tests.cli.ci.tests.preview_pr_comment /tmp/dispatcher-preview

Named ``preview_`` so pytest does not collect it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

from ddev.cli.ci.tests.pr_comment import (
    render_comment,
    render_compact_comment,
    render_minimal_comment,
    render_run_summary,
)
from ddev.cli.ci.tests.progress import DispatcherProgress, ExecutionState, ProgressError
from ddev.cli.ci.tests.status import Status
from ddev.utils.platform import PlatformName
from tests.cli.ci.tests.helpers import attempt, batch_progress, failing_report, job_progress, planned_batch

RUN_URL = "https://github.com/DataDog/integrations-core/actions/runs"


def initial() -> DispatcherProgress:
    return DispatcherProgress(batches=tuple(planned_batch(f"batch-{index:02d}") for index in (1, 2, 3)), done=False)


def retrying() -> DispatcherProgress:
    return DispatcherProgress(
        batches=(
            batch_progress("batch-01", *[job_progress(attempt(), target=f"postgres-{index}") for index in range(4)]),
            batch_progress(
                "batch-02",
                *[job_progress(attempt(), target=f"mysql-{index}") for index in range(4)],
                run_id=122,
                workflow_url=f"{RUN_URL}/122",
            ),
            batch_progress(
                "batch-03",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_connection"),)), target="postgres"),
                job_progress(attempt(Status.FAILURE, failed_steps=("Run E2E tests",)), target="redis"),
                *[job_progress(attempt(), target=f"ntp-{index}") for index in range(2)],
                state=ExecutionState.RETRYING,
                status=None,
                current_attempt=2,
                max_attempts=3,
                run_id=123,
                workflow_url=f"{RUN_URL}/123",
            ),
        ),
        done=False,
    )


def final() -> DispatcherProgress:
    return DispatcherProgress(
        batches=(
            batch_progress("batch-01", *[job_progress(attempt(), target=f"postgres-{index}") for index in range(4)]),
            batch_progress(
                "batch-02",
                *[job_progress(attempt(), target=f"mysql-{index}") for index in range(4)],
                run_id=122,
                workflow_url=f"{RUN_URL}/122",
            ),
            batch_progress(
                "batch-03",
                job_progress(attempt(Status.FAILURE), attempt(Status.SUCCESS, number=2), target="postgres"),
                job_progress(attempt(Status.FAILURE), attempt(Status.FAILURE, number=2), target="redis"),
                job_progress(attempt(Status.SKIPPED), target="consul"),
                job_progress(
                    attempt(Status.FAILURE, reports=(failing_report("test_connection", "test_timeout"),)),
                    target="vault",
                ),
                status=Status.FAILURE,
                run_id=123,
                current_attempt=2,
                max_attempts=3,
                workflow_url=f"{RUN_URL}/123",
            ),
        ),
        done=True,
    )


def incomplete() -> DispatcherProgress:
    """Nothing failed, but not a clean pass either: the only scenario with the ⚠️ heading."""
    return DispatcherProgress(
        batches=(
            batch_progress("batch-01", *[job_progress(attempt(), target=f"postgres-{index}") for index in range(3)]),
            batch_progress(
                "batch-02",
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="mysql"),
                job_progress(attempt(), target="redis"),
                run_id=122,
                workflow_url=f"{RUN_URL}/122",
            ),
            batch_progress(
                "batch-03",
                job_progress(attempt(), target="consul"),
                error=ProgressError.NO_JOB_RESULTS,
                run_id=123,
                workflow_url=f"{RUN_URL}/123",
            ),
        ),
        done=True,
    )


def mixed() -> DispatcherProgress:
    """Failures, an unestablished result and a retry at once, spread across several integrations.

    The case the alert's unestablished count exists for: those results get no section of their own,
    so the count in the alert is the only place the comment admits to them.
    """
    batches = final().batches
    last = batches[-1]
    widened = replace(
        last,
        jobs_progress=(
            *last.jobs_progress,
            job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="mysql"),
        ),
    )
    return DispatcherProgress(batches=(*batches[:-1], widened), done=True)


def wide() -> DispatcherProgress:
    """One integration losing twelve targets to the same two tests: the compressed group shape."""
    return DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(
                            Status.FAILURE,
                            reports=(failing_report("test_metadata_manager", "test_persistent_cache"),),
                            failed_steps=("Run the tests",),
                        ),
                        target="base",
                        environment=f"py3.1{index % 4}",
                        platform=PlatformName.WINDOWS if index % 2 else PlatformName.LINUX,
                        minimum_base_package=index >= 6,
                    )
                    for index in range(12)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )


def at_scale() -> DispatcherProgress:
    """More failing integrations than get a group of their own: the "Show 10 more" disclosures."""
    jobs = [
        job_progress(attempt(Status.FAILURE, failed_steps=("Run the tests",)), target=f"integration-{index:02d}")
        for index in range(1, 26)
    ]
    return DispatcherProgress(
        batches=(
            batch_progress("batch-01", *jobs[:13], status=Status.FAILURE),
            batch_progress("batch-02", *jobs[13:], status=Status.FAILURE, run_id=122, workflow_url=f"{RUN_URL}/122"),
        ),
        done=True,
    )


def main(destination: Path):
    # Simulate the Dispatcher metadata required by the running footer.
    os.environ.setdefault("GITHUB_SERVER_URL", "https://github.com")
    os.environ.setdefault("GITHUB_REPOSITORY", "DataDog/integrations-core")
    os.environ.setdefault("GITHUB_RUN_ID", "12345")

    destination.mkdir(parents=True, exist_ok=True)
    scenarios = (
        ("01-initial", initial()),
        ("02-retrying", retrying()),
        ("03-final", final()),
        ("04-incomplete", incomplete()),
        ("05-mixed", mixed()),
        ("06-wide", wide()),
        ("07-at-scale", at_scale()),
    )
    for name, progress in scenarios:
        path = destination / f"{name}.md"
        path.write_text(render_comment(progress), encoding="utf-8")
        print(f"{path} ({path.stat().st_size} bytes)")

    # The run-summary form, from the failing scenario: the note it prepends only shows up when the
    # comment could not be written.
    summary = destination / "08-run-summary-comment-failed.md"
    summary.write_text(render_run_summary(render_comment(final()), pr_comment_failed=True), encoding="utf-8")
    print(f"{summary} ({summary.stat().st_size} bytes)")

    # The two fallback tiers, never seen in a normal run, from the scenario that triggers them: the
    # integration count is what makes a body too long, so it is what the fallbacks shed.
    for name, render in (("09-compact", render_compact_comment), ("10-minimal", render_minimal_comment)):
        path = destination / f"{name}.md"
        path.write_text(render(at_scale()), encoding="utf-8")
        print(f"{path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dispatcher-preview"))
