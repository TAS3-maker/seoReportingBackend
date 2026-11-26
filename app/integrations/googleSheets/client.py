from collections.abc import Callable

from google.auth.credentials import Credentials
from googleapiclient.discovery import build


class SheetsApiError(RuntimeError):
    """A Google Sheets error with a message safe to return to the workspace."""


def _build_sheets_resource(credentials: Credentials):
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


class SheetsClient:
    def __init__(self, credentials: Credentials, fallback_credentials: Callable[[], Credentials] | None):
        self.primary = _build_sheets_resource(credentials)
        self.fallback_credentials = fallback_credentials

    def build_fallback(self):
        return _build_sheets_resource(self.fallback_credentials())


def get_sheets_client(*, credentials: Credentials, fallback_credentials: Callable[[], Credentials] | None = None):
    return SheetsClient(credentials, fallback_credentials)
