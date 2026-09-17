# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
"""Render progress and optional shutdown context as the shared Dispatcher PR report.

The layout is target-first: it answers "did my integration break" before "which batch ran it", so
failures are grouped into one disclosure per integration rather than one entry per failed job.

The footer adds the commit and workflow URL from the environment.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from ddev.cli.ci.tests.progress import ExecutionState, ProgressError
from ddev.cli.ci.tests.status import Status
from ddev.event_bus.shutdown import ShutdownKind, ShutdownRequest
from ddev.utils.github_actions import get_commit_sha, get_workflow_run_url
from ddev.utils.github_async import COMMENT_BODY_LIMIT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ddev.cli.ci.tests.progress import (
        BatchProgress,
        DispatcherProgress,
        JobAttemptProgress,
        JobProgress,
    )

    # A tier's section builder: given the snapshot and the bytes left, render the section or nothing.
    type SectionBuilder = Callable[[DispatcherProgress, int], str | None]

    # What qualifies a failed target within its group: the noun ("test" or "step") and the name.
    type Qualifier = tuple[str, str]

# Hidden first line of every Dispatcher comment. It brands the comment and is how the run reporter finds
# an existing one to edit, so nothing else may write it.
COMMENT_MARKER = "<!-- ddev-dispatcher-tests -->"

# 1x1 solid-colour pixels the bar is drawn from, pinned to master: a fork's raw URL has no such file
# until it rebases. See `.github/assets/README.md`.
PROGRESS_BAR_ASSETS = "https://raw.githubusercontent.com/DataDog/integrations-core/master/.github/assets"

# Rendered size of the whole bar, in pixels.
PROGRESS_BAR_WIDTH = 240
PROGRESS_BAR_HEIGHT = 10

PROGRESS_BAR_SEGMENTS = ("passed", "failed", "skipped", "pending")

# Terminal but unfinished, which no other state in a report expresses: the rest derive from `done`.
CANCELLED_HEADING = "## 🚫 Dispatcher tests · cancelled"
CANCELLED_NOTE = "Anything below is what had been gathered by then, and batches still running were asked to stop too."
# Said instead when no batch ever reported, where the note above would point at results that are absent.
CANCELLED_WITHOUT_RESULTS_NOTE = "The run was cancelled before any batch reported, so there are no results to show."
# Shutdown kind overrides progress.done in a terminal report.
FAILED_HEADING = "## 🛑 Dispatcher tests · stopped by a fatal error"
TIMED_OUT_HEADING = "## 🛑 Dispatcher tests · stopped at the time limit"
STOPPED_NOTE = "Anything below is what had been gathered by then; the remote jobs still running were asked to stop."
STOPPED_WITHOUT_RESULTS_NOTE = "The run stopped before any batch reported, so there are no results to show."
SHUTDOWN_HEADINGS = {
    ShutdownKind.CANCELLED: CANCELLED_HEADING,
    ShutdownKind.FAILED: FAILED_HEADING,
    ShutdownKind.TIMED_OUT: TIMED_OUT_HEADING,
}
SHUTDOWN_ALERT_LEAD = {
    ShutdownKind.CANCELLED: "The run was cancelled before it finished.",
    ShutdownKind.FAILED: "The run stopped on a fatal error and did not finish.",
    ShutdownKind.TIMED_OUT: "The run reached its time limit and stopped before it finished.",
}
# One line is the whole budget for a terminal reason: a traceback would be noise in a comment, and an
# unbounded error string could crowd out the results it is meant to qualify.
SHUTDOWN_REASON_LIMIT = 512

# Said in every report while Dispatcher runs in shadow mode: it does not decide merges yet, so its
# result must not be mistaken for the merge signal.
SHADOW_NOTICE = "> **Dispatcher beta: informational only**\n> Existing CI remains the merge signal."

# Blocks are joined by a blank line, so each one costs two bytes beyond its own length. Newlines are
# one byte in UTF-8, so this is the same number in either unit.
SECTION_SEPARATOR = 2

# Room held back in every section for its own "N more not shown" line, so truncating a section can
# never itself be what silently drops the notice that truncation happened.
OVERFLOW_RESERVE = 160

PROGRESS_ERROR_TEXT = {
    ProgressError.TIMED_OUT: "timed out before results were gathered",
    ProgressError.NO_JOB_RESULTS: "the workflow reported no job results",
    ProgressError.NO_ARTIFACTS: "no artifacts were downloaded for this job",
}

# Said inside a failing integration's group, where the reader is looking at targets rather than
# plumbing: an unestablished result is not a pass, and the group must not read as one.
NO_ARTIFACTS_WARNING = "artifacts could not be downloaded — test results unknown"

# Prepended to the run summary when the pull-request comment could not be written. The run summary is
# then the only place the result exists, so it says so rather than looking like the intended surface.
RUN_SUMMARY_COMMENT_FAILED_NOTE = (
    "> [!WARNING]\n"
    "> **The pull request comment could not be updated.** This summary is the full report.\n"
    "> See the workflow logs for why the comment write failed."
)

# The alert explains that unfinished results keep updating; the footer links to the run.
ALERT_RUNNING_NOTE = "This comment updates automatically."

# Emoji-only chips: the batch strip is one line, so a batch's state has to fit in one glyph.
STATUS_CHIP = {
    Status.SUCCESS: "✅",
    Status.FAILURE: "❌",
    Status.SKIPPED: "⏭️",
}

# Integrations given a group of their own before the rest go behind a disclosure, and the size of
# each of those disclosures. A comment body is static Markdown, so a disclosure reveals content that
# is already in the body rather than fetching it: every hidden group still costs its own bytes.
GROUP_LIMIT = 10

# Failed targets listed individually in a group before they collapse onto one line. Past this the
# bullets are the largest thing in the comment and say the least, since they repeat one qualifier.
TARGET_PREVIEW = 4


def _size(text: str) -> int:
    """UTF-8 byte length: the unit the client's guard measures, so the budget measures it too."""
    return len(text.encode("utf-8"))


