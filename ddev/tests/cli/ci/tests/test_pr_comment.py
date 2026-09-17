# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Tests for the PR comment renderer.

The first section pins the whole rendered body for each state the comment goes through, because the
layout is specified as exact Markdown and a shape that drifts is the defect. Everything after it
asserts the reporting rules rather than the prose: an unfinished run must be unmistakable, a batch's
status comes from the workflow rather than its jobs, unavailable results never read as success, and
nothing is dropped silently.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable

import pytest
from markdown_it import MarkdownIt

from ddev.cli.ci.tests.pr_comment import (
    ALERT_RUNNING_NOTE,
    CANCELLED_HEADING,
    CANCELLED_NOTE,
    CANCELLED_WITHOUT_RESULTS_NOTE,
    COMMENT_MARKER,
    FAILED_HEADING,
    GROUP_LIMIT,
    PROGRESS_BAR_ASSETS,
    PROGRESS_BAR_WIDTH,
    SHUTDOWN_HEADINGS,
    SHUTDOWN_REASON_LIMIT,
    STOPPED_NOTE,
    STOPPED_WITHOUT_RESULTS_NOTE,
    render_comment,
    render_compact_comment,
    render_minimal_comment,
    render_run_summary,
    render_shutdown_notice,
    summary_line,
)
from ddev.cli.ci.tests.progress import DispatcherProgress, ExecutionState, ProgressError
from ddev.cli.ci.tests.status import Status
from ddev.event_bus.shutdown import ShutdownKind, ShutdownRequest
from ddev.utils.platform import PlatformName
from tests.cli.ci.tests.helpers import (
    attempt,
    batch_progress,
    failing_report,
    job_progress,
    planned_batch,
    uniform_progress,
)

# GitHub's own ceiling, from the 422 it returns: "body is too long (maximum is 65536 characters)".
# Not imported from the renderer on purpose — a test that reads the same constant it is checking
# would pass no matter what that constant said.
GITHUB_COMMENT_HARD_LIMIT = 65_536

# What the autouse `github_actions_env` fixture makes the footer link to.
DISPATCH_RUN_URL = "https://github.com/DataDog/integrations-core/actions/runs/12345"
# `batch_progress`'s default, which the batch strip links each batch to.
BATCH_RUN_URL = "https://github.com/o/r/actions/runs/121"
# `attempt`'s default, which each failed target links to.
TARGET_JOB_URL = "https://github.com/o/r/actions/runs/1/job/9"


def _progress_bar_of(body: str) -> dict[str, int]:
    """The rendered progress bar, as the pixel width of each segment it drew."""
    return {segment: int(width) for segment, width in re.findall(r'progress-(\w+)\.png" width="(\d+)"', body)}


def _bar(**segments: int) -> str:
    """The bar the renderer draws for these segment widths, as one `<img>` per segment."""
    return "".join(
        f'<img src="{PROGRESS_BAR_ASSETS}/progress-{segment}.png" width="{width}" height="10" alt="">'
        for segment, width in segments.items()
    )


def _batch_strip_of(body: str) -> str:
    """The one line the batch state is rendered onto."""
    return next(line for line in body.splitlines() if line.startswith("Batches · "))


def _group_summaries_of(body: str) -> list[str]:
    """Every failing integration's summary line, in the order the comment lists them."""
    return re.findall(r"^(?:<summary>)?([❌⚠️].*? — \d+ targets?, .*?)(?:</summary>)?$", body, re.MULTILINE)


def shutdown_request(kind: ShutdownKind) -> ShutdownRequest:
    """A representative request for *kind*, for the tests that render a stopped run."""
    if kind is ShutdownKind.CANCELLED:
        return ShutdownRequest.cancelled()
    if kind is ShutdownKind.FAILED:
        return ShutdownRequest.failed(RuntimeError("a fatal error"))
    return ShutdownRequest.timed_out(RuntimeError("a fatal error"))


@pytest.fixture
def on_a_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit a finished run's footer names, which the shared env fixture leaves unset."""
    monkeypatch.setenv("GITHUB_SHA", "ff9caa5eb1f0c3d2a4b6")


# ---------------------------------------------------------------------------
# The whole body, per state
#
# The layout is specified as exact Markdown, down to the blank line after every `</summary>` that
# GitHub needs in order to parse the disclosure's contents at all. Substring assertions cannot catch
# a lost blank line or a block that moved, so these pin the body and the rest of the file does not.
# ---------------------------------------------------------------------------


def test_a_queued_run_renders_the_plan_and_nothing_else():
    """Nothing has run, so the comment is the plan: no links yet, and no failure section."""
    progress = DispatcherProgress(
        batches=(planned_batch("batch-01", job_count=3), planned_batch("batch-02", job_count=2)),
        done=False,
    )

    assert render_comment(progress) == (
        f"""{COMMENT_MARKER}

## 🔄 Dispatcher tests · in progress

> **Dispatcher beta: informational only**
> Existing CI remains the merge signal.

> [!NOTE]
> **Tests are still running.** 2 of 2 batches have not finished yet. 5 of 5 jobs have not reported. \
This comment updates automatically.

{_bar(pending=240)}&nbsp; **0/5 jobs**

⏳ 5 pending

Batches · ⏳ `batch-01` 0/3 · ⏳ `batch-02` 0/2 — *links available after dispatch*

<sub>⏳ Dispatcher running — [GitHub Run]({DISPATCH_RUN_URL}).</sub>"""
    )


def test_a_running_run_reports_the_failures_it_has_and_warns_of_the_ones_it_may_get():
    """Two integrations have failed in a finished batch while two batches are still reporting."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *_ddev_targets(),
                *_checkpoint_targets(),
                status=Status.FAILURE,
            ),
            batch_progress(
                "batch-02",
                job_progress(attempt(), target="vault"),
                job_progress(target="consul"),
                state=ExecutionState.RUNNING,
                status=None,
            ),
            batch_progress(
                "batch-03",
                job_progress(attempt(), target="nginx"),
                state=ExecutionState.ARTIFACT_DOWNLOAD,
                status=None,
            ),
        ),
        done=False,
    )

    assert render_comment(progress) == (
        f"""{COMMENT_MARKER}

## 🔄 Dispatcher tests · in progress

> **Dispatcher beta: informational only**
> Existing CI remains the merge signal.

> [!NOTE]
> **Tests are still running.** 2 of 3 batches have not finished yet. 1 of 7 jobs have not reported. \
This comment updates automatically.

{_bar(passed=69, failed=137, pending=34)}&nbsp; **6/7 jobs**

✅ 2 passed · ❌ 4 failed · ⏳ 1 pending

Batches · ❌ [batch-01]({BATCH_RUN_URL}) 4/4 · 🔄 [batch-02]({BATCH_RUN_URL}) 1/2 · \
📥 [batch-03]({BATCH_RUN_URL}) 1/1

<details>
<summary>❌ <code>checkpoint_harmony_endpoint</code> — 2 targets, 1 step</summary>

- [`py3.13 / linux`]({TARGET_JOB_URL}) · batch-01 · step `Run the tests`
- [`py3.13 / linux / minimum base package`]({TARGET_JOB_URL}) · batch-01 · step `Run the tests`

</details>

<details>
<summary>❌ <code>ddev</code> — 2 targets, 1 test</summary>

- [`default / linux`]({TARGET_JOB_URL}) · batch-01 · test `test_dispatch_tests_plans_from_hatch_toml`
- [`default / windows`]({TARGET_JOB_URL}) · batch-01 · test `test_dispatch_tests_plans_from_hatch_toml`

</details>

More failures may appear as `batch-02` and `batch-03` report.

