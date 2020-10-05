from django.urls import path
from .views import GithubLoginView
from .views.gitlab import GitlabLoginView

urlpatterns = [
    path("github", GithubLoginView.as_view(), name="github-login",),
    path("gitlab", GitlabLoginView.as_view(), name="gitlab-login",),
]