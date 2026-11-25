import json

from cryptography.exceptions import UnsupportedAlgorithm
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account

from app.integrations.google_oauth import TOKEN_URL

SERVICE_ACCOUNT_FIELD = "google_service_account_json_enc"


class InvalidServiceAccount(ValueError):
    pass


def service_account_credentials(document: str, *, scopes: tuple[str, ...] = ()):
    try:
        info = json.loads(document)
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise InvalidServiceAccount

        fields = ("client_email", "private_key", "private_key_id")
        if any(not isinstance(info.get(field), str) or not info[field].strip() for field in fields):
            raise InvalidServiceAccount
        if info.get("token_uri") != TOKEN_URL or info.get("universe_domain", "googleapis.com") != "googleapis.com":
            raise InvalidServiceAccount

        # Only pass signing fields and the fixed Google endpoint to the SDK.
        signing_info = {field: info[field] for field in fields}
        return service_account.Credentials.from_service_account_info(
            {**signing_info, "token_uri": TOKEN_URL}, scopes=scopes,
        )
    except (GoogleAuthError, ValueError, TypeError, RecursionError, UnsupportedAlgorithm):
        raise InvalidServiceAccount from None