<sub>⏳ Dispatcher running — [GitHub Run]({DISPATCH_RUN_URL}).</sub>"""
    )


def test_a_failed_run_groups_every_kind_of_bad_news_by_integration(on_a_commit):
    """Failed tests, a failed step, missing artifacts alongside a failure, and a lone unknown result.

    One group per integration regardless of which of those it is, because the reader's question is
    about the integration and not about which of the four shapes its answer happens to take.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress("batch-01", *_ddev_targets(), *_checkpoint_targets(), status=Status.FAILURE),
            batch_progress("batch-02", *_kafka_targets(), _kuma_target(), status=Status.FAILURE),
            batch_progress(
                "batch-03",
                job_progress(
                    attempt(Status.FAILURE, reports=(failing_report("test_pg_stat_statements_dealloc_v2"),)),
                    target="postgres",
                    environment="py3.13-18.0-C",
                ),
                job_progress(attempt(), target="mysql"),
                status=Status.FAILURE,
            ),
            batch_progress(
                "batch-04",
                job_progress(attempt(), target="redisdb"),
                job_progress(attempt(), target="zk"),
            ),
        ),
        done=True,
    )

    assert render_comment(progress) == (
        f"""{COMMENT_MARKER}

## ❌ Dispatcher tests · failed

> **Dispatcher beta: informational only**
> Existing CI remains the merge signal.

> [!CAUTION]
> **4 integrations failed.** 7 of 11 jobs failed; 3 results could not be established.

{_bar(passed=87, failed=153)}&nbsp; **11/11 jobs**

✅ 4 passed · ❌ 7 failed

Batches · ❌ [batch-01]({BATCH_RUN_URL}) 4/4 · ❌ [batch-02]({BATCH_RUN_URL}) 3/3 · \
❌ [batch-03]({BATCH_RUN_URL}) 2/2 · ✅ [batch-04]({BATCH_RUN_URL}) 2/2

<details>
<summary>❌ <code>checkpoint_harmony_endpoint</code> — 2 targets, 1 step</summary>

- [`py3.13 / linux`]({TARGET_JOB_URL}) · batch-01 · step `Run the tests`
- [`py3.13 / linux / minimum base package`]({TARGET_JOB_URL}) · batch-01 · step `Run the tests`

</details>

<details>
<summary>❌ <code>ddev</code> — 2 targets, 1 test</summary>

- [`default / linux`]({TARGET_JOB_URL}) · batch-01 · test `test_dispatch_tests_plans_from_hatch_toml`
- [`default / windows`]({TARGET_JOB_URL}) · batch-01 · test `test_dispatch_tests_plans_from_hatch_toml`

</details>

<details>
<summary>❌ <code>kafka_actions</code> — 2 targets, ⚠️ no artifacts</summary>

- [`py3.12 / linux`]({TARGET_JOB_URL}) · batch-02 · step `Run ./.github/actions/setup-ddev`
- [`py3.12 / linux / minimum base package`]({TARGET_JOB_URL}) · batch-02 · step `Run ./.github/actions/setup-ddev`

⚠️ artifacts could not be downloaded — test results unknown

</details>

<details>
<summary>❌ <code>postgres</code> — 1 target, 1 test</summary>

- [`py3.13-18.0-C / linux`]({TARGET_JOB_URL}) · batch-03 · test `test_pg_stat_statements_dealloc_v2`

</details>

<details>
<summary>⚠️ <code>kuma</code> — 1 target, result not established</summary>

- [`py3.13-2.10.6 / linux`]({TARGET_JOB_URL}) · batch-02

⚠️ artifacts could not be downloaded — test results unknown

</details>

<sub>Dispatcher finished on `ff9caa5` — [GitHub Run]({DISPATCH_RUN_URL}).</sub>"""
    )


def test_a_clean_run_collapses_to_the_batch_strip(on_a_commit):
    """Nothing failed, so there is nothing to disclose: the totals and the batches are the report."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(), target="redisdb"),
                job_progress(attempt(), target="nginx"),
                job_progress(attempt(), target="vault"),
            ),
            batch_progress(
                "batch-02",
                job_progress(attempt(), target="consul"),
                job_progress(attempt(), target="zk"),
            ),
        ),
        done=True,
    )

    assert render_comment(progress) == (
        f"""{COMMENT_MARKER}

## ✅ Dispatcher tests · passed

> **Dispatcher beta: informational only**
> Existing CI remains the merge signal.

{_bar(passed=240)}&nbsp; **5/5 jobs**

✅ 5 passed · nothing failed

Batches · ✅ [batch-01]({BATCH_RUN_URL}) 3/3 · ✅ [batch-02]({BATCH_RUN_URL}) 2/2

<sub>Dispatcher finished on `ff9caa5` — [GitHub Run]({DISPATCH_RUN_URL}).</sub>"""
    )


def _ddev_targets() -> list:
    """Two targets of one integration failing the same single test, on two platforms."""
    return [
        job_progress(
            attempt(
                Status.FAILURE,
                reports=(failing_report("test_dispatch_tests_plans_from_hatch_toml"),),
                failed_steps=("Run the tests",),
            ),
            target="ddev",
            environment="default",
            platform=platform,
        )
        for platform in (PlatformName.LINUX, PlatformName.WINDOWS)
    ]


def _checkpoint_targets() -> list:
    """Two targets failing a step, with no test-level detail: an ordinary job and its replica."""
    return [
        job_progress(
            attempt(Status.FAILURE, failed_steps=("Run the tests",)),
            target="checkpoint_harmony_endpoint",
            environment="py3.13",
            minimum_base_package=minimum,
        )
        for minimum in (False, True)
    ]


def _kafka_targets() -> list:
    """Two targets that failed in setup, so their artifacts never arrived either."""
    return [
        job_progress(
            attempt(
                Status.FAILURE,
                failed_steps=("Run ./.github/actions/setup-ddev",),
                error=ProgressError.NO_ARTIFACTS,
            ),
            target="kafka_actions",
            environment="py3.12",
            minimum_base_package=minimum,
        )
        for minimum in (False, True)
    ]


def _kuma_target():
    """A target that concluded successfully but whose artifacts never arrived: result unknown."""
    return job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="kuma", environment="py3.13-2.10.6")


# ---------------------------------------------------------------------------
# Grouping by integration
# ---------------------------------------------------------------------------


def test_groups_are_ordered_by_how_many_targets_they_lost():
    """The integration in the most trouble is the one a reader should see without scrolling."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(attempt(Status.FAILURE), target="postgres", environment=f"py3.1{index}")
                    for index in range(3)
                ],
                job_progress(attempt(Status.FAILURE), target="mysql"),
                *[
                    job_progress(attempt(Status.FAILURE), target="base", environment=f"py3.1{index}")
                    for index in range(2)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    integrations = re.findall(r"<summary>❌ <code>(\w+)</code>", render_comment(progress))

    assert integrations == ["postgres", "base", "mysql"]


def test_every_target_names_its_own_test_even_when_it_repeats():
    """A bullet must be readable on its own line, without tracking a reference back up the list.

    The alternative — saying it once and then "same test" — makes a reader scanning for a test name
    stop and look upward to find out what the reference pointed at, which is the opposite of what
    scanning a list of targets is for.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=("Install deps",)),
                        target="postgres",
                        environment=f"py3.1{index}",
                    )
                    for index in range(3)
                ],
                job_progress(
                    attempt(Status.FAILURE, reports=(failing_report("test_a"),)),
                    target="mysql",
                    environment="py3.13",
                ),
                job_progress(
                    attempt(Status.FAILURE, reports=(failing_report("test_a"),)),
                    target="mysql",
                    environment="py3.12",
                ),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert body.count("· step `Install deps`") == 3
    assert body.count("· test `test_a`") == 2
    assert "same step" not in body
    assert "same test" not in body


def test_a_target_with_nothing_to_name_is_still_listed():
    """A failed target with neither a single test nor a single step keeps its bullet, unqualified."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(
                    attempt(Status.FAILURE, reports=(failing_report("test_a"),)),
                    target="postgres",
                    environment="py3.13",
                ),
                job_progress(attempt(Status.FAILURE), target="postgres", environment="py3.12"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    bullets = [line for line in render_comment(progress).splitlines() if line.startswith("- ")]

    assert bullets[0].endswith("· test `test_a`")
    assert bullets[1].endswith("· batch-01")


@pytest.mark.parametrize(
    ("targets", "collapsed"),
    [pytest.param(4, False, id="at-the-preview-limit"), pytest.param(5, True, id="one-past-it")],
)
def test_the_targets_collapse_onto_one_line_only_once_there_are_too_many_to_list(targets: int, collapsed: bool):
    """The boundary between a bullet list and the compressed line, which an off-by-one would move."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=("Run the tests",)),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(targets)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert ("+ 1 more" in body) is collapsed
    assert (body.count("\n- [") == targets) is not collapsed


def test_a_group_spanning_batches_names_every_batch_it_lost_a_target_in():
    """An integration's targets are partitioned across batches, so one batch is not the whole story."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=("Run the tests",)),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(3)
                ],
                status=Status.FAILURE,
            ),
            batch_progress(
                "batch-02",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=("Run the tests",)),
                        target="base",
                        environment=f"py3.{index}",
                        platform=PlatformName.WINDOWS,
                    )
                    for index in range(3)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    assert "+ 2 more\nbatch-01, batch-02 · step `Run the tests`" in render_comment(progress)


