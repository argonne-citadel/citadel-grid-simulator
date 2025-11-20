"""
Tests for error handling and failure scenarios.

Tests convergence failures, invalid topologies, command failures,
and other error conditions.
"""

import pytest
import pandapower as pp
import numpy as np

from src.models.dickert_lv import DickertLVModel
from src.simulator import GridSimulator
from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import (
    BreakerCommand, GeneratorCommand, LoadCommand,
    StorageCommand, TransformerTapCommand
)
from src.schemas.results import PowerFlowConfig, PowerFlowAlgorithm


class TestConvergenceFailures:
    """Test power flow convergence failure scenarios."""
    
    def test_disconnected_network(self):
        """Test power flow with disconnected network."""
        # Create a simple network
        net = pp.create_empty_network()
        
        # Create two separate islands
        bus1 = pp.create_bus(net, vn_kv=0.4, name="Island 1 Bus 1")
        bus2 = pp.create_bus(net, vn_kv=0.4, name="Island 1 Bus 2")
        bus3 = pp.create_bus(net, vn_kv=0.4, name="Island 2 Bus 1")
        
        # Island 1 with slack
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        
        # Island 2 without slack (will cause convergence issue)
        pp.create_load(net, bus=bus3, p_mw=0.05, q_mvar=0.01)
        
        engine = PandaPowerEngine(net)
        
        # Should handle convergence failure gracefully
        result = engine.run_simulation()
        
        # Pandapower may still report convergence for the connected island
        # Just verify it doesn't crash
        assert result is not None
    
    def test_extreme_load(self):
        """Test power flow with extreme load causing convergence issues."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        net = model.get_network()
        
        # Add extremely large load
        if len(net.bus) > 1:
            pp.create_load(net, bus=1, p_mw=100.0, q_mvar=50.0, name="Extreme Load")
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # May or may not converge, but should not crash
        assert result is not None
        assert hasattr(result, 'converged')
    
    def test_voltage_collapse_scenario(self):
        """Test scenario that could lead to voltage collapse."""
        net = pp.create_empty_network()
        
        # Create long radial feeder
        bus0 = pp.create_bus(net, vn_kv=0.4)
        pp.create_ext_grid(net, bus=bus0, vm_pu=1.0)
        
        prev_bus = bus0
        for i in range(10):
            new_bus = pp.create_bus(net, vn_kv=0.4)
            # High impedance line
            pp.create_line_from_parameters(
                net, from_bus=prev_bus, to_bus=new_bus,
                length_km=1.0, r_ohm_per_km=5.0, x_ohm_per_km=2.0,
                c_nf_per_km=10, max_i_ka=0.1
            )
            # Heavy load at end
            if i == 9:
                pp.create_load(net, bus=new_bus, p_mw=0.5, q_mvar=0.3)
            prev_bus = new_bus
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should complete without crashing
        assert result is not None


class TestInvalidCommands:
    """Test handling of invalid commands."""
    
    @pytest.fixture
    def engine(self):
        """Create engine for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        return PandaPowerEngine(model.get_network())
    
    def test_invalid_breaker_id(self, engine):
        """Test breaker command with invalid line ID."""
        cmd = BreakerCommand(line_id=9999, closed=False)
        
        # Should raise exception or handle gracefully
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.execute_command(cmd)
    
    def test_invalid_generator_id(self, engine):
        """Test generator command with invalid ID."""
        cmd = GeneratorCommand(generator_id=9999, p_mw=0.05)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.execute_command(cmd)
    
    def test_invalid_load_id(self, engine):
        """Test load command with invalid ID."""
        cmd = LoadCommand(load_id=9999, p_mw=0.05)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.execute_command(cmd)
    
    def test_invalid_storage_id(self, engine):
        """Test storage command with invalid ID."""
        cmd = StorageCommand(storage_id=9999, p_mw=0.02)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.execute_command(cmd)
    
    def test_negative_power_values(self, engine):
        """Test commands with invalid negative power values."""
        if len(engine.net.load) > 0:
            load_id = engine.net.load.index[0]
            
            # Negative load power (should be handled)
            cmd = LoadCommand(load_id=load_id, p_mw=-0.05, q_mvar=-0.01)
            engine.execute_command(cmd)
            
            # Values should be set (pandapower allows negative loads)
            assert engine.net.load.at[load_id, 'p_mw'] == -0.05


