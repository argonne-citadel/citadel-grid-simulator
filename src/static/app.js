// Configuration
const POLL_INTERVAL = 5000; // 5 seconds
const RETRY_INTERVAL = 2000; // 2 seconds on error

// State
let currentVersion = -1;
let pollTimer = null;
let retrying = false;

// Initialize Plotly chart
function initializePlot(data) {
    const topology = data.topology;
    const state = data.state;

    const plotData = [];

    // Line data with loading percentage coloring
    topology.lines.forEach((line, idx) => {
        const isOpen = state.line_status[idx];
        const loading = state.line_loading[idx];

        // Add midpoint for better hover detection and coloring
        const midX = (line.x[0] + line.x[1]) / 2;
        const midY = (line.y[0] + line.y[1]) / 2;

        // Calculate color based on loading percentage (green -> yellow -> red)
        let lineColor;
        if (!isOpen) {
            lineColor = '#ef4444';  // Red for open lines
        } else if (loading < 50) {
            // Interpolate between green (#22c55e) and yellow (#fbbf24)
            const t = loading / 50;
            lineColor = `rgba(${Math.round(34 + (251-34)*t)}, ${Math.round(197 + (191-197)*t)}, ${Math.round(94 + (36-94)*t)}, 0.8)`;
        } else {
            // Interpolate between yellow (#fbbf24) and red (#ef4444)
            const t = (loading - 50) / 50;
            lineColor = `rgba(${Math.round(251 + (239-251)*t)}, ${Math.round(191 + (68-191)*t)}, ${Math.round(36 + (68-36)*t)}, 0.8)`;
        }

        // Line visual only - NO hover
        plotData.push({
            x: line.x,
            y: line.y,
            mode: 'lines',
            type: 'scattergl',
            name: `Line ${line.line_id}`,
            line: {
                color: lineColor,
                width: isOpen ? 2 : 1,
                dash: isOpen ? 'solid' : 'dash'
            },
            hoverinfo: 'skip',
            showlegend: false
        });

        // Separate midpoint marker for hover information
        plotData.push({
            x: [midX],
            y: [midY],
            mode: 'markers',
            type: 'scattergl',
            name: `Line ${line.line_id} Hover`,
            marker: {
                size: 8,
                color: lineColor,  // Use same color as the line visual
                showscale: false,  // Don't show colorbar for midpoint markers
                line: {
                    color: '#ffffff',
                    width: 1
                }
            },
            text: [`Line ${line.line_id}`],
            customdata: [[loading, line.from_bus, line.to_bus, isOpen]],
            hovertemplate: '<b>%{text}</b><br>' +
                          `From Bus %{customdata[1]} → To Bus %{customdata[2]}<br>` +
                          `Status: ${isOpen ? 'Closed' : 'Open'}<br>` +
                          `Loading: %{customdata[0]:.1f}%<br>` +
                          '<extra></extra>',
            showlegend: false
        });
    });

    // Add invisible trace for line loading colorbar legend
    plotData.push({
        x: [null],
        y: [null],
        mode: 'markers',
        type: 'scattergl',
        name: 'Line Loading Legend',
        marker: {
            size: 0,
            color: [0],
            colorscale: [[0, '#22c55e'], [0.5, '#fbbf24'], [1, '#ef4444']],
            cmin: 0,
            cmax: 100,
            showscale: true,
            colorbar: {
                title: 'Line Loading (%)',
                thickness: 20,
                len: 0.7,
                x: 1.15,
                y: 0.5
            }
        },
        hoverinfo: 'skip',
        showlegend: false
    });

    // Transformer connections (if any)
    if (topology.transformers && topology.transformers.length > 0) {
        // Collect all transformer midpoints for legend trace
        const trafoMidX = [];
        const trafoMidY = [];
        const trafoText = [];
        const trafoCustomdata = [];
        
        topology.transformers.forEach(trafo => {
            // Calculate midpoint for hover marker
            const midX = (trafo.x[0] + trafo.x[1]) / 2;
            const midY = (trafo.y[0] + trafo.y[1]) / 2;
            
            // Transformer line visual
            plotData.push({
                x: trafo.x,
                y: trafo.y,
                mode: 'lines',
                type: 'scattergl',
                name: `Trafo ${trafo.trafo_id}`,
                line: {
                    color: trafo.in_service ? '#fbbf24' : '#ef4444',  // Yellow if in service, red if out
                    width: 4,
                    dash: trafo.in_service ? 'solid' : 'dash'
                },
                hoverinfo: 'skip',
                showlegend: false
            });
            
            // Collect midpoint data
            trafoMidX.push(midX);
            trafoMidY.push(midY);
            trafoText.push(trafo.name);
            trafoCustomdata.push([trafo.hv_bus, trafo.lv_bus, trafo.rated_mva, trafo.hv_kv, trafo.lv_kv, trafo.in_service]);
        });
        
        // Single transformer marker trace for legend
        plotData.push({
            x: trafoMidX,
            y: trafoMidY,
            mode: 'markers',
            type: 'scattergl',
            name: 'Transformers',
            marker: {
                size: 12,
                symbol: 'hexagon',
                color: '#fbbf24',
                line: {
                    color: '#ffffff',
                    width: 2
                }
            },
            text: trafoText,
            customdata: trafoCustomdata,
            hovertemplate: '<b>%{text}</b><br>' +
                          'HV Bus %{customdata[0]} (%{customdata[3]:.1f} kV)<br>' +
                          'LV Bus %{customdata[1]} (%{customdata[4]:.1f} kV)<br>' +
                          'Rating: %{customdata[2]:.1f} MVA<br>' +
                          'Status: %{customdata[5]}<br>' +
                          '<extra></extra>',
            showlegend: true
        });
    }

    // Load connection edges (thin gray lines to buses)
    if (topology.loads && topology.loads.length > 0) {
        topology.loads.forEach(load => {
            const busIdx = topology.buses.metadata.findIndex(b => b.id === load.bus_id);
            if (busIdx !== -1) {
                plotData.push({
                    x: [load.x, topology.buses.x[busIdx]],
                    y: [load.y, topology.buses.y[busIdx]],
                    mode: 'lines',
                    type: 'scattergl',
                    line: {
                        color: '#6b7280',
                        width: 1,
                        dash: 'dot'
                    },
                    hoverinfo: 'skip',
                    showlegend: false
                });
            }
        });

        // Load markers (square symbols, colored by log power scale)
        const loadPowers = topology.loads.map(l => Math.abs(l.p_kw));
        
        plotData.push({
            x: topology.loads.map(l => l.x),
            y: topology.loads.map(l => l.y),
            mode: 'markers',
            type: 'scattergl',
            name: 'Loads',
            marker: {
                // Use same sqrt scaling as generators for consistency
                size: topology.loads.map(l => 10 + Math.sqrt(Math.abs(l.p_kw)) * 3),
                symbol: 'square',
                color: loadPowers.map(p => Math.log10(Math.max(p, 1))),  // Log scale
                colorscale: [[0, '#ffffff'], [0.5, '#86efac'], [1, '#22c55e']],  // White to light green to green
                cmin: 0,  // log10(1) = 0
                cmax: 3,  // log10(1000) = 3 (covers 1-1000 kW range)
                showscale: true,
                colorbar: {
                    title: 'Power (kW)',
                    thickness: 20,
                    len: 0.7,
                    x: 1.30,  // Position to right of line loading colorbar
                    y: 0.5,
                    tickvals: [0, 1, 2, 3],
                    ticktext: ['1', '10', '100', '1k']
                },
                line: {
                    color: '#ffffff',
                    width: 2
                }
            },
            text: topology.loads.map(l => l.name),
            customdata: topology.loads.map(l => [l.p_kw, l.bus_id]),
            hovertemplate: '<b>%{text}</b><br>' +
                          'Load: %{customdata[0]:.1f} kW<br>' +
                          'Bus: %{customdata[1]}<br>' +
                          '<extra></extra>',
            showlegend: true
        });
    }

    // Generator connection edges (thin gray lines to buses)
    if (topology.generators && topology.generators.length > 0) {
        topology.generators.forEach(gen => {
            const busIdx = topology.buses.metadata.findIndex(b => b.id === gen.bus_id);
            if (busIdx !== -1) {
                plotData.push({
                    x: [gen.x, topology.buses.x[busIdx]],
                    y: [gen.y, topology.buses.y[busIdx]],
                    mode: 'lines',
                    type: 'scattergl',
                    line: {
                        color: '#6b7280',
                        width: 1,
                        dash: 'dot'
                    },
                    hoverinfo: 'skip',
                    showlegend: false
                });
            }
        });

        // Generator markers (triangle symbols)
        // Size by nameplate/max power, color by current power output
        const genCurrentPowers = topology.generators.map(g => Math.abs(g.p_kw));
        
        plotData.push({
            x: topology.generators.map(g => g.x),
            y: topology.generators.map(g => g.y),
            mode: 'markers',
            type: 'scattergl',
            name: 'Generators',
            marker: {
                // Size based on nameplate/max capacity (scaled for visibility)
                size: topology.generators.map(g => 10 + Math.sqrt(Math.abs(g.max_p_kw)) * 3),
                symbol: 'triangle-up',
                // Color based on current power output
                color: genCurrentPowers.map(p => Math.log10(Math.max(p, 1))),
                colorscale: [[0, '#ffffff'], [0.5, '#86efac'], [1, '#22c55e']],
                cmin: 0,
                cmax: 3,
                showscale: false,
                line: {
                    color: '#ffffff',
                    width: 2
                }
            },
            text: topology.generators.map(g => g.name),
            customdata: topology.generators.map(g => [g.p_kw, g.max_p_kw, g.bus_id, g.type]),
            hovertemplate: '<b>%{text}</b><br>' +
                          'Output: %{customdata[0]:.1f} kW<br>' +
                          'Capacity: %{customdata[1]:.1f} kW<br>' +
                          'Bus: %{customdata[2]}<br>' +
                          'Type: %{customdata[3]}<br>' +
                          '<extra></extra>',
            showlegend: true
        });
    }

    // Storage connection edges (thin gray lines to buses)
    if (topology.storage && topology.storage.length > 0) {
        topology.storage.forEach(storage => {
            const busIdx = topology.buses.metadata.findIndex(b => b.id === storage.bus_id);
            if (busIdx !== -1) {
                plotData.push({
                    x: [storage.x, topology.buses.x[busIdx]],
                    y: [storage.y, topology.buses.y[busIdx]],
                    mode: 'lines',
                    type: 'scattergl',
                    line: {
                        color: '#6b7280',
                        width: 1,
                        dash: 'dot'
                    },
                    hoverinfo: 'skip',
                    showlegend: false
                });
            }
        });

        // Storage markers (diamond symbols)
        // Size based on current stored energy (capacity * SOC), color based on net power
        
        plotData.push({
            x: topology.storage.map(s => s.x),
            y: topology.storage.map(s => s.y),
            mode: 'markers',
            type: 'scattergl',
            name: 'Storage',
            marker: {
                // Size based on current stored energy (kWh * SOC%)
                size: topology.storage.map(s => {
                    const storedEnergy = s.max_e_kwh * s.soc_percent / 100;
                    return 15 + Math.min(30, storedEnergy / 3);
                }),
                symbol: 'diamond',
                // Color based on net power (absolute value with log scale)
                color: topology.storage.map(s => Math.log10(Math.max(Math.abs(s.p_kw), 1))),
                colorscale: [[0, '#ffffff'], [0.5, '#86efac'], [1, '#22c55e']],
                cmin: 0,
                cmax: 3,
                showscale: false,
                line: {
                    color: '#ffffff',
                    width: 2
                }
            },
            text: topology.storage.map(s => s.name),
            customdata: topology.storage.map(s => {
                const storedEnergy = s.max_e_kwh * s.soc_percent / 100;
                const status = s.p_kw > 0 ? 'Discharging' : s.p_kw < 0 ? 'Charging' : 'Idle';
                return [s.p_kw, s.bus_id, s.max_e_kwh, s.soc_percent, storedEnergy, status];
            }),
            hovertemplate: '<b>%{text}</b><br>' +
                          'Power: %{customdata[0]:.1f} kW (%{customdata[5]})<br>' +
                          'Bus: %{customdata[1]}<br>' +
                          'Stored: %{customdata[4]:.1f} kWh / %{customdata[2]:.1f} kWh<br>' +
                          'SOC: %{customdata[3]:.1f}%<br>' +
                          '<extra></extra>',
            showlegend: true
        });
    }

    // Bus data (scatter points with voltage as percentage deviation from nominal)
    // Convert voltage (pu) to percentage: 1.0 pu = 0%, 0.95 pu = -5%, 1.05 pu = +5%
    // Map to 0-100 scale where 50 = nominal (1.0 pu)
    const busVoltagePercent = state.bus_voltage.map(v => {
        // Convert pu to percentage deviation, then map to 0-100 scale
        // 0.90 pu (-10%) -> 0, 1.00 pu (0%) -> 50, 1.10 pu (+10%) -> 100
        const deviation = (v - 1.0) * 100;  // -10 to +10
        return 50 + (deviation * 5);  // Map to 0-100 scale
    });
    
    // Bus markers (without text to avoid blocking hover)
    const busTrace = {
        x: topology.buses.x,
        y: topology.buses.y,
        mode: 'markers',
        type: 'scattergl',
        name: 'Buses',
        marker: {
            size: 16,  // Increased from 12 to make easier to hover
            color: busVoltagePercent,
            colorscale: [[0, '#3b82f6'], [0.5, '#22c55e'], [1, '#ef4444']],  // Blue to green to red
            cmin: 0,
            cmax: 100,
            colorbar: {
                title: 'Bus Voltage (pu)',
                thickness: 20,
                len: 0.7,
                tickvals: [0, 25, 50, 75, 100],
                ticktext: ['0.90', '0.95', '1.00', '1.05', '1.10']
            },
            line: {
                color: '#ffffff',
                width: 1
            }
        },
        text: topology.buses.metadata.map(b => b.name),
        customdata: topology.buses.metadata.map((b, idx) => [b.vn_kv, state.bus_voltage[idx]]),
        hovertemplate: '<b>%{text}</b><br>' +
                      'Voltage: %{customdata[1]:.3f} pu<br>' +
                      'Nominal: %{customdata[0]:.1f} kV<br>' +
                      '<extra></extra>'
    };
    plotData.push(busTrace);
    
    // Bus labels (separate trace to avoid blocking hover)
    const busLabelTrace = {
        x: topology.buses.x,
        y: topology.buses.y,
        mode: 'text',
        type: 'scattergl',
        text: topology.buses.metadata.map(b => b.name),
        textposition: 'top center',
        textfont: {
            size: 10,
            color: '#ffffff'
        },
        hoverinfo: 'skip',
        showlegend: false
    };
    plotData.push(busLabelTrace);

    const layout = {
        title: {
            text: 'Power Grid Network Topology',
            font: {
                size: 18,
                color: '#ffffff'
            }
        },
        plot_bgcolor: '#1a1a1a',
        paper_bgcolor: '#1a1a1a',
        xaxis: {
            title: '',
            showgrid: true,
            gridcolor: '#404040',
            zeroline: false,
            color: '#ffffff'
        },
        yaxis: {
            title: '',
            showgrid: true,
            gridcolor: '#404040',
            zeroline: false,
            scaleanchor: 'x',
            scaleratio: 1,
            color: '#ffffff'
        },
        hovermode: 'closest',
        uirevision: 'static',  // Preserve zoom/pan
        margin: { l: 40, r: 40, t: 60, b: 40 },
        legend: {
            font: {
                color: '#ffffff'
            }
        }
    };

    const config = {
        responsive: true,
        displayModeBar: true,
        displaylogo: false,
        modeBarButtonsToRemove: ['lasso2d', 'select2d']
    };

    Plotly.newPlot('plotly-chart', plotData, layout, config);
    currentVersion = data.ver;

    console.log('Plot initialized with version', currentVersion);
}

