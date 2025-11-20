"""
Power flow result Pydantic models.

This module defines data models for power flow calculation results and configuration.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ============================================================================
# Power Flow Algorithm Enum
# ============================================================================


class PowerFlowAlgorithm(str, Enum):
    """Power flow solution algorithm."""

    NEWTON_RAPHSON = "newton_raphson"
    GAUSS_SEIDEL = "gauss_seidel"
    FAST_DECOUPLED = "fast_decoupled"
    DC = "dc"  # DC power flow (linear approximation)


# ============================================================================
# Power Flow Configuration
# ============================================================================


class PowerFlowConfig(BaseModel):
    """Configuration for power flow calculation."""

    algorithm: PowerFlowAlgorithm = Field(
        default=PowerFlowAlgorithm.NEWTON_RAPHSON,
        description="Power flow algorithm to use",
    )

    # Convergence criteria
    tolerance: float = Field(default=1e-6, gt=0, description="Convergence tolerance")
    max_iterations: int = Field(
        default=100, gt=0, description="Maximum number of iterations"
    )

    # Options
    enforce_q_limits: bool = Field(
        default=True, description="Enforce generator reactive power limits"
    )
    distributed_slack: bool = Field(
        default=False, description="Distribute slack bus power across generators"
    )

    # Numerical options
    init_vm_pu: float = Field(
        default=1.0, gt=0, description="Initial voltage magnitude guess in p.u."
    )
    init_va_degree: float = Field(
        default=0.0, description="Initial voltage angle guess in degrees"
    )


# ============================================================================
# Power Flow Result
# ============================================================================


class PowerFlowResult(BaseModel):
    """Result of a power flow calculation."""

    # Convergence status
    converged: bool = Field(description="Power flow converged successfully")
    iterations: int = Field(ge=0, description="Number of iterations performed")

    # Error metrics
    max_bus_p_mismatch: Optional[float] = Field(
        None, description="Maximum active power mismatch in MW"
    )
    max_bus_q_mismatch: Optional[float] = Field(
        None, description="Maximum reactive power mismatch in MVAr"
    )

    # Execution time
    execution_time_ms: float = Field(ge=0, description="Execution time in milliseconds")

    # Error message if not converged
    error_message: Optional[str] = Field(
        None, description="Error message if power flow failed"
    )

    # Configuration used
    config: PowerFlowConfig = Field(description="Configuration used for this run")
