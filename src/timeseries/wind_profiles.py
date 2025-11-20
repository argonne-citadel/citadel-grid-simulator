"""Wind generation profile generation."""

import numpy as np
import pandas as pd
from typing import Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class WindProfileGenerator:
    """
    Generate realistic wind generation profiles.

    Profiles include:
    - Wind speed to power conversion
    - Turbine cut-in/cut-out speeds
    - Stochastic wind variations
    - Seasonal patterns
    """

    def __init__(self, random_seed: Optional[int] = None):
        """
        Initialize the wind profile generator.

        Args:
            random_seed: Random seed for reproducibility
        """
        self.rng = np.random.default_rng(random_seed)
        logger.info("WindProfileGenerator initialized")

    def wind_power_curve(
        self,
        wind_speed: float,
        rated_capacity_kw: float,
        cut_in_speed: float = 3.0,
        rated_speed: float = 12.0,
        cut_out_speed: float = 25.0,
    ) -> float:
        """
        Convert wind speed to power output using simplified power curve.

        Args:
            wind_speed: Wind speed in m/s
            rated_capacity_kw: Rated turbine capacity in kW
            cut_in_speed: Minimum wind speed for generation (m/s)
            rated_speed: Wind speed at rated power (m/s)
            cut_out_speed: Maximum wind speed (turbine shuts down) (m/s)

        Returns:
            Power output in kW
        """
        if wind_speed < cut_in_speed or wind_speed > cut_out_speed:
            return 0.0
        elif wind_speed >= rated_speed:
            return rated_capacity_kw
        else:
            # Cubic relationship between cut-in and rated speed
            normalized_speed = (wind_speed - cut_in_speed) / (
                rated_speed - cut_in_speed
            )
            return rated_capacity_kw * (normalized_speed**3)

    def generate_wind_profile(
        self,
        num_days: int = 1,
        timestep_minutes: int = 60,
        rated_capacity_kw: float = 100.0,
        mean_wind_speed: float = 8.0,
        wind_variability: float = 3.0,
        cut_in_speed: float = 3.0,
        rated_speed: float = 12.0,
        cut_out_speed: float = 25.0,
    ) -> pd.DataFrame:
        """
        Generate wind generation profile.

        Args:
            num_days: Number of days to generate
            timestep_minutes: Timestep in minutes
            rated_capacity_kw: Rated turbine capacity in kW
            mean_wind_speed: Mean wind speed in m/s
            wind_variability: Standard deviation of wind speed (m/s)
            cut_in_speed: Cut-in wind speed (m/s)
            rated_speed: Rated wind speed (m/s)
            cut_out_speed: Cut-out wind speed (m/s)

        Returns:
            DataFrame with timestamp, wind_speed_ms, and generation_kw columns
        """
        num_steps = int(num_days * 24 * 60 / timestep_minutes)
        timestamps = pd.date_range(
            start=datetime.now(), periods=num_steps, freq=f"{timestep_minutes}min"
        )

        # Generate correlated wind speeds using AR(1) process
        # This creates more realistic temporal correlation
        wind_speeds = []
        current_wind = mean_wind_speed

        # AR(1) parameters
        phi = 0.9  # Autocorrelation coefficient

        for _ in range(num_steps):
            # AR(1) process: w(t) = phi * w(t-1) + (1-phi) * mean + noise
            noise = self.rng.normal(0, wind_variability)
            current_wind = phi * current_wind + (1 - phi) * mean_wind_speed + noise

            # Ensure non-negative wind speed
            current_wind = max(0, current_wind)

            wind_speeds.append(current_wind)

        # Convert wind speeds to power
        generation_profile = [
            self.wind_power_curve(
                ws, rated_capacity_kw, cut_in_speed, rated_speed, cut_out_speed
            )
            for ws in wind_speeds
        ]

        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "wind_speed_ms": wind_speeds,
                "generation_kw": generation_profile,
            }
        )

    def generate_multiple_wind(
        self,
        num_turbines: int = 2,
        num_days: int = 1,
        timestep_minutes: int = 60,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Generate multiple wind turbine profiles.

        Args:
            num_turbines: Number of wind turbines
            num_days: Number of days
            timestep_minutes: Timestep in minutes
            **kwargs: Additional arguments for profile generation

        Returns:
            DataFrame with timestamp and generation columns for each turbine
        """
        profiles = {}

        for i in range(num_turbines):
            profile = self.generate_wind_profile(
                num_days=num_days,
                timestep_minutes=timestep_minutes,
                rated_capacity_kw=self.rng.uniform(50.0, 150.0),
                mean_wind_speed=self.rng.uniform(6.0, 10.0),
                **kwargs,
            )
            profiles[f"wind_{i}"] = profile["generation_kw"].values

        # Combine into single DataFrame
        df = pd.DataFrame(profiles)
        df.insert(0, "timestamp", profile["timestamp"].values)

        logger.info(f"Generated {num_turbines} wind turbine profiles")

        return df

    def to_pandapower_format(
        self, profile_df: pd.DataFrame, sgen_indices: list
    ) -> pd.DataFrame:
        """
        Convert wind profiles to pandapower time-series format.

        Args:
            profile_df: DataFrame with wind profiles
            sgen_indices: List of static generator indices in pandapower network

        Returns:
            DataFrame formatted for pandapower time-series
        """
        # Map profile columns to sgen indices
        profile_cols = [col for col in profile_df.columns if col != "timestamp"]

        if len(profile_cols) != len(sgen_indices):
            logger.warning(
                f"Profile count ({len(profile_cols)}) != sgen count ({len(sgen_indices)})"
            )

        # Create mapping
        pp_format = {}
        for i, sgen_idx in enumerate(sgen_indices):
            if i < len(profile_cols):
                # Convert kW to MW (negative for generation)
                pp_format[f"sgen_{sgen_idx}"] = (
                    -profile_df[profile_cols[i]].values / 1000.0
                )

        return pd.DataFrame(pp_format)
