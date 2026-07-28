// Control Panel JavaScript
// Handles HMI/SCADA control operations with Zero-Trust authorization

// Operation log
const operationLog = [];
const MAX_LOG_ENTRIES = 50;

// Get current user and role
function getCurrentUser() {
    return document.getElementById('current-user').textContent;
}

function getCurrentRole() {
    return document.getElementById('user-role').value;
}

// Update authorization status display
function updateAuthStatus(authData) {
    const decision = authData.decision;
    const latency = authData.latency_ms;
    const reason = authData.reason || 'N/A';
    
    // Update decision
    const decisionEl = document.getElementById('auth-decision');
    decisionEl.textContent = decision.toUpperCase();
    decisionEl.className = `value ${decision}`;
    
    // Update latency
    const latencyEl = document.getElementById('auth-latency');
    latencyEl.textContent = `${latency.toFixed(2)} ms`;
    
    // Color code latency
    if (latency < 10) {
        latencyEl.style.color = '#22c55e'; // Green
    } else if (latency < 50) {
        latencyEl.style.color = '#fbbf24'; // Yellow
    } else {
        latencyEl.style.color = '#ef4444'; // Red
    }
    
    // Update reason
    document.getElementById('auth-reason').textContent = reason;
    
    // Update layer (if available)
    const layer = authData.layer || 'central';
    document.getElementById('auth-layer').textContent = layer;
}

// Show operation result
function showOperationResult(elementId, success, message) {
    const resultEl = document.getElementById(elementId);
    resultEl.className = `operation-result show ${success ? 'success' : 'error'}`;
    resultEl.innerHTML = `<pre>${message}</pre>`;
}

// Add log entry
function addLogEntry(operation, success, details) {
    const timestamp = new Date().toLocaleTimeString();
    
    const entry = {
        timestamp,
        operation,
        success,
        details
    };
    
    operationLog.unshift(entry);
    
    // Limit log size
    if (operationLog.length > MAX_LOG_ENTRIES) {
        operationLog.pop();
    }
    
    // Update log display
    updateLogDisplay();
}

// Update log display
function updateLogDisplay() {
    const logContainer = document.getElementById('log-entries');
    
    if (operationLog.length === 0) {
        logContainer.innerHTML = '<div style="color: #9ca3af; text-align: center; padding: 20px;">No operations yet</div>';
        return;
    }
    
    logContainer.innerHTML = operationLog.map(entry => `
        <div class="log-entry ${entry.success ? 'success' : 'error'}">
            <div class="timestamp">${entry.timestamp}</div>
            <div class="operation">${entry.operation}</div>
            <div class="details">${entry.details}</div>
        </div>
    `).join('');
}

// Read measurements
async function readMeasurements() {
    const btn = document.getElementById('btn-read-measurements');
    btn.disabled = true;
    
    try {
        const userId = getCurrentUser();
        const userRole = getCurrentRole();
        
        const response = await fetch(`/api/measurements?user_id=${userId}&user_roles=${userRole}`);
        const data = await response.json();
        
        if (data.success) {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show measurements
            const numBuses = Object.keys(data.measurements.buses).length;
            const numLines = Object.keys(data.measurements.lines).length;
            const message = `✓ Read ${numBuses} buses, ${numLines} lines\nTimestamp: ${data.measurements.timestamp.toFixed(2)}s`;
            showOperationResult('measurements-result', true, message);
            
            // Add to log
            addLogEntry(
                'Read Measurements',
                true,
                `Auth: ${data.authorization.latency_ms.toFixed(2)}ms, Exec: ${data.execution_latency_ms.toFixed(2)}ms`
            );
        } else {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show error
            showOperationResult('measurements-result', false, `✗ ${data.error}`);
            
            // Add to log
            addLogEntry(
                'Read Measurements',
                false,
                `Error: ${data.error}`
            );
        }
    } catch (error) {
        showOperationResult('measurements-result', false, `✗ Network error: ${error.message}`);
        addLogEntry('Read Measurements', false, `Network error: ${error.message}`);
    } finally {
        btn.disabled = false;
    }
}

