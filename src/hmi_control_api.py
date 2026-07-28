"""
HMI/SCADA Control API with Zero-Trust Authorization.

This module extends the visualization API with control operations that
integrate with the Zero-Trust authorization system via Envoy proxy.

Control Operations:
- Read measurements (voltages, currents, power)
- Control breakers (open/close)
- Set generator setpoints
- Set load setpoints

Authorization Flow:
- All control requests go through Envoy proxy
- Envoy calls OPA for authorization decision
- OPA queries Neo4j for context
- Decision returned with latency measurement
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)


@dataclass
class ControlResult:
    """Result of a control operation."""

    success: bool
    operation: str
    target: str
    value: Any
    authorization_latency_ms: float
    authorization_decision: str
    authorization_reason: Optional[str]
    execution_latency_ms: float
    error: Optional[str] = None


class HMIControlAPI:
    """
    HMI/SCADA control API with authorization integration.

    This provides REST endpoints for grid control operations that
    integrate with the Zero-Trust authorization system.
    """

    def __init__(self, simulator, authorization_wrapper=None):
        """
        Initialize control API.

        Args:
            simulator: GridSimulator instance
            authorization_wrapper: Optional AuthorizationWrapper for Zero-Trust
        """
        self.simulator = simulator
        self.auth_wrapper = authorization_wrapper

        # Create Flask blueprint
        self.blueprint = Blueprint("control", __name__)
        self._register_routes()

        logger.info("HMIControlAPI initialized")

    def _register_routes(self):
        """Register Flask routes."""

        @self.blueprint.route("/api/control/breaker/<int:breaker_id>", methods=["POST"])
        def control_breaker(breaker_id):
            """
            Control a breaker (open/close).

            Request body:
            {
                "action": "open" or "close",
                "user_id": "operator_alice",
                "user_roles": ["operator"]
            }
            """
            try:
                data = request.get_json()
                action = data.get("action")
                user_id = data.get("user_id", "unknown")
                user_roles = data.get("user_roles", ["operator"])

                if action not in ["open", "close"]:
                    return (
                        jsonify({"error": "Invalid action, must be 'open' or 'close'"}),
                        400,
                    )

                # Execute control operation with authorization
                result = self._execute_breaker_control(
                    breaker_id=breaker_id,
                    action=action,
                    user_id=user_id,
                    user_roles=user_roles,
                )

                return jsonify(
                    {
                        "success": result.success,
                        "operation": result.operation,
                        "target": result.target,
                        "value": result.value,
                        "authorization": {
                            "latency_ms": result.authorization_latency_ms,
                            "decision": result.authorization_decision,
                            "reason": result.authorization_reason,
                        },
                        "execution_latency_ms": result.execution_latency_ms,
                        "error": result.error,
                    }
                )

            except Exception as e:
                logger.error(f"Error in breaker control: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        @self.blueprint.route("/api/control/generator/<int:gen_id>", methods=["POST"])
        def control_generator(gen_id):
            """
            Set generator power setpoint.

            Request body:
            {
                "setpoint_kw": 100.0,
                "user_id": "engineer_dave",
                "user_roles": ["engineer"]
            }
            """
            try:
                data = request.get_json()
                setpoint_kw = data.get("setpoint_kw")
                user_id = data.get("user_id", "unknown")
                user_roles = data.get("user_roles", ["engineer"])

                if setpoint_kw is None:
                    return jsonify({"error": "Missing setpoint_kw"}), 400

                # Execute control operation with authorization
                result = self._execute_generator_control(
                    gen_id=gen_id,
                    setpoint_kw=setpoint_kw,
                    user_id=user_id,
                    user_roles=user_roles,
                )

                return jsonify(
                    {
                        "success": result.success,
                        "operation": result.operation,
                        "target": result.target,
                        "value": result.value,
                        "authorization": {
                            "latency_ms": result.authorization_latency_ms,
                            "decision": result.authorization_decision,
                            "reason": result.authorization_reason,
                        },
                        "execution_latency_ms": result.execution_latency_ms,
                        "error": result.error,
                    }
                )

            except Exception as e:
                logger.error(f"Error in generator control: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

        @self.blueprint.route("/api/measurements", methods=["GET"])
        def get_measurements():
            """
            Get all grid measurements.

            Query parameters:
            - user_id: User requesting measurements
            - user_roles: Comma-separated list of roles
            """
            try:
                user_id = request.args.get("user_id", "unknown")
                user_roles_str = request.args.get("user_roles", "operator")
                user_roles = [r.strip() for r in user_roles_str.split(",")]

                # Execute read operation with authorization
                result = self._execute_read_measurements(
                    user_id=user_id,
                    user_roles=user_roles,
                )

                return jsonify(
                    {
                        "success": result.success,
                        "measurements": result.value if result.success else None,
                        "authorization": {
                            "latency_ms": result.authorization_latency_ms,
                            "decision": result.authorization_decision,
                            "reason": result.authorization_reason,
                        },
                        "execution_latency_ms": result.execution_latency_ms,
                        "error": result.error,
                    }
                )

            except Exception as e:
                logger.error(f"Error reading measurements: {e}", exc_info=True)
                return jsonify({"error": str(e)}), 500

    def _execute_breaker_control(
        self,
        breaker_id: int,
        action: str,
        user_id: str,
        user_roles: list[str],
    ) -> ControlResult:
        """
        Execute breaker control with authorization.

        Args:
            breaker_id: Breaker ID
            action: "open" or "close"
            user_id: User performing operation
            user_roles: User's roles

        Returns:
            ControlResult with authorization and execution details
        """
        auth_start = time.perf_counter()

        # Authorize operation
        if self.auth_wrapper:
            try:
                decision = self.auth_wrapper.authorize_operation(
                    operation_type="control",
                    asset_id=f"breaker-{breaker_id}",
                    point_index=breaker_id,
                    user_id=user_id,
                    user_roles=user_roles,
                    value=1 if action == "close" else 0,
                )

                auth_latency_ms = (time.perf_counter() - auth_start) * 1000

                if not decision.allowed:
                    return ControlResult(
                        success=False,
                        operation="breaker_control",
                        target=f"breaker-{breaker_id}",
                        value=action,
                        authorization_latency_ms=auth_latency_ms,
                        authorization_decision="deny",
                        authorization_reason=decision.reason,
                        execution_latency_ms=0.0,
                        error="Authorization denied",
                    )

                auth_decision = "allow"
                auth_reason = decision.reason

            except Exception as e:
                auth_latency_ms = (time.perf_counter() - auth_start) * 1000
                logger.error(f"Authorization error: {e}")
                return ControlResult(
                    success=False,
                    operation="breaker_control",
                    target=f"breaker-{breaker_id}",
                    value=action,
                    authorization_latency_ms=auth_latency_ms,
                    authorization_decision="error",
                    authorization_reason=str(e),
                    execution_latency_ms=0.0,
                    error=f"Authorization error: {e}",
                )
        else:
            # No authorization wrapper - allow all (for testing)
            auth_latency_ms = 0.0
            auth_decision = "allow"
            auth_reason = "no_authorization_configured"

        # Execute operation
        exec_start = time.perf_counter()

        try:
            # TODO: Implement actual breaker control via grid simulator
            # For now, just simulate success
            logger.info(f"Breaker {breaker_id} {action} by {user_id}")

            exec_latency_ms = (time.perf_counter() - exec_start) * 1000

            return ControlResult(
                success=True,
                operation="breaker_control",
                target=f"breaker-{breaker_id}",
                value=action,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
            )

        except Exception as e:
            exec_latency_ms = (time.perf_counter() - exec_start) * 1000
            logger.error(f"Execution error: {e}")
            return ControlResult(
                success=False,
                operation="breaker_control",
                target=f"breaker-{breaker_id}",
                value=action,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
                error=f"Execution error: {e}",
            )

    def _execute_generator_control(
        self,
        gen_id: int,
        setpoint_kw: float,
        user_id: str,
        user_roles: list[str],
    ) -> ControlResult:
        """Execute generator control with authorization."""
        auth_start = time.perf_counter()

        # Authorize operation
        if self.auth_wrapper:
            try:
                decision = self.auth_wrapper.authorize_operation(
                    operation_type="control",
                    asset_id=f"generator-{gen_id}",
                    point_index=gen_id,
                    user_id=user_id,
                    user_roles=user_roles,
                    value=setpoint_kw,
                )

                auth_latency_ms = (time.perf_counter() - auth_start) * 1000

                if not decision.allowed:
                    return ControlResult(
                        success=False,
                        operation="generator_control",
                        target=f"generator-{gen_id}",
                        value=setpoint_kw,
                        authorization_latency_ms=auth_latency_ms,
                        authorization_decision="deny",
                        authorization_reason=decision.reason,
                        execution_latency_ms=0.0,
                        error="Authorization denied",
                    )

                auth_decision = "allow"
                auth_reason = decision.reason

            except Exception as e:
                # API boundary: log full exception for diagnosis but return an
                # error ControlResult so the HTTP handler doesn't crash mid-request.
                logger.exception(
                    "Authorization check failed for generator_control gen_id=%s",
                    gen_id,
                )
                auth_latency_ms = (time.perf_counter() - auth_start) * 1000
                return ControlResult(
                    success=False,
                    operation="generator_control",
                    target=f"generator-{gen_id}",
                    value=setpoint_kw,
                    authorization_latency_ms=auth_latency_ms,
                    authorization_decision="error",
                    authorization_reason=str(e),
                    execution_latency_ms=0.0,
                    error=f"Authorization error: {e}",
                )
        else:
            auth_latency_ms = 0.0
            auth_decision = "allow"
            auth_reason = "no_authorization_configured"

        # Execute operation
        exec_start = time.perf_counter()

        try:
            # TODO: Implement actual generator control
            logger.info(f"Generator {gen_id} setpoint {setpoint_kw} kW by {user_id}")

            exec_latency_ms = (time.perf_counter() - exec_start) * 1000

            return ControlResult(
                success=True,
                operation="generator_control",
                target=f"generator-{gen_id}",
                value=setpoint_kw,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
            )

        except Exception as e:
            # API boundary: log full exception for diagnosis but return an
            # error ControlResult so the HTTP handler doesn't crash mid-request.
            logger.exception("Execution failed for generator_control gen_id=%s", gen_id)
            exec_latency_ms = (time.perf_counter() - exec_start) * 1000
            return ControlResult(
                success=False,
                operation="generator_control",
                target=f"generator-{gen_id}",
                value=setpoint_kw,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
                error=f"Execution error: {e}",
            )

    def _execute_read_measurements(
        self,
        user_id: str,
        user_roles: list[str],
    ) -> ControlResult:
        """Execute read measurements with authorization."""
        auth_start = time.perf_counter()

        # Authorize operation
        if self.auth_wrapper:
            try:
                decision = self.auth_wrapper.authorize_operation(
                    operation_type="read",
                    asset_id="grid",
                    point_index=0,
                    user_id=user_id,
                    user_roles=user_roles,
                )

                auth_latency_ms = (time.perf_counter() - auth_start) * 1000

                if not decision.allowed:
                    return ControlResult(
                        success=False,
                        operation="read_measurements",
                        target="grid",
                        value=None,
                        authorization_latency_ms=auth_latency_ms,
                        authorization_decision="deny",
                        authorization_reason=decision.reason,
                        execution_latency_ms=0.0,
                        error="Authorization denied",
                    )

                auth_decision = "allow"
                auth_reason = decision.reason

            except Exception as e:
                # API boundary: log full exception but return an error
                # ControlResult so the HTTP handler doesn't crash mid-request.
                logger.exception("Authorization check failed for read_measurements")
                auth_latency_ms = (time.perf_counter() - auth_start) * 1000
                return ControlResult(
                    success=False,
                    operation="read_measurements",
                    target="grid",
                    value=None,
                    authorization_latency_ms=auth_latency_ms,
                    authorization_decision="error",
                    authorization_reason=str(e),
                    execution_latency_ms=0.0,
                    error=f"Authorization error: {e}",
                )
        else:
            auth_latency_ms = 0.0
            auth_decision = "allow"
            auth_reason = "no_authorization_configured"

        # Execute operation
        exec_start = time.perf_counter()

        try:
            # Get current state from simulator
            state = self.simulator.get_current_state()

            if state is None:
                raise Exception("No grid state available")

            # Extract measurements
            measurements = {
                "buses": {},
                "lines": {},
                "timestamp": self.simulator.current_time,
            }

            # Bus voltages
            for bus_id, bus_state in state.buses.items():
                measurements["buses"][bus_id] = {
                    "voltage_pu": bus_state.voltage_pu,
                    "voltage_kv": bus_state.voltage_kv,
                }

            # Line loadings
            for line_id, line_state in state.lines.items():
                measurements["lines"][line_id] = {
                    "loading_percent": line_state.loading_percent,
                    "current_a": line_state.current_a,
                }

            exec_latency_ms = (time.perf_counter() - exec_start) * 1000

            return ControlResult(
                success=True,
                operation="read_measurements",
                target="grid",
                value=measurements,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
            )

        except Exception as e:
            # API boundary: log full exception but return an error
            # ControlResult so the HTTP handler doesn't crash mid-request.
            logger.exception("Execution failed for read_measurements")
            exec_latency_ms = (time.perf_counter() - exec_start) * 1000
            return ControlResult(
                success=False,
                operation="read_measurements",
                target="grid",
                value=None,
                authorization_latency_ms=auth_latency_ms,
                authorization_decision=auth_decision,
                authorization_reason=auth_reason,
                execution_latency_ms=exec_latency_ms,
                error=f"Execution error: {e}",
            )
