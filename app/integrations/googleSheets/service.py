from googleapiclient.errors import HttpError

from app.integrations.googleSheets.client import SheetsApiError, SheetsClient


def _read_values(resource, spreadsheet_id: str, range_name: str):
    response = (
        resource.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        )
        .execute()
    )

    return response.get("values", [])


def _api_is_disabled(error: HttpError) -> bool:
    return error.status_code == 403 and isinstance(error.error_details, list) and any(
        isinstance(detail, dict) and detail.get("reason") == "SERVICE_DISABLED"
        for detail in error.error_details
    )


def get_sheet_data(service: SheetsClient, spreadsheet_id: str, range_name: str):
    try:
        return _read_values(service.primary, spreadsheet_id, range_name)
    except HttpError as error:
        if not _api_is_disabled(error):
            raise
        if service.fallback_credentials is None:
            raise SheetsApiError("The Google Sheets API is disabled in the workspace service account's Cloud project.") from error

    try:
        return _read_values(service.build_fallback(), spreadsheet_id, range_name)
    except HttpError as error:
        if _api_is_disabled(error):
            raise SheetsApiError("The Google Sheets API is disabled in both Cloud projects.") from error
        if error.status_code in (403, 404):
            raise SheetsApiError(
                "Google Sheets could not read this spreadsheet using the workspace service account. "
                "Check that the spreadsheet is shared with the service account email."
            ) from error
        raise