def _code(text: str) -> str:
    """A Markdown code span around arbitrary text, fenced wide enough to survive its own backticks.

    Test ids and workflow step names come from outside. A single-backtick span would end early on the
    first backtick in one and let the rest of it render as markup, so the fence is always longer than
    the longest run inside it. HTML in a code span renders as literal text, so nothing else is needed.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # A span may neither open nor close on a backtick; one space of padding is stripped on render.
    padding = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _join_names(names: list[str]) -> str:
    """Names in prose: `a`, `a and b`, `a, b and c`."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def render_comment(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """First of three tiers, budgeted in bytes against the client's own limit so the two cannot drift.

    The message's ``revision`` is deliberately not rendered: internal ordering metadata, already logged.
    """
    return _render(progress, (partial(_failures, detail=True, expand_rest=True),), shutdown=shutdown)


def render_compact_comment(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """Second tier: the first `GROUP_LIMIT` integrations keep their detail, the rest become a pointer.

    What makes a body too long is the number of failing integrations, so the disclosures holding the
    integrations past that limit go. The note naming their batches stays, since it is then the only
    route to them.
    """
    return _render(progress, (partial(_failures, detail=True, expand_rest=False),), shutdown=shutdown)


def render_minimal_comment(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """Last tier: batches, totals and a summary line per failing integration, without its targets.

    The per-target and per-test lists are the dominant cost — a group of 12 targets failing 3 tests
    each runs to ~1.4 kB against ~70 bytes for its summary line — so dropping them is what makes this
    fit. Only the batch strip is unbudgeted, and it would need ~1,400 batches to exhaust the limit.
    """
    return _render(progress, (partial(_failures, detail=False, expand_rest=False),), shutdown=shutdown)


def _render(
    progress: DispatcherProgress,
    sections: tuple[SectionBuilder, ...],
    *,
    shutdown: ShutdownRequest | None = None,
) -> str:
    """Assemble a body from the header, whichever *sections* this tier keeps, and the footer."""
    header = _header(progress, shutdown=shutdown)
    footer = _footer(progress, shutdown=shutdown)

    # The header and footer always survive; the detail sections compete for what is left. Two
    # newlines join every block, so each section costs its own length plus that separator.
    remaining = COMMENT_BODY_LIMIT - _size(header) - _size(footer) - 4
    built = []
    for build in sections:
        section = build(progress, remaining - SECTION_SEPARATOR)
        if section is None:
            continue
        built.append(section)
        remaining -= _size(section) + SECTION_SEPARATOR

    return "\n\n".join([header, *built, footer])


def render_shutdown_notice(request: ShutdownRequest) -> str:
    """Render a terminal notice when no progress snapshot exists."""
    blocks = [COMMENT_MARKER, SHUTDOWN_HEADINGS[request.kind], SHADOW_NOTICE]
    blocks.append(_shutdown_alert(request, without_results=True))
    blocks.append(_footer(None, shutdown=request))
    return "\n\n".join(blocks)


def render_run_summary(body: str, *, pr_comment_failed: bool) -> str:
    """Turn a rendered comment *body* into the report written to the GitHub Actions run summary.

    Not a second renderer, so the run page and the pull request cannot disagree. Two differences only:
    the marker goes, since nothing looks a run summary up, and a failed comment write is announced,
    since a reader who arrived from the run page has no other way to know one was attempted.
    """
    report = body.removeprefix(COMMENT_MARKER).lstrip("\n")
    if not pr_comment_failed:
        return report

    return f"{RUN_SUMMARY_COMMENT_FAILED_NOTE}\n\n{report}"


def summary_line(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """Summarize progress and its terminal state for logs."""
    if shutdown is None:
        state = "complete" if progress.done else "in progress"
    else:
        state = f"stopped ({shutdown.kind.value})"
    return (
        f"Dispatcher tests {state}: {progress.complete}/{progress.total} jobs, "
        f"{progress.passed} passed, {progress.failed} failed, {progress.skipped} skipped"
    )


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def _header(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """Marker, heading, notice, alert, totals and the batch strip: never truncated."""
    blocks = [COMMENT_MARKER, _heading(progress, shutdown=shutdown), SHADOW_NOTICE]
    alert = _alert(progress, shutdown=shutdown)
    if alert is not None:
        blocks.append(alert)
    blocks.append(_totals(progress))
    blocks.append(_batch_strip(progress))
    return "\n\n".join(blocks)


def _heading(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str:
    """The run's outcome in one line. A failure outranks an unestablished result, which outranks a pass."""
    if shutdown is not None:
        return SHUTDOWN_HEADINGS[shutdown.kind]
    if not progress.done:
        return "## 🔄 Dispatcher tests · in progress"
    if _has_failure(progress):
        return "## ❌ Dispatcher tests · failed"
    if _unavailable_count(progress):
        return "## ⚠️ Dispatcher tests · results incomplete"
    return "## ✅ Dispatcher tests · passed"


