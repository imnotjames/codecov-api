import enum


class OrderingParameter(enum.Enum):
    NAME = "name"
    COVERAGE = "coverage"


class RepositoryOrdering(enum.Enum):
    COMMIT_DATE = "latest_commit_at"
    COVERAGE = "coverage"
    ID = "repoid"
    NAME = "name"


class OrderingDirection(enum.Enum):
    ASC = "ascending"
    DESC = "descending"


class CoverageLine(enum.Enum):
    H = "hit"
    M = "miss"
    P = "partial"


class ComparisonError(enum.Enum):
    MISSING_BASE_REPORT = "missing_base_report"
    MISSING_HEAD_REPORT = "missing_head_report"


class TypeProjectOnboarding(enum.Enum):
    PERSONAL = "PERSONAL"
    YOUR_ORG = "YOUR_ORG"
    OPEN_SOURCE = "OPEN_SOURCE"
    EDUCATIONAL = "EDUCATIONAL"


class GoalOnboarding(enum.Enum):
    STARTING_WITH_TESTS = "STARTING_WITH_TESTS"
    IMPROVE_COVERAGE = "IMPROVE_COVERAGE"
    MAINTAIN_COVERAGE = "MAINTAIN_COVERAGE"
    TEAM_REQUIREMENTS = "TEAM_REQUIREMENTS"
    OTHER = "OTHER"


class PullRequestState(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class UploadState(enum.Enum):
    STARTED = "started"
    UPLOADED = "uploaded"
    PROCESSED = "processed"
    ERROR = "error"
    COMPLETE = "complete"


class UploadType(enum.Enum):
    UPLOADED = "uploaded"
    CARRIEDFORWARD = "carriedforward"


class UploadErrorEnum(enum.Enum):
    FILE_NOT_IN_STORAGE = "file_not_in_storage"
    REPORT_EXPIRED = "report_expired"
    REPORT_EMPTY = "report_empty"


class LoginProvider(enum.Enum):
    GITHUB = "github"
    GITHUB_ENTERPRISE = "github_enterprise"
    GITLAB = "gitlab"
    GITLAB_ENTERPRISE = "gitlab_enterprise"
    BITBUCKET = "bitbucket"
    BITBUCKET_SERVER = "bitbucket_server"


class CommitErrorGeneralType(enum.Enum):
    yaml_error = ("YAML_ERROR", "commit error of yaml type")
    bot_error = ("BOT_ERROR", "commit error of bot type")

    def __init__(self, slug, description):
        self.description = description
        self.slug = slug

    @classmethod
    def get_error_type_from_string(cls, error_type_str):
        for el in cls:
            if el.slug == error_type_str:
                return el
        return None


class CommitErrorCode(enum.Enum):
    invalid_yaml = ("invalid_yaml", CommitErrorGeneralType.yaml_error)
    yaml_client_error = ("yaml_client_error", CommitErrorGeneralType.yaml_error)
    yaml_unknown_error = ("yaml_unknown_error", CommitErrorGeneralType.yaml_error)
    repo_bot_invalid = ("repo_bot_invalid", CommitErrorGeneralType.bot_error)

    def __init__(self, db_string, error_type):
        self.db_string = db_string
        self.error_type = error_type

    @classmethod
    def get_codes_from_type(cls, error_type):
        return [item for item in cls if item.error_type == error_type]