// Update plot with new data
function updatePlot(data) {
    const state = data.state;
    const topology = data.topology;

    const numLines = topology.lines.length;
    const numTransformers = (topology.transformers || []).length;
    const numLoads = (topology.loads || []).length;
    const numGenerators = (topology.generators || []).length;
    const numStorage = (topology.storage || []).length;

    // Calculate bus trace index
    // Trace order: line-visuals (N), line-midpoints (N), line-loading-legend (1), transformer-visuals (N), transformer-markers (1), load-edges, load-markers, gen-edges, gen-markers, storage-edges, storage-markers, buses, bus-labels
    let traceIdx = numLines * 2;  // Each line has 2 traces (visual + midpoint)
    traceIdx += 1;  // Line loading legend trace
    traceIdx += numTransformers;  // Transformer line visuals (one per transformer)
    if (numTransformers > 0) traceIdx += 1;  // Single transformer marker trace for legend
    if (numLoads > 0) traceIdx += numLoads + 1;  // edges + marker trace
    if (numGenerators > 0) traceIdx += numGenerators + 1;  // edges + marker trace
    if (numStorage > 0) traceIdx += numStorage + 1;  // edges + marker trace
    const busTraceIdx = traceIdx;

    // Update bus voltages
    const busVoltagePercent = state.bus_voltage.map(v => {
        const deviation = (v - 1.0) * 100;
        return 50 + (deviation * 5);
    });
    
    Plotly.restyle('plotly-chart', {
        'marker.color': [busVoltagePercent]
    }, [busTraceIdx]);

    // Update line status and loading
    for (let i = 0; i < numLines; i++) {
        const isOpen = state.line_status[i];
        const loading = state.line_loading[i];

        // Calculate color based on loading percentage (green -> yellow -> red)
        let lineColor;
        if (!isOpen) {
            lineColor = '#ef4444';
        } else if (loading < 50) {
            const t = loading / 50;
            lineColor = `rgba(${Math.round(34 + (251-34)*t)}, ${Math.round(197 + (191-197)*t)}, ${Math.round(94 + (36-94)*t)}, 0.8)`;
        } else {
            const t = (loading - 50) / 50;
            lineColor = `rgba(${Math.round(251 + (239-251)*t)}, ${Math.round(191 + (68-191)*t)}, ${Math.round(36 + (68-36)*t)}, 0.8)`;
        }

        // Update line visual trace (every 2*i)
        Plotly.restyle('plotly-chart', {
            'line.color': lineColor,
            'line.width': isOpen ? 2 : 1,
            'line.dash': isOpen ? 'solid' : 'dash'
        }, [i * 2]);

        // Update line midpoint marker trace (every 2*i + 1)
        Plotly.restyle('plotly-chart', {
            'marker.color': [lineColor],  // Use same color as line visual
            'customdata': [[[loading, topology.lines[i].from_bus, topology.lines[i].to_bus, isOpen]]]
        }, [i * 2 + 1]);
    }

    currentVersion = data.ver;
    console.log('Plot updated, version', currentVersion);
}