def _alert(progress: DispatcherProgress, *, shutdown: ShutdownRequest | None = None) -> str | None:
    """A native GitHub alert, so an unfinished run cannot be mistaken for a final one at a glance.

    A terminal failure is counted in integrations, because that is the unit the failures below are
    grouped into, and only the integrations that really failed are counted: a group holding nothing
    but unestablished results is rendered as a warning, so counting it here would contradict the
    body. Those results are carried in the same sentence instead, since they get no section of
    their own and the body would otherwise read as though every result had been established.
    """
    if shutdown is not None:
        return _shutdown_alert(shutdown)
    if not progress.done:
        phase = "Tests finished; collecting results." if _collecting_results(progress) else "Tests are still running."
        return f"> [!NOTE]\n> **{phase}** {_outstanding(progress)} {ALERT_RUNNING_NOTE}"

    unavailable = _unavailable_count(progress)
    if _has_failure(progress):
        groups = sum(1 for group in _failure_groups(progress) if group.failed)
        if not groups:
            # A batch's workflow failed with no tracked job failing: there is no integration to name.
            # A job count would read as "0 of N jobs failed", so the body is where to look instead.
            established = f"{_unavailable_phrase(unavailable)}. " if unavailable else ""
            return f"> [!CAUTION]\n> **Dispatcher tests failed.** {established}See the failures below."
        plural = "s" if groups > 1 else ""
        counts = f"{progress.failed} of {progress.total} jobs failed"
        if unavailable:
            counts += f"; {_unavailable_phrase(unavailable)}"
        return f"> [!CAUTION]\n> **{groups} integration{plural} failed.** {counts}."

    if unavailable:
        # Deliberately not a CAUTION: nothing failed, and there is no failures section to send anyone to.
        return f"> [!WARNING]\n> **{_unavailable_phrase(unavailable)}.** Nothing failed, but this is not a clean pass."
    return None


def _collecting_results(progress: DispatcherProgress) -> bool:
    return any(batch.state is ExecutionState.ARTIFACT_DOWNLOAD for batch in progress.batches) and all(
        batch.state in (ExecutionState.ARTIFACT_DOWNLOAD, ExecutionState.FINISHED) for batch in progress.batches
    )


def _unavailable_phrase(count: int) -> str:
    plural = "s" if count > 1 else ""
    return f"{count} result{plural} could not be established"


def _outstanding(progress: DispatcherProgress) -> str:
    """What is left to do, counted in batches because they are the unit that actually finishes.

    A retrying batch has every job reported while the batch runs on, so a pending-jobs count alone can
    read as ``0`` on a run that is far from done.
    """
    unfinished = sum(1 for batch in progress.batches if batch.state is not ExecutionState.FINISHED)
    total = len(progress.batches)
    # The noun agrees with the total ("1 of 2 batches"), the verb with the outstanding count.
    plural = "es" if total != 1 else ""
    verb = "have" if unfinished != 1 else "has"
    outstanding = f"{unfinished} of {total} batch{plural} {verb} not finished yet."

    pending = progress.total - progress.complete
    if pending:
        outstanding += f" {pending} of {progress.total} jobs have not reported."
    return outstanding


