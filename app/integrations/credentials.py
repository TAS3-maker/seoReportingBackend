from typing import Any, Literal

from fastapi import HTTPException, status

from app.integrations.google_oauth import (
    GoogleOAuthError,
    credentials_for_connection,
    get_connection,
)
from app.integrations.google_service_account import (
    InvalidServiceAccount,
    SERVICE_ACCOUNT_FIELD,
    service_account_credentials,
)
from app.integrations.workspace_secrets import workspace_secret

Source = Literal["gsc", "ga4", "google_sheets"]
SOURCE_SCOPES: dict[Source, str] = {
    "gsc": "https://www.googleapis.com/auth/webmasters.readonly",
    "ga4": "https://www.googleapis.com/auth/analytics.readonly",
    "google_sheets": "https://www.googleapis.com/auth/spreadsheets.readonly",
}


def user_credentials_for_client(client: dict[str, Any]):
    connection_id = client.get("google_connection_id")
    if not connection_id:
        raise HTTPException(409, "Connect a Google account and import this website before refreshing Google data.")
    connection = get_connection(client["workspace_id"], connection_id)
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Google account linked to this website was disconnected. Reconnect it in Settings.",
        )
    try:
        return credentials_for_connection(connection)
    except GoogleOAuthError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def build_source_client(client: dict[str, Any], source: Source):
    if client.get("google_connection_id"):
        credentials = user_credentials_for_client(client)
    else:
        document = workspace_secret(client["workspace_id"], SERVICE_ACCOUNT_FIELD)
        if not document:
            raise HTTPException(409, "Add a Google service account JSON in Workspace Settings to refresh Google data.")
        try:
            credentials = service_account_credentials(document, scopes=(SOURCE_SCOPES[source],))
        except InvalidServiceAccount:
            raise HTTPException(409, "Replace the Google service account JSON in Workspace Settings.") from None

    if source == "gsc":
        from app.integrations.gsc.client import get_gsc_client

        return get_gsc_client(credentials=credentials)
    if source == "ga4":
        from app.integrations.ga4.client import get_ga4_client

        return get_ga4_client(credentials=credentials)
    from app.integrations.googleSheets.client import get_sheets_client

    return get_sheets_client(credentials=credentials)
