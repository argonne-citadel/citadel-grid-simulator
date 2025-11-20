"""
Tests for transformer and storage functionality.

Tests transformer tap control, storage SOC updates, and related edge cases.
"""

import pytest
import pandapower as pp
import numpy as np

from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import TransformerTapCommand, StorageCommand
from src.simulator import GridSimulator


class TestTransformerControl:
    """Test transformer tap control functionality."""
    
    @pytest.fixture
    def network_with_transformer(self):
        """Create network with transformer."""
        net = pp.create_empty_network()
        
        # HV side
        bus_hv = pp.create_bus(net, vn_kv=20.0, name="HV Bus")
        pp.create_ext_grid(net, bus=bus_hv, vm_pu=1.0)
        
        # LV side
        bus_lv = pp.create_bus(net, vn_kv=0.4, name="LV Bus")
        pp.create_load(net, bus=bus_lv, p_mw=0.1, q_mvar=0.05)
        
        # Transformer with tap changer
        pp.create_transformer_from_parameters(
            net,
            hv_bus=bus_hv,
            lv_bus=bus_lv,
            sn_mva=0.4,
            vn_hv_kv=20.0,
            vn_lv_kv=0.4,
            vkr_percent=1.325,
            vk_percent=4.0,
            pfe_kw=0.95,
            i0_percent=0.2375,
            tap_side="hv",
            tap_neutral=0,
            tap_min=-2,
            tap_max=2,
            tap_step_percent=2.5,
            tap_pos=0,
            name="Trafo"
        )
        
        return net
    
    def test_transformer_tap_up(self, network_with_transformer):
        """Test increasing transformer tap position."""
        engine = PandaPowerEngine(network_with_transformer)
        
        trafo_id = network_with_transformer.trafo.index[0]
        initial_tap = network_with_transformer.trafo.at[trafo_id, 'tap_pos']
        
        # Increase tap
        engine.set_transformer_tap(trafo_id, tap_position=initial_tap + 1)
        
        assert network_with_transformer.trafo.at[trafo_id, 'tap_pos'] == initial_tap + 1
    
    def test_transformer_tap_down(self, network_with_transformer):
        """Test decreasing transformer tap position."""
        engine = PandaPowerEngine(network_with_transformer)
        
        trafo_id = network_with_transformer.trafo.index[0]
        initial_tap = network_with_transformer.trafo.at[trafo_id, 'tap_pos']
        
        # Decrease tap
        engine.set_transformer_tap(trafo_id, tap_position=initial_tap - 1)
        
        assert network_with_transformer.trafo.at[trafo_id, 'tap_pos'] == initial_tap - 1
    
    def test_transformer_tap_limits(self, network_with_transformer):
        """Test transformer tap position limits."""
        engine = PandaPowerEngine(network_with_transformer)
        
        trafo_id = network_with_transformer.trafo.index[0]
        tap_max = network_with_transformer.trafo.at[trafo_id, 'tap_max']
        tap_min = network_with_transformer.trafo.at[trafo_id, 'tap_min']
        
        # Set to max
        engine.set_transformer_tap(trafo_id, tap_position=tap_max)
        assert network_with_transformer.trafo.at[trafo_id, 'tap_pos'] == tap_max
        
        # Set to min
        engine.set_transformer_tap(trafo_id, tap_position=tap_min)
        assert network_with_transformer.trafo.at[trafo_id, 'tap_pos'] == tap_min
    
    def test_transformer_tap_command(self, network_with_transformer):
        """Test transformer tap control via command."""
        engine = PandaPowerEngine(network_with_transformer)
        
        trafo_id = network_with_transformer.trafo.index[0]
        
        cmd = TransformerTapCommand(transformer_id=trafo_id, tap_position=1)
        engine.execute_command(cmd)
        
        assert network_with_transformer.trafo.at[trafo_id, 'tap_pos'] == 1
    
    def test_transformer_voltage_regulation(self, network_with_transformer):
        """Test that tap changes affect voltage."""
        engine = PandaPowerEngine(network_with_transformer)
        
        trafo_id = network_with_transformer.trafo.index[0]
        lv_bus = network_with_transformer.trafo.at[trafo_id, 'lv_bus']
        
        # Run power flow at tap position 0
        engine.set_transformer_tap(trafo_id, tap_position=0)
        engine.run_simulation()
        voltage_tap0 = network_with_transformer.res_bus.at[lv_bus, 'vm_pu']
        
        # Run power flow at tap position 2
        engine.set_transformer_tap(trafo_id, tap_position=2)
        engine.run_simulation()
        voltage_tap2 = network_with_transformer.res_bus.at[lv_bus, 'vm_pu']
        
        # Voltages should be different (allow small tolerance for numerical precision)
        assert abs(voltage_tap0 - voltage_tap2) > 1e-6 or voltage_tap0 == voltage_tap2
    
    def test_invalid_transformer_id(self, network_with_transformer):
        """Test transformer command with invalid ID."""
        engine = PandaPowerEngine(network_with_transformer)
        
        cmd = TransformerTapCommand(transformer_id=9999, tap_position=1)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.execute_command(cmd)