def _totals(progress: DispatcherProgress) -> str:
    """The bar, and the counts behind it as a paragraph of its own.

    A zero is left out rather than printed: a queued run reads as "855 pending", not as three zeroes
    with the pending count hidden at the end of them.
    """
    counts = []
    if progress.passed:
        counts.append(f"✅ {progress.passed} passed")
    if progress.failed:
        counts.append(f"❌ {progress.failed} failed")
    if progress.skipped:
        counts.append(f"⏭️ {progress.skipped} skipped")
    pending = progress.total - progress.complete
    if pending:
        counts.append(f"⏳ {pending} pending")
    # Only worth saying once the run is over; while it runs, a zero failure count is not yet news.
    if progress.done and not progress.failed:
        counts.append("nothing failed")

    # A non-breaking space, so Markdown does not collapse the gap after the bar.
    jobs = f"{_progress_bar(progress)}&nbsp; **{progress.complete}/{progress.total} jobs**"
    return f"{jobs}\n\n{' · '.join(counts)}" if counts else jobs


def _progress_bar(progress: DispatcherProgress) -> str:
    """One image per segment, scaled by ``width``, and nothing at all when no job was planned."""
    pending = progress.total - progress.complete
    # Every job in a retrying batch has reported, so `complete == total` is reachable while the run is
    # unfinished, and a full bar there would contradict the heading next to it.
    if not progress.done and not pending and not _collecting_results(progress):
        pending = 1

    counts = (progress.passed, progress.failed, progress.skipped, pending)
    total = max(progress.total, sum(counts))
    if total <= 0:
        return ""

    # No whitespace between the tags: markdown renders it as a gap in the middle of the bar.
    return "".join(
        f'<img src="{PROGRESS_BAR_ASSETS}/progress-{segment}.png" '
        f'width="{width}" height="{PROGRESS_BAR_HEIGHT}" alt="">'
        for segment, width in zip(PROGRESS_BAR_SEGMENTS, _segment_widths(counts, total), strict=True)
        if width
    )


def _segment_widths(counts: tuple[int, ...], total: int) -> list[int]:
    """Pixel width per segment, summing to exactly ``PROGRESS_BAR_WIDTH``."""
    widths = [round(PROGRESS_BAR_WIDTH * count / total) for count in counts]
    # A segment rounded down to nothing would erase a result, such as one failure among hundreds.
    for index, count in enumerate(counts):
        if count and not widths[index]:
            widths[index] = 1

    # That floor and the rounding both drift, so the widest segment absorbs the difference.
    drift = PROGRESS_BAR_WIDTH - sum(widths)
    if drift:
        widest = widths.index(max(widths))
        widths[widest] = max(1, widths[widest] + drift)
    return widths


def _batch_strip(progress: DispatcherProgress) -> str:
    """Every batch on one line, including the ones that have not started.

    Batch state is secondary to the failures below it, so it gets a line rather than a table: a reader
    who wants a batch wants its link, and a reader who wants a failure wants it out of the way.
    """
    if not progress.batches:
        return "_No batches were planned._"

    entries = []
    for batch in progress.batches:
        done = sum(job.complete for job in batch.jobs_progress)
        entries.append(f"{_batch_chip(batch)} {_batch_link(batch)} {done}/{len(batch.jobs_progress)}")

    strip = f"Batches · {' · '.join(entries)}"
    # Said once at the end rather than against each batch: before dispatch it is true of all of them.
    if any(batch.workflow_url is None for batch in progress.batches):
        strip += " — *links available after dispatch*"
    return strip


def _batch_link(batch: BatchProgress) -> str:
    """The batch id, linked to its workflow run once there is one to link to."""
    if batch.workflow_url is None:
        return _code(batch.batch_id)
    return f"[{batch.batch_id}]({batch.workflow_url})"


def _batch_chip(batch: BatchProgress) -> str:
    """The batch's state, in one glyph.

    ``status`` is taken verbatim, never re-derived from ``jobs_progress``: it is the workflow's own
    conclusion, so a batch can be failed while every tracked job passed (a setup or upload step).
    Rolling the jobs up here would render that batch as passed and hide a real failure.
    """
    if batch.state is ExecutionState.ARTIFACT_DOWNLOAD:
        return "📥"
    if batch.state is ExecutionState.FINISHED:
        chip = STATUS_CHIP.get(batch.status) if batch.status is not None else None
        return chip if chip is not None else "❔"
    # A rerun is Dispatcher's own business, so at the batch level it is simply unfinished work. Which
    # jobs were retried is reported per job, where it is actionable.
    if batch.state in (ExecutionState.RUNNING, ExecutionState.RETRYING):
        return "🔄"
    return "⏳"


