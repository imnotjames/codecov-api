import pytest
from decimal import Decimal
from django.test import TestCase
from ddf import G
from datetime import datetime, timedelta, date, time
from pytz import UTC
from random import randint
from math import isclose
from factory.faker import faker
from unittest.mock import patch
from rest_framework.reverse import reverse

from core.tests.factories import RepositoryFactory, OwnerFactory
from codecov.tests.base_test import InternalAPITest
from core.models import Commit
from internal_api.chart.filters import apply_default_filters, apply_simple_filters
from internal_api.chart.helpers import (
    annotate_commits_with_totals,
    apply_grouping,
    validate_params,
    ChartQueryRunner,
)
from dateutil.relativedelta import relativedelta
from rest_framework.exceptions import ValidationError

fake = faker.Faker()


def generate_random_totals(
    include_complexity=True, lines=None, hits=None, partials=None
):
    lines = lines or randint(5, 5000)
    hits = hits or randint(0, lines)
    partials = partials or randint(0, lines - hits)
    misses = lines - hits - partials
    coverage = (hits + partials) / lines
    complexity = randint(0, 5) if include_complexity else 0
    complexity_total = randint(complexity, 10) if include_complexity else 0

    totals = {
        "n": lines,
        "h": hits,
        "p": partials,
        "m": misses,
        "c": coverage,
        "C": complexity,
        "N": complexity_total
        # Not currenly used: diff, files, sessions, branches, methods
    }
    return totals


def setup_commits(
    repo,
    num_commits,
    branch="master",
    start_date=None,
    meets_default_filters=True,
    **kwargs,
):
    """
    Generate random commits with different configurations, to accommodate different testing scenarios.

    :param repo: repo to associate the commits with
    :param num_commits: number of commits to create
    :param branch: branch to associate with the commit; randomly generated by DDF if none provided
    :param start_date: if provided, commit timestamp will be set to after this date.
    for more info on acceptable values for start_date, see: https://faker.readthedocs.io/en/master/providers/faker.providers.date_time.html#faker.providers.date_time.Provider.date_time_between 
    :param meets_default_filters: when true the commit will meet all the conditions for the initial filtering done on commits
    :param kwargs: passed to generate_random_totals to manually set totals values
    """
    for _ in range(num_commits):
        timestamp = (
            fake.date_time_between(start_date=start_date, tzinfo=UTC)
            if start_date
            else fake.date_time(tzinfo=UTC)
        )

        totals = generate_random_totals(**kwargs) if meets_default_filters else None
        state = "complete" if meets_default_filters else "pending"
        ci_passed = True if meets_default_filters else False
        deleted = False if meets_default_filters else True

        G(
            Commit,
            repository=repo,
            branch=branch,
            timestamp=timestamp,
            totals=totals,
            state=state,
            ci_passed=ci_passed,
            deleted=deleted,
        )


def check_grouping_correctness(grouped_queryset, initial_queryset, data):
    """
    Used to test "apply_grouping" correctness. Programmatically verify that commits were grouped
    by the correct unit of time, and that within that grouping the correct commit was returned based on the
    query params provided.

    :param grouped_queryset: the queryset generated by calling "apply_grouping" on the initial_queryset
    :param initial_queryset: the annotated and filtered queryset provided to the apply_grouping call
    :param data: the grouping and filtering parameters that were applied when generating the grouping
    """
    grouping_unit = data.get("grouping_unit")
    agg_function = data.get("agg_function")
    agg_value = data.get("agg_value")

    """
    For each of the grouped commits, retrieve all the commits from the initial queryset that are within the same time window
    and verify that we can't find any that that better match the given aggregation function better.

    For example, if we grouped by max coverage per month, we'll get the commits for that month and verify that none of them have a coverage
    value greater than the grouped commit.
    """
    for commit in grouped_queryset:
        # Get the unit of time we grouped by so we can filter for all the commits in this commit's time window
        relative_delta_args = (
            {f"{grouping_unit}s": 1}
            if grouping_unit != "quarter"
            else {
                "months": 3
            }  # relativedelta doesn't except quarter as an argument so set that manually
        )

        # example: if agg_function is "min" and agg_value is "coverage", this will pass
        # "coverage__lt: <commit.coverage>" to the filter call below, to check if any commits in this window had lower coverage
        filtering_key = agg_value + ("__lt" if agg_function == "min" else "__gt")
        filtering_value_args = {filtering_key: getattr(commit, agg_value)}

        assert (
            not initial_queryset.filter(
                timestamp__gt=commit.truncated_date,
                timestamp__lt=commit.truncated_date
                + relativedelta(**relative_delta_args),
                repository__name=commit.repository.name,
            )
            .filter(
                **filtering_value_args
            )  # make two filter calls to avoid potentially filtering by timestamp multiple time in the same call which makes django unhappy
            .exclude(commitid=commit.commitid)
            .exists()
        )