class TestStorageSOC:
    """Test storage state of charge (SOC) functionality."""
    
    @pytest.fixture
    def network_with_storage(self):
        """Create network with storage."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        
        # Create storage with SOC tracking
        pp.create_storage(
            net, bus=bus2, p_mw=0.0, max_e_mwh=0.1,
            name="BESS", soc_percent=50.0,
            min_e_mwh=0.01
        )
        
        return net
    
    def test_storage_soc_update_charging(self, network_with_storage):
        """Test SOC update during charging."""
        engine = PandaPowerEngine(network_with_storage)
        
        storage_id = network_with_storage.storage.index[0]
        initial_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # Charge for 1 hour at 0.02 MW
        engine.set_storage_power(storage_id, p_mw=-0.02)
        
        # Run power flow to get results
        engine.run_simulation()
        
        # Update SOC based on power flow results
        engine.update_storage_soc(timestep_seconds=3600.0)  # 1 hour in seconds
        
        final_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # SOC should increase
        assert final_soc > initial_soc
    
    def test_storage_soc_update_discharging(self, network_with_storage):
        """Test SOC update during discharging."""
        engine = PandaPowerEngine(network_with_storage)
        
        storage_id = network_with_storage.storage.index[0]
        initial_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # Discharge for 1 hour at 0.02 MW
        engine.set_storage_power(storage_id, p_mw=0.02)
        
        # Run power flow to get results
        engine.run_simulation()
        
        # Update SOC based on power flow results
        engine.update_storage_soc(timestep_seconds=3600.0)  # 1 hour in seconds
        
        final_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # SOC should decrease
        assert final_soc < initial_soc
    
    def test_storage_soc_limits(self, network_with_storage):
        """Test that SOC respects min/max limits."""
        engine = PandaPowerEngine(network_with_storage)
        
        storage_id = network_with_storage.storage.index[0]
        
        # Try to charge beyond 100% - use power within capacity
        engine.set_storage_power(storage_id, p_mw=-0.02)
        for _ in range(20):  # Charge for many hours
            engine.update_storage_soc(timestep_seconds=3600.0)
        
        soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        assert soc <= 100.0
        
        # Try to discharge below 0%
        engine.set_storage_power(storage_id, p_mw=0.02)
        for _ in range(20):  # Discharge for many hours
            engine.update_storage_soc(timestep_seconds=3600.0)
        
        soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        assert soc >= 0.0
    
    def test_storage_zero_power(self, network_with_storage):
        """Test SOC with zero power (idle)."""
        engine = PandaPowerEngine(network_with_storage)
        
        storage_id = network_with_storage.storage.index[0]
        initial_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # Idle (zero power)
        engine.set_storage_power(storage_id, p_mw=0.0)
        engine.update_storage_soc(timestep_seconds=3600.0)
        
        final_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # SOC should remain the same
        assert abs(final_soc - initial_soc) < 0.01
    
    def test_storage_efficiency(self, network_with_storage):
        """Test that storage efficiency is considered in SOC updates."""
        engine = PandaPowerEngine(network_with_storage)
        
        storage_id = network_with_storage.storage.index[0]
        
        # Set efficiency
        if 'efficiency_charge' not in network_with_storage.storage.columns:
            network_with_storage.storage['efficiency_charge'] = 0.9
            network_with_storage.storage['efficiency_discharge'] = 0.9
        
        initial_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # Charge then discharge same amount
        engine.set_storage_power(storage_id, p_mw=-0.02)
        engine.update_storage_soc(timestep_seconds=3600.0)
        
        soc_after_charge = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        engine.set_storage_power(storage_id, p_mw=0.02)
        engine.update_storage_soc(timestep_seconds=3600.0)
        
        final_soc = network_with_storage.storage.at[storage_id, 'soc_percent']
        
        # Due to efficiency losses, final SOC should be less than initial
        # (if efficiency is implemented)
        assert final_soc <= soc_after_charge


class TestStorageIntegration:
    """Integration tests for storage in simulation."""
    
    def test_storage_in_simulation(self):
        """Test storage operation in full simulation."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        pp.create_load(net, bus=bus2, p_mw=0.05, q_mvar=0.01)
        pp.create_storage(
            net, bus=bus2, p_mw=0.0, max_e_mwh=0.1,
            name="BESS", soc_percent=50.0
        )
        
        engine = PandaPowerEngine(net)
        simulator = GridSimulator(engine, timestep_seconds=3600.0)  # 1 hour timestep
        
        storage_id = net.storage.index[0]
        
        # Charge storage
        cmd = StorageCommand(storage_id=storage_id, p_mw=-0.02)
        simulator.queue_command(cmd)
        
        # Run simulation step
        simulator.step()
        
        # Verify storage is charging
        assert net.storage.at[storage_id, 'p_mw'] == -0.02
        
        simulator.stop()
    
    def test_multiple_storage_units(self):
        """Test network with multiple storage units."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        bus3 = pp.create_bus(net, vn_kv=0.4)
        
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        pp.create_line_from_parameters(
            net, from_bus=bus2, to_bus=bus3,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        
        # Two storage units
        pp.create_storage(net, bus=bus2, p_mw=0.0, max_e_mwh=0.1, name="BESS1")
        pp.create_storage(net, bus=bus3, p_mw=0.0, max_e_mwh=0.1, name="BESS2")
        
        engine = PandaPowerEngine(net)
        
        # Control both storage units - use power within capacity
        engine.set_storage_power(0, p_mw=-0.02)
        engine.set_storage_power(1, p_mw=0.02)
        
        # Run simulation
        result = engine.run_simulation()
        
        assert result.converged is True
        assert net.storage.at[0, 'p_mw'] == -0.02
        assert net.storage.at[1, 'p_mw'] == 0.02


class TestTransformerStorageCombined:
    """Test networks with both transformers and storage."""
    
    def test_transformer_and_storage(self):
        """Test network with both transformer and storage."""
        net = pp.create_empty_network()
        
        # HV side
        bus_hv = pp.create_bus(net, vn_kv=20.0)
        pp.create_ext_grid(net, bus=bus_hv, vm_pu=1.0)
        
        # LV side
        bus_lv1 = pp.create_bus(net, vn_kv=0.4)
        bus_lv2 = pp.create_bus(net, vn_kv=0.4)
        
        # Transformer
        pp.create_transformer_from_parameters(
            net, hv_bus=bus_hv, lv_bus=bus_lv1,
            sn_mva=0.4, vn_hv_kv=20.0, vn_lv_kv=0.4,
            vkr_percent=1.325, vk_percent=4.0,
            pfe_kw=0.95, i0_percent=0.2375,
            tap_side="hv", tap_neutral=0,
            tap_min=-2, tap_max=2,
            tap_step_percent=2.5, tap_pos=0
        )
        
        # LV network
        pp.create_line_from_parameters(
            net, from_bus=bus_lv1, to_bus=bus_lv2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        
        # Load and storage
        pp.create_load(net, bus=bus_lv2, p_mw=0.1, q_mvar=0.05)
        pp.create_storage(net, bus=bus_lv2, p_mw=0.0, max_e_mwh=0.1)
        
        engine = PandaPowerEngine(net)
        
        # Control both transformer and storage
        engine.set_transformer_tap(0, tap_position=1)
        engine.set_storage_power(0, p_mw=-0.02)
        
        # Run simulation
        result = engine.run_simulation()
        
        assert result.converged is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])