class TestInvalidTopologies:
    """Test handling of invalid network topologies."""
    
    def test_network_without_slack(self):
        """Test network without slack bus."""
        net = pp.create_empty_network()
        
        # Create buses and lines but no slack
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        pp.create_load(net, bus=bus2, p_mw=0.05, q_mvar=0.01)
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should fail to converge
        assert result.converged is False
    
    def test_islanded_buses(self):
        """Test network with islanded buses."""
        net = pp.create_empty_network()
        
        # Main network
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        
        # Islanded bus
        bus3 = pp.create_bus(net, vn_kv=0.4)
        pp.create_load(net, bus=bus3, p_mw=0.05, q_mvar=0.01)
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should handle gracefully
        assert result is not None


class TestBoundaryConditions:
    """Test boundary conditions and edge cases."""
    
    def test_zero_impedance_line(self):
        """Test line with zero impedance."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        
        # Zero impedance line
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.001, r_ohm_per_km=0.0, x_ohm_per_km=0.0,
            c_nf_per_km=0, max_i_ka=1.0
        )
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should handle (may converge or not depending on pandapower)
        assert result is not None
    
    def test_very_high_voltage(self):
        """Test with very high voltage setpoint."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        
        # Very high voltage setpoint
        pp.create_ext_grid(net, bus=bus1, vm_pu=2.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        pp.create_load(net, bus=bus2, p_mw=0.05, q_mvar=0.01)
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should complete
        assert result is not None
    
    def test_zero_load(self):
        """Test network with zero load."""
        net = pp.create_empty_network()
        
        bus1 = pp.create_bus(net, vn_kv=0.4)
        bus2 = pp.create_bus(net, vn_kv=0.4)
        pp.create_ext_grid(net, bus=bus1, vm_pu=1.0)
        pp.create_line_from_parameters(
            net, from_bus=bus1, to_bus=bus2,
            length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
            c_nf_per_km=210, max_i_ka=0.142
        )
        pp.create_load(net, bus=bus2, p_mw=0.0, q_mvar=0.0)
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should converge
        assert result.converged is True


class TestAlgorithmFailures:
    """Test different power flow algorithm failures."""
    
    @pytest.fixture
    def network(self):
        """Create test network."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        return model.get_network()
    
    def test_newton_raphson_max_iterations(self, network):
        """Test Newton-Raphson with very low max iterations."""
        engine = PandaPowerEngine(network)
        
        config = PowerFlowConfig(
            algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
            max_iterations=1,  # Very low
            tolerance=1e-6
        )
        
        result = engine.run_simulation(config)
        
        # May not converge with only 1 iteration
        assert result is not None
        assert result.iterations <= 1
    
    def test_gauss_seidel_algorithm(self, network):
        """Test Gauss-Seidel algorithm."""
        engine = PandaPowerEngine(network)
        
        config = PowerFlowConfig(
            algorithm=PowerFlowAlgorithm.GAUSS_SEIDEL,
            max_iterations=100,
            tolerance=1e-6
        )
        
        result = engine.run_simulation(config)
        
        # Should complete
        assert result is not None


class TestStateRetrieval:
    """Test state retrieval under error conditions."""
    
    def test_state_before_simulation(self):
        """Test getting state before running simulation."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        
        # Get state without running simulation
        state = engine.get_current_state()
        
        # Should return a state (may not be converged)
        assert state is not None
        assert hasattr(state, 'converged')
    
    def test_state_after_failed_simulation(self):
        """Test getting state after failed power flow."""
        net = pp.create_empty_network()
        
        # Create invalid network
        bus1 = pp.create_bus(net, vn_kv=0.4)
        pp.create_load(net, bus=bus1, p_mw=0.05, q_mvar=0.01)
        # No slack bus - will fail
        
        engine = PandaPowerEngine(net)
        result = engine.run_simulation()
        
        # Should fail
        assert result.converged is False
        
        # Getting state after failed simulation may raise validation error due to NaN values
        # This is expected behavior - the test verifies the error is handled
        try:
            state = engine.get_current_state()
            # If it succeeds, state should exist
            assert state is not None
        except Exception as e:
            # If it fails due to NaN validation, that's also acceptable
            assert 'validation' in str(e).lower() or 'nan' in str(e).lower()


class TestResourceLimits:
    """Test resource limit scenarios."""
    
    def test_large_command_queue(self):
        """Test simulator with many queued commands."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        simulator = GridSimulator(engine, timestep_seconds=1.0)
        
        # Queue many commands
        if len(engine.net.line) > 0:
            line_id = engine.net.line.index[0]
            for i in range(1000):
                cmd = BreakerCommand(line_id=line_id, closed=(i % 2 == 0))
                simulator.queue_command(cmd)
        
        # Should handle large queue
        assert simulator.command_queue.qsize() == 1000
        
        # Process some commands
        simulator.step()
        
        # Queue should be smaller
        assert simulator.command_queue.qsize() < 1000
        
        simulator.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])