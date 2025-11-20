"""Load profile generation for residential and commercial loads."""

import numpy as np
import pandas as pd
from typing import Optional, Literal
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class LoadProfileGenerator:
    """
    Generate realistic load profiles for residential and commercial customers.

    Profiles include:
    - Daily patterns (peak/off-peak)
    - Seasonal variations
    - Stochastic noise
    - Weekday/weekend differences
    """

    def __init__(self, random_seed: Optional[int] = None):
        """
        Initialize the load profile generator.

        Args:
            random_seed: Random seed for reproducibility
        """
        self.rng = np.random.default_rng(random_seed)
        logger.info("LoadProfileGenerator initialized")

    def generate_residential_profile(
        self,
        num_days: int = 1,
        timestep_minutes: int = 60,
        base_load_kw: float = 2.0,
        peak_load_kw: float = 5.0,
        season: Literal["summer", "winter", "spring", "fall"] = "summer",
        noise_level: float = 0.15,
    ) -> pd.DataFrame:
        """
        Generate residential load profile.

        Typical residential pattern:
        - Morning peak: 6-9 AM
        - Evening peak: 5-10 PM
        - Overnight minimum: 11 PM - 5 AM

        Args:
            num_days: Number of days to generate
            timestep_minutes: Timestep in minutes (default: 60)
            base_load_kw: Base load in kW (overnight)
            peak_load_kw: Peak load in kW
            season: Season for temperature-dependent loads
            noise_level: Stochastic noise level (0-1)

        Returns:
            DataFrame with timestamp and load_kw columns
        """
        num_steps = int(num_days * 24 * 60 / timestep_minutes)
        timestamps = pd.date_range(
            start=datetime.now(), periods=num_steps, freq=f"{timestep_minutes}min"
        )

        load_profile = []

        for ts in timestamps:
            hour = ts.hour
            is_weekend = ts.weekday() >= 5

            # Base daily pattern
            if 0 <= hour < 6:  # Overnight
                base = base_load_kw
            elif 6 <= hour < 9:  # Morning peak
                base = base_load_kw + (peak_load_kw - base_load_kw) * 0.7
            elif 9 <= hour < 17:  # Daytime
                base = base_load_kw + (peak_load_kw - base_load_kw) * 0.4
            elif 17 <= hour < 22:  # Evening peak
                base = peak_load_kw
            else:  # Late evening
                base = base_load_kw + (peak_load_kw - base_load_kw) * 0.5

            # Weekend adjustment (flatter profile)
            if is_weekend:
                base = base * 0.9 + base_load_kw * 0.1

            # Seasonal adjustment
            seasonal_factor = {
                "summer": 1.2,  # AC load
                "winter": 1.15,  # Heating load
                "spring": 0.95,
                "fall": 0.95,
            }[season]

            base *= seasonal_factor

            # Add stochastic noise
            noise = self.rng.normal(0, noise_level * base)
            load = max(0.1, base + noise)  # Ensure positive

            load_profile.append(load)

        return pd.DataFrame({"timestamp": timestamps, "load_kw": load_profile})

    def generate_commercial_profile(
        self,
        num_days: int = 1,
        timestep_minutes: int = 60,
        base_load_kw: float = 10.0,
        peak_load_kw: float = 50.0,
        business_type: Literal["office", "retail", "industrial"] = "office",
        noise_level: float = 0.1,
    ) -> pd.DataFrame:
        """
        Generate commercial load profile.

        Args:
            num_days: Number of days to generate
            timestep_minutes: Timestep in minutes
            base_load_kw: Base load in kW (off-hours)
            peak_load_kw: Peak load in kW (business hours)
            business_type: Type of commercial building
            noise_level: Stochastic noise level (0-1)

        Returns:
            DataFrame with timestamp and load_kw columns
        """
        num_steps = int(num_days * 24 * 60 / timestep_minutes)
        timestamps = pd.date_range(
            start=datetime.now(), periods=num_steps, freq=f"{timestep_minutes}min"
        )

        load_profile = []

        for ts in timestamps:
            hour = ts.hour
            is_weekend = ts.weekday() >= 5

            # Business type patterns
            if business_type == "office":
                # Office: 8 AM - 6 PM weekdays
                if is_weekend:
                    base = base_load_kw * 0.3
                elif 8 <= hour < 18:
                    base = peak_load_kw
                else:
                    base = base_load_kw

            elif business_type == "retail":
                # Retail: 9 AM - 9 PM daily
                if 9 <= hour < 21:
                    base = peak_load_kw * (0.9 if is_weekend else 1.0)
                else:
                    base = base_load_kw

            else:  # industrial
                # Industrial: 24/7 with reduced weekend
                if is_weekend:
                    base = peak_load_kw * 0.6
                else:
                    base = peak_load_kw * 0.9

            # Add stochastic noise
            noise = self.rng.normal(0, noise_level * base)
            load = max(0.1, base + noise)

            load_profile.append(load)

        return pd.DataFrame({"timestamp": timestamps, "load_kw": load_profile})

    def generate_multiple_loads(
        self,
        num_residential: int = 5,
        num_commercial: int = 2,
        num_days: int = 1,
        timestep_minutes: int = 60,
        **kwargs,
    ) -> pd.DataFrame:
        """
        Generate multiple load profiles.

        Args:
            num_residential: Number of residential loads
            num_commercial: Number of commercial loads
            num_days: Number of days
            timestep_minutes: Timestep in minutes
            **kwargs: Additional arguments for profile generation

        Returns:
            DataFrame with timestamp and load columns for each customer
        """
        profiles = {}

        # Generate residential loads
        for i in range(num_residential):
            profile = self.generate_residential_profile(
                num_days=num_days,
                timestep_minutes=timestep_minutes,
                base_load_kw=self.rng.uniform(1.5, 3.0),
                peak_load_kw=self.rng.uniform(4.0, 6.0),
                **kwargs,
            )
            profiles[f"residential_{i}"] = profile["load_kw"].values

        # Generate commercial loads
        for i in range(num_commercial):
            profile = self.generate_commercial_profile(
                num_days=num_days,
                timestep_minutes=timestep_minutes,
                base_load_kw=self.rng.uniform(8.0, 15.0),
                peak_load_kw=self.rng.uniform(40.0, 60.0),
                **kwargs,
            )
            profiles[f"commercial_{i}"] = profile["load_kw"].values

        # Combine into single DataFrame
        df = pd.DataFrame(profiles)
        df.insert(0, "timestamp", profile["timestamp"].values)

        logger.info(
            f"Generated {num_residential} residential and {num_commercial} commercial load profiles"
        )

        return df

    def to_pandapower_format(
        self, profile_df: pd.DataFrame, load_indices: list
    ) -> pd.DataFrame:
        """
        Convert load profiles to pandapower time-series format.

        Args:
            profile_df: DataFrame with load profiles
            load_indices: List of load indices in pandapower network

        Returns:
            DataFrame formatted for pandapower time-series
        """
        # Map profile columns to load indices
        profile_cols = [col for col in profile_df.columns if col != "timestamp"]

        if len(profile_cols) != len(load_indices):
            logger.warning(
                f"Profile count ({len(profile_cols)}) != load count ({len(load_indices)})"
            )

        # Create mapping
        pp_format = {}
        for i, load_idx in enumerate(load_indices):
            if i < len(profile_cols):
                # Convert kW to MW
                pp_format[f"load_{load_idx}"] = (
                    profile_df[profile_cols[i]].values / 1000.0
                )

        return pd.DataFrame(pp_format)
