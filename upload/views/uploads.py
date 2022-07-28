import logging

from django.forms import ValidationError
from django.http import HttpRequest, HttpResponseNotAllowed, HttpResponseNotFound
from django.utils import timezone
from rest_framework.generics import ListCreateAPIView
from rest_framework.permissions import AllowAny, BasePermission

from core.models import Commit, Repository
from reports.models import CommitReport
from services.archive import ArchiveService, MinioEndpoints
from upload.serializers import UploadSerializer

log = logging.getLogger(__name__)


class UploadViews(ListCreateAPIView):
    serializer_class = UploadSerializer
    permission_classes = [
        # TODO: implement the correct permissions
        AllowAny,
    ]

    def perform_create(self, serializer):
        repository = self.get_repo()
        commit = self.get_commit()
        report = self.get_report()
        archive_service = ArchiveService(repository)
        path = MinioEndpoints.raw.get_path(
            version="v4",
            date=timezone.now().strftime("%Y-%m-%d"),
            repo_hash=archive_service.storage_hash,
            commit_sha=commit.commitid,
            reportid=report.external_id,
        )
        instance = serializer.save(storage_path=path, report_id=report.id)
        self.activate_repo(repository)
        return instance

    def list(self, request: HttpRequest, repo: str, commit_sha: str, reportid: str):
        return HttpResponseNotAllowed(permitted_methods=["POST"])

    def activate_repo(self, repository):
        # Only update the fields if needed
        if (
            repository.activated == True
            and repository.active == True
            and repository.deleted == False
        ):
            return
        repository.activated = True
        repository.active = True
        repository.deleted = False
        repository.save(update_fields=["activated", "active", "deleted", "updatestamp"])

    def get_repo(self) -> Repository:
        # TODO this is not final - how is getting the repo is still in discuss
        repoid = self.kwargs["repo"]
        try:
            repository = Repository.objects.get(name=repoid)
            return repository
        except Repository.DoesNotExist:
            raise ValidationError(f"Repository {repoid} not found")

    def get_commit(self) -> Commit:
        commit_sha = self.kwargs["commit_sha"]
        repository = self.get_repo()
        try:
            commit = Commit.objects.get(
                commitid=commit_sha, repository__repoid=repository.repoid
            )
            return commit
        except Commit.DoesNotExist:
            raise ValidationError(f"Commit {commit_sha} not found")

    def get_report(self) -> CommitReport:
        report_id = self.kwargs["reportid"]
        commit = self.get_commit()
        try:
            report = CommitReport.objects.get(
                external_id__exact=report_id, commit__commitid=commit.commitid
            )
            return report
        except CommitReport.DoesNotExist:
            raise ValidationError(f"Report {report_id} not found")
