from collections.abc import Callable

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.api_core.exceptions import PermissionDenied
from google.auth.credentials import Credentials


class GA4DataApiError(RuntimeError):
    """A GA4 Data API error with a message safe to return to the workspace."""


class _GA4ReportClient:
    def __init__(self, credentials: Credentials, fallback_credentials: Callable[[], Credentials] | None):
        self._client = BetaAnalyticsDataClient(credentials=credentials)
        self._fallback_credentials = fallback_credentials

    def run_report(self, request):
        try:
            return self._client.run_report(request)
        except PermissionDenied as error:
            if error.reason != "SERVICE_DISABLED":
                raise
            if self._fallback_credentials is None:
                raise GA4DataApiError(
                    "The Google Analytics Data API is disabled in the workspace service account's Cloud project."
                ) from error

        # All GA4 calls in this refresh use the same workspace credential after the switch.
        self._client = BetaAnalyticsDataClient(credentials=self._fallback_credentials())
        self._fallback_credentials = None
        try:
            return self._client.run_report(request)
        except PermissionDenied as error:
            if error.reason == "SERVICE_DISABLED":
                raise GA4DataApiError("The Google Analytics Data API is disabled in both Cloud projects.") from error
            raise GA4DataApiError(
                "Google denied the workspace service account's GA4 request. Check its access to this GA4 property."
            ) from error


def get_ga4_client(*, credentials: Credentials, fallback_credentials: Callable[[], Credentials] | None = None):
    return _GA4ReportClient(credentials, fallback_credentials)
