// Configuration
const POLL_INTERVAL = 5000; // 5 seconds
const RETRY_INTERVAL = 2000; // 2 seconds on error

// Simulation wall-clock epoch: Monday 08:00.  sim_time (seconds) is
// scaled by SIM_TIME_SCALE and added to this to produce a day-of-week /
// time-of-day display.
const SIM_START_EPOCH = new Date(2025, 0, 6, 8, 0, 0); // Mon Jan 6 2025 08:00
const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

// Scenario seconds per sim_time second.  60 → each real second advances
// the scenario clock by 1 minute, so a 30-min session covers ~30 hours
// of grid operations (just over one full day).
const SIM_TIME_SCALE = 60;

function simClock(simTimeSeconds) {
    const scenarioMs = simTimeSeconds * SIM_TIME_SCALE * 1000;
    const d = new Date(SIM_START_EPOCH.getTime() + scenarioMs);
    const day = DAY_NAMES[d.getDay()];
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${day} ${hh}:${mm}`;
}

// State
let currentVersion = -1;
let pollTimer = null;
let retrying = false;

/**
 * Map line loading % to a display color.
 *
 * Thresholds aligned with NERC FAC-008/FAC-009 facility rating practice:
 *   0 %  → white  (#ffffff): unloaded, visually quiet
 *  75 %  → green  (#22c55e): NERC N-1 contingency planning limit
 * 100 %  → yellow (#fbbf24): NormAmps continuous thermal rating
 * 115 %  → orange (#f97316): EmergAmps short-term emergency rating
 * 125 %+ → red    (#ef4444): beyond emergency rating, critical
 *
 * Open (tripped) lines always display red regardless of loading.
 */
function lineLoadColor(loading, isOpen) {
    if (!isOpen) return '#ef4444';  // Tripped/open line

    // Colour stops: [loading threshold, [R, G, B]]
    const stops = [
        [  0, [255, 255, 255]],   // white
        [ 75, [ 34, 197,  94]],   // green  #22c55e  — NERC N-1 planning limit
        [100, [251, 191,  36]],   // yellow #fbbf24  — NormAmps thermal limit
        [115, [249, 115,  22]],   // orange #f97316  — EmergAmps limit
        [125, [239,  68,  68]],   // red    #ef4444  — beyond emergency rating
    ];

    // Find which segment loading falls in
    let lo = stops[0], hi = stops[stops.length - 1];
    for (let i = 0; i < stops.length - 1; i++) {
        if (loading <= stops[i + 1][0]) {
            lo = stops[i];
            hi = stops[i + 1];
            break;
        }
    }

    const t = lo[0] === hi[0] ? 1 : (loading - lo[0]) / (hi[0] - lo[0]);
    const r = Math.round(lo[1][0] + (hi[1][0] - lo[1][0]) * t);
    const g = Math.round(lo[1][1] + (hi[1][1] - lo[1][1]) * t);
    const b = Math.round(lo[1][2] + (hi[1][2] - lo[1][2]) * t);
    return `rgba(${r}, ${g}, ${b}, 0.9)`;
}

// ── Symlog color scale helpers ────────────────────────────────────────────────
//
// A pure log scale breaks at zero and can't represent negative values.
// symlog(x) = sign(x) * log(1 + |x| / T) is linear within ±T kW and
// log-compressed beyond it, keeping zero at centre and supporting both signs.
// This makes small loads/generators visually distinct even when a large unit
// dominates the range.
//
const SYMLOG_THRESHOLD = 10;  // kW — linear region ±10 kW, log above

function symlog(x) {
    return Math.sign(x) * Math.log1p(Math.abs(x) / SYMLOG_THRESHOLD);
}

/**
 * Return Plotly tickvals/ticktext for an asymmetric symlog colorbar.
 * minKw/maxKw are raw kW bounds of the data (not symlog-transformed).
 * tickvals are in symlog space; ticktext shows original kW values.
 */
function symlogTicks(minKw, maxKw) {
    const candidates = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];
    const kwVals = new Set([0]);
    for (const v of candidates) {
        if ( v <= maxKw * 1.05) kwVals.add( v);
        if (-v >= minKw * 1.05) kwVals.add(-v);
    }
    const sorted = [...kwVals].sort((a, b) => a - b);
    return {
        tickvals: sorted.map(v => symlog(v)),
        ticktext: sorted.map(v => v === 0 ? '0' : `${v > 0 ? '+' : ''}${v}`)
    };
}

/**
 * Compute Plotly {colorscale, cmin, cmax} for a symlog-transformed power axis.
 * White is anchored at 0 kW; the colorscale stop is placed at the correct
 * normalized position so purple→white→green maps correctly even when
 * cmin ≠ -cmax (asymmetric scale).
 *
 *   minKw ≤ 0, maxKw ≥ 0  → full diverging purple–white–green
 *   minKw = 0              → white–green only (generators, always positive)
 *   maxKw = 0              → purple–white only (loads, always consuming)
 */
function symlogColorscale(minKw, maxKw) {
    // Guard against zero-range
    const sMin = symlog(minKw === maxKw ? minKw - 1 : minKw);
    const sMax = symlog(minKw === maxKw ? maxKw + 1 : maxKw);
    const whitePos = Math.max(0, Math.min(1, (0 - sMin) / (sMax - sMin)));
    let colorscale;
    if (whitePos < 0.001) {
        colorscale = [[0, '#ffffff'], [1, '#22c55e']];           // all positive
    } else if (whitePos > 0.999) {
        colorscale = [[0, '#a855f7'], [1, '#ffffff']];           // all negative
    } else {
        colorscale = [[0, '#a855f7'], [whitePos, '#ffffff'], [1, '#22c55e']];
    }
    return { colorscale, cmin: sMin, cmax: sMax };
}
// ─────────────────────────────────────────────────────────────────────────────

// Initialize Plotly chart
function initializePlot(data) {
    const topology = data.topology;
    const state = data.state;

    const plotData = [];

    // Consolidate line segments by NERC loading band — one trace per band
    // gives per-segment color while keeping total trace count low (~6 bands).
    // scattergl doesn't support per-segment colors in a single trace.
    // Three size tiers:
    //   Huge  (IEEE 8500, 4000+ lines): tiny markers to avoid total overlap
    //   Large (CyDERMS composite, 100-1500 lines): medium markers, legible
    //   Small (Baran-Wu 33, <100 lines): full-size markers with labels
    const isHugeModel = topology.lines.length > 1500;
    const isLargeModel = topology.lines.length > 100;

    // Build bus ID → name lookup for hover tooltips
    const busIdToName = {};
    if (topology.buses && topology.buses.metadata) {
        topology.buses.metadata.forEach(b => { busIdToName[b.id] = b.name; });
    }

    const bands = [
        { max:  75, color: 'rgba(34, 197, 94, 0.7)',  label: '< 75%' },
        { max: 100, color: 'rgba(251, 191, 36, 0.8)', label: '75-100%' },
        { max: 115, color: 'rgba(249, 115, 22, 0.9)', label: '100-115%' },
        { max: 999, color: 'rgba(239, 68, 68, 0.9)',  label: '> 115%' },
        { max: -1,  color: '#ffffff',                   label: 'Unloaded' },
    ];
    const trippedBand = { color: '#ef4444', label: 'Tripped' };

    // Bucket line segments and midpoints by band
    const bandData = bands.map(() => ({ lx: [], ly: [], mx: [], my: [], text: [], custom: [] }));
    const trippedData = { lx: [], ly: [], mx: [], my: [], text: [], custom: [] };

    topology.lines.forEach((line, idx) => {
        const isOpen = state.line_status[idx];
        const loading = state.line_loading[idx] || 0;
        const fromName = busIdToName[line.from_bus] || line.from_bus;
        const toName = busIdToName[line.to_bus] || line.to_bus;
        const midpoint = [(line.x[0] + line.x[1]) / 2, (line.y[0] + line.y[1]) / 2];

        let bucket;
        if (!isOpen) {
            bucket = trippedData;
        } else if (loading <= 0.01) {
            bucket = bandData[4]; // Unloaded
        } else {
            for (let i = 0; i < 4; i++) {
                if (loading <= bands[i].max) { bucket = bandData[i]; break; }
            }
            if (!bucket) bucket = bandData[3];
        }

        bucket.lx.push(line.x[0], line.x[1], null);
        bucket.ly.push(line.y[0], line.y[1], null);
        bucket.mx.push(midpoint[0]);
        bucket.my.push(midpoint[1]);
        bucket.text.push(`Line ${line.line_id}`);
        bucket.custom.push([loading.toFixed(1), fromName, toName, isOpen ? 'Closed' : 'Open']);
    });

    // Emit one line trace + one marker trace per band
    bands.forEach((band, i) => {
        const bd = bandData[i];
        if (bd.lx.length === 0) return;
        plotData.push({
            x: bd.lx, y: bd.ly, mode: 'lines', type: 'scattergl',
            line: { color: band.color, width: 1 },
            hoverinfo: 'skip', showlegend: false
        });
        plotData.push({
            x: bd.mx, y: bd.my, mode: 'markers', type: 'scatter',
            name: band.label, marker: { size: isHugeModel ? 3 : isLargeModel ? 5 : 8, color: band.color },
            text: bd.text, customdata: bd.custom,
            hovertemplate: '<b>%{text}</b><br>%{customdata[1]} → %{customdata[2]}<br>' +
                          'Status: %{customdata[3]}<br>Loading: %{customdata[0]}%<extra></extra>',
            showlegend: false
        });
    });
    if (trippedData.lx.length > 0) {
        plotData.push({
            x: trippedData.lx, y: trippedData.ly, mode: 'lines', type: 'scattergl',
            line: { color: trippedBand.color, width: 2, dash: 'dash' },
            hoverinfo: 'skip', showlegend: false
        });
        plotData.push({
            x: trippedData.mx, y: trippedData.my, mode: 'markers', type: 'scatter',
            name: 'Tripped', marker: { size: isHugeModel ? 5 : isLargeModel ? 7 : 10, color: trippedBand.color },
            text: trippedData.text, customdata: trippedData.custom,
            hovertemplate: '<b>%{text}</b><br>%{customdata[1]} → %{customdata[2]}<br>' +
                          'Status: OPEN<br>Loading: %{customdata[0]}%<extra></extra>',
            showlegend: false
        });
    }

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
            // Colorscale normalized over cmin=0 / cmax=125.
            // Stops at NERC FAC-008 thresholds: 75%=N-1 limit, 100%=NormAmps, 115%=EmergAmps.
            colorscale: [
                [0/125,   '#ffffff'],   // 0 %   white
                [75/125,  '#22c55e'],   // 75 %  green  — N-1 planning limit
                [100/125, '#fbbf24'],   // 100 % yellow — NormAmps
                [115/125, '#f97316'],   // 115 % orange — EmergAmps
                [125/125, '#ef4444'],   // 125 % red    — beyond emergency
            ],
            cmin: 0,
            cmax: 125,
            showscale: true,
            colorbar: {
                title: 'Line Loading (%)',
                thickness: 20,
                len: 0.7,
                x: 1.15,
                y: 0.5,
                tickvals: [0, 75, 100, 115, 125],
                ticktext: ['0%', '75%', '100%', '115%', '125%']
            }
        },
        hoverinfo: 'skip',
        showlegend: false
    });

    // Transformer connections — split into power transformers vs voltage regulators.
    // Detection: regulators have hv_kv ≈ lv_kv (same-kV trafos with tap changers).
    if (topology.transformers && topology.transformers.length > 0) {
        // Separate power transformers from voltage regulators
        const powerTrafos = [];
        const regulators = [];
        topology.transformers.forEach(trafo => {
            const kvRatio = Math.abs(trafo.hv_kv - trafo.lv_kv) / Math.max(trafo.hv_kv, trafo.lv_kv);
            if (kvRatio < 0.01 && trafo.tap_position !== null && trafo.tap_position !== undefined) {
                regulators.push(trafo);
            } else {
                powerTrafos.push(trafo);
            }
        });

        // Power transformer traces (yellow hexagon — unchanged)
        if (powerTrafos.length > 0) {
            const ptLineX = [];
            const ptLineY = [];
            const ptMidX = [];
            const ptMidY = [];
            const ptText = [];
            const ptCustom = [];

            powerTrafos.forEach(trafo => {
                ptLineX.push(trafo.x[0], trafo.x[1], null);
                ptLineY.push(trafo.y[0], trafo.y[1], null);
                ptMidX.push((trafo.x[0] + trafo.x[1]) / 2);
                ptMidY.push((trafo.y[0] + trafo.y[1]) / 2);
                ptText.push(trafo.name);
                ptCustom.push([trafo.hv_bus, trafo.lv_bus, trafo.rated_mva, trafo.hv_kv, trafo.lv_kv, trafo.in_service]);
            });

            plotData.push({
                x: ptLineX, y: ptLineY,
                mode: 'lines', type: 'scattergl',
                line: { color: '#fbbf24', width: isHugeModel ? 1 : isLargeModel ? 2 : 4 },
                hoverinfo: 'skip', showlegend: false
            });
            plotData.push({
                x: ptMidX, y: ptMidY,
                mode: 'markers', type: 'scatter',
                name: 'Transformers',
                marker: {
                    size: isHugeModel ? 3 : isLargeModel ? 8 : 12,
                    symbol: 'hexagon',
                    color: '#fbbf24',
                    line: { color: '#ffffff', width: isHugeModel ? 0 : 2 }
                },
                text: ptText, customdata: ptCustom,
                hovertemplate: '<b>%{text}</b><br>' +
                              'HV Bus %{customdata[0]} (%{customdata[3]:.1f} kV)<br>' +
                              'LV Bus %{customdata[1]} (%{customdata[4]:.1f} kV)<br>' +
                              'Rating: %{customdata[2]:.1f} MVA<br>' +
                              'Status: %{customdata[5]}<br>' +
                              '<extra></extra>',
                showlegend: true
            });
        }

        // Voltage regulator traces (orange diamond with tap info)
        if (regulators.length > 0) {
            const regLineX = [];
            const regLineY = [];
            const regMidX = [];
            const regMidY = [];
            const regText = [];
            const regCustom = [];

            regulators.forEach(trafo => {
                regLineX.push(trafo.x[0], trafo.x[1], null);
                regLineY.push(trafo.y[0], trafo.y[1], null);
                regMidX.push((trafo.x[0] + trafo.x[1]) / 2);
                regMidY.push((trafo.y[0] + trafo.y[1]) / 2);
                regText.push(trafo.name);
                regCustom.push([trafo.hv_bus, trafo.lv_bus, trafo.rated_mva,
                               trafo.hv_kv, trafo.lv_kv, trafo.in_service,
                               trafo.tap_position, trafo.tap_min, trafo.tap_max]);
            });

            plotData.push({
                x: regLineX, y: regLineY,
                mode: 'lines', type: 'scattergl',
                line: { color: '#f97316', width: isHugeModel ? 1 : isLargeModel ? 2 : 3, dash: 'dash' },
                hoverinfo: 'skip', showlegend: false
            });
            plotData.push({
                x: regMidX, y: regMidY,
                mode: 'markers', type: 'scatter',
                name: 'Regulators',
                marker: {
                    size: isHugeModel ? 5 : isLargeModel ? 10 : 14,
                    symbol: 'diamond',
                    color: '#f97316',
                    line: { color: '#ffffff', width: isHugeModel ? 0 : 2 }
                },
                text: regText, customdata: regCustom,
                hovertemplate: '<b>%{text}</b> (Regulator)<br>' +
                              'HV Bus %{customdata[0]} (%{customdata[3]:.1f} kV)<br>' +
                              'LV Bus %{customdata[1]} (%{customdata[4]:.1f} kV)<br>' +
                              'Rating: %{customdata[2]:.1f} MVA<br>' +
                              'Tap: %{customdata[6]} [%{customdata[7]}..%{customdata[8]}]<br>' +
                              'Status: %{customdata[5]}<br>' +
                              '<extra></extra>',
                showlegend: true
            });
        }
    }

    // Shunt capacitors/reactors — square markers at bus position
    if (topology.shunts && topology.shunts.length > 0) {
        // Connection edges (thin lines to bus)
        const shuntEdgeX = [];
        const shuntEdgeY = [];
        topology.shunts.forEach(shunt => {
            const busIdx = topology.buses.metadata.findIndex(b => b.id === shunt.bus_id);
            if (busIdx !== -1) {
                shuntEdgeX.push(shunt.x, topology.buses.x[busIdx], null);
                shuntEdgeY.push(shunt.y, topology.buses.y[busIdx], null);
            }
        });
        if (shuntEdgeX.length > 0) {
            plotData.push({
                x: shuntEdgeX, y: shuntEdgeY,
                mode: 'lines', type: 'scattergl',
                line: { color: '#6b7280', width: 1, dash: 'dot' },
                hoverinfo: 'skip', showlegend: false
            });
        }

        plotData.push({
            x: topology.shunts.map(s => s.x),
            y: topology.shunts.map(s => s.y),
            mode: 'markers', type: 'scatter',
            name: 'Shunt Caps',
            marker: {
                size: isHugeModel ? 4 : isLargeModel ? 7 : 10,
                symbol: 'square',
                color: '#06b6d4',  // Cyan/teal
                line: { color: '#ffffff', width: isHugeModel ? 0 : 1 }
            },
            text: topology.shunts.map(s => s.name),
            customdata: topology.shunts.map(s => [s.q_mvar * 1000, s.bus_id, s.in_service]),
            hovertemplate: '<b>%{text}</b><br>' +
                          'Q: %{customdata[0]:.0f} kVAr<br>' +
                          'Bus: %{customdata[1]}<br>' +
                          'In Service: %{customdata[2]}<br>' +
                          '<extra></extra>',
            showlegend: true
        });
    }

    // Switches — short line segments between bus pairs
    if (topology.switches && topology.switches.length > 0) {
        const closedX = [];
        const closedY = [];
        const closedText = [];
        const closedCustom = [];
        const openX = [];
        const openY = [];
        const openText = [];
        const openCustom = [];

        topology.switches.forEach(sw => {
            // Midpoint for marker placement
            const midX = (sw.x[0] + sw.x[1]) / 2;
            const midY = (sw.y[0] + sw.y[1]) / 2;

            if (sw.closed) {
                closedX.push(sw.x[0], sw.x[1], null);
                closedY.push(sw.y[0], sw.y[1], null);
                closedText.push(sw.name);
                closedCustom.push([sw.bus, sw.element, sw.switch_type, 'Closed']);
            } else {
                openX.push(sw.x[0], sw.x[1], null);
                openY.push(sw.y[0], sw.y[1], null);
                openText.push(sw.name);
                openCustom.push([sw.bus, sw.element, sw.switch_type, 'Open']);
            }
        });

        // Closed switches — solid green segments
        if (closedX.length > 0) {
            plotData.push({
                x: closedX, y: closedY,
                mode: 'lines', type: 'scattergl',
                name: 'Switches (Closed)',
                line: { color: '#22c55e', width: isHugeModel ? 2 : isLargeModel ? 3 : 5 },
                hoverinfo: 'skip',
                showlegend: true
            });
        }

        // Open switches — dashed red segments
        if (openX.length > 0) {
            plotData.push({
                x: openX, y: openY,
                mode: 'lines', type: 'scattergl',
                name: 'Switches (Open)',
                line: { color: '#ef4444', width: isHugeModel ? 2 : isLargeModel ? 3 : 5, dash: 'dash' },
                hoverinfo: 'skip',
                showlegend: true
            });
        }

        // Switch midpoint markers for hover info
        const allMidX = [];
        const allMidY = [];
        const allSwText = [];
        const allSwCustom = [];
        const allSwColors = [];
        topology.switches.forEach(sw => {
            allMidX.push((sw.x[0] + sw.x[1]) / 2);
            allMidY.push((sw.y[0] + sw.y[1]) / 2);
            allSwText.push(sw.name);
            allSwCustom.push([sw.bus, sw.element, sw.switch_type, sw.closed ? 'Closed' : 'Open']);
            allSwColors.push(sw.closed ? '#22c55e' : '#ef4444');
        });

        plotData.push({
            x: allMidX, y: allMidY,
            mode: 'markers', type: 'scatter',
            name: 'Switch Status',
            marker: {
                size: isHugeModel ? 4 : isLargeModel ? 6 : 8,
                symbol: 'x',
                color: allSwColors,
                line: { color: '#ffffff', width: 1 }
            },
            text: allSwText, customdata: allSwCustom,
            hovertemplate: '<b>%{text}</b><br>' +
                          'Bus %{customdata[0]} ↔ Bus %{customdata[1]}<br>' +
                          'Type: %{customdata[2]}<br>' +
                          'Status: %{customdata[3]}<br>' +
                          '<extra></extra>',
            showlegend: false
        });
    }

    // Shared power color scale (loads + generators + storage).
    // White anchored at 0 kW; cmin = symlog of max consumption (negative),
    // cmax = symlog of max generation (positive). Asymmetric around white.
    const _allNegKw  = [
        ...(topology.loads      || []).map(l => -l.p_kw),
        ...(topology.storage    || []).map(s =>  s.p_kw).filter(p => p < 0),
    ];
    const _allPosKw  = [
        ...(topology.generators || []).map(g =>  g.p_kw),
        ...(topology.storage    || []).map(s =>  s.p_kw).filter(p => p > 0),
    ];
    const powerMinKw     = Math.min(..._allNegKw, -1);
    const powerMaxKw     = Math.max(..._allPosKw,  1);
    const sharedPowerCs  = symlogColorscale(powerMinKw, powerMaxKw);
    const sharedPowerTicks = symlogTicks(powerMinKw, powerMaxKw);

    // Load connection edges (thin gray lines to buses) — consolidated into one trace
    if (topology.loads && topology.loads.length > 0) {
        const loadEdgeX = [];
        const loadEdgeY = [];
        topology.loads.forEach(load => {
            const busIdx = topology.buses.metadata.findIndex(b => b.id === load.bus_id);
            if (busIdx !== -1) {
                loadEdgeX.push(load.x, topology.buses.x[busIdx], null);
                loadEdgeY.push(load.y, topology.buses.y[busIdx], null);
            }
        });

        // Single trace for all load connection edges
        if (loadEdgeX.length > 0) {
            plotData.push({
                x: loadEdgeX,
                y: loadEdgeY,
                mode: 'lines',
                type: 'scattergl',
                line: { color: '#6b7280', width: 1, dash: 'dot' },
                hoverinfo: 'skip',
                showlegend: false
            });
        }

        // Load markers (inverted triangle symbols, colored by drain intensity)
        // Negate p_kw so consumption maps to the purple side of the shared diverging scale.
        const loadPowers = topology.loads.map(l => symlog(-l.p_kw));

        plotData.push({
            x: topology.loads.map(l => l.x),
            y: topology.loads.map(l => l.y),
            mode: 'markers',
            type: 'scatter',
            name: 'Loads',
            marker: {
                size: topology.loads.map(l => isHugeModel ? 3 : isLargeModel ? 5 + Math.log10(Math.max(Math.abs(l.p_kw), 1)) * 3.5 : 8 + Math.log10(Math.max(Math.abs(l.p_kw), 1)) * 5),
                symbol: 'triangle-down',
                color: loadPowers,
                colorscale: sharedPowerCs.colorscale,
                cmin: sharedPowerCs.cmin,
                cmax: sharedPowerCs.cmax,
                showscale: true,
                colorbar: {
                    title: 'Power (kW)',
                    thickness: 20,
                    len: 0.7,
                    x: 1.30,
                    y: 0.5,
                    tickvals: sharedPowerTicks.tickvals,
                    ticktext: sharedPowerTicks.ticktext,
                },
                line: {
                    color: '#ffffff',
                    width: 2
                }
            },
            text: topology.loads.map(l => l.name),
            customdata: topology.loads.map(l => [l.p_kw, busIdToName[l.bus_id] || l.bus_id]),
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
        // Size by nameplate/max power, color by current power output on shared scale.
        const genPowers = topology.generators.map(g => symlog(g.p_kw));

        plotData.push({
            x: topology.generators.map(g => g.x),
            y: topology.generators.map(g => g.y),
            mode: 'markers',
            type: 'scatter',
            name: 'Generators',
            marker: {
                size: topology.generators.map(g => isHugeModel ? 8 : isLargeModel ? 5 + Math.log10(Math.max(Math.abs(g.max_p_kw), 1)) * 3.5 : 8 + Math.log10(Math.max(Math.abs(g.max_p_kw), 1)) * 5),
                symbol: 'triangle-up',
                color: genPowers,
                colorscale: sharedPowerCs.colorscale,
                cmin: sharedPowerCs.cmin,
                cmax: sharedPowerCs.cmax,
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
        // Size based on current stored energy (capacity * SOC), color on shared power scale.
        const storRawPowers = topology.storage.map(s => s.p_kw);
        const storPowers    = storRawPowers.map(p => symlog(p));

        plotData.push({
            x: topology.storage.map(s => s.x),
            y: topology.storage.map(s => s.y),
            mode: 'markers',
            type: 'scatter',
            name: 'Storage',
            marker: {
                // Size based on current stored energy (kWh * SOC%)
                size: topology.storage.map(s => {
                    const storedEnergy = s.max_e_kwh * s.soc_percent / 100;
                    return 15 + Math.min(30, storedEnergy / 3);
                }),
                symbol: 'diamond',
                // Color: purple=charging (drain), white=idle, green=discharging (supply)
                color: storPowers,
                colorscale: sharedPowerCs.colorscale,
                cmin: sharedPowerCs.cmin,
                cmax: sharedPowerCs.cmax,
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
        type: 'scatter',
        name: 'Buses',
        marker: {
            size: isHugeModel ? 4 : isLargeModel ? 8 : 16,
            color: busVoltagePercent,
            // Diverging scale centred on nominal (1.0 pu).
            // Stops align with ANSI C84.1 limits: ±5% = teal/orange, ±10% = brown/red.
            // Cool (teal) for undervoltage warning, warm (orange/red) for overvoltage —
            // thermally opposite sides prevent yellow/orange muddle.
            colorscale: [[0, '#92400e'], [0.25, '#06b6d4'], [0.5, '#22c55e'], [0.75, '#f97316'], [1, '#ef4444']],
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
    
    // Bus labels — skip for models with >100 buses (labels overlap and slow rendering)
    if (topology.buses.metadata.length <= 100) {
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
    }

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

    // Use Plotly.react for updates — preserves zoom/pan state.
    // Falls back to newPlot on first render (no existing chart).
    Plotly.react('plotly-chart', plotData, layout, config);
    currentVersion = data.ver;

    console.log('Plot initialized with version', currentVersion);
}

// Update plot with new data
function updatePlot(data) {
    const state = data.state;
    const topology = data.topology;

    // With consolidated traces, the simplest correct approach for updates
    // is to re-render the full plot. Plotly.react() is efficient — it diffs
    // the data and only redraws what changed.
    initializePlot(data);
    currentVersion = data.ver;
}

// Update status display
function updateStatus(data) {
    const state = data.state;
    const topology = data.topology;

    // Simulation wall-clock (day-of-week + time-of-day)
    document.getElementById('sim-clock').textContent =
        simClock(state.sim_time);

    // Simulation time (seconds elapsed)
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

    // topology.generators[i].p_kw = Pmpp (rated setpoint) from topology — same source
    // used by individual node display.  Both totals and nodes read this unified value.

    // Ext_grid import: positive = grid supplying local load,
    // negative = local generation exporting to grid.
    const extGridMW = state.ext_grid_import_mw || 0;
    const extGridLabel = extGridMW >= 0
        ? `${(extGridMW * 1000).toFixed(0)} kW import`
        : `${(-extGridMW * 1000).toFixed(0)} kW export`;
    document.getElementById('total-grid').textContent = extGridLabel;

    // Total PV generation - DERType.SOLAR_PV = 'solar_pv'
    const totalPV = (topology.generators || []).reduce(
        (sum, gen) => gen.type === 'solar_pv' ? sum + gen.p_kw : sum, 0);
    document.getElementById('total-pv').textContent = `${totalPV.toFixed(0)} kW`;

    // Total Wind generation - DERType.WIND = 'wind'
    const totalWind = (topology.generators || []).reduce(
        (sum, gen) => gen.type === 'wind' ? sum + gen.p_kw : sum, 0);
    document.getElementById('total-wind').textContent = `${totalWind.toFixed(0)} kW`;

    // Total BESS (positive = discharging, negative = charging)
    const totalBESS = topology.storage ? 
        topology.storage.reduce((sum, s) => sum + s.p_kw, 0) : 0;
    const bessEl = document.getElementById('total-bess');
    bessEl.textContent = `${totalBESS.toFixed(0)} kW`;
    // Color code: green for charging (negative), red for discharging (positive)
    // positive = discharging (helping supply load) → green; negative = charging → amber
    bessEl.style.color = totalBESS > 0 ? '#4ade80' : totalBESS < 0 ? '#fbbf24' : '#ffffff';
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