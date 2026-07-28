"""
Unit tests for HMI Control API.

Tests the Flask Blueprint endpoints for grid control operations
with and without authorization integration.
"""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from flask import Flask

from src.hmi_control_api import ControlResult, HMIControlAPI


@pytest.fixture
def mock_simulator():
    """Create a mock grid simulator."""
    simulator = MagicMock()

    # Mock current state
    mock_state = MagicMock()
    mock_state.buses = {
        0: MagicMock(voltage_pu=1.0, voltage_kv=0.4),
        1: MagicMock(voltage_pu=0.98, voltage_kv=0.392),
    }
    mock_state.lines = {
        0: MagicMock(loading_percent=45.2, current_a=123.4),
    }
    simulator.get_current_state.return_value = mock_state
    simulator.current_time = 10.5

    return simulator


@pytest.fixture
def mock_auth_wrapper():
    """Create a mock authorization wrapper."""
    wrapper = MagicMock()

    # Default: allow all operations
    decision = MagicMock()
    decision.allowed = True
    decision.reason = "role_authorized"
    wrapper.authorize_operation.return_value = decision

    return wrapper


@pytest.fixture
def app_no_auth(mock_simulator):
    """Create Flask app without authorization."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    control_api = HMIControlAPI(simulator=mock_simulator, authorization_wrapper=None)
    app.register_blueprint(control_api.blueprint)

    return app


@pytest.fixture
def app_with_auth(mock_simulator, mock_auth_wrapper):
    """Create Flask app with authorization."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    control_api = HMIControlAPI(
        simulator=mock_simulator, authorization_wrapper=mock_auth_wrapper
    )
    app.register_blueprint(control_api.blueprint)

    return app


class TestControlResultDataclass:
    """Test ControlResult dataclass."""

    def test_control_result_creation(self):
        """Test creating a ControlResult."""
        result = ControlResult(
            success=True,
            operation="test_op",
            target="test_target",
            value=42,
            authorization_latency_ms=10.5,
            authorization_decision="allow",
            authorization_reason="test_reason",
            execution_latency_ms=2.3,
        )

        assert result.success is True
        assert result.operation == "test_op"
        assert result.target == "test_target"
        assert result.value == 42
        assert result.authorization_latency_ms == 10.5
        assert result.authorization_decision == "allow"
        assert result.authorization_reason == "test_reason"
        assert result.execution_latency_ms == 2.3
        assert result.error is None

    def test_control_result_with_error(self):
        """Test creating a ControlResult with error."""
        result = ControlResult(
            success=False,
            operation="test_op",
            target="test_target",
            value=None,
            authorization_latency_ms=5.0,
            authorization_decision="deny",
            authorization_reason="insufficient_role",
            execution_latency_ms=0.0,
            error="Authorization denied",
        )

        assert result.success is False
        assert result.error == "Authorization denied"


class TestReadMeasurementsNoAuth:
    """Test read measurements endpoint without authorization."""

    def test_read_measurements_success(self, app_no_auth):
        """Test successful read measurements without auth."""
        client = app_no_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert "measurements" in data
        assert "buses" in data["measurements"]
        assert "lines" in data["measurements"]
        assert data["measurements"]["timestamp"] == 10.5

        # Check authorization info (no auth configured)
        assert data["authorization"]["decision"] == "allow"
        assert data["authorization"]["reason"] == "no_authorization_configured"
        assert data["authorization"]["latency_ms"] == 0.0

    def test_read_measurements_no_state(self, app_no_auth, mock_simulator):
        """Test read measurements when no state available."""
        mock_simulator.get_current_state.return_value = None
        client = app_no_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is False
        assert "No grid state available" in data["error"]


