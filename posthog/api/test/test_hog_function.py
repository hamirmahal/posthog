import json
from typing import Any, Optional
from unittest.mock import ANY, MagicMock, patch

from django.db import connection
from freezegun import freeze_time
from inline_snapshot import snapshot
from rest_framework import status
from django.test.utils import override_settings

from common.hogvm.python.operation import HOGQL_BYTECODE_VERSION, Operation
from posthog.api.test.test_hog_function_templates import MOCK_NODE_TEMPLATES
from posthog.api.hog_function_template import HogFunctionTemplates
from posthog.constants import AvailableFeature
from posthog.models.action.action import Action
from posthog.models.hog_functions.hog_function import DEFAULT_STATE, HogFunction
from posthog.test.base import APIBaseTest, ClickhouseTestMixin, QueryMatchingTest
from posthog.cdp.templates.webhook.template_webhook import template as template_webhook
from posthog.cdp.templates.slack.template_slack import template as template_slack
from posthog.models.team import Team
from posthog.api.hog_function import MAX_HOG_CODE_SIZE_BYTES, MAX_TRANSFORMATIONS_PER_TEAM


EXAMPLE_FULL = {
    "name": "HogHook",
    "hog": "fetch(inputs.url, {\n  'headers': inputs.headers,\n  'body': inputs.payload,\n  'method': inputs.method\n});",
    "type": "destination",
    "enabled": True,
    "inputs_schema": [
        {"key": "url", "type": "string", "label": "Webhook URL", "required": True},
        {"key": "payload", "type": "json", "label": "JSON Payload", "required": True},
        {
            "key": "method",
            "type": "choice",
            "label": "HTTP Method",
            "choices": [
                {"label": "POST", "value": "POST"},
                {"label": "PUT", "value": "PUT"},
                {"label": "PATCH", "value": "PATCH"},
                {"label": "GET", "value": "GET"},
            ],
            "required": True,
        },
        {"key": "headers", "type": "dictionary", "label": "Headers", "required": False},
    ],
    "inputs": {
        "url": {
            "value": "http://localhost:2080/0e02d917-563f-4050-9725-aad881b69937",
        },
        "method": {"value": "POST"},
        "headers": {
            "value": {"version": "v={event.properties.$lib_version}"},
        },
        "payload": {
            "value": {
                "event": "{event}",
                "groups": "{groups}",
                "nested": {"foo": "{event.url}"},
                "person": "{person}",
                "event_url": "{f'{event.url}-test'}",
            },
        },
    },
    "filters": {
        "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
        "actions": [{"id": "9", "name": "Test Action", "type": "actions", "order": 1}],
        "filter_test_accounts": True,
    },
}


def get_db_field_value(field, model_id):
    cursor = connection.cursor()
    cursor.execute(f"select {field} from posthog_hogfunction where id='{model_id}';")
    return cursor.fetchone()[0]


class TestHogFunctionAPIWithoutAvailableFeature(ClickhouseTestMixin, APIBaseTest, QueryMatchingTest):
    def setUp(self):
        super().setUp()
        with patch("posthog.api.hog_function_template.get_hog_function_templates") as mock_get_templates:
            mock_get_templates.return_value.status_code = 200
            mock_get_templates.return_value.json.return_value = MOCK_NODE_TEMPLATES
            HogFunctionTemplates._load_templates()  # Cache templates to simplify tests

    def _create_slack_function(self, data: Optional[dict] = None):
        payload = {
            "name": "Slack",
            "template_id": template_slack.id,
            "type": "destination",
            "inputs": {
                "slack_workspace": {"value": 1},
                "channel": {"value": "#general"},
            },
        }

        payload.update(data or {})

        return self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data=payload,
        )

    def test_create_hog_function_works_for_free_template(self):
        response = self._create_slack_function()
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["created_by"]["id"] == self.user.id
        assert response.json()["hog"] == template_slack.hog
        assert response.json()["inputs_schema"] == template_slack.inputs_schema

    def test_free_users_cannot_override_hog_or_schema(self):
        response = self._create_slack_function(
            {
                "hog": "fetch(inputs.url);",
                "inputs_schema": [
                    {"key": "url", "type": "string", "label": "Webhook URL", "required": True},
                ],
            }
        )
        new_response = response.json()
        # These did not change
        assert new_response["hog"] == template_slack.hog
        assert new_response["inputs_schema"] == template_slack.inputs_schema

    def test_free_users_cannot_use_without_template(self):
        response = self._create_slack_function({"template_id": None})

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json()["detail"] == "The Data Pipelines addon is required to create custom functions."

    def test_free_users_cannot_create_non_free_templates(self):
        response = self._create_slack_function(
            {
                "template_id": template_webhook.id,
            }
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json()["detail"] == "The Data Pipelines addon is required for this template."

    def test_free_users_can_update_non_free_templates(self):
        self.organization.available_product_features = [
            {"key": AvailableFeature.DATA_PIPELINES, "name": AvailableFeature.DATA_PIPELINES}
        ]
        self.organization.save()

        response = self._create_slack_function(
            {
                "name": template_webhook.name,
                "template_id": template_webhook.id,
                "inputs": {
                    "url": {"value": "https://example.com"},
                },
            }
        )

        assert response.json()["template"]["status"] == template_webhook.status

        self.organization.available_product_features = []
        self.organization.save()

        payload = {
            "name": template_webhook.name,
            "template_id": template_webhook.id,
            "inputs": {
                "url": {"value": "https://example.com/posthog-webhook-updated"},
            },
        }

        update_response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/",
            data=payload,
        )

        assert update_response.status_code == status.HTTP_200_OK, update_response.json()
        assert update_response.json()["inputs"]["url"]["value"] == "https://example.com/posthog-webhook-updated"


