from asgiref.sync import sync_to_async

from core.models import Commit
from graphql_api.commands.base import BaseInteractor


class FetchCommitInteractor(BaseInteractor):
    @sync_to_async
    def execute(self, repository, commit_id):
        return Commit.objects.filter(repository=repository, commitid=commit_id).first()
