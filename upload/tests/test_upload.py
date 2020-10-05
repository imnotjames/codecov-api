from rest_framework.test import APITestCase
from rest_framework.reverse import reverse
from rest_framework import status
from rest_framework.exceptions import ValidationError, NotFound
from unittest.mock import patch
from json import dumps
from yaml import YAMLError
from django.test import TestCase
from django.conf import settings
from django.test import RequestFactory
from urllib.parse import urlencode
from ddf import G

from core.models import Repository
from codecov_auth.models import Owner

from upload.helpers import (
    parse_params,
    get_global_tokens,
    determine_repo_for_upload,
    determine_upload_branch_to_use,
    determine_upload_pr_to_use,
    determine_upload_commitid_to_use,
)


def mock_get_config_side_effect(*args):
    if args == ("github", "global_upload_token"):
        return "githubuploadtoken"
    if args == ("gitlab", "global_upload_token"):
        return "gitlabuploadtoken"
    if args == ("bitbucket_server", "global_upload_token"):
        return "bitbucketserveruploadtoken"


class UploadHandlerHelpersTest(TestCase):
    def test_parse_params_validates_valid_input(self):
        request_params = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "slug": "codecov/codecov-api",
            "token": "testbtznwf3ooi3xlrsnetkddj5od731pap9",
            "service": "circleci",
            "pull_request": "undefined",
            "flags": "this-is-a-flag,this-is-another-flag",
            "name": "",
            "branch": "HEAD",
            "param_doesn't_exist_but_still_should_not_error": True,
            "s3": 123,
            "build_url": "https://thisisabuildurl.com",
            "_did_change_merge_commit": False,
            "parent": "123abc",
        }

        expected_result = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "slug": "codecov/codecov-api",
            "owner": "codecov",
            "repo": "codecov-api",
            "token": "testbtznwf3ooi3xlrsnetkddj5od731pap9",
            "service": "circleci",
            "pr": None,
            "pull_request": None,
            "flags": "this-is-a-flag,this-is-another-flag",
            "param_doesn't_exist_but_still_should_not_error": True,
            "s3": 123,
            "build_url": "https://thisisabuildurl.com",
            "job": None,
            "using_global_token": False,
            "branch": None,
            "_did_change_merge_commit": False,
            "parent": "123abc",
        }

        parsed_params = parse_params(request_params)
        assert expected_result == parsed_params

    def test_parse_params_errors_for_invalid_input(self):
        request_params = {
            "version": "v5",
            "slug": "not-a-valid-slug",
            "token": "testbtznwf3ooi3xlrsnetkddj5od731pap9",
            "service": "circleci",
            "pr": 123,
            "flags": "not_a_valid_flag!!?!",
            "s3": "this should be an integer",
            "build_url": "not a valid url!",
            "_did_change_merge_commit": "yup",
            "parent": 123,
        }

        with self.assertRaises(ValidationError) as err:
            parse_params(request_params)

        assert len(err.exception.detail) == 9

    def test_parse_params_transforms_input(self):
        request_params = {
            "version": "v4",
            "commit": "3BE5C52BD748C508a7e96993c02cf3518c816e84",
            "slug": "codecov/subgroup/codecov-api",
            "service": "travis-org",
            "pull_request": "439",
            "pr": "",
            "branch": "origin/test-branch",
            "travis_job_id": "travis-jobID",
            "build": "nil",
        }

        expected_result = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",  # converted to lower case
            "slug": "codecov/subgroup/codecov-api",
            "owner": "codecov:subgroup",  # extracted from slug
            "repo": "codecov-api",  # extracted from slug
            "service": "travis",  # "travis-org" converted to "travis"
            "pr": "439",  # populated from "pull_request" field since none was provided
            "pull_request": "439",
            "branch": "test-branch",  # "origin/" removed from name
            "job": "travis-jobID",  # populated from "travis_job_id" since none was provided
            "travis_job_id": "travis-jobID",
            "build": None,  # "nil" coerced to None
            "using_global_token": False,
        }

        parsed_params = parse_params(request_params)
        assert expected_result == parsed_params

        request_params = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "pr": "967",
            "pull_request": "null",
            "branch": "refs/heads/another-test-branch",
            "job": "jobID",
            "travis_job_id": "travis-jobID",
        }

        expected_result = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "owner": None,  # set to "None" since slug wasn't provided
            "repo": None,  # set to "None" since slug wasn't provided
            "pr": "967",  # not populated from "pull_request"
            "pull_request": None,  # "null" coerced to None
            "branch": "another-test-branch",  # "refs/heads" removed
            "job": "jobID",  # not populated from "travis_job_id"
            "travis_job_id": "travis-jobID",
            "using_global_token": False,
            "service": None,  # defaulted to None if not provided and not using global upload token
        }

        parsed_params = parse_params(request_params)
        assert expected_result == parsed_params

        request_params = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "slug": "",
            "pr": "",
            "pull_request": "156",
            "job": None,
            "travis_job_id": "travis-jobID",
        }

        expected_result = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "owner": None,  # set to "None" since slug wasn't provided
            "repo": None,  # set to "None" since slug wasn't provided
            "pr": "156",  # populated from "pull_request"
            "pull_request": "156",
            "job": "travis-jobID",  # populated from "travis_job_id"
            "travis_job_id": "travis-jobID",
            "service": None,  # defaulted to None if not provided and not using global upload token
            "using_global_token": False,
        }

        parsed_params = parse_params(request_params)
        assert expected_result == parsed_params

    @patch("upload.helpers.get_config")
    def test_parse_params_recognizes_global_token(self, mock_get_config):
        mock_get_config.side_effect = mock_get_config_side_effect

        request_params = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "token": "bitbucketserveruploadtoken",
        }

        expected_result = {
            "version": "v4",
            "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            "token": "bitbucketserveruploadtoken",
            "using_global_token": True,
            "service": "bitbucket_server",
            "job": None,
            "owner": None,
            "pr": None,
            "repo": None,
        }

        parsed_params = parse_params(request_params)
        assert expected_result == parsed_params

    @patch("upload.helpers.get_config")
    def test_get_global_tokens(self, mock_get_config):
        mock_get_config.side_effect = mock_get_config_side_effect

        expected_result = {
            "githubuploadtoken": "github",
            "gitlabuploadtoken": "gitlab",
            "bitbucketserveruploadtoken": "bitbucket_server",
        }

        global_tokens = get_global_tokens()
        assert expected_result == global_tokens

    def test_determine_repo_upload(self):
        with self.subTest("token found"):
            org = G(Owner)
            repo = G(Repository, author=org)

            params = {
                "version": "v4",
                "using_global_token": False,
                "token": repo.upload_token,
            }

            assert repo == determine_repo_for_upload(params)

        with self.subTest("token not found"):
            org = G(Owner)
            repo = G(Repository, author=org)

            params = {
                "version": "v4",
                "using_global_token": False,
                "token": "testbtznwf3ooi3xlrsnetkddj5od731pap9",
            }

            with self.assertRaises(NotFound):
                determine_repo_for_upload(params)

        with self.subTest("missing token or service"):
            params = {
                "version": "v4",
                "using_global_token": False,
                "service": None,
            }

            with self.assertRaises(ValidationError):
                determine_repo_for_upload(params)

    def test_determine_upload_branch_to_use(self):
        with self.subTest("no branch and no pr provided"):
            upload_params = {"branch": None, "pr": None}
            repo_default_branch = "defaultbranch"

            expected_value = "defaultbranch"
            assert expected_value == determine_upload_branch_to_use(
                upload_params, repo_default_branch
            )

        with self.subTest("pullid in branch name"):
            upload_params = {"branch": "pr/123", "pr": None}
            repo_default_branch = "defaultbranch"

            expected_value = None
            assert expected_value == determine_upload_branch_to_use(
                upload_params, repo_default_branch
            )

        with self.subTest("branch and no pr provided"):
            upload_params = {"branch": "uploadbranch", "pr": None}
            repo_default_branch = "defaultbranch"

            expected_value = "uploadbranch"
            assert expected_value == determine_upload_branch_to_use(
                upload_params, repo_default_branch
            )

        with self.subTest("branch and pr provided"):
            upload_params = {"branch": "uploadbranch", "pr": "123"}
            repo_default_branch = "defaultbranch"

            expected_value = "uploadbranch"
            assert expected_value == determine_upload_branch_to_use(
                upload_params, repo_default_branch
            )

    def test_determine_upload_pr_to_use(self):
        with self.subTest("pullid in branch"):
            upload_params = {"branch": "pr/123", "pr": "456"}

            expected_value = "123"
            assert expected_value == determine_upload_pr_to_use(upload_params)

        with self.subTest("pullid in arguments, no pullid in branch"):
            upload_params = {"branch": "uploadbranch", "pr": "456"}

            expected_value = "456"
            assert expected_value == determine_upload_pr_to_use(upload_params)

        with self.subTest("pullid not provided"):
            upload_params = {"branch": "uploadbranch", "pr": None}

            expected_value = None
            assert expected_value == determine_upload_pr_to_use(upload_params)

    def test_determine_upload_commitid_to_use(self):
        with self.subTest("not a github commit"):
            upload_params = {
                "service": "bitbucket",
                "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
            }

            expected_value = "3be5c52bd748c508a7e96993c02cf3518c816e84"

            assert expected_value == determine_upload_commitid_to_use(upload_params)