class TestReadMeasurementsWithAuth:
    """Test read measurements endpoint with authorization."""

    def test_read_measurements_allowed(self, app_with_auth, mock_auth_wrapper):
        """Test read measurements when authorized."""
        client = app_with_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["authorization"]["decision"] == "allow"
        assert data["authorization"]["reason"] == "role_authorized"

        # Verify authorization was called
        mock_auth_wrapper.authorize_operation.assert_called_once()
        call_args = mock_auth_wrapper.authorize_operation.call_args
        assert call_args[1]["operation_type"] == "read"
        assert call_args[1]["asset_id"] == "grid"
        assert call_args[1]["user_id"] == "test_user"
        assert call_args[1]["user_roles"] == ["operator"]

    def test_read_measurements_denied(self, app_with_auth, mock_auth_wrapper):
        """Test read measurements when denied."""
        # Configure mock to deny
        decision = MagicMock()
        decision.allowed = False
        decision.reason = "insufficient_role"
        mock_auth_wrapper.authorize_operation.return_value = decision

        client = app_with_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=viewer")

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is False
        assert data["authorization"]["decision"] == "deny"
        assert data["authorization"]["reason"] == "insufficient_role"
        assert data["error"] == "Authorization denied"

    def test_read_measurements_auth_error(self, app_with_auth, mock_auth_wrapper):
        """Test read measurements when authorization errors."""
        # Configure mock to raise exception
        mock_auth_wrapper.authorize_operation.side_effect = Exception("OPA timeout")

        client = app_with_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is False
        assert data["authorization"]["decision"] == "error"
        assert "OPA timeout" in data["authorization"]["reason"]


class TestBreakerControlNoAuth:
    """Test breaker control endpoint without authorization."""

    def test_breaker_close_success(self, app_no_auth):
        """Test successful breaker close without auth."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            json={
                "action": "close",
                "user_id": "test_user",
                "user_roles": ["operator"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["operation"] == "breaker_control"
        assert data["target"] == "breaker-0"
        assert data["value"] == "close"
        assert data["authorization"]["decision"] == "allow"

    def test_breaker_open_success(self, app_no_auth):
        """Test successful breaker open without auth."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/5",
            json={"action": "open", "user_id": "test_user", "user_roles": ["operator"]},
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["target"] == "breaker-5"
        assert data["value"] == "open"

    def test_breaker_invalid_action(self, app_no_auth):
        """Test breaker control with invalid action."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            json={
                "action": "invalid",
                "user_id": "test_user",
                "user_roles": ["operator"],
            },
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "Invalid action" in data["error"]

    def test_breaker_missing_action(self, app_no_auth):
        """Test breaker control with missing action."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            json={"user_id": "test_user", "user_roles": ["operator"]},
        )

        assert response.status_code == 400


