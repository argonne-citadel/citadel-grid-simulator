"""
Prometheus metrics for grid simulator authorization.

This module provides Prometheus metrics collection for Zero-Trust authorization
performance measurement, including latency histograms, decision counters, and
error tracking.
"""

import time
from typing import Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

# Authorization decision counters
authorization_decisions_total = Counter(
    "grid_authorization_decisions_total",
    "Total number of authorization decisions",
    ["decision", "operation_type", "asset_type", "layer"],
)

# Authorization latency histogram (in milliseconds)
authorization_latency_ms = Histogram(
    "grid_authorization_latency_milliseconds",
    "Authorization decision latency in milliseconds",
    ["layer", "operation_type"],
    buckets=[
        0.1,
        0.5,
        1.0,
        5.0,
        10.0,
        25.0,
        50.0,
        100.0,
        250.0,
        500.0,
        1000.0,
        2500.0,
        5000.0,
    ],
)

# Edge Wasm latency histogram (in microseconds for higher precision)
edge_wasm_latency_us = Histogram(
    "grid_authorization_edge_wasm_latency_microseconds",
    "Edge Wasm authorization latency in microseconds",
    ["operation_type"],
    buckets=[1, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 1000],
)

# Central OPA latency histogram (in milliseconds)
central_opa_latency_ms = Histogram(
    "grid_authorization_central_opa_latency_milliseconds",
    "Central OPA authorization latency in milliseconds",
    ["operation_type"],
    buckets=[1, 5, 10, 25, 50, 75, 100, 150, 200, 300, 500, 1000, 2000],
)