def test_a_wide_group_whose_targets_failed_for_different_reasons_claims_none_of_them():
    """A shared qualifier is only shared if every target has it, or the line invents a common cause.

    The compressed form has one slot for what the targets share, so five different failing steps
    cannot go in it. They are listed below the targets instead of being dropped, which is the one
    place a reader can learn them without opening five jobs.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=(f"Step {index}",)),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(5)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "+ 1 more\nbatch-01\n" in body
    # No step is promoted to the shared line, which would claim a common cause they do not have.
    assert "batch-01 · step" not in body
    # They are named by the list under the targets rather than lost.
    assert "5 failed steps across 5 targets:" in body
    assert "- `Step 4`" in body


def test_a_target_with_no_environment_is_not_labelled_with_a_stray_separator():
    """A target that defines no environments has an empty `BatchJob.environment`."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, failed_steps=("Install deps",)), target="ddev", environment=""),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "[`linux`]" in body
    assert "[` / linux`]" not in body


def test_a_wide_group_lists_its_targets_on_one_line_and_its_tests_once():
    """Twelve targets failing the same three tests is two lines and a list, not twelve bullets.

    Bullet-per-target with three test names repeated against each is the single largest thing this
    renderer can emit, and it is almost entirely the same text over and over.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(
                            Status.FAILURE,
                            reports=(failing_report("test_a", "test_b", "test_c"),),
                            failed_steps=("Run the tests",),
                        ),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(12)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>base</code> — 12 targets, 3 tests</summary>" in body
    # Four targets are named and the rest are counted, all on one line.
    assert (
        f"[`py3.0 / linux`]({TARGET_JOB_URL}) · [`py3.1 / linux`]({TARGET_JOB_URL}) · "
        f"[`py3.2 / linux`]({TARGET_JOB_URL}) · [`py3.3 / linux`]({TARGET_JOB_URL}) · + 8 more\nbatch-01"
    ) in body
    assert "py3.4 / linux" not in body
    # The tests they share are listed once, fully qualified, rather than per target.
    assert "All 12 failed the same 3 tests:" in body
    assert body.count("- `tests.test_check::test_a`") == 1
    assert "- `tests.test_check::test_b`" in body


def test_a_wide_group_whose_targets_failed_differently_does_not_claim_they_are_the_same():
    """ "All N failed the same M tests" is a claim about the targets, so it must be checked."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, reports=(failing_report(f"test_{index}", "test_shared"),)),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(5)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "failed the same" not in body
    assert "6 failed tests across 5 targets:" in body


def test_a_target_failing_several_steps_still_names_them():
    """A target with more than one failed step gets no qualifier on its bullet, so without a list
    the names the gatherer deliberately collects are counted by the summary and never shown.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(
                    attempt(Status.FAILURE, failed_steps=("Run the tests", "Upload the job's reports")),
                    target="kuma",
                ),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>kuma</code> — 1 target, 2 steps</summary>" in body
    assert "All 1 failed the same 2 steps:" in body
    assert "- `Run the tests`" in body
    assert "- `Upload the job's reports`" in body


def test_a_group_whose_targets_failed_different_steps_does_not_claim_they_are_the_same():
    """The same claim as for tests, and just as checkable: these targets share one of the two."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(
                    attempt(Status.FAILURE, failed_steps=("Run the tests", "Set up Python")),
                    target="kuma",
                    environment="py3.12",
                ),
                job_progress(
                    attempt(Status.FAILURE, failed_steps=("Run the tests", "Configure ddev")),
                    target="kuma",
                    environment="py3.13",
                ),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "failed the same" not in body
    assert "3 failed steps across 2 targets:" in body
    assert "- `Set up Python`" in body
    assert "- `Configure ddev`" in body


def test_steps_are_not_listed_when_tests_already_explain_the_group():
    """The summary counts tests, not steps, when both are known, and the body must agree with it.

    A failing test is what a reader acts on; the step that ran it adds nothing but length.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(
                    attempt(
                        Status.FAILURE,
                        failed_steps=("Run the tests", "Upload the job's reports"),
                        reports=(failing_report("test_a", "test_b"),),
                    ),
                    target="kuma",
                ),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>kuma</code> — 1 target, 2 tests</summary>" in body
    assert "failed steps" not in body
    assert "Upload the job's reports" not in body


def test_a_single_failed_step_is_named_on_its_bullet_and_not_listed_twice():
    """One step is a qualifier, so a list of one would repeat the bullet it sits under."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, failed_steps=("Run the tests",)), target="kuma"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert body.count("`Run the tests`") == 1
    assert "failed the same" not in body


def test_a_compressed_group_names_its_only_test_when_the_target_line_cannot():
    """The compressed line names a qualifier only when every target shares one, so a group mixing a
    test failure with a target that reported nothing carried the test name nowhere: the summary
    counted `1 test` and the body never said which.
    """
    jobs = [
        job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="base", environment="py3.13")
    ]
    jobs += [
        job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="base", environment=f"py3.1{index}")
        for index in range(4)
    ]
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE),),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>base</code> — 5 targets, 1 test</summary>" in body
    assert "1 failed test across 5 targets:" in body
    assert "- `tests.test_check::test_a`" in body


def test_a_compressed_group_names_its_only_step_when_the_target_line_cannot():
    """The same hole for steps, which shared the threshold that assumed a bullet had named them."""
    jobs = [
        job_progress(
            attempt(Status.FAILURE, failed_steps=("Run the tests",)), target="base", environment=f"py3.1{index}"
        )
        for index in range(4)
    ]
    jobs.append(job_progress(attempt(Status.FAILURE), target="base", environment="py3.9"))
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE),),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>base</code> — 5 targets, 1 step</summary>" in body
    assert "1 failed step across 5 targets:" in body
    assert "- `Run the tests`" in body


def test_a_shared_qualifier_is_not_also_listed_below_the_targets():
    """When every target does share the one test, the compressed line carries it and the list must
    not repeat it. This is the duplication the lowered threshold would otherwise introduce.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, reports=(failing_report("test_a"),)),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(5)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "batch-01 · test `test_a`" in body
    assert "failed test across" not in body
    assert "failed the same" not in body
    assert body.count("test_a") == 1


def test_a_wide_group_failing_several_steps_names_them_once():
    """The compressed form carries no per-target qualifier at all, so the list is the only place
    a reader can learn which steps failed.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                *[
                    job_progress(
                        attempt(Status.FAILURE, failed_steps=("Run the tests", "Set up Python")),
                        target="base",
                        environment=f"py3.{index}",
                    )
                    for index in range(12)
                ],
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>❌ <code>base</code> — 12 targets, 2 steps</summary>" in body
    assert "All 12 failed the same 2 steps:" in body
    assert body.count("- `Set up Python`") == 1


@pytest.mark.parametrize(
    ("job_attempt", "expected"),
    [
        pytest.param(
            attempt(Status.FAILURE, reports=(failing_report("test_a", "test_b"),)),
            "2 tests",
            id="named-tests",
        ),
        pytest.param(
            attempt(Status.FAILURE, failed_steps=("Install deps", "Upload")),
            "2 steps",
            id="failed-steps-only",
        ),
        pytest.param(
            attempt(Status.FAILURE, failed_steps=("Install deps",), error=ProgressError.NO_ARTIFACTS),
            "⚠️ no artifacts",
            id="failed-with-artifacts-missing",
        ),
        pytest.param(attempt(error=ProgressError.NO_ARTIFACTS), "result not established", id="only-unknown"),
        pytest.param(attempt(Status.FAILURE), "no failure detail", id="failed-with-nothing-to-show"),
        pytest.param(
            dataclasses.replace(attempt(Status.FAILURE), reports=None),
            "details pending",
            id="artifacts-not-collected-yet",
        ),
    ],
)
def test_a_group_says_what_is_known_about_why_it_failed(job_attempt, expected: str):
    """A named test outranks a step, and a missing artifact outranks the step that failed to fetch it.

    The step that fails when artifacts go missing is the collection step, so naming it tells a reader
    nothing about their integration while implying the failure was understood.
    """
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(job_attempt), status=Status.FAILURE),),
        done=True,
    )

    assert f"— 1 target, {expected}</summary>" in render_comment(progress)