// Control breaker
async function controlBreaker(action) {
    const breakerId = parseInt(document.getElementById('breaker-id').value);
    const btn = document.getElementById(`btn-breaker-${action}`);
    btn.disabled = true;
    
    try {
        const userId = getCurrentUser();
        const userRole = getCurrentRole();
        
        const response = await fetch(`/api/control/breaker/${breakerId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                action: action,
                user_id: userId,
                user_roles: [userRole]
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show success
            const message = `✓ Breaker ${breakerId} ${action}ed\nAuth: ${data.authorization.latency_ms.toFixed(2)}ms\nExec: ${data.execution_latency_ms.toFixed(2)}ms`;
            showOperationResult('breaker-result', true, message);
            
            // Add to log
            addLogEntry(
                `Breaker ${breakerId} ${action.toUpperCase()}`,
                true,
                `Auth: ${data.authorization.latency_ms.toFixed(2)}ms, Decision: ${data.authorization.decision}`
            );
        } else {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show error
            showOperationResult('breaker-result', false, `✗ ${data.error}\nReason: ${data.authorization.reason}`);
            
            // Add to log
            addLogEntry(
                `Breaker ${breakerId} ${action.toUpperCase()}`,
                false,
                `Denied: ${data.authorization.reason}`
            );
        }
    } catch (error) {
        showOperationResult('breaker-result', false, `✗ Network error: ${error.message}`);
        addLogEntry(`Breaker ${breakerId} ${action.toUpperCase()}`, false, `Network error: ${error.message}`);
    } finally {
        btn.disabled = false;
    }
}

// Set generator setpoint
async function setGeneratorSetpoint() {
    const genId = parseInt(document.getElementById('gen-id').value);
    const setpoint = parseFloat(document.getElementById('gen-setpoint').value);
    const btn = document.getElementById('btn-gen-setpoint');
    btn.disabled = true;
    
    try {
        const userId = getCurrentUser();
        const userRole = getCurrentRole();
        
        const response = await fetch(`/api/control/generator/${genId}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                setpoint_kw: setpoint,
                user_id: userId,
                user_roles: [userRole]
            })
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show success
            const message = `✓ Generator ${genId} setpoint: ${setpoint} kW\nAuth: ${data.authorization.latency_ms.toFixed(2)}ms\nExec: ${data.execution_latency_ms.toFixed(2)}ms`;
            showOperationResult('gen-result', true, message);
            
            // Add to log
            addLogEntry(
                `Generator ${genId} Setpoint`,
                true,
                `${setpoint} kW, Auth: ${data.authorization.latency_ms.toFixed(2)}ms`
            );
        } else {
            // Update authorization status
            updateAuthStatus(data.authorization);
            
            // Show error
            showOperationResult('gen-result', false, `✗ ${data.error}\nReason: ${data.authorization.reason}`);
            
            // Add to log
            addLogEntry(
                `Generator ${genId} Setpoint`,
                false,
                `Denied: ${data.authorization.reason}`
            );
        }
    } catch (error) {
        showOperationResult('gen-result', false, `✗ Network error: ${error.message}`);
        addLogEntry(`Generator ${genId} Setpoint`, false, `Network error: ${error.message}`);
    } finally {
        btn.disabled = false;
    }
}

// Clear log
function clearLog() {
    operationLog.length = 0;
    updateLogDisplay();
}

// Initialize event listeners
window.addEventListener('load', () => {
    // Read measurements button
    document.getElementById('btn-read-measurements').addEventListener('click', readMeasurements);
    
    // Breaker control buttons
    document.getElementById('btn-breaker-open').addEventListener('click', () => controlBreaker('open'));
    document.getElementById('btn-breaker-close').addEventListener('click', () => controlBreaker('close'));
    
    // Generator control button
    document.getElementById('btn-gen-setpoint').addEventListener('click', setGeneratorSetpoint);
    
    // Clear log button
    document.getElementById('btn-clear-log').addEventListener('click', clearLog);
    
    // Initialize log display
    updateLogDisplay();
    
    console.log('Control panel initialized');
});