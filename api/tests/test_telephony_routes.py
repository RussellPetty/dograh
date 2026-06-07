from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.telephony import router
from api.services.auth.depends import get_user


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_user] = lambda: SimpleNamespace(
        id=7,
        selected_organization_id=11,
    )
    return app


def _workflow(*, workflow_id: int = 33, user_id: int = 99):
    return SimpleNamespace(
        id=workflow_id,
        user_id=user_id,
        organization_id=11,
        template_context_variables={"template_key": "template-value"},
    )


def _provider():
    return SimpleNamespace(
        PROVIDER_NAME="twilio",
        WEBHOOK_ENDPOINT="twilio/voice",
        validate_config=Mock(return_value=True),
        initiate_call=AsyncMock(
            return_value=SimpleNamespace(
                caller_number="+15550001111",
                provider_metadata={"call_id": "call-123"},
            )
        ),
    )


def test_initiate_call_executes_as_workflow_owner_for_shared_org_workflow():
    app = _make_test_app()
    client = TestClient(app)

    workflow = _workflow()
    provider = _provider()
    quota_mock = AsyncMock(
        return_value=SimpleNamespace(has_quota=True, error_message="")
    )

    with (
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.check_dograh_quota_by_user_id",
            new=quota_mock,
        ),
        patch(
            "api.routes.telephony.get_default_telephony_provider",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "api.routes.telephony.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api.example.com", "wss://ignored")),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(
            return_value=SimpleNamespace(test_phone_number=None)
        )
        mock_db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=55)
        )
        mock_db.get_workflow = AsyncMock(return_value=workflow)
        mock_db.create_workflow_run = AsyncMock(
            return_value=SimpleNamespace(
                id=501,
                name="WR-TEL-OUT-00000001",
                initial_context={"template_key": "template-value"},
            )
        )
        mock_db.update_workflow_run = AsyncMock()

        response = client.post(
            "/telephony/initiate-call",
            json={"workflow_id": workflow.id, "phone_number": "+15551234567"},
        )

    assert response.status_code == 200
    quota_mock.assert_awaited_once_with(workflow.user_id, workflow_id=workflow.id)
    mock_db.get_workflow.assert_awaited_once_with(workflow.id, organization_id=11)

    create_call = mock_db.create_workflow_run.await_args
    create_args = create_call.args
    create_kwargs = create_call.kwargs
    assert create_args[1] == workflow.id
    assert create_kwargs["user_id"] == workflow.user_id
    assert create_kwargs["organization_id"] == workflow.organization_id
    assert create_kwargs["initial_context"]["template_key"] == "template-value"

    initiate_kwargs = provider.initiate_call.await_args.kwargs
    assert initiate_kwargs["workflow_id"] == workflow.id
    assert initiate_kwargs["user_id"] == workflow.user_id
    assert "user_id=99" in initiate_kwargs["webhook_url"]


def test_initiate_call_merges_per_call_context_variables_into_initial_context():
    """Per-call context_variables land at the top level of the run's
    initial_context — the exact dict the prompt renderer reads from
    (run_pipeline merges initial_context -> call_context_vars ->
    PipecatEngine._call_context_vars -> render_template). Per-call values
    override the workflow's template_context_variables defaults."""
    from api.utils.template_renderer import render_template

    app = _make_test_app()
    client = TestClient(app)

    # Workflow template provides a default "reason" that the per-call value
    # should override, plus an untouched template key.
    workflow = SimpleNamespace(
        id=33,
        user_id=99,
        organization_id=11,
        template_context_variables={
            "template_key": "template-value",
            "reason": "default-reason",
        },
    )
    provider = _provider()
    quota_mock = AsyncMock(
        return_value=SimpleNamespace(has_quota=True, error_message="")
    )

    with (
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.check_dograh_quota_by_user_id",
            new=quota_mock,
        ),
        patch(
            "api.routes.telephony.get_default_telephony_provider",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "api.routes.telephony.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api.example.com", "wss://ignored")),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(
            return_value=SimpleNamespace(test_phone_number=None)
        )
        mock_db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=55)
        )
        mock_db.get_workflow = AsyncMock(return_value=workflow)
        mock_db.create_workflow_run = AsyncMock(
            return_value=SimpleNamespace(
                id=501,
                name="WR-TEL-OUT-00000001",
                initial_context={"template_key": "template-value"},
            )
        )
        mock_db.update_workflow_run = AsyncMock()

        response = client.post(
            "/telephony/initiate-call",
            json={
                "workflow_id": workflow.id,
                "phone_number": "+15551234567",
                "context_variables": {
                    "reason": "loan refinance",
                    "goals": "lower monthly payment",
                    "first_name": "Dana",
                },
            },
        )

    assert response.status_code == 200

    initial_context = mock_db.create_workflow_run.await_args.kwargs["initial_context"]
    # Per-call values present at the top level (where render_template looks).
    assert initial_context["reason"] == "loan refinance"
    assert initial_context["goals"] == "lower monthly payment"
    assert initial_context["first_name"] == "Dana"
    # Per-call value overrode the workflow template default for the same key.
    assert initial_context["reason"] != "default-reason"
    # Untouched template key is preserved alongside the per-call values.
    assert initial_context["template_key"] == "template-value"

    # Prove the prompt renderer (the real one used by PipecatEngine via
    # _call_context_vars) resolves the placeholders against this dict.
    prompt = "Hi {{first_name}}, calling about {{reason}} to {{goals}}."
    rendered = render_template(prompt, initial_context)
    assert rendered == "Hi Dana, calling about loan refinance to lower monthly payment."


