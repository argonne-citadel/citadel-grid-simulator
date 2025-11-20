"""Solar PV generation profile generation."""

import numpy as np
import pandas as pd
from typing import Optional, Literal
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class SolarProfileGenerator:
    """
    Generate realistic solar PV generation profiles.

    Profiles include:
    - Solar irradiance curves (sunrise to sunset)
    - Seasonal sun angle variations
    - Cloud cover effects
    - Inverter efficiency
    """

    def __init__(self, random_seed: Optional[int] = None):
        """
        Initialize the solar profile generator.

        Args:
            random_seed: Random seed for reproducibility
        """
        self.rng = np.random.default_rng(random_seed)
        logger.info("SolarProfileGenerator initialized")

    def generate_solar_profile(
        self,
        num_days: int = 1,
        timestep_minutes: int = 60,
        rated_capacity_kw: float = 10.0,
        latitude: float = 40.0,
        season: Literal["summer", "winter", "spring", "fall"] = "summer",
        cloud_cover: Literal["clear", "partly_cloudy", "cloudy"] = "clear",
        inverter_efficiency: float = 0.96,
    ) -> pd.DataFrame:
        """
        Generate solar PV generation profile.

        Args:
            num_days: Number of days to generate
            timestep_minutes: Timestep in minutes
            rated_capacity_kw: Rated PV capacity in kW
            latitude: Latitude for sun angle calculation
            season: Season (affects day length and sun angle)
            cloud_cover: Cloud cover condition
            inverter_efficiency: Inverter efficiency (0-1)

        Returns:
            DataFrame with timestamp and generation_kw columns
        """
        num_steps = int(num_days * 24 * 60 / timestep_minutes)
        timestamps = pd.date_range(
            start=datetime.now(), periods=num_steps, freq=f"{timestep_minutes}min"
        )

        # Seasonal parameters
        seasonal_params = {
            "summer": {"sunrise": 5, "sunset": 20, "peak_factor": 1.0},
            "winter": {"sunrise": 7, "sunset": 17, "peak_factor": 0.6},
            "spring": {"sunrise": 6, "sunset": 19, "peak_factor": 0.85},
            "fall": {"sunrise": 6, "sunset": 18, "peak_factor": 0.75},
        }
        params = seasonal_params[season]

        # Cloud cover factors
        cloud_factors = {"clear": 1.0, "partly_cloudy": 0.7, "cloudy": 0.3}
        cloud_factor = cloud_factors[cloud_cover]

        generation_profile = []

        for ts in timestamps:
            hour = ts.hour + ts.minute / 60.0

            # Solar irradiance curve (simplified cosine model)
            if params["sunrise"] <= hour <= params["sunset"]:
                # Normalized hour (0 at sunrise, 1 at sunset)
                day_length = params["sunset"] - params["sunrise"]
                norm_hour = (hour - params["sunrise"]) / day_length

                # Cosine curve for solar irradiance
                # Peak at solar noon (middle of day)
                irradiance_factor = np.cos((norm_hour - 0.5) * np.pi)
                irradiance_factor = max(0, irradiance_factor)

                # Apply seasonal peak factor
                irradiance_factor *= params["peak_factor"]

                # Apply cloud cover
                if cloud_cover == "partly_cloudy":
                    # Add stochastic cloud variations
                    cloud_variation = self.rng.uniform(0.5, 1.0)
                    irradiance_factor *= cloud_variation
                else:
                    irradiance_factor *= cloud_factor

                # Calculate generation
                generation = rated_capacity_kw * irradiance_factor * inverter_efficiency

                # Add small measurement noise
                noise = self.rng.normal(0, 0.02 * generation)
                generation = max(0, generation + noise)
            else:
                # No generation at night
                generation = 0.0

            generation_profile.append(generation)

        return pd.DataFrame(
            {"timestamp": timestamps, "generation_kw": generation_profile}
        )

    def generate_multiple_solar(
        self,
        num_systems: int = 3,
        num_days: int = 1,
        timestep_minutes: int = 60,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Generate multiple solar PV profiles.

        Args:
            num_systems: Number of PV systems
            num_days: Number of days
            timestep_minutes: Timestep in minutes
            **kwargs: Additional arguments for profile generation

        Returns:
            DataFrame with timestamp and generation columns for each system
        """
        profiles = {}

        for i in range(num_systems):
            profile = self.generate_solar_profile(
                num_days=num_days,
                timestep_minutes=timestep_minutes,
                rated_capacity_kw=self.rng.uniform(5.0, 15.0),
                **kwargs,
            )
            profiles[f"solar_{i}"] = profile["generation_kw"].values

        # Combine into single DataFrame
        df = pd.DataFrame(profiles)
        df.insert(0, "timestamp", profile["timestamp"].values)

        logger.info(f"Generated {num_systems} solar PV profiles")

        return df

    def generate_with_variability(
        self,
        num_days: int = 1,
        timestep_minutes: int = 60,
        rated_capacity_kw: float = 10.0,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Generate solar profile with realistic day-to-day variability.

        Each day has different cloud conditions to simulate realistic weather.

        Args:
            num_days: Number of days to generate
            timestep_minutes: Timestep in minutes
            rated_capacity_kw: Rated capacity in kW
            **kwargs: Additional arguments

        Returns:
            DataFrame with variable solar generation
        """
        all_profiles = []

        cloud_conditions = ["clear", "partly_cloudy", "cloudy"]

        for day in range(num_days):
            # Random cloud condition for each day
            cloud_cover = self.rng.choice(cloud_conditions, p=[0.4, 0.4, 0.2])

            daily_profile = self.generate_solar_profile(
                num_days=1,
                timestep_minutes=timestep_minutes,
                rated_capacity_kw=rated_capacity_kw,
                cloud_cover=cloud_cover,
                **kwargs,
            )

            all_profiles.append(daily_profile)

        # Concatenate all days
        combined = pd.concat(all_profiles, ignore_index=True)

        logger.info(f"Generated {num_days} days of variable solar profiles")

        return combined

    def to_pandapower_format(
        self, profile_df: pd.DataFrame, sgen_indices: list
    ) -> pd.DataFrame:
        """
        Convert solar profiles to pandapower time-series format.

        Args:
            profile_df: DataFrame with solar profiles
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