def test_an_unknown_result_joins_its_integration_rather_than_a_section_of_its_own():
    """One integration, one place to look, whether its result is bad or simply absent."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="postgres"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="postgres", environment="py3.13"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert body.count("<details>") == 1
    assert "<summary>❌ <code>postgres</code> — 2 targets, 1 test</summary>" in body
    assert "⚠️ artifacts could not be downloaded — test results unknown" in body
    assert "Unavailable results" not in body


def test_a_group_with_only_unknown_results_is_a_warning_not_a_failure():
    """Nothing failed here; the answer is missing, and the chip must not claim otherwise."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="kuma"),
                job_progress(attempt(error=ProgressError.TIMED_OUT), target="kuma", environment="py3.13"),
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "<summary>⚠️ <code>kuma</code> — 2 targets, results not established</summary>" in body
    assert "❌ <code>kuma</code>" not in body
    # One line per distinct reason, so two targets failing the same way say it once.
    assert body.count("⚠️ artifacts could not be downloaded") == 1
    assert "⚠️ timed out before results were gathered" in body


def test_a_replica_is_distinguishable_from_its_ordinary_job():
    """The pair shares integration, environment and platform, so the variant is all that tells them apart."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, failed_steps=("Run unit tests",))),
                job_progress(attempt(Status.FAILURE, failed_steps=("Run unit tests",)), minimum_base_package=True),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "[`py3.12 / linux`]" in body
    assert "[`py3.12 / linux / minimum base package`]" in body


def test_a_target_without_a_job_url_is_still_named():
    """A job whose URL never arrived must not vanish from its group for want of a link."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, failed_steps=("Install deps",), job_url=None)),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "- `py3.12 / linux` · batch-01 · step `Install deps`" in body
    assert "](" not in body.split("<details>")[1].split("</details>")[0]


# ---------------------------------------------------------------------------
# Many failing integrations
# ---------------------------------------------------------------------------


def _many_failing(count: int, *, tests_per_integration: int = 0) -> DispatcherProgress:
    """*count* integrations, one failed target each, spread across two batches.

    With *tests_per_integration* set, each one also fails that many tests, which is how a run gets
    large enough for the byte budget to start refusing things.
    """
    jobs: dict[str, list] = {"batch-01": [], "batch-02": []}
    for index in range(1, count + 1):
        batch = "batch-01" if index % 2 else "batch-02"
        reports = (
            (failing_report(*[f"test_number_{number}" for number in range(tests_per_integration)]),)
            if tests_per_integration
            else ()
        )
        jobs[batch].append(
            job_progress(
                attempt(Status.FAILURE, failed_steps=("Run the tests",), reports=reports),
                target=f"integration-{index:02d}",
            )
        )
    return DispatcherProgress(
        batches=(
            batch_progress("batch-01", *jobs["batch-01"], status=Status.FAILURE),
            batch_progress("batch-02", *jobs["batch-02"], status=Status.FAILURE),
        ),
        done=True,
    )


POINTER = "listed in the failed batches links above"


def test_integrations_past_the_limit_go_behind_one_disclosure():
    """Twelve failing integrations is ten groups, a pointer, and the rest one click away.

    Every integration stays expandable — the point of the layout is that a reader can open the one
    they care about — but past `GROUP_LIMIT` they stop competing for the top of the comment.
    """
    body = render_comment(_many_failing(12))

    assert body.count("<summary>❌ <code>integration-") == 12
    # The first ten are groups of their own, in order.
    first_ten = body.split("2 other integrations are")[0]
    assert re.findall(r"<code>(integration-\d+)</code>", first_ten) == [
        f"integration-{index:02d}" for index in range(1, GROUP_LIMIT + 1)
    ]
    assert f"2 other integrations are {POINTER}" in body
    assert body.count("<summary>Show 2 more</summary>") == 1


def test_every_hidden_integration_goes_behind_the_same_disclosure():
    """One disclosure, not a chain of them: the reader opens it once and has the whole tail."""
    body = render_comment(_many_failing(25))

    assert f"15 other integrations are {POINTER}" in body
    assert body.count("<summary>Show ") == 1
    assert "<summary>Show 15 more</summary>" in body
    assert body.count("<summary>❌ <code>integration-") == 25


def test_one_integration_past_the_limit_is_said_in_the_singular():
    body = render_comment(_many_failing(GROUP_LIMIT + 1))

    assert f"1 other integration is {POINTER}" in body
    assert "<summary>Show 1 more</summary>" in body


def test_at_or_under_the_limit_nothing_is_hidden():
    body = render_comment(_many_failing(GROUP_LIMIT))

    assert body.count("<summary>❌ <code>integration-") == GROUP_LIMIT
    assert "other integration" not in body
    assert "Show " not in body


def test_a_disclosure_too_large_to_fit_is_dropped_and_the_pointer_remains():
    """The degradation that the byte budget actually forces, and the shape it has to leave behind.

    Keeping part of the tail would be worse than keeping none of it: nothing in the body would say
    which part was kept, so a reader could not tell a missing integration from a passing one.
    """
    progress = _many_failing(24, tests_per_integration=160)

    body = render_comment(progress)

    assert len(body.encode("utf-8")) <= GITHUB_COMMENT_HARD_LIMIT
    # The ten groups meant to be shown all survive, with their detail.
    assert body.count("<summary>❌ <code>integration-") == GROUP_LIMIT
    assert "test_number_0" in body
    # The disclosure is gone entirely rather than half-filled, and the pointer accounts for it.
    assert "Show " not in body
    assert f"14 other integrations are {POINTER}" in body


def test_the_pointer_is_not_replaced_by_the_generic_overflow_note():
    """The pointer says where the missing integrations went; the overflow note does not.

    Falling back to "N more not shown" here would drop the only sentence that tells a reader the
    comment is not the whole list.
    """
    body = render_comment(_many_failing(24, tests_per_integration=160))

    assert POINTER in body
    assert "not shown — the comment reached its size limit" not in body


def test_the_compact_tier_keeps_the_pointer_and_drops_what_it_points_past():
    """What makes a body too long is the integration count, so that is what the fallback sheds.

    The pointer survives because with the disclosure gone it is the only thing that accounts for
    the integrations the alert counted but the body does not show.
    """
    progress = _many_failing(25)

    compact = render_compact_comment(progress)

    assert compact.count("<summary>❌ <code>integration-") == GROUP_LIMIT
    assert f"15 other integrations are {POINTER}" in compact
    assert "Show " not in compact
    assert len(compact.encode("utf-8")) < len(render_comment(progress).encode("utf-8"))


# ---------------------------------------------------------------------------
# Batch strip
# ---------------------------------------------------------------------------


def test_batch_status_is_the_workflow_not_a_roll_up_of_its_jobs():
    """A workflow can fail in a setup or upload step while every tracked job passes.

    The `BatchProgress.status` field is the workflow's own conclusion, so re-deriving it from the jobs
    would render this batch as passed and hide a real failure.
    """
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), job_progress(attempt()), status=Status.FAILURE),),
        done=True,
    )

    body = render_comment(progress)

    assert _batch_strip_of(body).startswith("Batches · ❌ ")
    assert "## ❌ Dispatcher tests · failed" in body
    # There is no failed job to group, so say so rather than print an empty section.
    assert "the workflow failed with no tracked job failure" in body


def test_a_finished_batch_with_no_status_says_so_rather_than_guessing():
    """A finished batch has a `None` `status` only if something went wrong upstream."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), status=None, error=ProgressError.NO_JOB_RESULTS),),
        done=True,
    )

    body = render_comment(progress)

    assert _batch_strip_of(body).startswith("Batches · ❔ ")
    assert "✅" not in _batch_strip_of(body)


def test_a_running_batch_and_a_retrying_batch_are_indistinguishable():
    """Both are just unfinished work, so they must not render differently.

    A rerun is Dispatcher's own business; surfacing it at the batch level asks the reader to reason
    about scheduling when all they can act on is the result.
    """
    bodies = [
        render_comment(
            DispatcherProgress(
                batches=(
                    batch_progress(
                        "batch-01", job_progress(), state=state, status=None, current_attempt=2, max_attempts=3
                    ),
                ),
                done=False,
            )
        )
        for state in (ExecutionState.RUNNING, ExecutionState.RETRYING)
    ]

    assert bodies[0] == bodies[1]
    assert _batch_strip_of(bodies[0]).startswith("Batches · 🔄 ")
    assert "retrying" not in bodies[0]
    assert "attempt" not in bodies[0]


def test_a_collecting_batch_reads_as_collecting_whatever_its_status_is():
    """Its conclusion is known but its results are not, and the results are what the comment is for."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(dataclasses.replace(attempt(Status.FAILURE), reports=None)),
                state=ExecutionState.ARTIFACT_DOWNLOAD,
                status=Status.FAILURE,
            ),
        ),
        done=False,
    )

    body = render_comment(progress)

    assert _batch_strip_of(body) == f"Batches · 📥 [batch-01]({BATCH_RUN_URL}) 1/1"
    assert "**Tests finished; collecting results.**" in body
    # The execution outcome is reported even though the test detail has not arrived.
    assert "— 1 target, details pending</summary>" in body


