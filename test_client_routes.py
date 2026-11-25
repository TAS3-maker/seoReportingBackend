import json
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

from bson import ObjectId
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.api.v1.routers.client_routes import (
    _gen_token,
    _verify_pass,
    client_router,
    create_client,
    delete_client,
    login_workspace,
    register_workspace,
    update_client,
)
from app.core.security import decrypt_secret
from app.integrations.credentials import build_source_client
from app.integrations.google_service_account import SERVICE_ACCOUNT_FIELD
from app.middlewares.protect_route import protect_route
from app.requestModels.req_models import (
    ClientCreateRequest,
    ClientUpdateRequest,
    LoginRequest,
    RegisterRequest,
)


class WorkspaceRouteTests(unittest.TestCase):
    def setUp(self):
        self.workspaces = Mock()
        self.workspace_id = ObjectId()
        self.workspaces.insert_one.return_value.inserted_id = self.workspace_id
        self.database = MagicMock()
        self.database.__getitem__.return_value = self.workspaces

    def test_password_verification_rejects_unsupported_hash_algorithm(self):
        self.assertFalse(_verify_pass("password", "unsupported$1$00$00"))

    @patch("app.api.v1.routers.client_routes.get_database")
    @patch("app.api.v1.routers.client_routes._gen_token", return_value="token")
    def test_registration_creates_workspace_without_client_integrations(
        self,
        gen_token,
        get_database,
    ):
        get_database.return_value = self.database
        request = RegisterRequest(
            username="workspace-owner",
            fullname="Workspace Owner",
            password="example-password",
        )

        result = register_workspace(request, Response())

        self.workspaces.create_index.assert_called_once_with(
            "username",
            unique=True,
        )
        document = self.workspaces.insert_one.call_args.args[0]
        self.assertEqual(document["username"], request.username)
        self.assertEqual(document["fullname"], request.fullname)
        self.assertNotIn("password", document)
        self.assertNotIn("integrations", document)
        self.assertTrue(document["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertEqual(result["clients"], [])
        self.assertEqual(result["workspace_id"], str(self.workspace_id))
        gen_token.assert_called_once()

    @patch("app.api.v1.routers.client_routes.get_database")
    def test_registration_rejects_duplicate_workspace_username(self, get_database):
        get_database.return_value = self.database
        self.workspaces.insert_one.side_effect = DuplicateKeyError("duplicate")
        request = RegisterRequest(
            username="workspace-owner",
            fullname="Workspace Owner",
            password="example-password",
        )

        with self.assertRaises(HTTPException) as error:
            register_workspace(request, Response())

        self.assertEqual(error.exception.status_code, 409)

    @patch("app.api.v1.routers.client_routes._gen_token", return_value="token")
    @patch("app.api.v1.routers.client_routes._verify_pass", return_value=True)
    @patch("app.api.v1.routers.client_routes.get_database")
    def test_login_authenticates_against_workspaces_collection(
        self,
        get_database,
        verify_password,
        gen_token,
    ):
        workspace_id = str(self.workspace_id)
        self.workspaces.find_one.return_value = {
            "_id": self.workspace_id,
            "username": "workspace-owner",
            "fullname": "Workspace Owner",
            "password_hash": "hashed-password",
        }
        get_database.return_value = self.database

        result = login_workspace(
            LoginRequest(
                username="workspace-owner",
                password="example-password",
            ),
            Response(),
        )

        self.workspaces.find_one.assert_called_once_with(
            {"username": "workspace-owner"}
        )
        verify_password.assert_called_once_with(
            "example-password",
            "hashed-password",
        )
        gen_token.assert_called_once_with(
            {
                "id": workspace_id,
                "workspace_id": workspace_id,
                "username": "workspace-owner",
                "fullname": "Workspace Owner",
            },
            unittest.mock.ANY,
        )
        self.assertEqual(result["workspace_id"], workspace_id)

    def test_auth_cookie_allows_cross_site_frontend_requests(self):
        response = Response()

        with patch.dict(
            os.environ,
            {
                "JWT_SECRET": "test-secret-that-is-at-least-32-bytes",
                "JWT_ALG": "HS256",
            },
        ):
            _gen_token({"workspace_id": str(ObjectId())}, response)

        cookie = response.headers["set-cookie"].lower()
        self.assertIn("samesite=none", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)


class WorkspaceServiceAccountTests(unittest.TestCase):
    def setUp(self):
        self.workspace_id = ObjectId()
        self.workspace = {"_id": self.workspace_id, "username": "owner", "fullname": "Owner"}
        self.workspaces = Mock()
        self.workspaces.find_one.return_value = self.workspace
        self.workspaces.update_one.side_effect = self.update_workspace
        database = {"workspaces": self.workspaces}
        patches = (
            patch("app.api.v1.routers.client_routes.get_database", return_value=database),
            patch("app.integrations.workspace_secrets.get_database", return_value=database),
            patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        app = FastAPI()
        app.include_router(client_router, prefix="/api/v1")
        app.dependency_overrides[protect_route] = lambda: {"workspace_id": str(self.workspace_id)}
        self.app = app
        self.http = TestClient(app)
        self.addCleanup(self.http.close)
        self.url = "/api/v1/me"

    def update_workspace(self, query, update):
        self.assertEqual(query, {"_id": self.workspace_id})
        self.workspace.update(update["$set"])
        return Mock(matched_count=1)

    def test_uploaded_key_is_encrypted_and_replacement_is_used_on_next_refresh(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        document = {
            "type": "service_account",
            "client_email": "first@example.iam.gserviceaccount.com",
            "private_key_id": "test-key-id",
            "private_key": private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        client = {"workspace_id": str(self.workspace_id)}
        self.assertFalse(self.http.get("/api/v1/me").json()["has_google_service_account"])
        with self.assertRaises(HTTPException) as missing:
            build_source_client(client, "gsc")
        self.assertEqual(missing.exception.status_code, 409)
        self.assertIn("Workspace Settings", missing.exception.detail)

        for email in (document["client_email"], "replacement@example.iam.gserviceaccount.com"):
            document["client_email"] = email
            response = self.http.patch(self.url, json={"google_service_account_json": json.dumps(document)})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["has_google_service_account"])
            self.assertNotIn("PRIVATE KEY", response.text)
            self.assertNotIn(SERVICE_ACCOUNT_FIELD, response.json())
            encrypted = self.workspace[SERVICE_ACCOUNT_FIELD]
            self.assertNotIn(email, encrypted)
            self.assertEqual(json.loads(decrypt_secret(encrypted)), document)
            profile = self.http.get("/api/v1/me").json()
            self.assertTrue(profile["has_google_service_account"])
            self.assertNotIn(SERVICE_ACCOUNT_FIELD, profile)
            with patch("app.integrations.gsc.client.get_gsc_client") as build:
                build_source_client(client, "gsc")
            credentials = build.call_args.kwargs["credentials"]
            self.assertEqual(credentials.service_account_email, email)
            self.assertEqual(credentials.scopes, ("https://www.googleapis.com/auth/webmasters.readonly",))

        response = self.http.patch(self.url, json={
            "google_service_account_json": json.dumps({**document, "token_uri": "https://example.test/token"}),
        })
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE KEY", response.text)
        self.assertEqual(self.workspace[SERVICE_ACCOUNT_FIELD], encrypted)

        response = self.http.patch(self.url, json={"openai_api_key": "example-key"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_google_service_account"])
        self.assertEqual(self.workspace[SERVICE_ACCOUNT_FIELD], encrypted)

    def test_upload_requires_authentication_and_sanitizes_parse_errors(self):
        for invalid_value in ('{"private_key": "private-value"', {"private_key": "private-value"}, None):
            with self.subTest(value=invalid_value):
                response = self.http.patch(self.url, json={"google_service_account_json": invalid_value})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("private-value", response.text)
        response = self.http.patch(self.url, json={"google_service_account_json": "x" * (16 * 1024 + 1)})
        self.assertEqual(response.status_code, 413)

        del self.app.dependency_overrides[protect_route]
        self.assertEqual(self.http.patch(self.url, json={"google_service_account_json": "{}"}).status_code, 401)
        self.workspaces.update_one.assert_not_called()


class ClientManagementRouteTests(unittest.TestCase):
    def setUp(self):
        self.clients = Mock()
        self.clients.insert_one.return_value.inserted_id = ObjectId()
        self.database = MagicMock()
        self.database.__getitem__.return_value = self.clients
        self.workspace_id = str(ObjectId())

    @patch("app.api.v1.routers.client_routes.get_database")
    def test_create_client_attaches_it_to_authenticated_workspace(
        self,
        get_database,
    ):
        get_database.return_value = self.database
        request = ClientCreateRequest(
            name="Acme",
            integrations={
                "gsc": {
                    "enabled": True,
                    "property_url": "https://acme.example/",
                }
            },
        )

        result = create_client(
            request,
            {"workspace_id": self.workspace_id},
        )

        self.clients.create_index.assert_called_once_with("workspace_id")
        stored = self.clients.insert_one.call_args.args[0]
        self.assertEqual(stored["workspace_id"], self.workspace_id)
        self.assertEqual(stored["name"], "Acme")
        self.assertEqual(
            result["client_id"],
            str(self.clients.insert_one.return_value.inserted_id),
        )
        self.assertEqual(
            result["integrations"]["gsc"]["property_url"],
            "https://acme.example/",
        )

    @patch("app.api.v1.routers.client_routes.get_workspace_client")
    @patch("app.api.v1.routers.client_routes.get_database")
    def test_update_client_is_scoped_to_workspace(
        self,
        get_database,
        get_workspace_client,
    ):
        client_id = str(ObjectId())
        get_database.return_value = self.database
        get_workspace_client.return_value = {
            "_id": ObjectId(client_id),
            "name": "Acme Updated",
            "integrations": {
                "gsc": {
                    "enabled": False,
                    "property_url": "https://acme.example/",
                }
            },
        }
        request = ClientUpdateRequest(name="Acme Updated")

        result = update_client(
            client_id,
            request,
            {"workspace_id": self.workspace_id},
        )

        query = self.clients.update_one.call_args.args[0]
        self.assertEqual(
            query,
            {
                "_id": ObjectId(client_id),
                "workspace_id": self.workspace_id,
            },
        )
        self.assertEqual(result["name"], "Acme Updated")
        get_workspace_client.assert_any_call(self.workspace_id, client_id)

    @patch("app.api.v1.routers.client_routes.get_workspace_client")
    @patch("app.api.v1.routers.client_routes.get_database")
    def test_delete_client_cascades_only_within_authenticated_workspace(
        self,
        get_database,
        get_workspace_client,
    ):
        client_id = str(ObjectId())
        collection_names = (
            "clients",
            "ga4_reports",
            "gsc_search_reports",
            "gsc_sites",
            "google_sheets_reports",
            "gsc_data",
        )
        collections = {name: Mock() for name in collection_names}
        collections["clients"].delete_one.return_value.deleted_count = 1
        database = MagicMock()
        database.__getitem__.side_effect = collections.__getitem__
        get_database.return_value = database
        get_workspace_client.return_value = {"_id": ObjectId(client_id)}

        with patch("app.reports.storage.delete_client_reports") as delete_reports:
            result = delete_client(
                client_id,
                {"workspace_id": self.workspace_id},
            )
        delete_reports.assert_called_once_with(self.workspace_id, client_id)

        get_workspace_client.assert_called_once_with(
            self.workspace_id,
            client_id,
        )
        for collection_name in collection_names[1:]:
            collections[collection_name].delete_many.assert_called_once_with(
                {
                    "workspace_id": self.workspace_id,
                    "client_id": client_id,
                }
            )
        collections["clients"].delete_one.assert_called_once_with(
            {
                "_id": ObjectId(client_id),
                "workspace_id": self.workspace_id,
            }
        )
        self.assertEqual(result["client_id"], client_id)


if __name__ == "__main__":
    unittest.main()