# ---------------------------------------------------------------------------
# Failures, grouped by integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedTarget:
    """One of an integration's targets that failed or whose result could not be established.

    The batch travels with the job because a group spans batches: an integration's targets are
    partitioned across them, and the batch is what a reader needs in order to open the right run.
    """

    job: JobProgress
    attempt: JobAttemptProgress
    batch_id: str


@dataclass(frozen=True)
class FailureGroup:
    """Every failed or unestablished target of one integration."""

    integration: str
    targets: tuple[FailedTarget, ...]

    @property
    def failed(self) -> bool:
        """Whether anything here actually failed, as opposed to never reporting a result.

        A group holds both kinds, so the two must not be conflated: a job can conclude `success`
        and still carry an error, because its status is the workflow's conclusion while its error
        says whether the Dispatcher managed to collect its results afterwards. Counting such a
        target as a failure claims the integration is broken on evidence that never arrived.
        """
        return any(target.attempt.status is Status.FAILURE for target in self.targets)


def _failure_groups(progress: DispatcherProgress) -> list[FailureGroup]:
    """One group per integration with something to answer for, worst first.

    An unestablished result joins its integration's group rather than getting a section of its own: it
    is the same question ("is this integration broken?") with the answer missing, and splitting the
    two sent a reader to two places to find out about one integration.
    """
    grouped: dict[str, list[FailedTarget]] = {}
    for batch, job in _jobs_with_batches(progress):
        attempt = job.latest
        if attempt is None or (attempt.status is not Status.FAILURE and attempt.error is None):
            continue
        grouped.setdefault(job.job.target, []).append(FailedTarget(job, attempt, batch.batch_id))

    groups = [FailureGroup(integration, tuple(targets)) for integration, targets in grouped.items()]
    # Real failures first, then most failed targets, then by name so two runs of the same shape
    # render the same way. Groups holding only unestablished results sort last: they are not
    # actionable, so they must never displace a failure from the groups that get shown.
    groups.sort(key=lambda group: (not group.failed, -len(group.targets), group.integration))
    return groups


def _failures(
    progress: DispatcherProgress, budget: int, *, detail: bool = True, expand_rest: bool = True
) -> str | None:
    """The failing integrations, plus anything that failed outside a tracked job.

    With *detail* off each group keeps its summary line but not its targets. With *expand_rest* off
    the integrations past `GROUP_LIMIT` keep only the note pointing at the batches that hold them.
    """
    groups = _failure_groups(progress)
    shown = [_group(group, detail=detail) for group in groups[:GROUP_LIMIT]]
    hidden = groups[GROUP_LIMIT:]
    pointer = _hidden_groups_note(hidden) if hidden else None

    blocks = shown + _batch_notes(progress)
    if blocks and (trailer := _more_failures_trailer(progress)) is not None:
        blocks.append(trailer)
    if not blocks:
        return None

    # The pointer is the only thing that accounts for the integrations the alert counted but the
    # body does not show, so its room comes out of the budget before the groups compete for the
    # rest. Letting it be truncated away would leave a body that looks complete and is not.
    reserved = OVERFLOW_RESERVE + (_size(pointer) + SECTION_SEPARATOR if pointer is not None else 0)
    kept, dropped = _pack(blocks, budget - reserved)
    groups_kept = min(len(shown), len(kept))
    if pointer is not None:
        kept.insert(groups_kept, pointer)

    if dropped:
        # Not even the groups meant to be shown fit, so there is no budget left to put the rest in.
        kept.append(_overflow_note(dropped, "failing integration"))
        return "\n\n".join(kept)

    # The rest go behind one disclosure, and only whole. A body that cannot hold it keeps the
    # pointer instead: half the integrations the pointer accounts for would be worse than none,
    # because nothing in the body would say which half was kept.
    if expand_rest and hidden:
        disclosure = _show_more(hidden)
        spent = sum(_size(block) + SECTION_SEPARATOR for block in kept)
        if _size(disclosure) + SECTION_SEPARATOR <= budget - spent:
            kept.insert(groups_kept + 1, disclosure)

    return "\n\n".join(kept)


def _group(group: FailureGroup, *, detail: bool = True) -> str:
    """One integration as a collapsed disclosure, or just its summary line when *detail* is off."""
    plural = "s" if len(group.targets) > 1 else ""
    summary = (
        f"{_group_chip(group)} <code>{html.escape(group.integration)}</code> — "
        f"{len(group.targets)} target{plural}, {_group_detail(group)}"
    )
    if not detail:
        return summary

    # The blank line after `</summary>` is load-bearing: without it GitHub does not parse the Markdown
    # inside the disclosure, and the targets render as one run-on line of literal text.
    return f"<details>\n<summary>{summary}</summary>\n\n{_group_body(group)}\n\n</details>"