@pytest.mark.parametrize("job_finished", [False, True], ids=["awaiting-job-status", "job-passed"])
def test_workflow_only_failure_requires_observed_job_outcomes(job_finished: bool):
    job = job_progress(attempt()) if job_finished else job_progress()
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job, state=ExecutionState.ARTIFACT_DOWNLOAD, status=Status.FAILURE),),
        done=False,
    )

    body = render_comment(progress)

    assert _batch_strip_of(body).startswith("Batches · 📥 ")
    assert ("the workflow failed with no tracked job failure" in body) is job_finished


def test_a_running_attempt_has_not_reported_yet():
    running = dataclasses.replace(attempt(), state=ExecutionState.RUNNING, status=None, conclusion=None, reports=None)
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(running), state=ExecutionState.RUNNING, status=None),),
        done=False,
    )

    body = render_comment(progress)

    assert "**0/1 jobs**" in body
    assert _batch_strip_of(body).endswith(" 0/1")
    assert "⏳ 1 pending" in body


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProgressError.TIMED_OUT, "timed out before results were gathered"),
        (ProgressError.NO_JOB_RESULTS, "the workflow reported no job results"),
        (ProgressError.NO_ARTIFACTS, "no artifacts were downloaded for this job"),
    ],
)
def test_batch_errors_render_as_unavailable_never_as_success(error: ProgressError, expected: str):
    """A batch-level error belongs to no integration, so it is reported against the batch."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), status=Status.FAILURE, error=error),),
        done=True,
    )

    body = render_comment(progress)

    assert f"⚠️ `batch-01` — {expected}" in body
    assert "Dispatcher tests · passed" not in body


# ---------------------------------------------------------------------------
# Headings, alerts and totals
# ---------------------------------------------------------------------------


def test_the_failure_alert_is_counted_in_the_unit_the_failures_are_grouped_into():
    """The alert's number is the number of disclosures below it, so a reader can check it."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE), target="postgres"),
                job_progress(attempt(Status.FAILURE), target="postgres", environment="py3.13"),
                job_progress(attempt(Status.FAILURE), target="mysql"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "> **2 integrations failed.** 3 of 3 jobs failed." in body
    assert len(_group_summaries_of(body)) == 2


def test_one_failing_integration_is_said_in_the_singular():
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(Status.FAILURE)), status=Status.FAILURE),),
        done=True,
    )

    assert "> **1 integration failed.** 1 of 1 jobs failed." in render_comment(progress)


def test_a_failure_with_no_integration_behind_it_still_announces_itself():
    """A workflow that failed outside every tracked job has no integration for the alert to name."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), status=Status.FAILURE),),
        done=True,
    )

    body = render_comment(progress)

    assert "> **Dispatcher tests failed.** See the failures below." in body
    assert "integrations failed" not in body
    # No job failed, so a job count here would read as "0 of 1 jobs failed".
    assert "jobs failed" not in body


def test_the_unestablished_count_rides_along_with_a_failure():
    """A failure takes the alert, so without this the unknown results vanish from the comment.

    They get no section of their own any more, so the alert is the only place the total appears.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="redis"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="vault"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="consul"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    # The whole line, not its tail: the count of failing integrations is the half a fragment hid.
    assert "> **1 integration failed.** 1 of 3 jobs failed; 2 results could not be established." in render_comment(
        progress
    )


def test_nothing_failed_but_nothing_is_certain_either():
    """A job can conclude `success` while its artifacts never arrive, so its result is unknown.

    Reporting that as passed would present an unestablished result as a green one. It is not a
    failure either: a CAUTION would promise failures the comment does not have.
    """
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(error=ProgressError.NO_ARTIFACTS))),),
        done=True,
    )

    body = render_comment(progress)

    assert "## ⚠️ Dispatcher tests · results incomplete" in body
    assert "Dispatcher tests · passed" not in body
    assert "[!CAUTION]" not in body
    assert "> **1 result could not be established.** Nothing failed, but this is not a clean pass." in body
    # Still a final answer, so the footer says so rather than reading as still running.
    assert "Dispatcher finished" in body
    assert ALERT_RUNNING_NOTE not in body


def test_a_batch_error_without_a_failed_status_reads_as_incomplete():
    """A workflow can conclude successfully and still report nothing to gather."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", status=Status.SUCCESS, error=ProgressError.NO_JOB_RESULTS),),
        done=True,
    )

    body = render_comment(progress)

    assert "## ⚠️ Dispatcher tests · results incomplete" in body
    assert "reported no job results" in body
    assert "[!CAUTION]" not in body


def test_a_real_failure_outranks_an_unavailable_result():
    """A failure is the more actionable of the two, so it decides the heading."""
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_connection"),)), target="postgres"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="vault"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "## ❌ Dispatcher tests · failed" in body
    assert "results incomplete" not in body
    assert "> **1 integration failed.** 1 of 2 jobs failed; 1 result could not be established." in body
    assert "[!WARNING]" not in body
    # Both are still reported; only the heading has to choose.
    assert "<summary>❌ <code>postgres</code>" in body
    assert "<summary>⚠️ <code>vault</code>" in body


def test_an_unestablished_result_is_not_counted_as_a_failed_integration():
    """A job can conclude `success` and still carry `NO_ARTIFACTS`, because its status is the
    workflow's conclusion while its error says whether the Dispatcher collected its results
    afterwards. Counting those as failures told a reader four integrations were broken when one was.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="redis"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="vault"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="consul"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="etcd"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "> **1 integration failed.** 1 of 4 jobs failed; 3 results could not be established." in body
    # The three are still rendered, as warnings rather than failures.
    assert body.count("<summary>⚠️ <code>") == 3
    assert body.count("<summary>❌ <code>") == 1


def test_a_clean_run_with_unknown_results_never_says_they_failed():
    """Nothing failed, so nothing in the body may say anything failed.

    Past `GROUP_LIMIT` the pointer accounted for the rest with the word "failed", contradicting
    the alert three lines above it on the same run.
    """
    jobs = [
        job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target=f"integration-{index:02d}")
        for index in range(GROUP_LIMIT + 4)
    ]
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", *jobs, status=Status.SUCCESS),),
        done=True,
    )

    body = render_comment(progress)

    assert "## ⚠️ Dispatcher tests · results incomplete" in body
    assert f"> **{GROUP_LIMIT + 4} results could not be established.** Nothing failed" in body
    assert "[!CAUTION]" not in body
    assert f"4 other integrations are {POINTER}" in body
    # No integration may be described as having failed; the batch strip's own wording is not that.
    assert "integration has failed" not in body
    assert "integrations have failed" not in body
    assert "integrations failed" not in body