class TestHogFunctionAPI(ClickhouseTestMixin, APIBaseTest, QueryMatchingTest):
    def setUp(self):
        super().setUp()

        self.organization.available_product_features = [
            {"key": AvailableFeature.DATA_PIPELINES, "name": AvailableFeature.DATA_PIPELINES}
        ]
        self.organization.save()

        with patch("posthog.api.hog_function_template.get_hog_function_templates") as mock_get_templates:
            mock_get_templates.return_value.status_code = 200
            mock_get_templates.return_value.json.return_value = MOCK_NODE_TEMPLATES
            HogFunctionTemplates._load_templates()  # Cache templates to simplify tests

        # Create the action referenced in EXAMPLE_FULL
        if not Action.objects.filter(id=9, team=self.team).exists():
            Action.objects.create(id=9, name="Test Action", team=self.team, created_by=self.user)

    def _get_function_activity(
        self,
        function_id: Optional[int] = None,
    ) -> list:
        params: dict = {"scope": "HogFunction", "page": 1, "limit": 20}
        if function_id:
            params["item_id"] = function_id
        activity = self.client.get(f"/api/projects/{self.team.pk}/activity_log", data=params)
        self.assertEqual(activity.status_code, status.HTTP_200_OK)
        return activity.json().get("results")

    def _filter_expected_keys(self, actual_data, expected_structure):
        if isinstance(expected_structure, list) and expected_structure and isinstance(expected_structure[0], dict):
            return [self._filter_expected_keys(item, expected_structure[0]) for item in actual_data]
        elif isinstance(expected_structure, dict):
            return {
                key: self._filter_expected_keys(actual_data.get(key), expected_value)
                for key, expected_value in expected_structure.items()
            }
        else:
            return actual_data

    def test_create_hog_function(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "type": "destination",
                "name": "Fetch URL",
                "description": "Test description",
                "hog": "fetch(inputs.url);",
                "inputs": {},
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["created_by"]["id"] == self.user.id
        assert response.json() == {
            "id": ANY,
            "type": "destination",
            "name": "Fetch URL",
            "description": "Test description",
            "created_at": ANY,
            "created_by": ANY,
            "updated_at": ANY,
            "enabled": False,
            "hog": "fetch(inputs.url);",
            "bytecode": ["_H", HOGQL_BYTECODE_VERSION, 32, "url", 32, "inputs", 1, 2, 2, "fetch", 1, 35],
            "transpiled": None,
            "inputs_schema": [],
            "inputs": {},
            "filters": {"bytecode": ["_H", HOGQL_BYTECODE_VERSION, 29]},
            "icon_url": None,
            "template": None,
            "masking": None,
            "mappings": None,
            "status": {"rating": 0, "state": 0, "tokens": 0},
            "execution_order": None,
        }

        id = response.json()["id"]
        expected_activities = [
            {
                "activity": "created",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": None,
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
        ]
        actual_activities = self._get_function_activity(id)
        filtered_actual_activities = [
            self._filter_expected_keys(actual_activity, expected_activity)
            for actual_activity, expected_activity in zip(actual_activities, expected_activities)
        ]
        assert filtered_actual_activities == expected_activities

    def test_creates_with_template_id(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Fetch URL",
                "description": "Test description",
                "hog": "fetch(inputs.url);",
                "inputs": {"url": {"value": "https://example.com"}},
                "template_id": template_webhook.id,
                "type": "destination",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()

        assert response.json()["hog"] == "fetch(inputs.url);"
        assert response.json()["template"] == {
            "type": "destination",
            "free": False,
            "name": template_webhook.name,
            "description": template_webhook.description,
            "id": template_webhook.id,
            "status": template_webhook.status,
            "icon_url": template_webhook.icon_url,
            "category": template_webhook.category,
            "inputs_schema": template_webhook.inputs_schema,
            "hog": template_webhook.hog,
            "filters": None,
            "masking": None,
            "mappings": None,
            "mapping_templates": None,
            "sub_templates": response.json()["template"]["sub_templates"],
        }

    def test_creates_with_template_values_if_not_provided(self, *args):
        payload: dict = {
            "template_id": template_webhook.id,
            "type": "destination",
        }
        response = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data=payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json() == {
            "attr": "inputs__url",
            "code": "invalid_input",
            "detail": "This field is required.",
            "type": "validation_error",
        }

        payload["inputs"] = {"url": {"value": "https://example.com"}}

        response = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data=payload)
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["hog"] == template_webhook.hog
        assert response.json()["inputs_schema"] == template_webhook.inputs_schema
        assert response.json()["name"] == template_webhook.name
        assert response.json()["description"] == template_webhook.description
        assert response.json()["icon_url"] == template_webhook.icon_url

    def test_deletes_via_update(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "name": "Fetch URL",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        id = response.json()["id"]

        list_res = self.client.get(f"/api/projects/{self.team.id}/hog_functions/")
        assert list_res.status_code == status.HTTP_200_OK, list_res.json()
        # Assert that it isn't in the list
        assert (
            next((item for item in list_res.json()["results"] if item["id"] == response.json()["id"]), None) is not None
        )

        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/",
            data={"deleted": True},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()

        list_res = self.client.get(f"/api/projects/{self.team.id}/hog_functions/")
        assert list_res.status_code == status.HTTP_200_OK, list_res.json()
        assert next((item for item in list_res.json()["results"] if item["id"] == response.json()["id"]), None) is None

        expected_activities = [
            {
                "activity": "updated",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": [
                        {
                            "action": "changed",
                            "after": True,
                            "before": False,
                            "field": "deleted",
                            "type": "HogFunction",
                        }
                    ],
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
            {
                "activity": "created",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": None,
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
        ]
        actual_activities = self._get_function_activity(id)
        filtered_actual_activities = [
            self._filter_expected_keys(actual_activity, expected_activity)
            for actual_activity, expected_activity in zip(actual_activities, expected_activities)
        ]
        assert filtered_actual_activities == expected_activities

    def test_can_undelete_hog_function(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={**EXAMPLE_FULL},
        )
        id = response.json()["id"]

        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{id}/",
            data={"deleted": True},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()
        assert (
            self.client.get(f"/api/projects/{self.team.id}/hog_functions/{id}").status_code == status.HTTP_404_NOT_FOUND
        )

        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{id}/",
            data={"deleted": False},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()
        assert self.client.get(f"/api/projects/{self.team.id}/hog_functions/{id}").status_code == status.HTTP_200_OK

    def test_inputs_required(self, *args):
        payload = {
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "url", "type": "string", "label": "Webhook URL", "required": True},
            ],
            "type": "destination",
        }
        # Check required
        res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json() == {
            "type": "validation_error",
            "code": "invalid_input",
            "detail": "This field is required.",
            "attr": "inputs__url",
        }

    def test_validation_error_on_invalid_type(self, *args):
        payload = {
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "url", "type": "string", "label": "Webhook URL", "required": True},
            ],
            "type": "invalid_type",
        }
        # Check required
        res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json() == {
            "type": "validation_error",
            "code": "invalid_choice",
            "detail": '"invalid_type" is not a valid choice.',
            "attr": "type",
        }

    def test_inputs_mismatch_type(self, *args):
        payload = {
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "string", "type": "string"},
                {"key": "dictionary", "type": "dictionary"},
                {"key": "boolean", "type": "boolean"},
            ],
            "type": "destination",
        }

        bad_inputs = {
            "string": 123,
            "dictionary": 123,
            "boolean": 123,
        }

        for key, value in bad_inputs.items():
            res = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/", data={**payload, "inputs": {key: {"value": value}}}
            )
            assert res.json() == {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": f"Value must be a {key}.",
                "attr": f"inputs__{key}",
            }, f"Did not get error for {key}, got {res.json()}"
            assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()

    def test_validates_input_schema(self, *args):
        payload = {
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "not-a-key", "type": "not-valid", "label": "Webhook URL"},
            ],
            "type": "destination",
        }
        # Check required
        res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        assert res.status_code == status.HTTP_400_BAD_REQUEST

        assert res.json() == {
            "type": "validation_error",
            "code": "invalid_choice",
            "detail": '"not-valid" is not a valid choice.',
            "attr": "inputs_schema__0__type",
        }

    def test_secret_inputs_not_returned(self, *args):
        payload = {
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "url", "type": "string", "label": "Webhook URL", "secret": True, "required": True},
            ],
            "inputs": {
                "url": {
                    "value": "I AM SECRET",
                },
            },
            "type": "destination",
        }
        expectation = {
            "url": {
                "secret": True,
            }
        }
        # Fernet encryption is deterministic, but has a temporal component and utilizes os.urandom() for the IV
        with freeze_time("2024-01-01T00:01:00Z"):
            with patch("os.urandom", return_value=b"\x00" * 16):
                res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        assert res.status_code == status.HTTP_201_CREATED, res.json()
        assert res.json()["inputs"] == expectation
        res = self.client.get(f"/api/projects/{self.team.id}/hog_functions/{res.json()['id']}")
        assert res.json()["inputs"] == expectation

        # Finally check the DB has the real value
        obj = HogFunction.objects.get(id=res.json()["id"])
        assert obj.inputs == {}
        assert obj.encrypted_inputs == {
            "url": {
                "bytecode": [
                    "_H",
                    1,
                    32,
                    "I AM SECRET",
                ],
                "value": "I AM SECRET",
                "order": 0,
            },
        }

        raw_encrypted_inputs = get_db_field_value("encrypted_inputs", obj.id)

        assert (
            raw_encrypted_inputs
            == "gAAAAABlkgC8AAAAAAAAAAAAAAAAAAAAAKvzDjuLG689YjjVhmmbXAtZSRoucXuT8VtokVrCotIx3ttPcVufoVt76dyr2phbuotMldKMVv_Y6uzMDZFjX1Uvej4GHsYRbsTN_txcQHNnU7zvLee83DhHIrThEjceoq8i7hbfKrvqjEi7GCGc_k_Gi3V5KFxDOfLKnke4KM4s"
        )

    def test_secret_inputs_not_updated_if_not_changed(self, *args):
        payload = {
            "type": "destination",
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "secret1", "type": "string", "label": "Secret 1", "secret": True, "required": True},
                {"key": "secret2", "type": "string", "label": "Secret 2", "secret": True, "required": False},
            ],
            "inputs": {
                "secret1": {
                    "value": "I AM SECRET",
                },
            },
        }
        res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        assert res.json()["inputs"] == {"secret1": {"secret": True}}, res.json()
        res = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{res.json()['id']}",
            data={
                "inputs": {
                    "secret1": {
                        "secret": True,
                    },
                    "secret2": {
                        "value": "I AM ALSO SECRET",
                    },
                },
            },
        )
        assert res.json()["inputs"] == {"secret1": {"secret": True}, "secret2": {"secret": True}}, res.json()

        # Finally check the DB has the real value
        obj = HogFunction.objects.get(id=res.json()["id"])
        assert obj.encrypted_inputs["secret1"]["value"] == "I AM SECRET"
        assert obj.encrypted_inputs["secret2"]["value"] == "I AM ALSO SECRET"

    def test_secret_inputs_updated_if_changed(self, *args):
        payload = {
            "type": "destination",
            "name": "Fetch URL",
            "hog": "fetch(inputs.url);",
            "inputs_schema": [
                {"key": "secret1", "type": "string", "label": "Secret 1", "secret": True, "required": True},
                {"key": "secret2", "type": "string", "label": "Secret 2", "secret": True, "required": False},
            ],
            "inputs": {
                "secret1": {
                    "value": "I AM SECRET",
                },
            },
        }
        res = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data={**payload})
        id = res.json()["id"]
        assert res.json().get("inputs") == {"secret1": {"secret": True}}, res.json()
        res = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{res.json()['id']}",
            data={
                "inputs": {
                    "secret1": {
                        "value": "I AM CHANGED",
                    },
                    "secret2": {
                        "value": "I AM ALSO SECRET",
                    },
                },
            },
        )
        assert res.json().get("inputs") == {"secret1": {"secret": True}, "secret2": {"secret": True}}, res.json()

        # Finally check the DB has the real value
        obj = HogFunction.objects.get(id=res.json()["id"])
        assert obj.encrypted_inputs["secret1"]["value"] == "I AM CHANGED"
        assert obj.encrypted_inputs["secret2"]["value"] == "I AM ALSO SECRET"

        # changes to encrypted inputs aren't persisted
        expected_activities = [
            {
                "activity": "updated",
                "created_at": ANY,
                "detail": {
                    "changes": [
                        {
                            "action": "changed",
                            "after": "masked",
                            "before": "masked",
                            "field": "encrypted_inputs",
                            "type": "HogFunction",
                        }
                    ],
                    "name": "Fetch URL",
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
            {
                "activity": "created",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": None,
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
        ]
        actual_activities = self._get_function_activity(id)
        filtered_actual_activities = [
            self._filter_expected_keys(actual_activity, expected_activity)
            for actual_activity, expected_activity in zip(actual_activities, expected_activities)
        ]
        assert filtered_actual_activities == expected_activities

    def test_generates_hog_bytecode(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "hog": "let i := 0;\nwhile(i < 3) {\n  i := i + 1;\n  fetch(inputs.url, {\n    'headers': {\n      'x-count': f'{i}'\n    },\n    'body': inputs.payload,\n    'method': inputs.method\n  });\n}",
            },
        )
        # JSON loads for one line comparison
        assert response.json()["bytecode"] == json.loads(
            f'["_H", {HOGQL_BYTECODE_VERSION}, 33, 0, 33, 3, 36, 0, 15, 40, 45, 33, 1, 36, 0, 6, 37, 0, 32, "url", 32, "inputs", 1, 2, 32, "headers", 32, "x-count", 36, 0, 42, 1, 32, "body", 32, "payload", 32, "inputs", 1, 2, 32, "method", 32, "method", 32, "inputs", 1, 2, 42, 3, 2, "fetch", 2, 35, 39, -52, 35]'
        ), response.json()

    def test_generates_inputs_bytecode(self, *args):
        response = self.client.post(f"/api/projects/{self.team.id}/hog_functions/", data=EXAMPLE_FULL)
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["inputs"] == {
            "url": {
                "value": "http://localhost:2080/0e02d917-563f-4050-9725-aad881b69937",
                "bytecode": [
                    "_H",
                    HOGQL_BYTECODE_VERSION,
                    32,
                    "http://localhost:2080/0e02d917-563f-4050-9725-aad881b69937",
                ],
                "order": 0,
            },
            "payload": {
                "value": {
                    "event": "{event}",
                    "groups": "{groups}",
                    "nested": {"foo": "{event.url}"},
                    "person": "{person}",
                    "event_url": "{f'{event.url}-test'}",
                },
                "order": 1,
                "bytecode": {
                    "event": ["_H", HOGQL_BYTECODE_VERSION, 32, "event", 1, 1],
                    "groups": ["_H", HOGQL_BYTECODE_VERSION, 32, "groups", 1, 1],
                    "nested": {"foo": ["_H", HOGQL_BYTECODE_VERSION, 32, "url", 32, "event", 1, 2]},
                    "person": ["_H", HOGQL_BYTECODE_VERSION, 32, "person", 1, 1],
                    "event_url": [
                        "_H",
                        HOGQL_BYTECODE_VERSION,
                        32,
                        "url",
                        32,
                        "event",
                        1,
                        2,
                        32,
                        "-test",
                        2,
                        "concat",
                        2,
                    ],
                },
            },
            "method": {"value": "POST", "order": 2},
            "headers": {
                "value": {"version": "v={event.properties.$lib_version}"},
                "bytecode": {
                    "version": [
                        "_H",
                        HOGQL_BYTECODE_VERSION,
                        32,
                        "v=",
                        32,
                        "$lib_version",
                        32,
                        "properties",
                        32,
                        "event",
                        1,
                        3,
                        2,
                        "concat",
                        2,
                    ]
                },
                "order": 3,
            },
        }

    def test_generates_filters_bytecode(self, *args):
        action = Action.objects.create(
            team=self.team,
            name="test action",
            steps_json=[{"event": "$pageview", "url": "docs", "url_matching": "contains"}],
        )

        self.team.test_account_filters = [
            {
                "key": "email",
                "value": "@posthog.com",
                "operator": "not_icontains",
                "type": "person",
            }
        ]
        self.team.save()
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "filters": {
                    "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                    "actions": [{"id": f"{action.id}", "name": "Test Action", "type": "actions", "order": 1}],
                    "filter_test_accounts": True,
                },
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["filters"] == {
            "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
            "actions": [{"id": f"{action.id}", "name": "Test Action", "type": "actions", "order": 1}],
            "filter_test_accounts": True,
            "bytecode": [
                "_H",
                HOGQL_BYTECODE_VERSION,
                32,
                "%@posthog.com%",
                32,
                "email",
                32,
                "properties",
                32,
                "person",
                1,
                3,
                2,
                "toString",
                1,
                20,
                32,
                "$pageview",
                32,
                "event",
                1,
                1,
                11,
                3,
                2,
                32,
                "%@posthog.com%",
                32,
                "email",
                32,
                "properties",
                32,
                "person",
                1,
                3,
                2,
                "toString",
                1,
                20,
                32,
                "$pageview",
                32,
                "event",
                1,
                1,
                11,
                32,
                "%docs%",
                32,
                "$current_url",
                32,
                "properties",
                1,
                2,
                17,
                3,
                2,
                3,
                2,
                4,
                2,
            ],
        }

    def test_saves_masking_config(self, *args):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "masking": {"ttl": 60, "threshold": 20, "hash": "{person.properties.email}"},
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["masking"] == snapshot(
            {
                "ttl": 60,
                "threshold": 20,
                "hash": "{person.properties.email}",
                "bytecode": ["_H", HOGQL_BYTECODE_VERSION, 32, "email", 32, "properties", 32, "person", 1, 3],
            }
        )

    @patch("posthog.permissions.posthoganalytics.feature_enabled", return_value=True)
    def test_loads_status_when_enabled_and_available(self, *args):
        with patch("posthog.plugins.plugin_server_api.requests.get") as mock_get:
            mock_get.return_value.status_code = status.HTTP_200_OK
            mock_get.return_value.json.return_value = {"state": 1, "tokens": 0, "rating": 0}

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data=EXAMPLE_FULL,
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()

            response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}")
            assert response.json()["status"] == {"state": 1, "tokens": 0, "rating": 0}

    def test_does_not_crash_when_status_not_available(self, *args):
        with patch("posthog.plugins.plugin_server_api.requests.get") as mock_get:
            # Mock the api actually throwing fully
            mock_get.side_effect = lambda x: Exception("oh no")

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data=EXAMPLE_FULL,
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()
            response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}")
            assert response.json()["status"] == DEFAULT_STATE

    def test_patches_status_on_enabled_update(self, *args):
        with patch("posthog.plugins.plugin_server_api.requests.get") as mock_get:
            with patch("posthog.plugins.plugin_server_api.requests.patch") as mock_patch:
                mock_get.return_value.status_code = status.HTTP_200_OK
                mock_get.return_value.json.return_value = {"state": 4, "tokens": 0, "rating": 0}

                response = self.client.post(
                    f"/api/projects/{self.team.id}/hog_functions/",
                    data={
                        **EXAMPLE_FULL,
                        "name": "Fetch URL",
                    },
                )
                id = response.json()["id"]

                assert response.json()["status"]["state"] == 4

                self.client.patch(
                    f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/",
                    data={"enabled": False},
                )

                assert mock_patch.call_count == 0

                self.client.patch(
                    f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/",
                    data={"enabled": True},
                )

                assert mock_patch.call_count == 1
                mock_patch.assert_called_once_with(
                    f"http://localhost:6738/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/status",
                    json={"state": 2},
                )

        expected_activities = [
            {
                "activity": "updated",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": [
                        {
                            "action": "changed",
                            "after": True,
                            "before": False,
                            "field": "enabled",
                            "type": "HogFunction",
                        }
                    ],
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
            {
                "activity": "updated",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": [
                        {
                            "action": "changed",
                            "after": False,
                            "before": True,
                            "field": "enabled",
                            "type": "HogFunction",
                        }
                    ],
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
            {
                "activity": "created",
                "created_at": ANY,
                "detail": {
                    "name": "Fetch URL",
                    "changes": None,
                    "short_id": None,
                    "trigger": None,
                    "type": "destination",
                },
                "item_id": id,
                "scope": "HogFunction",
                "user": {
                    "email": "user1@posthog.com",
                    "first_name": "",
                },
            },
        ]
        actual_activities = self._get_function_activity(id)
        filtered_actual_activities = [
            self._filter_expected_keys(actual_activity, expected_activity)
            for actual_activity, expected_activity in zip(actual_activities, expected_activities)
        ]
        assert filtered_actual_activities == expected_activities

    def test_list_with_filters_filter(self, *args):
        action1 = Action.objects.create(
            team=self.team,
            name="test action",
            steps_json=[{"event": "$pageview", "url": "docs", "url_matching": "contains"}],
        )

        action2 = Action.objects.create(
            team=self.team,
            name="test action",
            steps_json=[{"event": "$pageview", "url": "docs", "url_matching": "contains"}],
        )

        self.team.test_account_filters = [
            {
                "key": "email",
                "value": "@posthog.com",
                "operator": "not_icontains",
                "type": "person",
            }
        ]
        self.team.save()
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "filters": {
                    "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                    "actions": [
                        {"id": f"{action1.id}", "name": "Test Action", "type": "actions", "order": 1},
                        {"id": f"{action2.id}", "name": "Test Action 2", "type": "actions", "order": 1},
                    ],
                    "filter_test_accounts": True,
                },
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()

        filters: Any = {"filter_test_accounts": True}
        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?filters={json.dumps(filters)}")
        assert len(response.json()["results"]) == 1

        filters = {"filter_test_accounts": False}
        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?filters={json.dumps(filters)}")
        assert len(response.json()["results"]) == 0

        filters = {"actions": [{"id": f"other"}]}
        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?filters={json.dumps(filters)}")
        assert len(response.json()["results"]) == 0

        filters = {"actions": [{"id": f"{action1.id}"}]}
        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?filters={json.dumps(filters)}")
        assert len(response.json()["results"]) == 1

        filters = {"actions": [{"id": f"{action2.id}"}]}
        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?filters={json.dumps(filters)}")
        assert len(response.json()["results"]) == 1

    def test_list_with_type_filter(self, *args):
        response_destination = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "filters": {
                    "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                },
            },
        )

        destination_id = response_destination.json()["id"]

        response_transform = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "HogTransform",
                "hog": "return event",
                "type": "transformation",
                "template_id": "template-geoip",
                "enabled": True,
            },
        )

        assert response_transform.status_code == status.HTTP_201_CREATED, response_transform.json()

        transformation_id = response_transform.json()["id"]

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/")
        assert len(response.json()["results"]) == 2

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?type=destination")
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["id"] == destination_id

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?type=transformation")
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["id"] == transformation_id

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?type=destination,site_app")
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["id"] == destination_id

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?type=destination,transformation")
        assert len(response.json()["results"]) == 2

    def test_list_with_enabled_filter(self, *args):
        response_destination = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                **EXAMPLE_FULL,
                "filters": {
                    "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                },
            },
        )

        destination_id = response_destination.json()["id"]

        response_transform = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "HogTransform",
                "hog": "return event",
                "type": "transformation",
                "template_id": "template-geoip",
                "enabled": False,
            },
        )

        transformation_id = response_transform.json()["id"]

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/")
        assert len(response.json()["results"]) == 2

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?enabled=true")

        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["id"] == destination_id

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?enabled=false")
        assert len(response.json()["results"]) == 1
        assert response.json()["results"][0]["id"] == transformation_id

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/?enabled=true,false")
        assert len(response.json()["results"]) == 2

    def test_create_hog_function_with_site_app_type(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Site App Function",
                "hog": "export function onLoad() { console.log('Hello, site_app'); }",
                "type": "site_app",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["bytecode"] is None
        assert "Hello, site_app" in response.json()["transpiled"]

    def test_create_hog_function_with_site_destination_type(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Site Destination Function",
                "hog": "export function onLoad() { console.log('Hello, site_destination'); }",
                "type": "site_destination",
            },
        )

        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["bytecode"] is None
        assert "Hello, site_destination" in response.json()["transpiled"]

    def test_cannot_modify_type_of_existing_hog_function(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data=EXAMPLE_FULL,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.json()

        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{response.json()['id']}/",
            data={"type": "site_app"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json() == {
            "attr": "type",
            "detail": "Cannot modify the type of an existing function",
            "code": "invalid_input",
            "type": "validation_error",
        }

    def test_transpiled_field_not_populated_for_other_types(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data=EXAMPLE_FULL,
        )

        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["bytecode"] is not None
        assert response.json()["transpiled"] is None

    def test_create_hog_function_with_invalid_typescript(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Invalid Site App Function",
                "hog": "export function onLoad() { console.log('Missing closing brace');",
                "type": "site_app",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert "detail" in response.json()
        assert "Error in TypeScript code" in response.json()["detail"]

    def test_create_typescript_destination_with_inputs(self):
        payload = {
            "name": "TypeScript Destination Function",
            "hog": "export function onLoad() { console.log(inputs.message); }",
            "type": "site_destination",
            "inputs_schema": [
                {"key": "message", "type": "string", "label": "Message", "required": True},
            ],
            "inputs": {
                "message": {
                    "value": "Hello, TypeScript {arrayMap(a -> a, [1, 2, 3])}!",
                },
            },
        }

        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data=payload,
        )
        result = response.json()

        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert result["bytecode"] is None
        assert "Hello, TypeScript" in result["transpiled"]
        inputs = result["inputs"]
        inputs["message"]["transpiled"]["stl"].sort()
        assert result["inputs"] == {
            "message": {
                "order": 0,
                "transpiled": {
                    "code": 'concat("Hello, TypeScript ", arrayMap(__lambda((a) => a), [1, 2, 3]), "!")',
                    "lang": "ts",
                    "stl": sorted(["__lambda", "concat", "arrayMap"]),
                },
                "value": "Hello, TypeScript {arrayMap(a -> a, [1, 2, 3])}!",
            }
        }

    def test_validates_mappings(self):
        payload = {
            "name": "TypeScript Destination Function",
            "hog": "export function onLoad() { console.log(inputs.message); }",
            "type": "site_destination",
            "mappings": [
                {
                    "inputs": {"message": {"value": "Hello, TypeScript {arrayMap(a -> a, [1, 2, 3])}!"}},
                    "inputs_schema": [
                        {"key": "message", "type": "string", "label": "Message", "required": True},
                        {"key": "required_field", "type": "string", "label": "Required", "required": True},
                    ],
                },
            ],
        }

        def create(payload):
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data=payload,
            )
            return response

        response = create(payload)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json() == snapshot(
            {
                "type": "validation_error",
                "code": "invalid_input",
                "detail": "This field is required.",
                "attr": "mappings__0__inputs__required_field",
            }
        )

    def test_compiles_valid_mappings(self):
        payload = {
            "name": "TypeScript Destination Function",
            "hog": "print(inputs.message)",
            "type": "destination",
            "mappings": [
                {
                    "inputs": {"message": {"value": "Hello, {arrayMap(a -> a, [1, 2, 3])}!"}},
                    "inputs_schema": [
                        {"key": "message", "type": "string", "label": "Message", "required": True},
                    ],
                    "filters": {
                        "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                        "filter_test_accounts": True,
                    },
                },
            ],
        }

        def create(payload):
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data=payload,
            )
            return response

        response = create(payload)
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["mappings"] == snapshot(
            [
                {
                    "inputs_schema": [
                        {
                            "type": "string",
                            "key": "message",
                            "label": "Message",
                            "required": True,
                            "secret": False,
                            "hidden": False,
                        }
                    ],
                    "inputs": {
                        "message": {
                            "value": "Hello, {arrayMap(a -> a, [1, 2, 3])}!",
                            "bytecode": [
                                "_H",
                                1,
                                32,
                                "Hello, ",
                                52,
                                "lambda",
                                1,
                                0,
                                3,
                                36,
                                0,
                                38,
                                53,
                                0,
                                33,
                                1,
                                33,
                                2,
                                33,
                                3,
                                43,
                                3,
                                2,
                                "arrayMap",
                                2,
                                32,
                                "!",
                                2,
                                "concat",
                                3,
                            ],
                            "order": 0,
                        }
                    },
                    "filters": {
                        "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                        "bytecode": [
                            "_H",
                            1,
                            32,
                            "%@posthog.com%",
                            32,
                            "email",
                            32,
                            "properties",
                            32,
                            "person",
                            1,
                            3,
                            2,
                            "toString",
                            1,
                            20,
                            32,
                            "$pageview",
                            32,
                            "event",
                            1,
                            1,
                            11,
                            3,
                            2,
                            4,
                            1,
                        ],
                        "filter_test_accounts": True,
                    },
                }
            ]
        )

    @override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[])
    def test_transformation_functions_require_template_when_disabled(self):
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Custom Transform",
                "type": "transformation",
                "hog": "return event",
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == {
            "type": "validation_error",
            "code": "invalid_input",
            "detail": "Transformation functions must be created from a template.",
            "attr": "template_id",
        }

    @override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[])
    def test_transformation_functions_preserve_template_code_when_disabled(self):
        with patch("posthog.api.hog_function_template.HogFunctionTemplates.template") as mock_template:
            mock_template.return_value = template_slack  # Use existing template instead of creating mock

            # First create with transformations enabled
            with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=["2"]):
                response = self.client.post(
                    f"/api/projects/{self.team.id}/hog_functions/",
                    data={
                        "name": "Template Transform",
                        "type": "transformation",
                        "template_id": template_slack.id,
                        "hog": "return event",
                        "inputs": {
                            "slack_workspace": {"value": 1},
                            "channel": {"value": "#general"},
                        },
                    },
                )
                assert response.status_code == status.HTTP_201_CREATED, response.json()
                function_id = response.json()["id"]

            # Try to update with transformations disabled
            response = self.client.patch(
                f"/api/projects/{self.team.id}/hog_functions/{function_id}/",
                data={
                    "hog": "return another_event",
                },
            )

            assert response.status_code == status.HTTP_200_OK
            assert response.json()["hog"] == template_slack.hog  # Original template code preserved

    @override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[])
    def test_transformation_uses_template_code_even_when_enabled(self):
        # Even with transformations enabled, we should still use template code
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "type": "transformation",
                "name": "Test Function",
                "description": "Test description",
                "template_id": template_slack.id,
                "hog": "return event",  # This should be ignored
                "inputs": {
                    "slack_workspace": {"value": 1},
                    "channel": {"value": "#general"},
                },
            },
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["hog"] == template_slack.hog  # Should always use template code

    def test_transformation_type_gets_execution_order_automatically(self):
        with patch("posthog.api.hog_function_template.HogFunctionTemplates.template") as mock_template:
            mock_template.return_value = template_slack

            # Create first transformation function
            response1 = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "type": "transformation",
                    "name": "First Transformation",
                    "template_id": template_slack.id,
                    "inputs": {
                        "slack_workspace": {"value": 1},
                        "channel": {"value": "#general"},
                    },
                },
            )
            assert response1.status_code == status.HTTP_201_CREATED
            assert response1.json()["execution_order"] == 1

            # Create second transformation function
            response2 = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "type": "transformation",
                    "name": "Second Transformation",
                    "template_id": template_slack.id,
                    "inputs": {
                        "slack_workspace": {"value": 1},
                        "channel": {"value": "#general"},
                    },
                },
            )
            assert response2.status_code == status.HTTP_201_CREATED
            assert response2.json()["execution_order"] == 2

            # Create a non-transformation function - should not get execution_order
            response3 = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    **EXAMPLE_FULL,  # This is fine for destination type
                    "type": "destination",
                    "name": "Destination Function",
                },
            )
            assert response3.status_code == status.HTTP_201_CREATED
            assert response3.json()["execution_order"] is None

    def test_list_hog_functions_ordered_by_execution_order_and_created_at(self):
        # Create functions with different execution orders and creation times
        with freeze_time("2024-01-01T00:00:00Z"):
            self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    **EXAMPLE_FULL,
                    "name": "Function 1",
                    "execution_order": 1,
                },
            ).json()

        with freeze_time("2024-01-02T00:00:00Z"):
            self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    **EXAMPLE_FULL,
                    "name": "Function 2",
                    "execution_order": 1,  # Same execution_order as fn1
                },
            ).json()

        with freeze_time("2024-01-03T00:00:00Z"):
            self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    **EXAMPLE_FULL,
                    "name": "Function 3",
                    "execution_order": 2,
                },
            ).json()

        with freeze_time("2024-01-04T00:00:00Z"):
            self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    **EXAMPLE_FULL,
                    "name": "Function 4",
                    "execution_order": None,  # No execution order
                },
            ).json()

        response = self.client.get(f"/api/projects/{self.team.id}/hog_functions/")
        assert response.status_code == status.HTTP_200_OK

        results = response.json()["results"]

        # Verify order: execution_order ASC, created_at ASC, nulls last
        assert [f["name"] for f in results] == [
            "Function 1",  # execution_order=1, created first
            "Function 2",  # execution_order=1, created second
            "Function 3",  # execution_order=2
            "Function 4",  # execution_order=null
        ]

    @override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=["2"])
    def test_transformation_code_editing_restricted_by_team(self):
        # Create team with ID 2
        team_2 = Team.objects.create(id=2, organization=self.organization, name="Team 2")
        self.team = team_2  # Switch to team 2 context

        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Custom Transform",
                "type": "transformation",
                "hog": "return event",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        assert response.json()["hog"] == "return event"

        # Create and switch to team 3
        team_3 = Team.objects.create(id=3, organization=self.organization, name="Team 3")
        self.team = team_3

        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Custom Transform",
                "type": "transformation",
                "hog": "return event",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
        assert response.json() == {
            "type": "validation_error",
            "code": "invalid_input",
            "detail": "Transformation functions must be created from a template.",
            "attr": "template_id",
        }

    @override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=["2"])
    def test_transformation_code_editing_with_template_restricted_by_team(self):
        with patch("posthog.api.hog_function_template.HogFunctionTemplates.template") as mock_template:
            mock_template.return_value = template_slack

            # Create and test with team ID 2
            team_2 = Team.objects.create(id=2, organization=self.organization, name="Team 2")
            self.team = team_2

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Template Transform",
                    "type": "transformation",
                    "template_id": template_slack.id,
                    "hog": "return event",
                    "inputs": {
                        "slack_workspace": {"value": 1},
                        "channel": {"value": "#general"},
                    },
                },
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()
            assert response.json()["hog"] == "return event"  # Custom code allowed

            # Create and test with team ID 3
            team_3 = Team.objects.create(id=3, organization=self.organization, name="Team 3")
            self.team = team_3

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Template Transform",
                    "type": "transformation",
                    "template_id": template_slack.id,
                    "hog": "return event",
                    "inputs": {
                        "slack_workspace": {"value": 1},
                        "channel": {"value": "#general"},
                    },
                },
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()
            assert response.json()["hog"] == template_slack.hog  # Template code enforced

    def test_can_call_a_test_invocation(self):
        with patch("posthog.api.hog_function.create_hog_invocation_test") as mock_create_hog_invocation_test:
            res = MagicMock(status_code=200, json=lambda: {"status": "success"})
            mock_create_hog_invocation_test.return_value = res

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/new/invocations/",
                data={
                    "configuration": {
                        **EXAMPLE_FULL,
                    },
                },
            )

            assert response.status_code == status.HTTP_200_OK, response.json()
            assert response.json() == {"status": "success"}

            assert mock_create_hog_invocation_test.call_count == 1
            assert mock_create_hog_invocation_test.call_args_list[0].kwargs["team_id"] == self.team.id
            assert mock_create_hog_invocation_test.call_args_list[0].kwargs["hog_function_id"] == "new"
            assert (
                mock_create_hog_invocation_test.call_args_list[0].kwargs["payload"]["configuration"]["type"]
                == "destination"
            )
            assert mock_create_hog_invocation_test.call_args_list[0].kwargs["payload"]["configuration"]["inputs"][
                "url"
            ] == {
                "bytecode": [
                    "_H",
                    1,
                    Operation.STRING,
                    "http://localhost:2080/0e02d917-563f-4050-9725-aad881b69937",
                ],
                "order": 0,
                "value": "http://localhost:2080/0e02d917-563f-4050-9725-aad881b69937",
            }

    def test_can_update_with_null_filters(self):
        # First create a function with filters
        response = self.client.post(
            f"/api/projects/{self.team.id}/hog_functions/",
            data={
                "name": "Test Function",
                "type": "destination",
                "hog": "print('hello world')",
                "filters": {
                    "events": [{"id": "$pageview", "name": "$pageview", "type": "events", "order": 0}],
                    "filter_test_accounts": True,
                },
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.json()
        function_id = response.json()["id"]

        # Verify filters were saved
        function = HogFunction.objects.get(id=function_id)
        assert function.filters.get("events") is not None
        assert function.filters.get("filter_test_accounts") is True
        assert function.filters.get("bytecode") is not None

        # Now update the function with null filters
        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{function_id}/",
            data={"filters": None},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()

        # Verify filters were updated to an empty object with valid bytecode
        function.refresh_from_db()
        assert function.filters.get("events", None) is None
        assert function.filters.get("filter_test_accounts", None) is None
        assert function.filters.get("bytecode") is not None

        # Also test with empty object
        response = self.client.patch(
            f"/api/projects/{self.team.id}/hog_functions/{function_id}/",
            data={"filters": {}},
        )
        assert response.status_code == status.HTTP_200_OK, response.json()

        # Verify filters remain an empty object with valid bytecode
        function.refresh_from_db()
        assert function.filters.get("events", None) is None
        assert function.filters.get("filter_test_accounts", None) is None
        assert function.filters.get("bytecode") is not None

    def test_limits_transformation_functions_per_team(self):
        """Test that we can create unlimited disabled transformations but only 20 enabled ones"""
        with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[self.team.id]):
            # 1. Create several disabled transformations (more than the limit)
            for i in range(5):
                response = self.client.post(
                    f"/api/projects/{self.team.id}/hog_functions/",
                    data={
                        "name": f"Disabled Transformation {i}",
                        "type": "transformation",
                        "hog": "return event",
                        "enabled": False,
                    },
                )
                assert response.status_code == status.HTTP_201_CREATED

            # 2. Create enabled transformations up to the limit
            for i in range(MAX_TRANSFORMATIONS_PER_TEAM):
                response = self.client.post(
                    f"/api/projects/{self.team.id}/hog_functions/",
                    data={
                        "name": f"Enabled Transformation {i}",
                        "type": "transformation",
                        "hog": "return event",
                        "enabled": True,
                    },
                )
                assert response.status_code == status.HTTP_201_CREATED

            # 3. Verify we hit the limit when trying to create one more enabled transformation
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "One Too Many",
                    "type": "transformation",
                    "hog": "return event",
                    "enabled": True,
                },
            )
            assert response.status_code == status.HTTP_400_BAD_REQUEST
            assert "Maximum of 20 enabled transformation functions" in response.json()["detail"]

            # 4. Verify we can still create disabled transformations when at the limit
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Another Disabled",
                    "type": "transformation",
                    "hog": "return event",
                    "enabled": False,
                },
            )
            assert response.status_code == status.HTTP_201_CREATED

            # 5. Test that we can enable after deleting an enabled one
            # First delete an enabled transformation
            enabled_transformation = HogFunction.objects.filter(
                team=self.team, type="transformation", deleted=False, enabled=True
            ).first()

            assert enabled_transformation is not None, "No enabled transformation found to delete"
            self.client.patch(
                f"/api/projects/{self.team.id}/hog_functions/{enabled_transformation.id}/",
                data={"deleted": True},
            )

            # Then enable a disabled transformation
            disabled_transformation = HogFunction.objects.filter(
                team=self.team, type="transformation", deleted=False, enabled=False
            ).first()

            assert disabled_transformation is not None, "No disabled transformation found to enable"
            response = self.client.patch(
                f"/api/projects/{self.team.id}/hog_functions/{disabled_transformation.id}/",
                data={"enabled": True},
            )
            assert response.status_code == status.HTTP_200_OK

    def test_validates_raw_hog_code_size(self):
        with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[self.team.id]):
            """Test that we validate the raw HOG code size before compiling it."""
            # Generate a large HOG code string that exceeds the maximum allowed size
            large_hog_code = "return " + "x" * (MAX_HOG_CODE_SIZE_BYTES + 1000)

            # Try to create a function with HOG code exceeding the size limit
            # No need to mock compile_hog as we're checking the string size directly
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Large HOG Code Function",
                    "type": "transformation",
                    "hog": large_hog_code,
                },
            )

            # Verify the creation was rejected with the correct error
            assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
            assert "HOG code exceeds maximum size" in response.json()["detail"]
            assert f"{MAX_HOG_CODE_SIZE_BYTES // 1024}KB" in response.json()["detail"]

    def test_validates_raw_hog_code_size_during_update(self):
        with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[self.team.id]):
            """Test that we validate the raw HOG code size when updating an existing function."""
            # First create a hog function with small, valid code
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Valid HOG Code Function",
                    "type": "transformation",
                    "hog": "return event",
                },
            )

            assert response.status_code == status.HTTP_201_CREATED, response.json()
            function_id = response.json()["id"]

            # Generate a large HOG code string for the update that exceeds the limit
            large_hog_code = "return " + "x" * (MAX_HOG_CODE_SIZE_BYTES + 1000)

            # Update the function with large HOG code
            update_response = self.client.patch(
                f"/api/projects/{self.team.id}/hog_functions/{function_id}/",
                data={
                    "hog": large_hog_code,
                },
            )

            # Verify the update was rejected with the correct error
            assert update_response.status_code == status.HTTP_400_BAD_REQUEST, update_response.json()
            assert "HOG code exceeds maximum size" in update_response.json()["detail"]
            assert f"{MAX_HOG_CODE_SIZE_BYTES // 1024}KB" in update_response.json()["detail"]

    def test_validation_catches_runtime_exceeded_in_python_vm_for_transformations(self):
        with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[self.team.id]):
            """Test that runtime exceeded errors during validation in our Python VM are properly handled for transformations"""
            # Create a function with an infinite loop that will exceed the 100ms validation timeout
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Slow Function",
                    "type": "transformation",
                    "hog": """
                    while (true) { print('hello'); } return event;
                    """,
                },
            )

            assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
            assert "Your function is taking too long to run (over 0.1 seconds)" in response.json()["detail"]

            # Test that the same code is allowed for destinations
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Slow Function",
                    "type": "destination",
                    "hog": """
                    while (true) { print('hello'); } return event;
                    """,
                },
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()

    def test_validation_catches_memory_exceeded_in_python_vm_for_transformations(self):
        with override_settings(HOG_TRANSFORMATIONS_CUSTOM_ENABLED_TEAMS=[self.team.id]):
            """Test that memory exceeded errors during validation in our Python VM are properly handled for transformations"""
            memory_hungry_code = """
                let arr := arrayMap(x -> toString(x), range(10000000));  // Create array with 10M strings
                return event;
                """

            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Memory Hungry Function",
                    "type": "transformation",
                    "hog": memory_hungry_code,
                },
            )

            assert response.status_code == status.HTTP_400_BAD_REQUEST, response.json()
            assert "Your function needs too much memory" in response.json()["detail"]

            # Test that the same code is allowed for destinations
            response = self.client.post(
                f"/api/projects/{self.team.id}/hog_functions/",
                data={
                    "name": "Memory Hungry Function",
                    "type": "destination",
                    "hog": memory_hungry_code,
                },
            )
            assert response.status_code == status.HTTP_201_CREATED, response.json()