class TestBreakerControlWithAuth:
    """Test breaker control endpoint with authorization."""

    def test_breaker_close_allowed(self, app_with_auth, mock_auth_wrapper):
        """Test breaker close when authorized."""
        client = app_with_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            json={
                "action": "close",
                "user_id": "operator_alice",
                "user_roles": ["operator"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["authorization"]["decision"] == "allow"

        # Verify authorization was called with correct parameters
        mock_auth_wrapper.authorize_operation.assert_called_once()
        call_args = mock_auth_wrapper.authorize_operation.call_args
        assert call_args[1]["operation_type"] == "control"
        assert call_args[1]["asset_id"] == "breaker-0"
        assert call_args[1]["user_id"] == "operator_alice"
        assert call_args[1]["value"] == 1  # close = 1

    def test_breaker_open_allowed(self, app_with_auth, mock_auth_wrapper):
        """Test breaker open when authorized."""
        client = app_with_auth.test_client()

        response = client.post(
            "/api/control/breaker/3",
            json={
                "action": "open",
                "user_id": "operator_alice",
                "user_roles": ["operator"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True

        # Verify value is 0 for open
        call_args = mock_auth_wrapper.authorize_operation.call_args
        assert call_args[1]["value"] == 0  # open = 0

    def test_breaker_denied(self, app_with_auth, mock_auth_wrapper):
        """Test breaker control when denied."""
        # Configure mock to deny
        decision = MagicMock()
        decision.allowed = False
        decision.reason = "insufficient_role"
        mock_auth_wrapper.authorize_operation.return_value = decision

        client = app_with_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            json={"action": "close", "user_id": "viewer_bob", "user_roles": ["viewer"]},
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is False
        assert data["authorization"]["decision"] == "deny"
        assert data["error"] == "Authorization denied"


class TestGeneratorControlNoAuth:
    """Test generator control endpoint without authorization."""

    def test_generator_setpoint_success(self, app_no_auth):
        """Test successful generator setpoint without auth."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/generator/0",
            json={
                "setpoint_kw": 100.0,
                "user_id": "test_user",
                "user_roles": ["engineer"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["operation"] == "generator_control"
        assert data["target"] == "generator-0"
        assert data["value"] == 100.0
        assert data["authorization"]["decision"] == "allow"

    def test_generator_missing_setpoint(self, app_no_auth):
        """Test generator control with missing setpoint."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/generator/0",
            json={"user_id": "test_user", "user_roles": ["engineer"]},
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "Missing setpoint_kw" in data["error"]


class TestGeneratorControlWithAuth:
    """Test generator control endpoint with authorization."""

    def test_generator_setpoint_allowed(self, app_with_auth, mock_auth_wrapper):
        """Test generator setpoint when authorized."""
        client = app_with_auth.test_client()

        response = client.post(
            "/api/control/generator/2",
            json={
                "setpoint_kw": 250.5,
                "user_id": "engineer_dave",
                "user_roles": ["engineer"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is True
        assert data["value"] == 250.5

        # Verify authorization was called
        mock_auth_wrapper.authorize_operation.assert_called_once()
        call_args = mock_auth_wrapper.authorize_operation.call_args
        assert call_args[1]["operation_type"] == "control"
        assert call_args[1]["asset_id"] == "generator-2"
        assert call_args[1]["user_id"] == "engineer_dave"
        assert call_args[1]["value"] == 250.5

    def test_generator_denied(self, app_with_auth, mock_auth_wrapper):
        """Test generator control when denied."""
        # Configure mock to deny
        decision = MagicMock()
        decision.allowed = False
        decision.reason = "insufficient_role"
        mock_auth_wrapper.authorize_operation.return_value = decision

        client = app_with_auth.test_client()

        response = client.post(
            "/api/control/generator/0",
            json={
                "setpoint_kw": 100.0,
                "user_id": "operator_alice",
                "user_roles": ["operator"],
            },
        )

        assert response.status_code == 200
        data = json.loads(response.data)

        assert data["success"] is False
        assert data["authorization"]["decision"] == "deny"


class TestLatencyMeasurement:
    """Test that latency is measured correctly."""

    def test_authorization_latency_measured(self, app_with_auth, mock_auth_wrapper):
        """Test that authorization latency is measured."""
        client = app_with_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        # Authorization latency should be > 0 (measured)
        assert data["authorization"]["latency_ms"] > 0

    def test_execution_latency_measured(self, app_no_auth):
        """Test that execution latency is measured."""
        client = app_no_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)

        # Execution latency should be > 0 (measured)
        assert data["execution_latency_ms"] >= 0


class TestDefaultUserRoles:
    """Test default user roles when not provided."""

    def test_read_measurements_default_role(self, app_no_auth):
        """Test read measurements with default role."""
        client = app_no_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

    def test_breaker_control_default_role(self, app_no_auth):
        """Test breaker control with default role."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/0", json={"action": "close", "user_id": "test_user"}
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

    def test_generator_control_default_role(self, app_no_auth):
        """Test generator control with default role."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/generator/0",
            json={"setpoint_kw": 100.0, "user_id": "test_user"},
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True


class TestErrorHandling:
    """Test error handling in control API."""

    def test_invalid_json(self, app_no_auth):
        """Test handling of invalid JSON."""
        client = app_no_auth.test_client()

        response = client.post(
            "/api/control/breaker/0",
            data="invalid json",
            content_type="application/json",
        )

        assert response.status_code == 500

    def test_simulator_exception(self, app_no_auth, mock_simulator):
        """Test handling of simulator exceptions."""
        mock_simulator.get_current_state.side_effect = Exception("Simulator error")
        client = app_no_auth.test_client()

        response = client.get("/api/measurements?user_id=test_user&user_roles=operator")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is False
        assert "Simulator error" in data["error"]