def test_a_workflow_failure_with_only_unknown_groups_keeps_the_unavailable_count():
    """No integration failed, so the alert cannot name one — but it must still say what is unknown.

    Dropping the count here would leave the groups below rendered as warnings with nothing in the
    alert to say how many results never arrived.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="vault"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="consul"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    assert "> **Dispatcher tests failed.** 2 results could not be established. See the failures below." in body
    assert "integrations failed" not in body


def test_a_real_failure_is_not_displaced_by_warning_only_groups():
    """Groups sort by target count, so wide unknown groups outranked a narrow real failure.

    Past `GROUP_LIMIT` that pushed the one actionable integration behind the disclosure while ten
    groups that nobody can act on held every visible slot.
    """
    jobs = [
        job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target=f"unknown-{index:02d}", environment=f"py3.{env}")
        for index in range(GROUP_LIMIT)
        for env in range(6)
    ]
    jobs.append(job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="redis"))
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE),),
        done=True,
    )

    body = render_comment(progress)

    shown, _, hidden = body.partition("<summary>Show ")
    assert "<summary>❌ <code>redis</code>" in shown
    assert "redis" not in hidden


def test_a_zero_is_left_out_of_the_totals_rather_than_printed():
    """Three zeroes with the real number last is the shape that made the counts unreadable."""
    queued = DispatcherProgress(batches=(planned_batch("batch-01", job_count=4),), done=False)

    assert "\n\n⏳ 4 pending\n" in render_comment(queued)


def test_a_finished_run_with_nothing_wrong_says_so_in_the_totals():
    """Nothing having failed is only news once the run is over, so an unfinished run stays quiet."""
    running = uniform_progress(done=False, complete=6)
    finished = uniform_progress(done=True)

    assert "nothing failed" not in render_comment(running)
    assert "✅ 10 passed · nothing failed" in render_comment(finished)


def test_skipped_is_shown_only_when_non_zero():
    passing = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)
    assert "skipped" not in render_comment(passing)

    with_skips = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(Status.SKIPPED))),), done=True
    )
    assert "⏭️ 1 skipped" in render_comment(with_skips)


def test_only_the_latest_attempt_counts_toward_totals():
    """A job retried to success counts once, as a pass — not once per execution."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(Status.FAILURE), attempt(Status.SUCCESS, number=2))),),
        done=True,
    )

    body = render_comment(progress)

    assert "**1/1 jobs**" in body
    assert "✅ 1 passed · nothing failed" in body
    assert "<details>" not in body


# ---------------------------------------------------------------------------
# In-progress signalling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("done", [False, True])
def test_progress_signals_agree_with_each_other(done: bool):
    """The in-progress signals are redundant on purpose; the bug to catch is one disagreeing.

    An unfinished run that renders like a final one is the worst failure this renderer can have.
    """
    # Two jobs either way: both reported when the run is done, one still outstanding when it is not.
    jobs = (job_progress(attempt()), job_progress(attempt(), target="vault") if done else job_progress())
    progress = DispatcherProgress(batches=(batch_progress("batch-01", *jobs),), done=done)

    body = render_comment(progress)

    assert ("in progress" in body) is not done
    assert ("[!NOTE]" in body) is not done
    assert ("pending" in body) is not done
    assert (ALERT_RUNNING_NOTE in body) is not done
    assert ("Dispatcher finished" in body) is done
    # A full bar next to "in progress" is the contradiction that would mislead most.
    assert ("pending" in _progress_bar_of(body)) is not done


def test_a_retrying_run_with_every_job_reported_still_reads_as_unfinished():
    """``complete == total`` on an unfinished run is reachable, not a contradiction.

    Every job in a retrying batch has reported, so a jobs-only signal reads as 100% while the run is
    far from over. The alert counts batches instead, and the bar refuses to fill.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress("batch-01", job_progress(attempt())),
            batch_progress(
                "batch-02",
                job_progress(attempt(Status.FAILURE), target="redis"),
                state=ExecutionState.RETRYING,
                status=None,
                current_attempt=2,
                max_attempts=3,
            ),
        ),
        done=False,
    )

    body = render_comment(progress)

    assert "**2/2 jobs**" in body
    assert "1 of 2 batches has not finished yet" in body
    # No pending jobs to report, so that clause is left out rather than printed as zero.
    assert "0 of 2 jobs have not reported" not in body
    # A full bar next to "in progress" is the contradiction this guards against.
    assert "pending" in _progress_bar_of(body)


def test_an_unfinished_run_warns_that_the_failures_are_not_the_final_list():
    """A reader who sees two failures and stops looking has been misled by a complete-looking list."""
    progress = DispatcherProgress(
        batches=(
            batch_progress("batch-01", job_progress(attempt(Status.FAILURE)), status=Status.FAILURE),
            batch_progress("batch-02", job_progress(), state=ExecutionState.RUNNING, status=None),
            batch_progress("batch-03", job_progress(), state=ExecutionState.PLANNED, status=None),
        ),
        done=False,
    )

    body = render_comment(progress)

    assert "More failures may appear as `batch-02` and `batch-03` report." in body


def test_the_warning_agrees_with_itself_when_only_one_batch_is_left():
    """One batch takes a singular verb; the plural form reads as a typo and undermines the notice."""
    progress = DispatcherProgress(
        batches=(
            batch_progress("batch-01", job_progress(attempt(Status.FAILURE)), status=Status.FAILURE),
            batch_progress("batch-02", job_progress(), state=ExecutionState.RUNNING, status=None),
        ),
        done=False,
    )

    assert "More failures may appear as `batch-02` reports." in render_comment(progress)


def test_a_finished_run_makes_no_such_promise():
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(Status.FAILURE)), status=Status.FAILURE),),
        done=True,
    )

    assert "More failures may appear" not in render_comment(progress)


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passed", "failed"),
    [
        pytest.param(1, 0, id="one-job"),
        pytest.param(3, 1, id="even-split"),
        pytest.param(199, 1, id="one-failure-among-hundreds"),
        pytest.param(0, 7, id="all-failed"),
    ],
)
def test_the_bar_is_exactly_its_width_and_never_drops_a_result(passed: int, failed: int):
    """A short bar is cosmetic, but a segment rounded down to nothing erases a result.

    One failure in two hundred jobs is where a reader most needs to see that anything failed at all.
    """
    jobs = [job_progress(attempt(), target=f"passed-{index}") for index in range(passed)]
    jobs += [job_progress(attempt(Status.FAILURE), target=f"failed-{index}") for index in range(failed)]
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE if failed else Status.SUCCESS),),
        done=True,
    )

    widths = _progress_bar_of(render_comment(progress))

    assert sum(widths.values()) == PROGRESS_BAR_WIDTH
    assert bool(widths.get("passed")) is bool(passed)
    assert bool(widths.get("failed")) is bool(failed)


def test_a_run_with_nothing_planned_draws_no_bar():
    """No jobs means no proportion to draw, and the batch strip already says so in words."""
    body = render_comment(DispatcherProgress(batches=(), done=True))

    assert _progress_bar_of(body) == {}
    assert "**0/0 jobs**" in body


# ---------------------------------------------------------------------------
# Safety: escaping, structure, budget, degenerate input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "marker"),
    [
        pytest.param('test_foo<bar> & "baz"</details>', "test", id="a-closing-tag-in-a-test-name"),
        pytest.param("test_eval[`ls` && <b>x</b>]", "test", id="a-backtick-in-a-test-name"),
        pytest.param("Run ``pytest`` <hack>", "step", id="a-run-of-backticks-in-a-step-name"),
        pytest.param("`leading and trailing`", "step", id="a-name-that-opens-and-closes-on-a-backtick"),
        pytest.param("```", "step", id="a-name-that-is-nothing-but-backticks"),
    ],
)
def test_names_from_outside_cannot_break_out_of_their_code_span(raw: str, marker: str):
    """Test output and workflow step names are arbitrary text and get the same treatment.

    They sit in a Markdown code span, so the fence has to be longer than the longest run of backticks
    inside the name. A fixed single backtick would end the span early on a name like these and let
    the rest of it render as markup — an image, a link or bold text injected into the report.
    """
    failure = (
        attempt(Status.FAILURE, failed_steps=(raw,), job_url=None)
        if marker == "step"
        else attempt(Status.FAILURE, reports=(failing_report(raw),), job_url=None)
    )
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(failure), status=Status.FAILURE),), done=True
    )

    body = render_comment(progress)

    line = next(line for line in body.splitlines() if f"· {marker} " in line)
    inline = next(token for token in MarkdownIt().parse(line.removeprefix("- ")) if token.type == "inline")
    children = inline.children or []

    assert raw in [child.content for child in children if child.type == "code_inline"]
    assert not {"image", "link_open", "strong_open", "html_inline"} & {child.type for child in children}


def test_html_tags_are_balanced():
    """An unbalanced tag silently swallows the rest of the comment on GitHub."""
    progress = DispatcherProgress(
        batches=(
            batch_progress("batch-01", job_progress(attempt())),
            batch_progress(
                "batch-02",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="postgres"),
                status=Status.FAILURE,
                error=ProgressError.TIMED_OUT,
            ),
        ),
        done=True,
    )

    body = render_comment(progress)

    for tag in ("details", "summary", "sub", "code"):
        assert body.count(f"<{tag}>") == body.count(f"</{tag}>"), tag


def test_a_disclosure_leaves_the_blank_line_github_needs_to_parse_it():
    """Without it GitHub renders the group's Markdown as one run-on line of literal text.

    This is the difference between a usable disclosure and a paragraph of backticks and brackets, and
    it is invisible in any assertion that only looks for the text inside.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, failed_steps=("Install deps",))),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    group = render_comment(progress).split("<details>\n")[1].split("\n</details>")[0]

    assert group.startswith("<summary>")
    assert "</summary>\n\n- " in group