def _show_more(groups: list[FailureGroup]) -> str:
    """Every integration past `GROUP_LIMIT`, collapsed behind one disclosure.

    Only the full tier expands anything, and it keeps every group's detail, so this is always
    rendered in full: a tier that sheds detail sheds this disclosure first.
    """
    body = "\n\n".join(_group(group) for group in groups)
    return f"<details>\n<summary>Show {len(groups)} more</summary>\n\n{body}\n\n</details>"


def _hidden_groups_note(hidden: list[FailureGroup]) -> str:
    """Where to look for the integrations that did not get a group of their own.

    Said whether or not the disclosure below it survived the budget, because it is the only thing
    that accounts for the difference between the count in the alert and the groups here. Worded
    without claiming they failed: the hidden groups can hold unestablished results as easily as
    failures, and the batch links are where either kind is answered.
    """
    plural = "s" if len(hidden) > 1 else ""
    verb = "are" if len(hidden) > 1 else "is"
    return f"{len(hidden)} other integration{plural} {verb} listed in the failed batches links above"


def _group_chip(group: FailureGroup) -> str:
    """A group with a real failure is a failure; one with only missing results is a warning."""
    return "❌" if group.failed else "⚠️"


def _group_detail(group: FailureGroup) -> str:
    """What is known about why the group failed, in the fewest words that stay true.

    Named tests come first because they are the only detail anyone acts on. Missing artifacts outrank
    a failed step, since the step that failed was the one collecting them and its name explains
    nothing about the integration.
    """
    if tests := _group_tests(group):
        return f"{len(tests)} test{'s' if len(tests) > 1 else ''}"

    if errors := [target for target in group.targets if target.attempt.error is not None]:
        if not group.failed:
            return f"result{'s' if len(errors) > 1 else ''} not established"
        if any(target.attempt.error is ProgressError.NO_ARTIFACTS for target in errors):
            return "⚠️ no artifacts"

    if steps := _group_steps(group):
        return f"{len(steps)} step{'s' if len(steps) > 1 else ''}"
    if all(target.attempt.reports is None for target in group.targets):
        return "details pending"
    return "no failure detail"


def _group_body(group: FailureGroup) -> str:
    """The group's targets, the tests or steps they share, and anything that could not be established."""
    compressed = len(group.targets) > TARGET_PREVIEW
    targets = _compressed_targets(group) if compressed else _target_bullets(group)
    # A bullet always names its own test or step, and the compressed line names one only when every
    # target shares it. Where neither happened, even a lone test has to be listed below, or the
    # summary counts it and nothing in the body ever says what it was.
    minimum = 1 if compressed and _shared_qualifier(group) is None else 2
    paragraphs = ["\n".join(targets)]
    if shared := _shared_tests(group, minimum=minimum):
        paragraphs.append("\n".join(shared))
    if shared := _shared_steps(group, minimum=minimum):
        paragraphs.append("\n".join(shared))
    if warnings := _group_warnings(group):
        paragraphs.append("\n".join(warnings))
    return "\n\n".join(paragraphs)


def _target_bullets(group: FailureGroup) -> list[str]:
    """One bullet per target, each naming its own test or step even when it repeats the one above.

    Repeats are not folded into a back-reference: a reader scanning the bullets for a test name has
    to be able to read it off the target's own line rather than tracking back up the list to find
    what the reference pointed at.
    """
    bullets = []
    for target in group.targets:
        bullet = f"- {_target_link(target)} · {target.batch_id}"
        if (qualifier := _target_qualifier(target)) is not None:
            noun, name = qualifier
            bullet += f" · {noun} {_code(name)}"
        bullets.append(bullet)
    return bullets


def _compressed_targets(group: FailureGroup) -> list[str]:
    """Past `TARGET_PREVIEW` targets the bullets say the same thing over and over.

    So the targets become one line of links and what they share becomes the next, which is what turns
    a 12-target group from twelve near-identical bullets into two lines.
    """
    shown = " · ".join(_target_link(target) for target in group.targets[:TARGET_PREVIEW])
    rest = len(group.targets) - TARGET_PREVIEW
    lines = [f"{shown} · + {rest} more" if rest > 0 else shown]

    shared = ", ".join(sorted({target.batch_id for target in group.targets}))
    if (qualifier := _shared_qualifier(group)) is not None:
        shared += f" · {qualifier[0]} {_code(qualifier[1])}"
    lines.append(shared)
    return lines


def _target_link(target: FailedTarget) -> str:
    label = _code(_target_label(target.job))
    return f"[{label}]({target.attempt.job_url})" if target.attempt.job_url else label