# Neo4j query latency histogram (in milliseconds)
neo4j_query_latency_ms = Histogram(
    "grid_authorization_neo4j_query_latency_milliseconds",
    "Neo4j context query latency in milliseconds",
    ["query_type"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

# Authorization errors counter
authorization_errors_total = Counter(
    "grid_authorization_errors_total",
    "Total number of authorization errors",
    ["error_type", "operation_type"],
)

# Current authorization statistics gauges
authorization_allow_rate = Gauge(
    "grid_authorization_allow_rate",
    "Current authorization allow rate (0.0-1.0)",
)

authorization_deny_rate = Gauge(
    "grid_authorization_deny_rate",
    "Current authorization deny rate (0.0-1.0)",
)

authorization_error_rate = Gauge(
    "grid_authorization_error_rate",
    "Current authorization error rate (0.0-1.0)",
)

# Grid operation metrics
grid_operations_total = Counter(
    "grid_operations_total",
    "Total number of grid operations",
    ["operation_type", "protocol", "authorized"],
)

grid_operation_latency_ms = Histogram(
    "grid_operation_latency_milliseconds",
    "End-to-end grid operation latency including authorization",
    ["operation_type", "protocol"],
    buckets=[0.1, 0.5, 1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0],
)

# Modbus-specific metrics
modbus_requests_total = Counter(
    "grid_modbus_requests_total",
    "Total number of Modbus requests",
    ["function_code", "authorized"],
)

modbus_write_operations_total = Counter(
    "grid_modbus_write_operations_total",
    "Total number of Modbus write operations",
    ["register_type", "authorized"],
)

# Attack scenario metrics
attack_detections_total = Counter(
    "grid_attack_detections_total",
    "Total number of attack detections",
    ["attack_type", "detection_method"],
)

false_positives_total = Counter(
    "grid_authorization_false_positives_total",
    "Total number of false positive authorization denials",
    ["operation_type"],
)

# System health metrics
authorization_service_up = Gauge(
    "grid_authorization_service_up",
    "Authorization service availability (1=up, 0=down)",
    ["service"],
)

authorization_timeout_total = Counter(
    "grid_authorization_timeouts_total",
    "Total number of authorization timeouts",
    ["service", "failure_mode"],
)

# Info metric for system configuration
authorization_config_info = Info(
    "grid_authorization_config",
    "Authorization system configuration",
)


class AuthorizationMetrics:
    """
    Helper class for recording authorization metrics.

    This class provides convenient methods for recording various authorization
    metrics and automatically updates derived metrics like rates.
    """

    def __init__(self):
        """Initialize metrics tracker."""
        self.total_requests = 0
        self.allowed_requests = 0
        self.denied_requests = 0
        self.error_requests = 0

        # Set initial configuration
        authorization_config_info.info(
            {
                "version": "1.0.0",
                "dual_layer_enabled": "false",
                "fail_open_on_timeout": "false",
            }
        )

    def record_decision(
        self,
        decision: str,
        operation_type: str,
        asset_type: Optional[str] = None,
        layer: str = "central",
        latency_ms: float = 0.0,
        edge_latency_us: Optional[float] = None,
        central_latency_ms: Optional[float] = None,
    ) -> None:
        """
        Record an authorization decision with metrics.

        Args:
            decision: Decision result ("allow", "deny", "error")
            operation_type: Type of operation ("read", "control", etc.)
            asset_type: Type of asset being accessed
            layer: Authorization layer ("edge", "central", "dual", "error")
            latency_ms: Total authorization latency in milliseconds
            edge_latency_us: Edge Wasm latency in microseconds (if applicable)
            central_latency_ms: Central OPA latency in milliseconds (if applicable)
        """
        # Update counters
        self.total_requests += 1
        if decision == "allow":
            self.allowed_requests += 1
        elif decision == "deny":
            self.denied_requests += 1
        elif decision == "error":
            self.error_requests += 1

        # Record decision counter
        authorization_decisions_total.labels(
            decision=decision,
            operation_type=operation_type,
            asset_type=asset_type or "unknown",
            layer=layer,
        ).inc()

        # Record latency histograms
        if latency_ms > 0:
            authorization_latency_ms.labels(
                layer=layer, operation_type=operation_type
            ).observe(latency_ms)

        if edge_latency_us is not None and edge_latency_us > 0:
            edge_wasm_latency_us.labels(operation_type=operation_type).observe(
                edge_latency_us
            )

        if central_latency_ms is not None and central_latency_ms > 0:
            central_opa_latency_ms.labels(operation_type=operation_type).observe(
                central_latency_ms
            )

        # Update rate gauges
        self._update_rates()

    def record_error(self, error_type: str, operation_type: str) -> None:
        """
        Record an authorization error.

        Args:
            error_type: Type of error ("timeout", "connection", "policy", etc.)
            operation_type: Type of operation that failed
        """
        authorization_errors_total.labels(
            error_type=error_type, operation_type=operation_type
        ).inc()

    def record_timeout(self, service: str, failure_mode: str = "fail_closed") -> None:
        """
        Record an authorization timeout.

        Args:
            service: Service that timed out ("opa", "neo4j", "envoy")
            failure_mode: How the timeout was handled ("fail_open", "fail_closed")
        """
        authorization_timeout_total.labels(
            service=service, failure_mode=failure_mode
        ).inc()

    def record_grid_operation(
        self,
        operation_type: str,
        protocol: str,
        authorized: bool,
        latency_ms: float = 0.0,
    ) -> None:
        """
        Record a grid operation.

        Args:
            operation_type: Type of operation ("read", "control", etc.)
            protocol: Protocol used ("modbus", "dnp3", "http")
            authorized: Whether operation was authorized
            latency_ms: End-to-end operation latency in milliseconds
        """
        grid_operations_total.labels(
            operation_type=operation_type,
            protocol=protocol,
            authorized="true" if authorized else "false",
        ).inc()

        if latency_ms > 0:
            grid_operation_latency_ms.labels(
                operation_type=operation_type, protocol=protocol
            ).observe(latency_ms)

    def record_modbus_request(
        self, function_code: int, authorized: bool, is_write: bool = False
    ) -> None:
        """
        Record a Modbus request.

        Args:
            function_code: Modbus function code
            authorized: Whether request was authorized
            is_write: Whether this is a write operation
        """
        modbus_requests_total.labels(
            function_code=str(function_code),
            authorized="true" if authorized else "false",
        ).inc()

        if is_write:
            # Determine register type from function code
            register_type = "coil" if function_code in [5, 15] else "holding_register"
            modbus_write_operations_total.labels(
                register_type=register_type,
                authorized="true" if authorized else "false",
            ).inc()

    def record_attack_detection(self, attack_type: str, detection_method: str) -> None:
        """
        Record an attack detection.

        Args:
            attack_type: Type of attack detected
            detection_method: How attack was detected
        """
        attack_detections_total.labels(
            attack_type=attack_type, detection_method=detection_method
        ).inc()

    def record_neo4j_query(self, query_type: str, latency_ms: float) -> None:
        """
        Record a Neo4j query.

        Args:
            query_type: Type of query ("asset_lookup", "similarity", "zone", etc.)
            latency_ms: Query latency in milliseconds
        """
        neo4j_query_latency_ms.labels(query_type=query_type).observe(latency_ms)

    def set_service_status(self, service: str, is_up: bool) -> None:
        """
        Set authorization service status.

        Args:
            service: Service name ("opa", "neo4j", "envoy")
            is_up: Whether service is up
        """
        authorization_service_up.labels(service=service).set(1 if is_up else 0)

    def update_config(self, config: dict) -> None:
        """
        Update configuration info metric.

        Args:
            config: Configuration dictionary
        """
        authorization_config_info.info({k: str(v) for k, v in config.items()})

    def _update_rates(self) -> None:
        """Update rate gauges based on current counters."""
        if self.total_requests > 0:
            authorization_allow_rate.set(self.allowed_requests / self.total_requests)
            authorization_deny_rate.set(self.denied_requests / self.total_requests)
            authorization_error_rate.set(self.error_requests / self.total_requests)
        else:
            authorization_allow_rate.set(0.0)
            authorization_deny_rate.set(0.0)
            authorization_error_rate.set(0.0)

    def get_statistics(self) -> dict:
        """
        Get current statistics.

        Returns:
            Dictionary with current statistics
        """
        return {
            "total_requests": self.total_requests,
            "allowed_requests": self.allowed_requests,
            "denied_requests": self.denied_requests,
            "error_requests": self.error_requests,
            "allow_rate": (
                self.allowed_requests / self.total_requests
                if self.total_requests > 0
                else 0.0
            ),
            "deny_rate": (
                self.denied_requests / self.total_requests
                if self.total_requests > 0
                else 0.0
            ),
            "error_rate": (
                self.error_requests / self.total_requests
                if self.total_requests > 0
                else 0.0
            ),
        }


# Global metrics instance
_metrics_instance: Optional[AuthorizationMetrics] = None


def get_metrics() -> AuthorizationMetrics:
    """
    Get global metrics instance (singleton).

    Returns:
        AuthorizationMetrics instance
    """
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = AuthorizationMetrics()
    return _metrics_instance


def get_prometheus_metrics() -> tuple[bytes, str]:
    """
    Get Prometheus metrics in text format.

    Returns:
        Tuple of (metrics_bytes, content_type)
    """
    return generate_latest(), CONTENT_TYPE_LATEST