class UploadHandlerRouteTest(APITestCase):

    # Wrap client calls
    def _get(self, kwargs=None):
        return self.client.get(reverse("upload-handler", kwargs=kwargs))

    def _options(self, kwargs=None, data=None):
        return self.client.options(reverse("upload-handler", kwargs=kwargs))

    def _post(self, kwargs=None, data=None, query=None):
        query_string = f"?{urlencode(query)}" if query else ""
        url = reverse("upload-handler", kwargs=kwargs) + query_string
        return self.client.post(url, data=data)

    def setUp(self):
        self.org = G(Owner)
        self.repo = G(Repository, author=self.org)

    def test_get_request_returns_405(self):
        response = self._get(kwargs={"version": "v4"})

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    # Test headers
    def test_options_headers(self):
        response = self._options(kwargs={"version": "v2"})

        headers = response._headers

        assert headers["accept"] == ("Accept", "text/*")
        assert headers["access-control-allow-origin"] == (
            "Access-Control-Allow-Origin",
            "*",
        )
        assert headers["access-control-allow-method"] == (
            "Access-Control-Allow-Method",
            "POST",
        )
        assert headers["access-control-allow-headers"] == (
            "Access-Control-Allow-Headers",
            "Origin, Content-Type, Accept, X-User-Agent",
        )

    def test_post_headers(self):
        with self.subTest("v2"):
            response = self._post(kwargs={"version": "v2"})

            headers = response._headers

            assert headers["access-control-allow-origin"] == (
                "Access-Control-Allow-Origin",
                "*",
            )
            assert headers["access-control-allow-headers"] == (
                "Access-Control-Allow-Headers",
                "Origin, Content-Type, Accept, X-User-Agent",
            )
            assert headers["content-type"] != ("Content-Type", "text/plain",)

        with self.subTest("v4"):
            response = self._post(kwargs={"version": "v4"})

            headers = response._headers

            assert headers["access-control-allow-origin"] == (
                "Access-Control-Allow-Origin",
                "*",
            )
            assert headers["access-control-allow-headers"] == (
                "Access-Control-Allow-Headers",
                "Origin, Content-Type, Accept, X-User-Agent",
            )
            assert headers["content-type"] == ("Content-Type", "text/plain",)

    def test_param_parsing(self):
        with self.subTest("valid"):
            query_params = {
                "commit": "3be5c52bd748c508a7e96993c02cf3518c816e84",
                "token": self.repo.upload_token,
                "pr": "",
                "pull_request": "9838",
                "branch": "",
                "flags": "",
                "build_url": "",
                "travis_job_id": "abc",
            }

            response = self._post(kwargs={"version": "v4"}, query=query_params)

            assert response.status_code == 200

        with self.subTest("invalid"):
            query_params = {
                "pr": 9838,
                "flags": "flags!!!",
            }

            response = self._post(kwargs={"version": "v5"}, query=query_params)

            assert response.status_code == status.HTTP_400_BAD_REQUEST