// Update status display
function updateStatus(data) {
    const state = data.state;
    const topology = data.topology;

    // Simulation time
    document.getElementById('sim-time').textContent =
        `${state.sim_time.toFixed(1)}s`;

    // Average voltage
    const avgVoltage = state.bus_voltage.reduce((a, b) => a + b, 0) / state.bus_voltage.length;
    document.getElementById('avg-voltage').textContent =
        `${avgVoltage.toFixed(3)} pu`;

    // Convergence status
    const convergedEl = document.getElementById('converged');
    convergedEl.textContent = state.converged ? 'Yes' : 'No';
    convergedEl.style.color = state.converged ? '#4ade80' : '#f87171';

    // Connection status
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    statusDot.className = 'status-dot';
    statusText.textContent = 'Connected';

    // Power totals
    // Total load
    const totalLoad = topology.loads ? topology.loads.reduce((sum, load) => sum + Math.abs(load.p_kw), 0) : 0;
    document.getElementById('total-load').textContent = `${totalLoad.toFixed(0)} kW`;

    // Total Grid (non-DER) generation (includes base generators with type 'wye' or 'Grid')
    const totalGrid = topology.generators ? 
        topology.generators.filter(g => !g.type || g.type === 'Grid' || g.type === 'wye').reduce((sum, gen) => sum + gen.p_kw, 0) : 0;
    document.getElementById('total-grid').textContent = `${totalGrid.toFixed(0)} kW`;

    // Total PV generation
    const totalPV = topology.generators ? 
        topology.generators.filter(g => g.type === 'PV').reduce((sum, gen) => sum + gen.p_kw, 0) : 0;
    document.getElementById('total-pv').textContent = `${totalPV.toFixed(0)} kW`;

    // Total Wind generation
    const totalWind = topology.generators ? 
        topology.generators.filter(g => g.type === 'Wind').reduce((sum, gen) => sum + gen.p_kw, 0) : 0;
    document.getElementById('total-wind').textContent = `${totalWind.toFixed(0)} kW`;

    // Total BESS (positive = discharging, negative = charging)
    const totalBESS = topology.storage ? 
        topology.storage.reduce((sum, s) => sum + s.p_kw, 0) : 0;
    const bessEl = document.getElementById('total-bess');
    bessEl.textContent = `${totalBESS.toFixed(0)} kW`;
    // Color code: green for charging (negative), red for discharging (positive)
    bessEl.style.color = totalBESS > 0 ? '#4ade80' : totalBESS < 0 ? '#f87171' : '#ffffff';
}

// Fetch and process state data
async function fetchState() {
    try {
        const response = await fetch('/state');

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        // Check if this is first load or topology changed
        if (currentVersion === -1) {
            initializePlot(data);
        } else if (data.ver !== currentVersion) {
            console.log('Topology version changed, reinitializing plot');
            initializePlot(data);
        } else {
            updatePlot(data);
        }

        updateStatus(data);

        // Reset retry state
        if (retrying) {
            retrying = false;
            console.log('Reconnected successfully');
        }

        // Schedule next poll
        pollTimer = setTimeout(fetchState, POLL_INTERVAL);

    } catch (error) {
        console.error('Error fetching state:', error);

        // Update status to show error
        const statusDot = document.getElementById('status-dot');
        const statusText = document.getElementById('status-text');
        statusDot.className = 'status-dot error';
        statusText.textContent = 'Connection Error';

        // Retry after shorter interval
        retrying = true;
        pollTimer = setTimeout(fetchState, RETRY_INTERVAL);
    }
}

// Start polling when page loads
window.addEventListener('load', () => {
    console.log('Starting grid visualization...');
    fetchState();
});

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (pollTimer) {
        clearTimeout(pollTimer);
    }
});