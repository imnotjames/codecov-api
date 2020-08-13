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
    aggregate_across_repositories,
    validate_params,
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

        data = {
            "owner_username": self.org1.username,
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - relativedelta(days=7),
            "end_date": datetime.now(tz=UTC),
            "repositories": [self.repo1_org1.name, self.repo2_org1.name],
        }
        queryset = apply_simple_filters(Commit.objects.all(), data, self.user)

        assert queryset.count() > 0
        for commit in queryset:
            assert commit.repository.name in data.get("repositories")
            assert commit.repository.author.username == data["owner_username"]
            assert commit.branch == data["branch"]
            assert commit.timestamp >= data["start_date"]
            assert commit.timestamp <= data["end_date"]

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
        G(Commit, totals={"n": 0, "h": 0, "p": 0, "m": 0, "c": 0, "C": 0, "N": 0})
        annotated_commits = annotate_commits_with_totals(Commit.objects.all())

        assert annotated_commits.count() > 0
        for commit in annotated_commits:
            # direct float equality checks in python are finicky so use "isclose" to check we got the expected value
            assert isclose(commit.coverage, commit.totals["c"])
            assert isclose(commit.lines, commit.totals["n"])
            assert isclose(commit.hits, commit.totals["h"])
            assert isclose(commit.misses, commit.totals["m"])
            assert isclose(commit.complexity, commit.totals["C"])
            assert isclose(commit.complexity_total, commit.totals["N"])
            assert isclose(
                commit.complexity_ratio,
                commit.totals["C"] / commit.totals["N"] if commit.totals["N"] else 0,
            )

    def test_apply_grouping(self):
        with self.subTest("min coverage"):
            setup_commits(self.repo1_org1, 20, start_date="-7d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "day",
                "agg_function": "min",
                "agg_value": "coverage",
                "start_date": datetime.now(tz=UTC) - relativedelta(days=7),
                "end_date": datetime.now(tz=UTC),
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
                "start_date": datetime.now(tz=UTC) - relativedelta(months=6),
                "end_date": datetime.now(tz=UTC),
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
                "start_date": datetime.now(tz=UTC) - relativedelta(days=7),
                "end_date": datetime.now(tz=UTC),
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
                "start_date": datetime.now(tz=UTC) - relativedelta(days=7),
                "end_date": datetime.now(tz=UTC),
                "repositories": [self.repo1_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)

        with self.subTest("most recent commit, multiple repos"):
            setup_commits(self.repo1_org1, 20, start_date="-365d")
            setup_commits(self.repo2_org1, 20, start_date="-365d")

            data = {
                "owner_username": self.org1.username,
                "grouping_unit": "quarter",
                "agg_function": "max",
                "agg_value": "timestamp",
                "start_date": datetime.now(tz=UTC) - relativedelta(months=12),
                "end_date": datetime.now(tz=UTC),
                "repositories": [self.repo1_org1.name, self.repo2_org1.name],
            }

            initial_queryset = annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, self.user
                )
            )
            grouped_queryset = apply_grouping(initial_queryset, data)
            check_grouping_correctness(grouped_queryset, initial_queryset, data)

            aggregate_across_repositories(grouped_queryset)

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

    def test_aggregate_across_repositories(self):
        repo2_org2 = RepositoryFactory(author=self.org2)
        repo3_org2 = RepositoryFactory(author=self.org2)
        repo4_org2 = RepositoryFactory(author=self.org2)
        user2 = OwnerFactory(
            service="github",
            organizations=[self.org2.ownerid],
            permission=[repo2_org2.repoid, repo3_org2.repoid, repo4_org2.repoid],
        )

        setup_commits(
            repo2_org2,
            1,
            start_date=datetime.today(),
            lines=108,
            hits=78,
            partials=10.5,
        )
        setup_commits(
            repo3_org2, 1, start_date=datetime.today(), lines=562, hits=208, partials=77
        )
        setup_commits(
            repo4_org2, 1, start_date=datetime.today(), lines=342, hits=315, partials=1
        )

        data = {
            "owner_username": self.org2.username,
            "grouping_unit": "day",
            "agg_function": "max",
            "agg_value": "timestamp",
            "start_date": datetime.combine(date.today(), time(0, tzinfo=UTC)),
            "repositories": [repo2_org2.name, repo3_org2.name, repo4_org2.name],
        }

        grouped_queryset = apply_grouping(
            annotate_commits_with_totals(
                apply_simple_filters(
                    apply_default_filters(Commit.objects.all()), data, user2
                )
            ),
            data,
        )

        result = aggregate_across_repositories(grouped_queryset)
        assert len(result) == 1
        assert result[0]["total_lines"] == 1012
        assert result[0]["total_hits"] == 601
        assert result[0]["total_partials"] == 88.5
        assert result[0]["date"].date() == date.today()


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


@patch("internal_api.permissions.RepositoryPermissionsService.has_read_permissions")
class OrganizationCoverageChartTest(InternalAPITest):
    def _retrieve(self, kwargs={}, data={}):
        return self.client.post(
            reverse("chart-coverage-organization", kwargs=kwargs),
            data=data,
            content_type="application/json",
        )

    def setUp(self):
        self.org1 = OwnerFactory()
        self.repo1_org1 = RepositoryFactory(author=self.org1)
        setup_commits(self.repo1_org1, 10, start_date="-4d")

        self.repo2_org1 = RepositoryFactory(author=self.org1)
        setup_commits(self.repo2_org1, 10, start_date="-4d")

        self.user = OwnerFactory(
            service="github",
            organizations=[self.org1.ownerid],
            permission=[self.repo1_org1.repoid, self.repo2_org1.repoid],
        )
        self.client.force_login(user=self.user)

    def test_no_permissions(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "day",
            "agg_function": "max",
            "agg_value": "coverage",
            "repositories": [self.repo1_org1.name, self.repo2_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = False
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 403

    def test_get_chart(self, mocked_get_permissions):
        data = {
            "branch": "master",
            "start_date": datetime.now(tz=UTC) - timedelta(7),
            "end_date": datetime.now(tz=UTC),
            "grouping_unit": "day",
            "agg_function": "max",
            "agg_value": "coverage",
            "repositories": [self.repo1_org1.name, self.repo2_org1.name],
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = True
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 200
        assert len(response.data["coverage"]) > 0
        for item in response.data["coverage"]:
            assert "weighted_coverage" in item
            assert "total_lines" in item
            assert "total_hits" in item
            assert "total_partials" in item
            assert "total_misses" in item

    def test_get_chart_default_params(self, mocked_get_permissions):
        data = {
            "grouping_unit": "day",
            "agg_function": "min",
            "agg_value": "timestamp",
        }

        kwargs = {"owner_username": self.org1.username, "service": "gh"}

        mocked_get_permissions.return_value = True
        response = self._retrieve(kwargs=kwargs, data=data)

        assert response.status_code == 200
        assert len(response.data["coverage"]) > 0
        for item in response.data["coverage"]:
            assert "weighted_coverage" in item
            assert "total_lines" in item
            assert "total_hits" in item
            assert "total_partials" in item
            assert "total_misses" in item