class CoverageChartHelpersTest(TestCase):
    def setUp(self):
        self.org1 = OwnerFactory(username="org1")
        self.repo1_org1 = RepositoryFactory(author=self.org1, name="repo1")
        setup_commits(self.repo1_org1, 10)

        self.repo2_org1 = RepositoryFactory(author=self.org1, name="repo2")
        setup_commits(self.repo2_org1, 10)

        self.org2 = OwnerFactory(username="org2")
        self.repo1_org2 = RepositoryFactory(author=self.org2, name="repo1")
        setup_commits(self.repo1_org2, 10)

        self.user = OwnerFactory(
            organizations=[self.org1.ownerid],
            permission=[
                self.repo1_org1.repoid,
                self.repo2_org1.repoid,
                self.repo1_org2.repoid,
            ],
        )

    def test_validate_params_invalid(self):
        data = {
            "agg_function": "potato",
            "grouping_unit": "potato",
            "coverage_timestamp_ordering": "potato",
            "repositories": [],
            "field_not_in_schema": True,
        }

        with self.assertRaises(ValidationError) as err:
            validate_params(data)

        # Check that only the expected validation errors occurred
        validation_errors = err.exception.detail
        assert len(validation_errors) == 5
        assert "owner_username" in validation_errors  # required field missing
        assert "grouping_unit" in validation_errors  # value not allowed
        assert "agg_function" in validation_errors  # value not allowed
        assert (
            "field_not_in_schema" in validation_errors
        )  # only fields in the schema are allowed in params
        assert "coverage_timestamp_ordering" in validation_errors  # value not allowed

    def test_validate_params_valid(self):
        data = {
            "owner_username": self.org1.username,
            "agg_function": "max",
            "agg_value": "coverage",
            "grouping_unit": "month",
            "coverage_timestamp_ordering": "increasing",
        }

        validate_params(data)

    def test_validate_params_agg_fields(self):
        data_aggregated = {
            "owner_username": self.org1.username,
            "grouping_unit": "day",
            "agg_function": "min",
            "agg_value": "timestamp",
        }
        validate_params(data_aggregated)

        # Check that aggregation parameters are not required when grouping by commit
        data_grouped_by_commit = {
            "owner_username": self.org1.username,
            "grouping_unit": "commit",
        }
        validate_params(data_grouped_by_commit)

        # Check that aggregation parameters are required when grouping by commit
        data_aggregated_missing_agg_fields = {
            "owner_username": self.org1.username,
            "grouping_unit": "month",
        }
        with self.assertRaises(ValidationError) as err:
            validate_params(data_aggregated_missing_agg_fields)

        validation_errors = err.exception.detail
        assert len(validation_errors) == 1
        assert "grouping_unit" in validation_errors

    def test_apply_default_filters(self):
        setup_commits(self.repo1_org1, 10, meets_default_filters=False)

        queryset = apply_default_filters(Commit.objects.all())

        assert queryset.count() > 0 and queryset.count() < Commit.objects.count()
        for commit in queryset:
            assert commit.state == "complete"
            assert commit.deleted is False
            assert commit.ci_passed is True
            assert commit.totals is not None

    def test_apply_simple_filters(self):
        setup_commits(self.repo1_org1, 10, start_date="-7d")
        setup_commits(self.repo1_org1, 2, branch="production", start_date="-7d")

        start_date = datetime.now(tz=UTC) - relativedelta(days=7)
        end_date = datetime.now(tz=UTC)
        data = {
            "owner_username": self.org1.username,
            "branch": "master",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "repositories": [self.repo1_org1.name, self.repo2_org1.name],
        }
        queryset = apply_simple_filters(Commit.objects.all(), data, self.user)

        assert queryset.count() > 0
        for commit in queryset:
            assert commit.repository.name in data.get("repositories")
            assert commit.repository.author.username == data["owner_username"]
            assert commit.branch == data["branch"]
            assert commit.timestamp >= start_date
            assert commit.timestamp <= end_date

    def test_apply_simple_filters_repo_filtering(self):
        """
            This test verifies that when no "repository" parameters are returned, we only return all repositories
            in the organization that the logged-in user has permissions to view.
        """
        no_permissions_repo = RepositoryFactory(
            author=self.org1, name="no_permissions_to_this_repo", private=True
        )
        setup_commits(no_permissions_repo, 10)

        data = {
            "owner_username": self.org1.username,
        }

        queryset = apply_simple_filters(Commit.objects.all(), data, self.user)
        assert queryset.count() > 0
        for commit in queryset:
            assert commit.repository.name != no_permissions_repo

    def test_apply_simple_filters_branch_filtering(self):
        # Verify that when no "branch" param is provided, we filter commits by the repo's default branch

        branch_test = RepositoryFactory(
            author=self.org1, name="branch_test", private=False, branch="main"
        )  # "main" is the default branch
        setup_commits(branch_test, 10, branch="main")
        setup_commits(branch_test, 10, branch="not_default")

        # we shouldn't get commits on "main" branch for a repo that has a different default branch
        setup_commits(self.repo1_org1, 10, branch="main")

        data = {
            "owner_username": self.org1.username,
            "repositories": [self.repo1_org1.name, branch_test.name],
        }
        queryset = apply_simple_filters(Commit.objects.all(), data, self.user)
        assert queryset.count() > 0
        for commit in queryset:
            assert (
                commit.repository.name == self.repo1_org1.name
                and commit.branch == "master"
            ) or (
                commit.repository.name == branch_test.name and commit.branch == "main"
            )

        # should still be able to query by non-default branch if desired
        data = {
            "owner_username": self.org1.username,
            "repositories": [branch_test.name],
            "branch": "not_default",
        }
        queryset = apply_simple_filters(Commit.objects.all(), data, self.user)
        assert queryset.count() > 0
        for commit in queryset:
            assert (
                commit.repository.name == branch_test.name
                and commit.branch == "not_default"
            )

    def test_annotate_commits_with_totals(self):
        with_complexity_commitid = "i230tky2"
        G(
            Commit,
            commitid=with_complexity_commitid,
            totals={"n": 0, "h": 0, "p": 0, "m": 0, "c": 0, "C": 0, "N": 1}
        )
        annotated_commits = annotate_commits_with_totals(
            Commit.objects.filter(commitid=with_complexity_commitid)
        )

        assert annotated_commits.count() > 0
        for commit in annotated_commits:
            # direct float equality checks in python are finicky so use "isclose" to check we got the expected value
            assert isclose(commit.coverage, commit.totals["c"])
            assert isclose(commit.complexity, commit.totals["C"])
            assert isclose(commit.complexity_total, commit.totals["N"])
            assert isclose(commit.complexity_ratio, commit.totals["C"] / commit.totals["N"])

    def test_annotate_commit_with_totals_no_complexity_sets_ratio_to_None(self):
        no_complexity_commitid = "sdfkjwepj42"
        G(
            Commit,
            commitid=no_complexity_commitid,
            totals={"n": 0, "h": 0, "p": 0, "m": 0, "c": 0, "C": 0, "N": 0}
        )
        annotated_commits = annotate_commits_with_totals(
            Commit.objects.filter(commitid=no_complexity_commitid)
        )

        assert annotated_commits.count() > 0
        for commit in annotated_commits:
            assert commit.complexity_ratio is None

    def test_apply_grouping(self):
        with self.subTest("min coverage"):
            setup_commits(self.repo1_org1, 20, start_date="-7d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "min",
                "agg_value": "coverage",
                "start_date": (datetime.now(tz=UTC) - relativedelta(days=7)).isoformat(),
                "end_date": datetime.now(tz=UTC).isoformat(),
                "repositories": [self.repo1_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)

        with self.subTest("max coverage"):
            setup_commits(self.repo1_org1, 20, start_date="-180d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "month",
                "agg_function": "max",
                "agg_value": "coverage",
                "start_date": (datetime.now(tz=UTC) - relativedelta(months=6)).isoformat(),
                "end_date": datetime.now(tz=UTC).isoformat(),
                "repositories": [self.repo1_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)

        with self.subTest("min complexity"):
            setup_commits(self.repo1_org1, 20, start_date="-7d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "max",
                "agg_value": "complexity",
                "start_date": (datetime.now(tz=UTC) - relativedelta(days=7)).isoformat(),
                "end_date": datetime.now(tz=UTC).isoformat(),
                "repositories": [self.repo1_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)

        with self.subTest("max complexity"):
            setup_commits(self.repo1_org1, 20, start_date="-7d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "max",
                "agg_value": "complexity",
                "start_date": (datetime.now(tz=UTC) - relativedelta(days=7)).isoformat(),
                "end_date": datetime.now(tz=UTC).isoformat(),
                "repositories": [self.repo1_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)


    def test_ordering(self):
        with self.subTest("order by increasing dates"):
            data = {
                "organization": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "min",
                "coverage_timestamp_ordering": "increasing",
                "agg_value": "coverage",
                "repositories": [self.repo1_org1.name],
            }

            queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            queryset = apply_grouping(queryset, data)

            results = queryset.values()
            # -1 because the last result doesn't need to be tested against
            for i in range(len(results) - 1):
                assert results[i]["timestamp"] < results[i + 1]["timestamp"]

        with self.subTest("order by decreasing dates"):
            data = {
                "organization": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "min",
                "coverage_timestamp_ordering": "decreasing",
                "agg_value": "coverage",
                "repositories": [self.repo1_org1.name],
            }

            queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            queryset = apply_grouping(queryset, data)

            results = queryset.values()
            # -1 because the last result doesn't need to be tested against
            for i in range(len(results) - 1):
                assert results[i]["timestamp"] > results[i + 1]["timestamp"]


class TestChartQueryRunnerQuery(TestCase):
    """
    Tests for the querying-part of the ChartQueryRunner.
    """
    def setUp(self):
        self.org = OwnerFactory()
        self.repo1 = RepositoryFactory(author=self.org)
        self.repo2 = RepositoryFactory(author=self.org)
        self.user = OwnerFactory(permission=[self.repo1.repoid, self.repo2.repoid])
        self.commit1 = G(
            model=Commit,
            repository=self.repo1,
            totals={"h": 100, "n": 120, "p": 10, "m": 10},
            branch=self.repo1.branch,
            state="complete"
        )
        self.commit2 = G(
            model=Commit,
            repository=self.repo2,
            totals={"h": 14, "n": 25, "p": 6, "m": 5},
            branch=self.repo2.branch,
            state="complete"
        )

    def test_query_aggregates_multiple_repository_totals(self):
        query_runner = ChartQueryRunner(
            user=self.user,
            request_params={
                "owner_username": self.org.username,
                "service": self.org.service,
                "end_date": str(datetime.now()),
                "grouping_unit": "day"
            }
        )

        results = query_runner.run_query()

        assert len(results) == 1
        assert results[0]["total_hits"] == 114
        assert results[0]["total_lines"] == 145
        assert results[0]["total_misses"] == 15
        assert results[0]["total_partials"] == 16

    def test_query_aggregates_with_latest_commit_if_no_recent_upload(self):
        # set timestamp to past, before 'start_date'
        self.commit1.timestamp = datetime.now() - timedelta(days=7)
        self.commit1.save()

        query_runner = ChartQueryRunner(
            user=self.user,
            request_params={
                "owner_username": self.org.username,
                "service": self.org.service,
                "start_date": str(datetime.now() - timedelta(days=1)),
                "grouping_unit": "day"
            }
        )

        results = query_runner.run_query()

        assert len(results) == 2

        # Day before commit2 is created, a few days after commit1 is created
        assert results[0]["total_hits"] == 100
        assert results[0]["total_lines"] == 120
        assert results[0]["total_misses"] == 10
        assert results[0]["total_partials"] == 10
        assert results[0]["coverage"] == Decimal('91.67')

        # Day commit2 is created
        assert results[1]["total_hits"] == 114
        assert results[1]["total_lines"] == 145
        assert results[1]["total_misses"] == 15
        assert results[1]["total_partials"] == 16
        assert results[1]["coverage"] == Decimal('89.66')

    @pytest.mark.skip(reason="flaky, skipping until re write")
    def test_query_supports_different_grouping_params(self):
        end_date = datetime.fromisoformat('2019-01-01')
        self.commit1.timestamp = end_date - timedelta(days=365)
        self.commit1.save()
        pairs = [("day", 365), ("week", 52), ("month", 12), ("quarter", 4), ("year", 1)]
        for grouping_unit, expected_num_datapoints in pairs:
            query_runner = ChartQueryRunner(
                user=self.user,
                request_params={
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "start_date": str(end_date - timedelta(days=365)),
                    "end_date": str(end_date),
                    "grouping_unit": grouping_unit
                }
            )

            results = query_runner.run_query()

            assert len(results) == expected_num_datapoints + 1 # We add one because the date range is inclusive

    def test_query_supports_reverse_ordering(self):
        self.commit1.timestamp = datetime.now() - timedelta(days=7)
        self.commit1.save()

        query_runner = ChartQueryRunner(
            user=self.user,
            request_params={
                "owner_username": self.org.username,
                "service": self.org.service,
                "start_date": str(datetime.now() - timedelta(days=1)),
                "grouping_unit": "day",
                "coverage_timestamp_ordering": "decreasing"
            }
        )

        results = query_runner.run_query()

        assert len(results) == 2
        assert results[0]["date"] > results[1]["date"]

    def test_query_doesnt_crash_if_no_commits(self):
        with self.subTest("no repos case"):
            self.org.repository_set.all().delete()
            ChartQueryRunner(
                user=self.user,
                request_params={
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day"
                }
            ).run_query()

        with self.subTest("no commits case"):
            repo = RepositoryFactory(author=self.org)
            self.user.permission = [repo.repoid]
            self.user.save()
            ChartQueryRunner(
                user=self.user,
                request_params={
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day"
                }
            ).run_query()


class TestChartQueryRunnerHelperMethods(TestCase):
    """
    Tests for the non-querying-parts of the ChartQueryRunner, such
    as validation and parameter transformation.
    """
    def setUp(self):
        self.org = OwnerFactory()
        self.user = OwnerFactory()

    def test_repoids(self):
        repo1, repo2 = RepositoryFactory(author=self.org), RepositoryFactory(author=self.org)
        self.user.permission = [repo1.repoid, repo2.repoid]
        self.user.save()
        qr = ChartQueryRunner(
            self.user,
            {
                "owner_username": self.org.username,
                "service": self.org.service,
                "grouping_unit": "day"
            }
        )

        with self.subTest("returns repoids"):
            assert qr.repoids == f"({repo2.repoid},{repo1.repoid})"

        with self.subTest("filters by supplied repo names"):
            qr = ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day",
                    "repositories": [repo1.name]
                }
            )
            assert qr.repoids == f"({repo1.repoid})"

    def test_interval(self):
        with self.subTest("translates quarter into 3 months"):
            assert ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "quarter"
                }
            ).interval == "3 months"

        with self.subTest("transforms grouping unit into '1 {grouping_unit}'"):
            for grouping_unit in ["day", "week", "month", "year"]:
                assert ChartQueryRunner(
                    self.user,
                    {
                        "owner_username": self.org.username,
                        "service": self.org.service,
                        "grouping_unit": grouping_unit
                    }
                ).interval == f"1 {grouping_unit}"

    def test_first_complete_commit_date_returns_date_of_first_complete_commit_in_repoids(self):
        repo1, repo2 = RepositoryFactory(author=self.org), RepositoryFactory(author=self.org)
        self.user.permission = [repo1.repoid, repo2.repoid]
        self.user.save()
        older_incomplete_commit = G(
            model=Commit,
            repository=repo1,
            branch=repo1.branch,
            state="pending",
            timestamp=datetime.now() - timedelta(days=7)
        )
        commit1 = G(
            model=Commit,
            repository=repo1,
            branch=repo1.branch,
            state="complete",
            timestamp=datetime.now() - timedelta(days=3)
        )
        commit2 = G(
            model=Commit,
            repository=repo2,
            branch=repo2.branch,
            state="complete"
        )

        qr = ChartQueryRunner(
            self.user,
            {
                "owner_username": self.org.username,
                "service": self.org.service,
                "grouping_unit": "day"
            }
        )

        assert qr.first_complete_commit_date == datetime.date(commit1.timestamp)

    def test_start_date(self):
        with self.subTest("returns parsed start date if supplied"):
            start_date = datetime.now()
            assert ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day",
                    "start_date": str(start_date)
                }
            ).start_date == datetime.date(start_date)

        with self.subTest("returns first_commit_date if not supplied"):
            repo = RepositoryFactory(author=self.org)
            self.user.permission = [repo.repoid]
            self.user.save()
            commit = G(
                model=Commit,
                repository=repo,
                branch=repo.branch,
                state="complete",
                timestamp=datetime.now() - timedelta(days=3)
            )
            assert ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day",
                }
            ).start_date == datetime.date(commit.timestamp)

    def test_end_date(self):
        with self.subTest("returns parsed end date if supplied"):
            end_date = datetime.now() - timedelta(days=7)
            assert ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day",
                    "end_date": str(end_date)
                }
            ).end_date == datetime.date(end_date)

        with self.subTest("returns datetime.now() if not supplied"):
            assert ChartQueryRunner(
                self.user,
                {
                    "owner_username": self.org.username,
                    "service": self.org.service,
                    "grouping_unit": "day",
                }
            ).end_date == datetime.date(datetime.now())