def test_large_run_stays_within_budget_and_accounts_for_what_it_left_out():
    """240 failing integrations with many tests each: far more detail than a comment can hold."""
    batches = tuple(
        batch_progress(
            f"batch-{index:02d}",
            *[
                job_progress(
                    attempt(
                        Status.FAILURE,
                        reports=(failing_report(*[f"test_number_{number}" for number in range(20)]),),
                    ),
                    target=f"integration-{index}-{slot}",
                )
                for slot in range(24)
            ],
            status=Status.FAILURE,
        )
        for index in range(10)
    )
    progress = DispatcherProgress(batches=batches, done=True)

    body = render_comment(progress)

    # Bytes, because that is the unit the client's guard measures and therefore the one the budget
    # targets. The body is dense with three-byte emoji and block-drawing characters, so a character
    # count would understate it.
    assert len(body.encode("utf-8")) <= GITHUB_COMMENT_HARD_LIMIT
    # The header survives intact: the notice, totals and every batch are the top content.
    assert "Dispatcher beta: informational only" in body
    assert "**240/240 jobs**" in body
    assert _batch_strip_of(body).count("[batch-") == 10
    # Ten groups keep their detail and the pointer accounts for every one of the other 230.
    assert body.count("<summary>❌ <code>integration-") == GROUP_LIMIT
    assert f"230 other integrations are {POINTER}" in body
    for tag in ("details", "summary", "sub", "code"):
        assert body.count(f"<{tag}>") == body.count(f"</{tag}>"), tag


def test_dropped_count_is_accurate_when_even_the_shown_groups_overflow():
    """The last resort, when the ten groups meant to be shown do not themselves fit.

    The note has to state the real number, and the count plus what survived has to add up to the
    ten groups the section set out to render.
    """
    body = render_comment(_many_failing(24, tests_per_integration=200))

    assert len(body.encode("utf-8")) <= GITHUB_COMMENT_HARD_LIMIT
    match = re.search(r"_(\d+) more failing integrations? not shown", body)
    assert match is not None
    assert body.count("<summary>❌ <code>integration-") + int(match.group(1)) == GROUP_LIMIT
    # Even here the pointer survives, because its room is taken out of the budget first.
    assert f"14 other integrations are {POINTER}" in body
    # And no disclosure is attempted: there is no budget left to put one in.
    assert "Show " not in body


def test_empty_snapshot_does_not_crash():
    body = render_comment(DispatcherProgress(batches=(), done=True))

    assert COMMENT_MARKER in body
    assert "**0/0 jobs**" in body
    assert "No batches were planned." in body


def test_no_internal_metadata_leaks_into_the_comment():
    """The reader gets state, not plumbing: the message revision is never rendered."""
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    for body in (render_comment(progress), render_compact_comment(progress), render_minimal_comment(progress)):
        assert re.search("revision", body, re.IGNORECASE) is None


# ---------------------------------------------------------------------------
# Footer and log line
# ---------------------------------------------------------------------------


def test_the_footer_of_a_finished_run_points_at_the_dispatcher_run(on_a_commit):
    """The one thing the rest of the comment cannot tell you: which commit, and where it ran.

    No status emoji either: a ✅ here read as "all good" on a run whose heading said it had failed.
    """
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt(Status.FAILURE)), status=Status.FAILURE),), done=True
    )

    body = render_comment(progress)

    assert body.endswith(f"<sub>Dispatcher finished on `ff9caa5` — [GitHub Run]({DISPATCH_RUN_URL}).</sub>")
    assert "✅" not in body.rsplit("<sub>", 1)[1]


def test_the_footer_says_what_it_can_outside_github_actions(monkeypatch):
    """Nothing to link to locally, so it states the outcome without inventing a URL."""
    for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SHA"):
        monkeypatch.delenv(name, raising=False)
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    assert render_comment(progress).endswith("<sub>Dispatcher finished.</sub>")


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        pytest.param("12345", f"⏳ Dispatcher running — [GitHub Run]({DISPATCH_RUN_URL}).", id="linked"),
        pytest.param(None, "⏳ Dispatcher running.", id="url-unavailable"),
    ],
)
def test_the_footer_of_an_unfinished_run_identifies_the_dispatcher(
    run_id: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
):
    """The running footer links when possible and still renders when the URL is unavailable."""
    if run_id is None:
        monkeypatch.delenv("GITHUB_RUN_ID")
    else:
        monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), job_progress()),), done=False
    )

    assert render_comment(progress).endswith(f"<sub>{expected}</sub>")


def test_summary_line_reports_state_and_counts():
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), job_progress()),), done=False
    )

    assert summary_line(progress) == "Dispatcher tests in progress: 1/2 jobs, 1 passed, 0 failed, 0 skipped"


@pytest.mark.parametrize("kind", list(ShutdownKind), ids=lambda kind: kind.value)
def test_summary_line_reports_a_stopped_run_as_stopped(kind: ShutdownKind):
    """A run that never finished is logged as stopped and why, not as still in progress."""
    progress = DispatcherProgress(
        batches=(batch_progress("batch-01", job_progress(attempt()), job_progress()),), done=False
    )

    assert summary_line(progress, shutdown=shutdown_request(kind)) == (
        f"Dispatcher tests stopped ({kind.value}): 1/2 jobs, 1 passed, 0 failed, 0 skipped"
    )


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


def test_the_run_summary_is_the_comment_without_the_marker():
    """The same report, on a surface that has nothing to find it by.

    The marker exists only so the run reporter can locate its comment. A run summary is written once and
    never looked up, so carrying the marker there would say nothing and would put it somewhere the
    run reporter's ownership rules do not apply.
    """
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)
    body = render_comment(progress)

    summary = render_run_summary(body, pr_comment_failed=False)

    assert COMMENT_MARKER not in summary
    assert summary == body.removeprefix(COMMENT_MARKER).lstrip("\n")


def test_a_clean_run_summary_adds_nothing_of_its_own():
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    summary = render_run_summary(render_comment(progress), pr_comment_failed=False)

    assert "[!WARNING]" not in summary
    assert summary.startswith("## ")


def test_a_failed_comment_write_is_announced_above_the_report():
    """The reader arrives from the run page with no idea a comment was attempted."""
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    summary = render_run_summary(render_comment(progress), pr_comment_failed=True)

    assert summary.startswith("> [!WARNING]")
    # The note precedes the heading, so it is read before the result it qualifies.
    assert summary.index("[!WARNING]") < summary.index("## ")
    assert "pull request comment could not be updated" in summary
    # The report itself is untouched below the note.
    assert summary.endswith(render_comment(progress).removeprefix(COMMENT_MARKER).lstrip("\n"))


def test_the_run_summary_preserves_a_report_that_reports_failures():
    """Whatever the comment would have said, the summary says — this is not a second renderer."""
    failing = job_progress(attempt(Status.FAILURE, reports=(failing_report("test_boom"),)))
    progress = DispatcherProgress(batches=(batch_progress("batch-01", failing, status=Status.FAILURE),), done=True)

    summary = render_run_summary(render_comment(progress), pr_comment_failed=True)

    assert "Dispatcher tests · failed" in summary
    assert "test_boom" in summary
    assert summary.count("[!WARNING]") == 1


def test_a_minimal_report_survives_the_run_summary():
    """The fallback body is what gets retained when the full one was rejected, so it must render."""
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    summary = render_run_summary(render_minimal_comment(progress), pr_comment_failed=True)

    assert COMMENT_MARKER not in summary
    assert "Dispatcher finished" in summary


def test_a_body_without_the_marker_is_passed_through_unharmed():
    """Stripping is not allowed to eat the first line of a body that never carried a marker."""
    summary = render_run_summary("## Some report\n\nbody", pr_comment_failed=False)

    assert summary == "## Some report\n\nbody"


# ---------------------------------------------------------------------------
# The three tiers
# ---------------------------------------------------------------------------

FALLBACK_TIERS = [
    pytest.param(render_compact_comment, id="compact"),
    pytest.param(render_minimal_comment, id="minimal"),
]

NOTICE_TIERS = [pytest.param(render_comment, id="full"), *FALLBACK_TIERS]


