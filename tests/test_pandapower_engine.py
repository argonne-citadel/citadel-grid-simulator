"""
Tests for PandaPowerEngine implementation.

Tests the PowerSystemEngine interface implementation for PandaPower,
including data conversion, state management, and control operations.
"""

import pytest
import pandapower as pp
from datetime import datetime

from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.common import BusType, DeviceStatus, DERType
from src.schemas.topology import BusInfo, LineInfo, GeneratorInfo, LoadInfo, StorageInfo
from src.schemas.state import BusState, LineState, GeneratorState, LoadState, StorageState
from src.schemas.commands import (
    BreakerCommand, GeneratorCommand, LoadCommand, 
    StorageCommand, TransformerTapCommand
)
from src.schemas.results import PowerFlowAlgorithm, PowerFlowConfig


@pytest.fixture
def simple_network():
    """Create a simple 2-bus test network."""
    net = pp.create_empty_network()
    
    # Create buses
    bus1 = pp.create_bus(net, vn_kv=0.4, name="Bus 1")
    bus2 = pp.create_bus(net, vn_kv=0.4, name="Bus 2")
    
    # Create external grid (slack bus)
    pp.create_ext_grid(net, bus=bus1, vm_pu=1.0, name="Grid Connection")
    
    # Create line
    pp.create_line_from_parameters(
        net, from_bus=bus1, to_bus=bus2,
        length_km=0.1, r_ohm_per_km=0.642, x_ohm_per_km=0.083,
        c_nf_per_km=210, max_i_ka=0.142, name="Line 1"
    )
    
    # Create load
    pp.create_load(net, bus=bus2, p_mw=0.05, q_mvar=0.01, name="Load 1")
    
    return net


@pytest.fixture
def network_with_der(simple_network):
    """Add DER to the simple network."""
    net = simple_network
    
    # Add PV generator
    pp.create_sgen(
        net, bus=1, p_mw=0.03, q_mvar=0.0,
        name="PV 1", type="PV"
    )
    
    # Add storage
    pp.create_storage(
        net, bus=1, p_mw=0.0, max_e_mwh=0.1,
        name="BESS 1"
    )
    
    return net


class TestEngineInitialization:
    """Test engine initialization and setup."""
    
    def test_create_engine_from_network(self, simple_network):
        """Test creating engine from existing network."""
        engine = PandaPowerEngine(simple_network)
        assert engine is not None
        assert engine.net is simple_network
        
    def test_create_engine_with_grid_stix(self, simple_network):
        """Test creating engine with Grid-STIX enabled."""
        engine = PandaPowerEngine(simple_network, enable_grid_stix=True)
        assert engine.grid_stix_enabled is True
        assert engine.get_grid_stix_annotator() is not None


class TestTopologyRetrieval:
    """Test topology information retrieval."""
    
    def test_get_topology(self, simple_network):
        """Test getting complete network topology."""
        engine = PandaPowerEngine(simple_network)
        topology = engine.get_topology()
        
        assert topology is not None
        assert len(topology.buses) == 2
        assert len(topology.lines) == 1
        assert len(topology.loads) == 1
        assert topology.name == "Network"
        assert topology.base_mva == 1.0
        assert topology.frequency_hz == 50.0
        
    def test_get_bus_info(self, simple_network):
        """Test getting individual bus information."""
        engine = PandaPowerEngine(simple_network)
        bus_info = engine.get_bus_info(0)
        
        assert bus_info is not None
        assert bus_info.bus_id == 0
        assert bus_info.name == "Bus 1"
        assert bus_info.voltage_nominal_kv == 0.4
        
    def test_get_line_info(self, simple_network):
        """Test getting individual line information."""
        engine = PandaPowerEngine(simple_network)
        line_info = engine.get_line_info(0)
        
        assert line_info is not None
        assert line_info.line_id == 0
        assert line_info.name == "Line 1"
        assert line_info.from_bus == 0
        assert line_info.to_bus == 1
        
    def test_get_load_info(self, simple_network):
        """Test getting individual load information."""
        engine = PandaPowerEngine(simple_network)
        load_info = engine.get_load_info(0)
        
        assert load_info is not None
        assert load_info.load_id == 0
        assert load_info.name == "Load 1"
        assert load_info.p_mw == 0.05
        assert load_info.q_mvar == 0.01
        
    def test_get_generator_info(self, network_with_der):
        """Test getting generator information."""
        engine = PandaPowerEngine(network_with_der)
        gen_info = engine.get_generator_info(0)
        
        assert gen_info is not None
        assert gen_info.generator_id == 0
        assert gen_info.name == "PV 1"
        
    def test_get_storage_info(self, network_with_der):
        """Test getting storage information."""
        engine = PandaPowerEngine(network_with_der)
        storage_info = engine.get_storage_info(0)
        
        assert storage_info is not None
        assert storage_info.storage_id == 0
        assert storage_info.name == "BESS 1"