@patch("internal_api.permissions.RepositoryPermissionsService.has_read_permissions")
class RepositoryCoverageChartTest(InternalAPITest):
    def _retrieve(self, kwargs={}, data={}):
        return self.client.post(
            reverse("chart-coverage-repository", kwargs=kwargs),
            data=data,
            content_type="application/json",
        )

    def setUp(self):
        self.org1 = OwnerFactory()
        self.repo1_org1 = RepositoryFactory(author=self.org1)
        setup_commits(self.repo1_org1, 10, start_date="-4d")

        self.user = OwnerFactory(
            service="github",
            organizations=[self.org1.ownerid],
            permission=[self.repo1_org1.repoid],
        )
        self.client.force_login(user=self.user)

    def test_no_permissions(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "commit",
            "repositories": [self.repo1_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = False
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 403

    # when "grouping_unit" is commit we just return all the commits with no grouping/aggregation
    @pytest.mark.skip(reason="flaky, skipping until re write")
    def test_get_commits_no_time_grouping(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "commit",
            "repositories": [self.repo1_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = True
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 200
        assert len(response.data["coverage"]) == 10
        assert len(response.data["complexity"]) == 10

    def test_get_commits_with_time_grouping(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "day",
            "agg_function": "max",
            "agg_value": "coverage",
            "repositories": [self.repo1_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = True
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 200
        assert len(response.data["coverage"]) > 0
        assert len(response.data["complexity"]) > 0

    def test_get_commits_with_coverage_change(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "day",
            "agg_function": "max",
            "agg_value": "coverage",
            "repositories": [self.repo1_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = True
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 200

        assert len(response.data["coverage"]) > 1
        # Verify that the coverage change was properly computed
        for index in range(len(response.data["coverage"])):
            commit = response.data["coverage"][index]

            # First commit should always have change = 0 since it changed nothing
            if index == 0:
                assert commit["coverage_change"] == 0
            else:
                assert commit["coverage_change"] == commit["coverage"] - response.data["coverage"][index - 1]["coverage"]


class TestOrganizationChartHandler(InternalAPITest):
    def setUp(self):
        self.org = OwnerFactory()
        self.repo1 = RepositoryFactory(author=self.org)
        self.repo2 = RepositoryFactory(author=self.org)
        self.user = OwnerFactory(permission=[self.repo1.repoid, self.repo2.repoid])
        self.commit1 = G(
            model=Commit,
            repository=self.repo1,
            totals={"h": 100, "n": 120, "p": 10, "m": 10},
            branch=self.repo1.branch,
            state="complete"
        )
        self.commit2 = G(
            model=Commit,
            repository=self.repo2,
            totals={"h": 14, "n": 25, "p": 6, "m": 5},
            branch=self.repo2.branch,
            state="complete"
        )
        self.client.force_login(user=self.user)

    def _get(self, kwargs={}, data={}):
        return self.client.get(
            reverse("chart-coverage-organization", kwargs=kwargs),
            data=data,
            content_type="application/json",
        )

    def test_basic_success(self):
        response = self._get(
            kwargs={
                "owner_username": self.org.username,
                "service": self.org.service,
            },
            data={
                "grouping_unit": "day",
                "repositories": [self.repo1.name, self.repo2.name]
            }
        )

        assert response.status_code == 200
        assert len(response.data["coverage"]) == 1
        assert response.data["coverage"][0]["total_hits"] == 114
        assert response.data["coverage"][0]["total_lines"] == 145
        assert response.data["coverage"][0]["total_misses"] == 15
        assert response.data["coverage"][0]["total_partials"] == 16