def _target_qualifier(target: FailedTarget) -> Qualifier | None:
    """The one test or step that explains this target, when there is exactly one.

    More than one of either is not a qualifier but a list, and a list belongs to the group rather than
    to the bullet: repeating three test names against each of twelve targets says nothing new.
    """
    if len(failed_tests := target.attempt.failed_tests) == 1:
        return ("test", failed_tests[0].name)
    if not failed_tests and len(target.attempt.failed_steps) == 1:
        return ("step", target.attempt.failed_steps[0])
    return None


def _shared_qualifier(group: FailureGroup) -> Qualifier | None:
    """The qualifier every target in the group shares, when they share one.

    A group whose targets failed differently has no common cause to name, and one whose targets all
    carry no qualifier has nothing to name either; both are `None`.
    """
    qualifiers = {_target_qualifier(target) for target in group.targets}
    return next(iter(qualifiers)) if len(qualifiers) == 1 else None


def _shared_tests(group: FailureGroup, *, minimum: int = 2) -> list[str]:
    """The group's failed tests, listed once, when a per-target qualifier cannot carry them.

    *minimum* is how many it takes to be worth listing: two where a qualifier already named a lone
    test, one where nothing did.
    """
    tests = _group_tests(group)
    if len(tests) < minimum:
        return []

    per_target = {frozenset(_target_tests(target)) for target in group.targets}
    plural = "s" if len(tests) > 1 else ""
    lead = (
        f"All {len(group.targets)} failed the same {len(tests)} test{plural}:"
        if len(per_target) == 1
        else f"{len(tests)} failed test{plural} across {len(group.targets)} targets:"
    )
    return [lead, *[f"- {_code(test)}" for test in tests]]


def _shared_steps(group: FailureGroup, *, minimum: int = 2) -> list[str]:
    """The group's failed steps, listed once, when a per-target qualifier cannot carry them.

    A target that failed more than one step gets no qualifier on its bullet, and the compressed form
    carries none at all, so without this the names the gatherer collects are counted by the summary
    and then discarded. Only when no test was named: the summary counts tests in that case, and the
    step that ran a failing test explains nothing the test does not.
    """
    if _group_tests(group):
        return []
    steps = _group_steps(group)
    if len(steps) < minimum:
        return []

    per_target = {frozenset(target.attempt.failed_steps) for target in group.targets}
    plural = "s" if len(steps) > 1 else ""
    lead = (
        f"All {len(group.targets)} failed the same {len(steps)} step{plural}:"
        if len(per_target) == 1
        else f"{len(steps)} failed step{plural} across {len(group.targets)} targets:"
    )
    return [lead, *[f"- {_code(step)}" for step in steps]]


def _group_warnings(group: FailureGroup) -> list[str]:
    """Why part of this group's result is unknown, said once per distinct reason."""
    reasons = {target.attempt.error for target in group.targets if target.attempt.error is not None}
    return [
        f"⚠️ {NO_ARTIFACTS_WARNING if error is ProgressError.NO_ARTIFACTS else PROGRESS_ERROR_TEXT[error]}"
        for error in ProgressError
        if error in reasons
    ]


def _target_tests(target: FailedTarget) -> list[str]:
    """This target's failed tests as fully qualified ids, in report order."""
    return [f"{case.classname}::{case.name}" for case in target.attempt.failed_tests]


def _group_tests(group: FailureGroup) -> list[str]:
    """Every distinct failed test across the group, in the order first seen."""
    return list(dict.fromkeys(test for target in group.targets for test in _target_tests(target)))


def _group_steps(group: FailureGroup) -> list[str]:
    """Every distinct failed step across the group, in the order first seen."""
    return list(dict.fromkeys(step for target in group.targets for step in target.attempt.failed_steps))


def _batch_notes(progress: DispatcherProgress) -> list[str]:
    """Failures and missing results that belong to a batch rather than to any one integration."""
    notes = []
    for batch in progress.batches:
        # A batch whose workflow failed without any tracked job failing is a real failure with
        # nothing to group; saying so beats a silent omission.
        if (
            batch.status is Status.FAILURE
            and all(job.complete for job in batch.jobs_progress)
            and not any(_is_failed(job) for job in batch.jobs_progress)
        ):
            notes.append(f"❌ {_code(batch.batch_id)} — the workflow failed with no tracked job failure")
        if batch.error is not None:
            notes.append(f"⚠️ {_code(batch.batch_id)} — {PROGRESS_ERROR_TEXT[batch.error]}")
    return notes