def _worst_case(integration_count: int = 60, tests_per_integration: int = 200) -> DispatcherProgress:
    """A finished run where every integration failed with a long list of failing tests."""
    jobs = [
        job_progress(
            attempt(
                Status.FAILURE,
                reports=(failing_report(*[f"test_number_{n}" for n in range(tests_per_integration)]),),
            ),
            target=f"integration-{index:03d}",
        )
        for index in range(integration_count)
    ]
    return DispatcherProgress(batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE),), done=True)


def test_each_tier_is_smaller_than_the_one_before():
    """The ladder only helps if every step down actually sheds bytes."""
    progress = _worst_case()

    full = len(render_comment(progress).encode("utf-8"))
    compact = len(render_compact_comment(progress).encode("utf-8"))
    minimal = len(render_minimal_comment(progress).encode("utf-8"))

    assert minimal < compact <= full


def test_every_tier_fits_the_limit_for_a_worst_case_run():
    progress = _worst_case()

    for render in (render_comment, render_compact_comment, render_minimal_comment):
        assert len(render(progress).encode("utf-8")) <= GITHUB_COMMENT_HARD_LIMIT, render.__name__


def test_the_last_tier_names_the_failing_integrations_but_not_the_failed_tests():
    """What the tier is for: enough to see which integrations failed, without their test lists."""
    progress = _worst_case(integration_count=1, tests_per_integration=3)

    body = render_minimal_comment(progress)

    # The integration and the shape of its failure survive.
    assert "❌ <code>integration-000</code> — 1 target, 3 tests" in body
    # The names do not, and neither do the disclosures that held them.
    assert "test_number_0" not in body
    assert "<details>" not in body
    assert "py3.12 / linux" not in body
    # The batching stays: it is the other half of what a reader needs.
    assert _batch_strip_of(body).startswith("Batches · ❌ ")


def test_the_last_tier_barely_grows_with_the_size_of_the_run():
    """The last tier grows per failing integration rather than per failed test, which makes it fit."""
    few = len(render_minimal_comment(_worst_case(1, tests_per_integration=1)).encode("utf-8"))
    many = len(render_minimal_comment(_worst_case(1, tests_per_integration=500)).encode("utf-8"))

    # Five hundred more failing tests in the same integration cost only the width of the count.
    assert many - few < 20


@pytest.mark.parametrize("render", NOTICE_TIERS)
def test_every_tier_reports_itself_as_informational(render: Callable[[DispatcherProgress], str]):
    """The report runs alongside the CI that decides merges, so it must say it is advisory."""
    progress = DispatcherProgress(batches=(batch_progress("batch-01", job_progress(attempt())),), done=True)

    body = render(progress)

    assert body.index("## ") < body.index("Dispatcher beta: informational only") < body.index("Batches · ")
    assert "Existing CI remains the merge signal" in body


@pytest.mark.parametrize("render", NOTICE_TIERS)
def test_every_tier_reports_results_it_could_not_establish(render: Callable[[DispatcherProgress], str]):
    """Unknown results have no section of their own, so the count in the alert is all every tier has.

    Without it a fallback body reads as though every result had been established.
    """
    progress = DispatcherProgress(
        batches=(
            batch_progress(
                "batch-01",
                job_progress(attempt(Status.FAILURE, reports=(failing_report("test_a"),)), target="redis"),
                job_progress(attempt(error=ProgressError.NO_ARTIFACTS), target="vault"),
                status=Status.FAILURE,
            ),
        ),
        done=True,
    )

    body = render(progress)

    assert "> **1 integration failed.** 1 of 2 jobs failed; 1 result could not be established." in body


def test_the_budget_is_measured_in_bytes_not_characters():
    """A body of non-ASCII test names must still fit, which a character budget would not guarantee."""
    jobs = [
        job_progress(
            attempt(Status.FAILURE, reports=(failing_report(*[f"test_ünïcödé_{n}_日本語" for n in range(200)]),)),
            target=f"tärget-{index}",
        )
        for index in range(60)
    ]
    progress = DispatcherProgress(batches=(batch_progress("batch-01", *jobs, status=Status.FAILURE),), done=True)

    body = render_comment(progress)

    assert len(body.encode("utf-8")) <= GITHUB_COMMENT_HARD_LIMIT
    # Characters alone would have left room that the bytes do not, which is the bug this rules out.
    assert len(body) < len(body.encode("utf-8"))


# ---------------------------------------------------------------------------
# Stopped runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ShutdownKind), ids=lambda kind: kind.value)
def test_a_stopped_run_with_nothing_gathered_still_says_it_ran(kind: ShutdownKind):
    """A stop before any snapshot still identifies the run and its terminal state."""
    body = render_shutdown_notice(shutdown_request(kind))

    assert body.startswith(COMMENT_MARKER)
    assert SHUTDOWN_HEADINGS[kind] in body
    assert "Dispatcher beta: informational only" in body
    assert DISPATCH_RUN_URL in body
    without_results_note = (
        CANCELLED_WITHOUT_RESULTS_NOTE if kind is ShutdownKind.CANCELLED else STOPPED_WITHOUT_RESULTS_NOTE
    )
    gathered_note = CANCELLED_NOTE if kind is ShutdownKind.CANCELLED else STOPPED_NOTE
    assert without_results_note in body
    assert gathered_note not in body
    if kind is not ShutdownKind.CANCELLED:
        assert "a fatal error" in body


@pytest.mark.parametrize("kind", list(ShutdownKind), ids=lambda kind: kind.value)
def test_a_stopped_run_keeps_what_it_gathered_without_still_reading_as_running(kind: ShutdownKind):
    """A terminal report keeps the snapshot without inviting further waiting."""
    progress = uniform_progress(done=False, complete=6)
    assert "Tests are still running" in render_comment(progress)

    body = render_comment(progress, shutdown=shutdown_request(kind))

    assert SHUTDOWN_HEADINGS[kind] in body
    assert "Dispatcher tests · in progress" not in body
    assert "Tests are still running" not in body
    assert ALERT_RUNNING_NOTE not in body
    # The batch strip keeps each batch's last-known state, which the alert explains stopped with the run.
    assert "batch-01" in body


def test_a_stopped_run_reports_its_reason_without_reading_as_a_cancellation():
    """A failure report identifies its cause without claiming cancellation."""
    progress = uniform_progress(done=False, complete=6)
    request = ShutdownRequest.failed(RuntimeError("The GitHub API is having a moment <img on 'pause'>"))

    body = render_comment(progress, shutdown=request)

    assert FAILED_HEADING in body
    assert "`The GitHub API is having a moment <img on 'pause'>`" in body
    assert CANCELLED_HEADING not in body


def test_a_terminal_reason_is_one_bounded_line():
    """Long, multiline exceptions cannot consume the terminal report's body budget."""
    error = RuntimeError("boom\nsecond line\n" + "x" * (SHUTDOWN_REASON_LIMIT * 4))

    body = render_shutdown_notice(ShutdownRequest.failed(error))

    reason_line = next(line for line in body.splitlines() if line.startswith("> Reason: "))
    assert reason_line.startswith("> Reason: `boom second line ")
    assert reason_line.endswith("...`")
    assert len(reason_line) - len("> Reason: ") <= SHUTDOWN_REASON_LIMIT + 4


def test_a_terminal_reason_renders_as_literal_text_rather_than_markup():
    """Exception text cannot introduce images, links or formatting into the report."""
    hostile = "`![image](https://example.invalid/pixel) <b>bold</b> **passed**`"

    body = render_shutdown_notice(ShutdownRequest.failed(RuntimeError(hostile)))

    reason_line = next(line for line in body.splitlines() if line.startswith("> Reason: "))
    inline = next(token for token in MarkdownIt().parse(reason_line.removeprefix("> ")) if token.type == "inline")
    children = inline.children or []

    assert hostile in [child.content for child in children if child.type == "code_inline"]
    assert not {"image", "link_open", "strong_open", "html_inline"} & {child.type for child in children}


def test_the_cancellation_alert_keeps_its_own_wording_under_a_fallback_tier():
    """Cancellation wording is distinct from failure wording in every tier, not just the first."""
    progress = uniform_progress(done=False, complete=6)
    request = ShutdownRequest.cancelled()

    assert CANCELLED_HEADING in render_compact_comment(progress, shutdown=request)
    assert CANCELLED_HEADING in render_minimal_comment(progress, shutdown=request)
    assert FAILED_HEADING not in render_compact_comment(progress, shutdown=request)