class TestPowerFlowSimulation:
    """Test power flow simulation."""
    
    def test_run_simulation_success(self, simple_network):
        """Test successful power flow simulation."""
        engine = PandaPowerEngine(simple_network)
        result = engine.run_simulation()
        
        assert result is not None
        assert result.converged is True
        assert result.iterations > 0
        assert result.execution_time_ms > 0
        assert result.error_message is None
        
    def test_run_simulation_with_config(self, simple_network):
        """Test power flow with custom configuration."""
        engine = PandaPowerEngine(simple_network)
        config = PowerFlowConfig(
            algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
            max_iterations=50,
            tolerance=1e-6
        )
        result = engine.run_simulation(config)
        
        assert result.converged is True
        assert result.config == config
        
    def test_get_convergence_status(self, simple_network):
        """Test convergence status check."""
        engine = PandaPowerEngine(simple_network)
        engine.run_simulation()
        
        assert engine.get_convergence_status() is True


class TestStateRetrieval:
    """Test grid state retrieval."""
    
    def test_get_current_state(self, simple_network):
        """Test getting current grid state."""
        engine = PandaPowerEngine(simple_network)
        engine.run_simulation()
        
        state = engine.get_current_state()
        
        assert state is not None
        assert state.converged is True
        assert len(state.buses) == 2
        assert len(state.lines) == 1
        assert len(state.loads) == 1
        assert state.total_generation_mw > 0
        assert state.total_load_mw > 0
        
    def test_bus_state_values(self, simple_network):
        """Test bus state contains correct values."""
        engine = PandaPowerEngine(simple_network)
        engine.run_simulation()
        
        state = engine.get_current_state()
        bus_state = state.buses[0]
        
        assert bus_state.bus_id == 0
        assert 0.9 <= bus_state.voltage_pu <= 1.1  # Reasonable voltage
        assert -180 <= bus_state.angle_deg <= 180
        assert isinstance(bus_state.timestamp, datetime)
        
    def test_line_state_values(self, simple_network):
        """Test line state contains correct values."""
        engine = PandaPowerEngine(simple_network)
        engine.run_simulation()
        
        state = engine.get_current_state()
        line_state = state.lines[0]
        
        assert line_state.line_id == 0
        assert line_state.p_from_mw is not None
        assert line_state.q_from_mvar is not None
        assert line_state.loading_percent >= 0


class TestBreakerControl:
    """Test breaker/line switching control."""
    
    def test_set_breaker_open(self, simple_network):
        """Test opening a breaker."""
        engine = PandaPowerEngine(simple_network)
        
        # Open the line
        engine.set_breaker_status(0, closed=False)
        
        assert simple_network.line.at[0, 'in_service'] == False
        
    def test_set_breaker_closed(self, simple_network):
        """Test closing a breaker."""
        engine = PandaPowerEngine(simple_network)
        
        # Open then close
        engine.set_breaker_status(0, closed=False)
        engine.set_breaker_status(0, closed=True)
        
        assert simple_network.line.at[0, 'in_service'] == True
        
    def test_breaker_command(self, simple_network):
        """Test breaker control via command."""
        engine = PandaPowerEngine(simple_network)
        
        cmd = BreakerCommand(line_id=0, closed=False)
        engine.execute_command(cmd)
        
        assert simple_network.line.at[0, 'in_service'] == False


class TestGeneratorControl:
    """Test generator setpoint control."""
    
    def test_set_generator_setpoint(self, network_with_der):
        """Test setting generator power setpoint."""
        engine = PandaPowerEngine(network_with_der)
        
        engine.set_generator_setpoint(0, p_mw=0.05, q_mvar=0.01)
        
        assert network_with_der.sgen.at[0, 'p_mw'] == 0.05
        assert network_with_der.sgen.at[0, 'q_mvar'] == 0.01
        
    def test_generator_command(self, network_with_der):
        """Test generator control via command."""
        engine = PandaPowerEngine(network_with_der)
        
        cmd = GeneratorCommand(generator_id=0, p_mw=0.04, q_mvar=0.0)
        engine.execute_command(cmd)
        
        assert network_with_der.sgen.at[0, 'p_mw'] == 0.04