def test_initiate_call_without_context_variables_is_unchanged():
    """Omitting context_variables leaves initial_context exactly as before:
    only template_context_variables + the standard telephony keys, with no
    extra/None entries introduced by the new field."""
    app = _make_test_app()
    client = TestClient(app)

    workflow = _workflow()  # template_context_variables={"template_key": ...}
    provider = _provider()
    quota_mock = AsyncMock(
        return_value=SimpleNamespace(has_quota=True, error_message="")
    )

    with (
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.check_dograh_quota_by_user_id",
            new=quota_mock,
        ),
        patch(
            "api.routes.telephony.get_default_telephony_provider",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "api.routes.telephony.get_backend_endpoints",
            new=AsyncMock(return_value=("https://api.example.com", "wss://ignored")),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(
            return_value=SimpleNamespace(test_phone_number=None)
        )
        mock_db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=55)
        )
        mock_db.get_workflow = AsyncMock(return_value=workflow)
        mock_db.create_workflow_run = AsyncMock(
            return_value=SimpleNamespace(
                id=501,
                name="WR-TEL-OUT-00000001",
                initial_context={"template_key": "template-value"},
            )
        )
        mock_db.update_workflow_run = AsyncMock()

        # Note: no "context_variables" key in the request body at all.
        response = client.post(
            "/telephony/initiate-call",
            json={"workflow_id": workflow.id, "phone_number": "+15551234567"},
        )

    assert response.status_code == 200

    initial_context = mock_db.create_workflow_run.await_args.kwargs["initial_context"]
    assert initial_context == {
        "template_key": "template-value",
        "phone_number": "+15551234567",
        "called_number": "+15551234567",
        "provider": "twilio",
        "telephony_configuration_id": 55,
    }


def test_initiate_call_rejects_existing_run_for_different_workflow():
    app = _make_test_app()
    client = TestClient(app)

    workflow = _workflow()
    provider = _provider()
    quota_mock = AsyncMock(
        return_value=SimpleNamespace(has_quota=True, error_message="")
    )

    with (
        patch("api.routes.telephony.db_client") as mock_db,
        patch(
            "api.routes.telephony.check_dograh_quota_by_user_id",
            new=quota_mock,
        ),
        patch(
            "api.routes.telephony.get_default_telephony_provider",
            new=AsyncMock(return_value=provider),
        ),
    ):
        mock_db.get_user_configurations = AsyncMock(
            return_value=SimpleNamespace(test_phone_number=None)
        )
        mock_db.get_default_telephony_configuration = AsyncMock(
            return_value=SimpleNamespace(id=55)
        )
        mock_db.get_workflow = AsyncMock(return_value=workflow)
        mock_db.get_workflow_run = AsyncMock(
            return_value=SimpleNamespace(
                id=501,
                workflow_id=44,
                name="WR-TEL-OUT-00000044",
                initial_context={},
            )
        )

        response = client.post(
            "/telephony/initiate-call",
            json={
                "workflow_id": workflow.id,
                "workflow_run_id": 501,
                "phone_number": "+15551234567",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "workflow_run_workflow_mismatch"
    mock_db.get_workflow_run.assert_awaited_once_with(501, organization_id=11)
    assert not mock_db.create_workflow_run.called
    assert provider.initiate_call.await_count == 0
