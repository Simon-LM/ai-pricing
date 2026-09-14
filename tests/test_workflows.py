"""What the weekly workflows are allowed to run, and where the full suite still runs.

Standard library only, like everything else here -- these read the YAML as text rather
than parsing it, because a PyYAML dependency would have to be installed on a runner that
deliberately installs nothing.

This pins one property, learned the hard way on 2026-09-07. Every refresh workflow used
to run `unittest discover -s tests`, the whole suite. OVH put a new model on sale, an OVH
test fixture went stale, and the resulting red blocked Eden AI's and Hugging Face's
refreshes -- whose own scrapes had succeeded, with figures that were simply thrown away.
Two providers went a week without an update over a fixture belonging to a third.

So a refresh runs the shared tests plus its own provider's, and no other provider's. That
is easy to undo by "simplifying" the step back to a discover, and nothing else would
notice, which is what these tests are for.

A second property joined it on 2026-09-14, learned the same way. Those tests read the
committed pricing.json -- the file as it stood *before* the scrape -- while the file a
refresh commits is a candidate built after it. Nothing tested the candidate, and a block
its own tests reject reached main and stayed red there, unnoticed because the one job
that runs the full suite is never triggered by a push made with GITHUB_TOKEN. Each
refresh now re-runs its checks against the candidate before committing, and tests.yml
runs on workflow_run as the net beneath that. Both are equally easy to drop by accident.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# provider slug -> the test modules only that provider's refresh may name
PROVIDER_MODULES = {
    "edenai": ["tests.scraper_tests.test_edenai"],
    "huggingface": ["tests.scraper_tests.test_huggingface"],
    "mistral": ["tests.scraper_tests.test_mistral"],
    "ovh": ["tests.scraper_tests.test_ovh", "tests.scraper_tests.test_ovh_coverage"],
}

# Run by every refresh, because they cover the plumbing all four share.
SHARED_MODULES = [
    "tests.test_pricing_validate",
    "tests.test_provider_runner",
    "tests.test_nextjs_flight",
    "tests.test_weekly_report",
]


def workflow(slug: str) -> str:
    return (WORKFLOWS / f"refresh-{slug}.yml").read_text(encoding="utf-8")


class TestARefreshRunsOnlyItsOwnProvidersTests(unittest.TestCase):
    def test_every_refresh_names_its_own_provider(self) -> None:
        for slug, modules in PROVIDER_MODULES.items():
            text = workflow(slug)
            for module in modules:
                self.assertTrue(
                    module in text, f"refresh-{slug}.yml does not run {module}"
                )

    def test_no_refresh_names_another_providers_tests(self) -> None:
        """The whole point. A provider's weekly job must not be gated on a fixture that
        belongs to someone else."""
        for slug, own in PROVIDER_MODULES.items():
            text = workflow(slug)
            for other_slug, others in PROVIDER_MODULES.items():
                if other_slug == slug:
                    continue
                for module in others:
                    if module in own:  # no overlap today, but do not assume it
                        continue
                    self.assertFalse(
                        module in text,
                        f"refresh-{slug}.yml runs {module}, so a problem in "
                        f"{other_slug} can stop {slug} from publishing.",
                    )

    def test_no_refresh_discovers_the_whole_suite(self) -> None:
        """`discover -s tests` is exactly how the single point of failure got there."""
        for slug in PROVIDER_MODULES:
            self.assertFalse(
                "discover -s tests" in workflow(slug),
                f"refresh-{slug}.yml discovers the whole suite again, which makes every "
                f"other provider able to block it. Name its modules instead.",
            )

    def test_every_refresh_runs_the_shared_tests(self) -> None:
        for slug in PROVIDER_MODULES:
            text = workflow(slug)
            for module in SHARED_MODULES:
                self.assertTrue(
                    module in text, f"refresh-{slug}.yml does not run {module}"
                )


class TestTheCandidateIsTestedBeforeItIsPublished(unittest.TestCase):
    """The file a refresh commits is not the file its tests read. Asking the question
    of the candidate too is what stops a block that fails its own tests from being
    published, which is exactly what happened on 2026-09-14."""

    def test_every_refresh_tests_the_file_it_is_about_to_commit(self) -> None:
        for slug in PROVIDER_MODULES:
            self.assertIn(
                "AI_PRICING_FILE=",
                workflow(slug),
                f"refresh-{slug}.yml never points the tests at its candidate, so it "
                f"can publish a block that fails them.",
            )

    def test_the_candidate_is_tested_before_it_is_committed(self) -> None:
        """Order is the whole point. After the commit it is already published."""
        for slug in PROVIDER_MODULES:
            text = workflow(slug)
            self.assertLess(
                text.index("AI_PRICING_FILE="),
                text.index("- name: Commit the figures"),
                f"refresh-{slug}.yml tests its candidate after committing it.",
            )

    def test_a_candidate_that_fails_stops_the_refresh(self) -> None:
        """The step runs with continue-on-error, so its outcome has to be read back
        or a failure is merely printed and then published anyway."""
        for slug in PROVIDER_MODULES:
            self.assertIn(
                "steps.verify.outcome == 'failure'",
                workflow(slug),
                f"refresh-{slug}.yml does not act on its candidate check failing.",
            )

    def test_a_refresh_tests_its_candidate_with_its_own_provider_only(self) -> None:
        """The same rule as the first test run: one provider's candidate check must not
        be gated on another provider's fixtures."""
        for slug, own in PROVIDER_MODULES.items():
            after = workflow(slug).split("AI_PRICING_FILE=", 1)[1].split("- name:", 1)[0]
            for other_slug, others in PROVIDER_MODULES.items():
                if other_slug == slug:
                    continue
                for module in others:
                    if module in own:
                        continue
                    self.assertNotIn(
                        module,
                        after,
                        f"refresh-{slug}.yml checks its candidate with {module}.",
                    )


class TestTheFullSuiteStillRunsSomewhere(unittest.TestCase):
    def test_the_push_workflow_discovers_everything(self) -> None:
        """Scoping the refreshes is only safe because this one is not scoped: a change
        that breaks another provider's tests must still fail before it reaches main."""
        self.assertTrue(
            "discover -s tests" in (WORKFLOWS / "tests.yml").read_text(encoding="utf-8"),
            "tests.yml no longer runs the whole suite, so nothing does.",
        )

    def test_the_full_suite_runs_after_every_refresh(self) -> None:
        """`on: push` does not cover the bot's commits -- a push made with GITHUB_TOKEN
        triggers nothing -- and those are most of the commits this repository gets.

        The name is read out of each refresh file rather than written here twice, so
        renaming a workflow without updating tests.yml fails instead of silently
        unhooking it."""
        tests_yml = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_run:", tests_yml, "tests.yml never sees a bot commit.")
        for slug in PROVIDER_MODULES:
            first_line = workflow(slug).splitlines()[0]
            name = first_line.split("name:", 1)[1].strip()
            self.assertIn(
                f'"{name}"',
                tests_yml,
                f"tests.yml does not run after refresh-{slug}.yml ({name!r}).",
            )


if __name__ == "__main__":
    unittest.main()