class TestLoadControl:
    """Test load adjustment control."""
    
    def test_set_load_demand(self, simple_network):
        """Test setting load demand."""
        engine = PandaPowerEngine(simple_network)
        
        engine.set_load_demand(0, p_mw=0.08, q_mvar=0.02)
        
        assert simple_network.load.at[0, 'p_mw'] == 0.08
        assert simple_network.load.at[0, 'q_mvar'] == 0.02
        
    def test_load_command(self, simple_network):
        """Test load control via command."""
        engine = PandaPowerEngine(simple_network)
        
        cmd = LoadCommand(load_id=0, p_mw=0.06, q_mvar=0.015)
        engine.execute_command(cmd)
        
        assert simple_network.load.at[0, 'p_mw'] == 0.06


class TestStorageControl:
    """Test energy storage control."""
    
    def test_set_storage_power(self, network_with_der):
        """Test setting storage power."""
        engine = PandaPowerEngine(network_with_der)
        
        engine.set_storage_power(0, p_mw=0.02)
        
        assert network_with_der.storage.at[0, 'p_mw'] == 0.02
        
    def test_storage_command(self, network_with_der):
        """Test storage control via command."""
        engine = PandaPowerEngine(network_with_der)
        
        cmd = StorageCommand(storage_id=0, p_mw=-0.01)  # Charging
        engine.execute_command(cmd)
        
        assert network_with_der.storage.at[0, 'p_mw'] == -0.01


class TestDataConversion:
    """Test data conversion between PandaPower and Pydantic models."""
    
    def test_bus_type_conversion(self, simple_network):
        """Test bus type conversion."""
        engine = PandaPowerEngine(simple_network)
        topology = engine.get_topology()
        
        # Slack bus should be converted correctly
        slack_bus = topology.buses[0]
        assert slack_bus.bus_type == BusType.SLACK
        
    def test_none_value_handling(self, simple_network):
        """Test handling of None/NaN values."""
        engine = PandaPowerEngine(simple_network)
        engine.run_simulation()
        
        state = engine.get_current_state()
        
        # Should not have None values in required fields
        for bus_state in state.buses.values():
            assert bus_state.voltage_pu is not None
            assert bus_state.angle_deg is not None


class TestErrorHandling:
    """Test error handling and edge cases."""
    
    def test_invalid_bus_id(self, simple_network):
        """Test handling of invalid bus ID."""
        engine = PandaPowerEngine(simple_network)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.get_bus_info(999)
            
    def test_invalid_line_id(self, simple_network):
        """Test handling of invalid line ID."""
        engine = PandaPowerEngine(simple_network)
        
        with pytest.raises((KeyError, IndexError, ValueError)):
            engine.set_breaker_status(999, closed=False)
            
    def test_simulation_before_state(self, simple_network):
        """Test getting state before running simulation."""
        engine = PandaPowerEngine(simple_network)
        
        # Should still return a state (may not be converged)
        state = engine.get_current_state()
        assert state is not None


class TestIntegration:
    """Integration tests combining multiple operations."""
    
    def test_control_and_simulate(self, network_with_der):
        """Test control operations followed by simulation."""
        engine = PandaPowerEngine(network_with_der)
        
        # Set controls
        engine.set_generator_setpoint(0, p_mw=0.04, q_mvar=0.0)
        engine.set_load_demand(0, p_mw=0.06, q_mvar=0.015)
        
        # Run simulation
        result = engine.run_simulation()
        assert result.converged is True
        
        # Get state
        state = engine.get_current_state()
        assert state.converged is True
        
    def test_multiple_commands(self, network_with_der):
        """Test executing multiple commands in sequence."""
        engine = PandaPowerEngine(network_with_der)
        
        commands = [
            GeneratorCommand(generator_id=0, p_mw=0.05),
            LoadCommand(load_id=0, p_mw=0.07),
            StorageCommand(storage_id=0, p_mw=0.01),
        ]
        
        for cmd in commands:
            engine.execute_command(cmd)
            
        # Verify all changes applied
        assert network_with_der.sgen.at[0, 'p_mw'] == 0.05
        assert network_with_der.load.at[0, 'p_mw'] == 0.07
        assert network_with_der.storage.at[0, 'p_mw'] == 0.01