def _more_failures_trailer(progress: DispatcherProgress) -> str | None:
    """Say that the failures above are not the final list while batches are still reporting."""
    if progress.done:
        return None
    unfinished = [batch.batch_id for batch in progress.batches if batch.state is not ExecutionState.FINISHED]
    if not unfinished:
        return None
    batches = _join_names([_code(batch_id) for batch_id in unfinished])
    verb = "report" if len(unfinished) > 1 else "reports"
    return f"More failures may appear as {batches} {verb}."


def _pack(entries: list[str], budget: int) -> tuple[list[str], int]:
    """Take entries in order while they fit. Returns what was kept and how many were dropped."""
    kept: list[str] = []
    for index, entry in enumerate(entries):
        cost = _size(entry) + (SECTION_SEPARATOR if kept else 0)
        if cost > budget:
            return kept, len(entries) - index
        kept.append(entry)
        budget -= cost
    return kept, 0


def _overflow_note(dropped: int, noun: str) -> str:
    plural = "s" if dropped > 1 else ""
    return f"_{dropped} more {noun}{plural} not shown — the comment reached its size limit._"


# ---------------------------------------------------------------------------
# Footer and shared helpers
# ---------------------------------------------------------------------------


def _shutdown_alert(request: ShutdownRequest, *, without_results: bool = False) -> str:
    """Explain the stop without implying absent results were gathered."""
    lead = f"> [!CAUTION]\n> **{SHUTDOWN_ALERT_LEAD[request.kind]}**"
    if request.kind is ShutdownKind.CANCELLED:
        note = CANCELLED_WITHOUT_RESULTS_NOTE if without_results else CANCELLED_NOTE
        return f"{lead} {note}"
    note = STOPPED_WITHOUT_RESULTS_NOTE if without_results else STOPPED_NOTE
    return f"{lead} {note}\n> Reason: {_shutdown_reason(request)}"


def _shutdown_reason(request: ShutdownRequest) -> str:
    """Render a bounded reason as literal Markdown, including embedded backticks."""
    reason = " ".join(str(request.error).split())
    if len(reason) > SHUTDOWN_REASON_LIMIT:
        reason = reason[: SHUTDOWN_REASON_LIMIT - 3].rstrip() + "..."
    return _code(reason)


def _footer(progress: DispatcherProgress | None, *, shutdown: ShutdownRequest | None = None) -> str:
    """Whether this is the last word, and where the run that produced it lives.

    No status emoji on a finished run: the outcome is the heading's job, and a ✅ here read as "all
    good" on a run that had failed. What a reader cannot get anywhere else in the comment is which
    commit was tested and where Dispatcher itself ran, so that is what this says.
    """
    if shutdown is None and (progress is None or not progress.done):
        note = "⏳ Dispatcher running"
        if run_url := get_workflow_run_url():
            note += f" — [GitHub Run]({run_url})"
        return f"<sub>{note}.</sub>"

    note = "Dispatcher finished" if shutdown is None else f"Dispatcher {shutdown.kind.value}"
    if sha := get_commit_sha():
        note += f" on {_code(sha)}"
    if run_url := get_workflow_run_url():
        note += f" — [GitHub Run]({run_url})"
    return f"<sub>{note}.</sub>"


def _jobs(progress: DispatcherProgress) -> Iterator[JobProgress]:
    return (job for batch in progress.batches for job in batch.jobs_progress)


def _jobs_with_batches(progress: DispatcherProgress) -> Iterator[tuple[BatchProgress, JobProgress]]:
    return ((batch, job) for batch in progress.batches for job in batch.jobs_progress)


def _is_failed(job: JobProgress) -> bool:
    return job.latest is not None and job.latest.status is Status.FAILURE


def _has_failure(progress: DispatcherProgress) -> bool:
    """Whether the run has a failure to answer for.

    A batch's own ``FAILURE`` counts, since a workflow can fail with no tracked job failing. An *error*
    does not: an unestablished result reads as incomplete, so the heading never claims a failure with
    nothing to show for it.
    """
    return progress.failed > 0 or any(batch.status is Status.FAILURE for batch in progress.batches)


def _unavailable_count(progress: DispatcherProgress) -> int:
    """How many results could not be established, batch-level and job-level together."""
    batches = sum(1 for batch in progress.batches if batch.error is not None)
    jobs = sum(1 for job in _jobs(progress) if job.latest is not None and job.latest.error is not None)
    return batches + jobs


def _target_label(job: JobProgress) -> str:
    """A target within its integration: the integration's own name is the group it sits in.

    A target that defines no environments contributes no segment rather than an empty one, so the
    label never opens with a stray separator.
    """
    parts = [part for part in (job.job.environment, str(job.job.platform)) if part]
    # Only the base package variant separates a replica from its ordinary job.
    if job.job.minimum_base_package:
        parts.append("minimum base package")
    return " / ".join(parts)
