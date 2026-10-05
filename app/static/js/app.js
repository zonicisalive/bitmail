/**
 * ==========================================================================
 * Bitmail Enterprise - Production Core Application
 * High-Throughput Mass Email Sender & Storage Vault
 * ==========================================================================
 */

// Universal API Authentication & Authorization Interceptor
(function() {
    const originalFetch = window.fetch;
    window.fetch = async function(url, options = {}) {
        options = options || {};
        options.headers = options.headers || {};
        const token = localStorage.getItem('bitmail_token');
        if (token) {
            if (options.headers instanceof Headers) {
                if (!options.headers.has('Authorization')) {
                    options.headers.set('Authorization', 'Bearer ' + token);
                }
            } else if (Array.isArray(options.headers)) {
                let hasAuth = false;
                for (const h of options.headers) {
                    if (h[0].toLowerCase() === 'authorization') { hasAuth = true; break; }
                }
                if (!hasAuth) options.headers.push(['Authorization', 'Bearer ' + token]);
            } else {
                if (!options.headers['Authorization'] && !options.headers['authorization']) {
                    options.headers['Authorization'] = 'Bearer ' + token;
                }
            }
        }
        const response = await originalFetch(url, options);
        if (response.status === 401 && typeof url === 'string' && url.startsWith('/api/') && !url.includes('/api/auth/login') && !url.includes('/api/auth/scan')) {
            handleSessionExpired();
        }
        return response;
    };
})();

// Safe JSON response parser that handles plain text / non-JSON error responses gracefully
async function safeJson(res) {
    const text = await res.text();
    try {
        return JSON.parse(text);
    } catch (e) {
        return { detail: text || res.statusText || 'Server communication error', success: false };
    }
}

// Global App State
const App = {
    currentTab: 'broadcast',
    currentUser: null,
    stats: {
        totalSent: 0,
        attempted: 0,
        failed: 0,
        deliveryRate: 0,
        openRate: 0,
        clickRate: 0,
        activeSubscribers: 0,
        suppressed: 0,
        vaultStored: 0,
        relay: { configured: false }
    },
    campaigns: [],
    vaultEmails: [],
    subscribers: [],
    lists: [],
    templates: [],
    smtpConfigs: [],
    activeBroadcast: null,
    broadcastPollInterval: null,
    audienceMode: 'paste',
    broadcastTimingMode: 'now',
    chartInstance: null,
    ws: null,
    wsReconnectTimeout: null,
    // Checkbox selections per table, keyed by BULK_KINDS name.
    selected: {},
    // Non-null while an edit modal is open; switches submit handlers to PUT.
    editing: { subscriber: null, smtp: null, template: null },
    customPlaceholders: [],
    lastFocusedInput: null,
    previewSource: 'broadcast',
    logs: [],
    logsStreamPaused: false,
    logsFilterDebounce: null,
    newTableColumns: [],
    currentBroadcastTablePlaceholders: [],
    currentStudioTablePlaceholders: [],
    currentInsertTablePreset: 'invoice',
    currentInsertTableContext: 'broadcast',
    deliverability: {
        lastDiagnostic: null,
        batchResults: [],
        lastBatchCleaned: [],
        batchFilter: 'all',
        lastBroadcastCleaned: []
    }
};

// ==========================================================================
// Initialization & Lifecycle
// ==========================================================================
document.addEventListener('DOMContentLoaded', async () => {
    initLucide();
    setupNavigation();
    setupEventListeners();
    initCustomPlaceholders();
    suppressUnwantedAutofill();
    
    // Check initial tab from body attribute or URL
    const initialTab = document.body.getAttribute('data-initial-tab') || 'broadcast';
    switchTab(initialTab, false);

    // Verify user authentication before initiating protected network requests
    const authenticated = await checkAuthSession();
    if (authenticated) {
        initWebSocket();
        await refreshAllData();
    }
});

function initLucide() {
    if (window.lucide) {
        window.lucide.createIcons();
    }
}

function suppressUnwantedAutofill() {
    try {
        // 1. Disable inputs inside all currently hidden modals so Chrome never treats them as active credential forms
        document.querySelectorAll('.modal-overlay.hidden, div[id^="modal-"].hidden').forEach(modal => {
            modal.querySelectorAll('input, select, textarea, button[type="submit"]').forEach(el => {
                el.disabled = true;
            });
        });

        // 2. Protect all non-auth inputs with strict attributes
        const inputs = document.querySelectorAll('input:not([name="username"]):not([name="password"]):not([type="password"]):not(#login-input-identity):not(#login-input-password)');
        inputs.forEach(input => {
            if (!input.hasAttribute('autocomplete') || input.getAttribute('autocomplete') === 'on') {
                input.setAttribute('autocomplete', 'off');
            }
            input.setAttribute('autocorrect', 'off');
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('spellcheck', 'false');
            input.setAttribute('data-lpignore', 'true');
            input.setAttribute('data-1p-ignore', 'true');
            input.setAttribute('data-bwignore', 'true');
            input.setAttribute('data-form-type', 'other');
        });

        // 3. Search inputs: apply readonly-until-focus lock to block Chrome password autofill dropdown
        ['global-search-input', 'subscriber-search-input', 'vault-search-input', 'logs-search-input'].forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                if (el.getAttribute('type') !== 'search') el.setAttribute('type', 'search');
                el.setAttribute('autocomplete', 'off');
                el.setAttribute('autocorrect', 'off');
                el.setAttribute('autocapitalize', 'off');
                el.setAttribute('spellcheck', 'false');
                el.setAttribute('data-lpignore', 'true');
                el.setAttribute('data-1p-ignore', 'true');
                el.setAttribute('data-bwignore', 'true');
                el.setAttribute('data-form-type', 'other');

                if (!el.hasAttribute('data-autofill-locked')) {
                    el.setAttribute('data-autofill-locked', 'true');
                    el.setAttribute('readonly', 'readonly');
                    el.addEventListener('focus', function() {
                        this.removeAttribute('readonly');
                    });
                    el.addEventListener('blur', function() {
                        this.setAttribute('readonly', 'readonly');
                    });
                }
            }
        });
    } catch (e) {
        console.warn('Autofill suppression error:', e);
    }
}

// ==========================================================================
// Real-Time WebSocket Dynamic Live Streaming
// ==========================================================================

// The dashboard badge must report the socket we actually have, not a fixed
// "Streaming" label that stays green through an outage.
const STREAM_STATES = {
    live: ['bg-emerald-400', 'Live', 'text-emerald-400'],
    reconnecting: ['bg-amber-400', 'Reconnecting', 'text-amber-400'],
    connecting: ['bg-slate-500', 'Connecting', 'text-slate-400'],
};

function setStreamState(state) {
    const [dotClass, text, textClass] = STREAM_STATES[state] || STREAM_STATES.connecting;
    const dot = document.getElementById('ws-dot') || document.getElementById('ws-status-dot');
    const lbl = document.getElementById('ws-label') || document.getElementById('ws-status-label');
    if (dot) dot.className = `w-1.5 h-1.5 rounded-full ${dotClass}${state === 'live' ? ' status-dot-pulse' : ''} shrink-0`;
    if (lbl) {
        lbl.textContent = text;
        lbl.className = `text-[11px] font-medium ${textClass}`;
    }
}

function initWebSocket() {
    const token = localStorage.getItem('bitmail_token');
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/live` + (token ? `?token=${encodeURIComponent(token)}` : '');

    const indicator = document.getElementById('ws-status-indicator');
    const label = document.getElementById('ws-status-label');

    try {
        App.ws = new WebSocket(wsUrl);

        App.ws.onopen = () => {
            console.log('[WebSocket] Live stream connected to Bitmail backend');
            if (indicator) {
                indicator.className = "flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-medium shrink-0";
            }
            const dot = document.getElementById('ws-status-dot');
            if (dot) dot.className = "w-1.5 h-1.5 rounded-full bg-emerald-400 status-dot-pulse shrink-0";
            if (label) {
                label.innerHTML = 'Live';
                label.className = "text-[11px] font-medium text-emerald-400";
            }
            setStreamState('live');
        };

        App.ws.onmessage = (event) => {
            try {
                const message = JSON.parse(event.data);
                handleWebSocketEvent(message);
            } catch (err) {
                console.error('[WebSocket] Failed to parse message:', err);
            }
        };

        App.ws.onclose = (evt) => {
            if (evt && evt.code === 1008) {
                console.warn('[WebSocket] Live stream closed: authentication required.');
                setStreamState('connecting');
                return;
            }
            console.warn('[WebSocket] Live stream closed. Reconnecting in 3s...');
            if (indicator) {
                indicator.className = "flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs font-medium shrink-0";
            }
            const dot = document.getElementById('ws-status-dot');
            if (dot) dot.className = "w-1.5 h-1.5 rounded-full bg-amber-400 status-dot-pulse shrink-0";
            if (label) {
                label.innerHTML = 'Reconnecting';
                label.className = "text-[11px] font-medium text-amber-400";
            }
            setStreamState('reconnecting');
            clearTimeout(App.wsReconnectTimeout);
            App.wsReconnectTimeout = setTimeout(initWebSocket, 3000);
        };

        App.ws.onerror = (err) => {
            console.error('[WebSocket] Error:', err);
            App.ws.close();
        };

        // Send periodic heartbeat ping
        if (!window.__wsPingInterval) {
            window.__wsPingInterval = setInterval(() => {
                if (App.ws && App.ws.readyState === WebSocket.OPEN) {
                    App.ws.send(JSON.stringify({ action: 'ping' }));
                }
            }, 25000);
        }

    } catch (e) {
        console.error('[WebSocket] Initialization error:', e);
        clearTimeout(App.wsReconnectTimeout);
        App.wsReconnectTimeout = setTimeout(initWebSocket, 3000);
    }
}

function handleWebSocketEvent(message) {
    const { type, data } = message;

    if (type === 'email_dispatched') {
        const isSuccess = data.status !== 'failed';
        App.stats.attempted = (App.stats.attempted || 0) + 1;
        if (isSuccess) {
            App.stats.totalSent = (App.stats.totalSent || 0) + 1;
        } else {
            App.stats.failed = (App.stats.failed || 0) + 1;
        }
        App.stats.deliveryRate = App.stats.attempted
            ? Math.round((App.stats.totalSent / App.stats.attempted) * 1000) / 10
            : 0;
        App.stats.vaultStored = (App.stats.vaultStored || 0) + 1;
        updateKpiCounters();

        // Append to live broadcast console if visible
        const logTerminal = document.getElementById('broadcast-log-terminal');
        if (logTerminal) {
            const timeStr = new Date().toLocaleTimeString();
            const logEntry = document.createElement('div');
            if (isSuccess) {
                logEntry.className = 'text-emerald-400 flex items-center justify-between text-xs py-0.5';
                logEntry.innerHTML = `<span>[${timeStr}] ✓ Delivered to <strong>${escapeHtml(data.recipient)}</strong> (ID: ${data.storage_id ? data.storage_id.slice(0, 10) : ''}...)</span><span class="text-slate-500">${data.sent_count}/${data.total}</span>`;
            } else {
                logEntry.className = 'text-rose-400 flex items-center justify-between text-xs py-0.5 bg-rose-500/10 px-2 rounded';
                const reason = data.error ? ` (${escapeHtml(String(data.error).slice(0, 90))})` : '';
                logEntry.innerHTML = `<span>[${timeStr}] ✗ Relay rejected <strong>${escapeHtml(data.recipient)}</strong>${reason}</span><span class="text-rose-300 font-semibold">${data.sent_count}/${data.total}</span>`;
            }
            logTerminal.appendChild(logEntry);
            logTerminal.scrollTop = logTerminal.scrollHeight;
        }

        // Update progress bar
        const progressBar = document.getElementById('broadcast-progress-bar');
        const progressText = document.getElementById('broadcast-progress-text');
        if (progressBar && data.progress_percent !== undefined) {
            progressBar.style.width = `${data.progress_percent}%`;
        }
        if (progressText) {
            progressText.innerText = `${data.sent_count} / ${data.total} processed (${data.progress_percent || 0}%)`;
        }

        // Prepend to dashboard activity feed
        prependActivityFeedItem({
            recipient: data.recipient,
            subject: data.subject || 'Broadcast message',
            status: data.status || 'delivered',
        });

    } else if (type === 'email_opened') {
        showToast(`Opened by ${data.recipient || 'a recipient'}`, 'info');
        fetchDashboardStats();

    } else if (type === 'email_clicked') {
        showToast(`Link clicked: ${data.target_url || ''}`, 'info');
        fetchDashboardStats();

    } else if (type === 'campaign_completed') {
        showToast(`✓ Broadcast ${data.campaign_id} complete! (${data.sent_count}/${data.total} delivered)`, 'success');
        const badge = document.getElementById('broadcast-status-badge');
        if (badge) {
            badge.className = "px-2.5 py-0.5 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
            badge.innerText = "Completed";
        }
        fetchCampaigns();
        fetchVaultEmails();

    } else if (type === 'campaign_scheduled_triggered') {
        showToast(`⏰ Scheduled broadcast "${data.name || data.campaign_id}" is now dispatching!`, 'info');
        fetchCampaigns();

    } else if (type === 'system_log') {
        handleIncomingSystemLog(data);

    } else if (type === 'scan_auth_approved') {
        // Direct QR Scan Approved in Real-Time!
        (async () => {
            let authToken = data.auth_token;
            let email = data.email;
            let name = data.name;

            if (!authToken) {
                const activeToken = (typeof currentScanSession !== 'undefined' ? currentScanSession?.token : null) || 
                                    (typeof authScanSession !== 'undefined' ? authScanSession?.token : null);
                const activeSessionId = data.session_id || 
                                        (typeof currentScanSession !== 'undefined' ? currentScanSession?.session_id : null) || 
                                        (typeof authScanSession !== 'undefined' ? authScanSession?.session_id : null);
                if (activeToken && activeSessionId) {
                    try {
                        const statusRes = await fetch(`/api/auth/scan/session/${activeSessionId}/status?token=${encodeURIComponent(activeToken)}`);
                        if (statusRes.ok) {
                            const sData = await statusRes.json();
                            if (sData.auth_token) {
                                authToken = sData.auth_token;
                                email = sData.user_email || email;
                                name = sData.user_name || name;
                            }
                        }
                    } catch (err) {
                        console.error('Failed to retrieve session token after approval:', err);
                    }
                }
            }

            if (authToken) {
                localStorage.setItem('bitmail_token', authToken);
                document.cookie = `bitmail_token=${authToken}; path=/; max-age=2592000; SameSite=Lax`;
            }
            showToast(`✓ Authenticated via QR Scan as ${email || 'User'}!`, 'success');
            
            App.currentUser = {
                email: email,
                name: name || email,
                role: 'admin'
            };
            updateUserDisplay(App.currentUser);

            // Auto-update broadcast sender fields
            const senderNameInput = document.getElementById('broadcast-sender-name');
            const senderEmailInput = document.getElementById('broadcast-sender-email');
            if (senderNameInput && name) senderNameInput.value = name;
            if (senderEmailInput && email) senderEmailInput.value = email;

            // Update modal status
            const statusText = document.getElementById('scan-status-text');
            if (statusText) {
                statusText.innerHTML = `<span class="text-emerald-400 font-bold">✓ Authenticated as ${escapeHtml(email || '')}!</span>`;
            }
            const statusTextAuth = document.getElementById('scan-status-text-auth');
            if (statusTextAuth) {
                statusTextAuth.innerHTML = `<span class="text-emerald-400 font-bold">✓ Authenticated as ${escapeHtml(email || '')}!</span>`;
            }

            // Close auth modals after brief confirmation
            setTimeout(() => {
                closeLoginModal();
                closeModal('modal-scan-login');
            }, 1000);

            // Refresh all data now that we are authenticated and connect WebSocket
            initWebSocket();
            refreshAllData();
        })();
    }
}

function updateKpiCounters() {
    renderSendHealth();
    renderEngagementTiles();

    const elNavVault = document.getElementById('nav-vault-badge');
    const elSidebarVault = document.getElementById('sidebar-stored-count');
    if (elNavVault) elNavVault.innerText = App.stats.vaultStored;
    if (elSidebarVault) elSidebarVault.innerText = `${Number(App.stats.vaultStored).toLocaleString()} Stored (.EML)`;
}

function prependActivityFeedItem(item) {
    const container = document.getElementById('dashboard-activity-feed');
    if (!container) return;

    // A live dispatch is 'delivered' or 'failed' - report the one that happened.
    const failed = item.status === 'failed';
    const placeholder = container.querySelector('.py-8');
    if (placeholder) container.innerHTML = '';

    const row = document.createElement('div');
    row.className = 'animate-fadeIn';
    row.innerHTML = activityRow({
        event_type: failed ? 'failed' : 'sent',
        recipient: item.recipient || item.recipient_email,
        event: failed ? `Rejected by relay: ${item.subject || ''}`.trim() : item.subject,
        timestamp: item.timestamp || new Date().toISOString().slice(0, 19).replace('T', ' '),
    });
    container.insertBefore(row, container.firstChild);
    while (container.children.length > 30) {
        container.removeChild(container.lastChild);
    }
    initLucide();
}

function setupNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            const href = item.getAttribute('href');
            if (href && href.startsWith('/docs')) return; // Allow Swagger external
            e.preventDefault();
            const tab = item.getAttribute('data-tab');
            if (tab) switchTab(tab, true);
        });
    });

    window.addEventListener('popstate', (e) => {
        if (e.state && e.state.tab) {
            switchTab(e.state.tab, false);
        }
    });
}

function setupEventListeners() {
    // Escape closes any open modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.querySelectorAll('.fixed.z-50:not(.hidden)').forEach(modal => {
                modal.classList.add('hidden');
            });
        }
    });

    // Track active/focused editor input for merge tag insertion
    document.addEventListener('focusin', (e) => {
        if (e.target && (e.target.id === 'broadcast-html' || e.target.id === 'broadcast-subject' || e.target.id === 'studio-tpl-html' || e.target.id === 'studio-tpl-subject')) {
            App.lastFocusedInput = e.target;
        }
    });
}

async function refreshAllData() {
    await Promise.all([
        fetchDashboardStats(),
        fetchCampaigns(),
        fetchVaultEmails(),
        fetchSubscribers(),
        fetchTemplates(),
        fetchSmtpConfigs(),
        fetchLogs(),
        fetchAvailablePlaceholders()
    ]);
    updateBroadcastEmailCount();
}

function toggleMobileSidebar() {
    const sidebar = document.querySelector('.glass-sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.toggle('mobile-open');
    if (backdrop) backdrop.classList.toggle('hidden');
}

function closeMobileSidebar() {
    const sidebar = document.querySelector('.glass-sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('mobile-open');
    if (backdrop) backdrop.classList.add('hidden');
}

// ==========================================================================
// Tab Routing
// ==========================================================================
function switchTab(tabId, updateUrl = true) {
    App.currentTab = tabId;
    closeMobileSidebar();

    if (updateUrl) {
        const path = tabId === 'dashboard' ? '/' : `/${tabId === 'templates' ? 'templates-studio' : (tabId === 'subscribers' ? 'customers' : tabId)}`;
        history.pushState({ tab: tabId }, '', path);
    }

    // Update active nav styles
    document.querySelectorAll('.nav-item').forEach(item => {
        if (item.getAttribute('data-tab') === tabId) {
            item.classList.add('active', 'text-white', 'bg-white/10');
            item.classList.remove('text-slate-300');
        } else {
            item.classList.remove('active', 'text-white', 'bg-white/10');
            item.classList.add('text-slate-300');
        }
    });

    // Show selected panel
    document.querySelectorAll('.app-panel').forEach(panel => {
        panel.classList.add('hidden');
    });

    const targetPanel = document.getElementById(`panel-${tabId}`);
    if (targetPanel) {
        targetPanel.classList.remove('hidden');
    }

    // Tab-specific refreshes
    if (tabId === 'dashboard') {
        renderDeliverabilityChart();
        fetchDashboardStats();
    } else if (tabId === 'campaigns') {
        fetchCampaigns();
    } else if (tabId === 'vault') {
        fetchVaultEmails();
    } else if (tabId === 'subscribers') {
        fetchSubscribers();
    } else if (tabId === 'templates') {
        fetchTemplates();
    } else if (tabId === 'smtp') {
        fetchSmtpConfigs();
    } else if (tabId === 'broadcast') {
        populateBroadcastDropdowns();
        updateBroadcastEmailCount();
    } else if (tabId === 'logs') {
        fetchLogs();
    } else if (tabId === 'deliverability') {
        initDeliverabilityPanel();
    }

    initLucide();
}

// ==========================================================================
// 1. Dashboard API & Charting
// ==========================================================================
async function fetchDashboardStats() {
    try {
        const res = await fetch('/api/dashboard/stats');
        if (!res.ok) return;
        const data = await res.json();

        // Zero is a real answer. Never substitute a flattering default.
        const num = (v) => Number(v) || 0;
        // `total_sent` in the API payload is the attempted total (sent + failed +
        // bounced). Deliveries are `delivered_count`; mixing them showed failed
        // sends as delivered.
        App.stats.attempted = num(data.attempted_count ?? data.total_sent);
        App.stats.totalSent = num(data.delivered_count);
        App.stats.failed = num(data.failed_count);
        App.stats.deliveryRate = num(data.delivery_rate);
        App.stats.openRate = num(data.open_rate);
        App.stats.clickRate = num(data.click_rate);
        App.stats.activeSubscribers = num(data.active_subscribers);
        App.stats.suppressed = num(data.suppressed_count);
        App.stats.vaultStored = num(data.total_stored_emails);
        App.stats.relay = data.relay || { configured: false };

        renderSendHealth();
        renderEngagementTiles();
        renderRelayBanner(App.stats.relay);

        const sideCount = document.getElementById('sidebar-stored-count');
        const navVault = document.getElementById('nav-vault-badge');
        const navSubs = document.getElementById('nav-subs-badge');
        if (sideCount) sideCount.innerText = `${App.stats.vaultStored.toLocaleString()} Stored (.EML)`;
        if (navVault) navVault.innerText = App.stats.vaultStored;
        if (navSubs) navSubs.innerText = App.stats.activeSubscribers;

        await fetchDashboardActivity();
    } catch (err) {
        console.warn('Stats fetch error:', err);
    }
}

function renderSendHealth() {
    const { attempted, totalSent, failed, deliveryRate } = App.stats;
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.innerText = value; };

    set('kpi-delivery-rate', `${deliveryRate}%`);
    set('kpi-attempted', attempted.toLocaleString());
    set('kpi-total-sent', totalSent.toLocaleString());
    set('kpi-failed', failed.toLocaleString());

    const failedEl = document.getElementById('kpi-failed');
    if (failedEl) {
        failedEl.className = `text-xl font-semibold tabular-nums mt-0.5 ${failed > 0 ? 'text-rose-400' : 'text-slate-500'}`;
    }

    const deliveredBar = document.getElementById('health-bar-delivered');
    const failedBar = document.getElementById('health-bar-failed');
    const label = document.getElementById('health-bar-label');
    const deliveredPct = attempted ? (totalSent / attempted) * 100 : 0;
    const failedPct = attempted ? (failed / attempted) * 100 : 0;
    if (deliveredBar) deliveredBar.style.width = `${deliveredPct}%`;
    if (failedBar) failedBar.style.width = `${failedPct}%`;
    if (label) {
        label.textContent = attempted
            ? `${totalSent.toLocaleString()} of ${attempted.toLocaleString()} messages accepted by the relay.`
            : 'No sends recorded yet.';
    }

    const callout = document.getElementById('failure-callout');
    const calloutText = document.getElementById('failure-callout-text');
    if (callout && calloutText) {
        callout.classList.toggle('hidden', failed === 0);
        if (failed > 0) {
            calloutText.textContent = `${failed.toLocaleString()} ${failed === 1 ? 'message' : 'messages'} were rejected by the relay. Each stored record carries the SMTP error.`;
        }
    }
}

function renderEngagementTiles() {
    const { attempted, openRate, clickRate, activeSubscribers, suppressed, vaultStored } = App.stats;
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.innerText = value; };

    set('kpi-open-rate', `${openRate}%`);
    set('kpi-click-rate', `${clickRate}%`);
    set('kpi-reachable', activeSubscribers.toLocaleString());
    set('kpi-vault-stored', vaultStored.toLocaleString());

    const opened = Math.round((openRate / 100) * attempted);
    const clicked = Math.round((clickRate / 100) * attempted);
    set('kpi-open-sub', attempted ? `${opened.toLocaleString()} of ${attempted.toLocaleString()} opened` : 'Awaiting first send');
    set('kpi-click-sub', attempted ? `${clicked.toLocaleString()} of ${attempted.toLocaleString()} clicked` : 'Awaiting first send');
    set('kpi-reachable-sub', suppressed ? `${suppressed.toLocaleString()} suppressed and skipped` : 'None suppressed');
}

function renderRelayBanner(relay) {
    const banner = document.getElementById('relay-banner');
    const title = document.getElementById('relay-banner-title');
    const body = document.getElementById('relay-banner-body');
    if (!banner || !title || !body) return;

    const base = 'rounded-xl border px-4 py-3 flex items-start gap-3 text-sm ';
    if (!relay || !relay.configured) {
        banner.className = base + 'border-rose-500/30 bg-rose-500/5 text-rose-200';
        title.textContent = 'No SMTP relay configured';
        body.textContent = 'Campaigns cannot leave the building until you add a relay. Nothing is sent and nothing is faked.';
    } else if (relay.is_sandbox) {
        banner.className = base + 'border-amber-500/30 bg-amber-500/5 text-amber-200';
        title.textContent = 'Dry-run relay active';
        body.textContent = `"${relay.name}" renders and archives every message but transmits nothing. Point a real relay at your leads when you are ready.`;
    } else {
        banner.className = base + 'border-white/10 bg-slate-900/60 text-slate-300';
        title.textContent = `Sending through ${relay.name}`;
        body.textContent = `${relay.host}:${relay.port} — live delivery to real inboxes.`;
    }
    banner.classList.remove('hidden');
    initLucide();
}

// The activity API returns event/recipient/timestamp with event types
// 'open' and 'click' - matching those names is what makes the feed readable.
const ACTIVITY_STYLES = {
    open:        { icon: 'eye',              tone: 'text-purple-400 bg-purple-500/10' },
    click:       { icon: 'mouse-pointer-click', tone: 'text-sky-400 bg-sky-500/10' },
    sent:        { icon: 'send',             tone: 'text-emerald-400 bg-emerald-500/10' },
    delivered:   { icon: 'check',            tone: 'text-emerald-400 bg-emerald-500/10' },
    queued:      { icon: 'clock',            tone: 'text-slate-400 bg-slate-500/10' },
    failed:      { icon: 'alert-triangle',   tone: 'text-rose-400 bg-rose-500/10' },
    bounce:      { icon: 'undo-2',           tone: 'text-amber-400 bg-amber-500/10' },
    unsubscribe: { icon: 'user-minus',       tone: 'text-amber-400 bg-amber-500/10' },
};

function activityRow(act) {
    const style = ACTIVITY_STYLES[act.event_type] || ACTIVITY_STYLES.queued;
    const who = act.recipient || act.campaign_name || 'Unknown recipient';
    const what = act.event || act.event_type || '';
    return `
        <div class="flex items-start gap-3 p-2.5 rounded-lg bg-slate-900/60 border border-white/5">
            <div class="p-1.5 rounded-md ${style.tone} shrink-0">
                <i data-lucide="${style.icon}" class="w-3.5 h-3.5"></i>
            </div>
            <div class="min-w-0 flex-1">
                <p class="text-xs font-semibold text-white truncate">${escapeHtml(who)}</p>
                <p class="text-[11px] text-slate-400 truncate">${escapeHtml(what)}</p>
            </div>
            <span class="text-[10px] text-slate-500 shrink-0 tabular-nums pt-0.5">${formatTimeAgo(act.timestamp)}</span>
        </div>
    `;
}

async function fetchDashboardActivity() {
    try {
        const res = await fetch('/api/dashboard/activity?limit=30');
        if (!res.ok) return;
        const activities = await res.json();

        const feedEl = document.getElementById('dashboard-activity-feed');
        const countEl = document.getElementById('activity-count');
        if (!feedEl) return;

        if (!activities || activities.length === 0) {
            if (countEl) countEl.textContent = '';
            feedEl.innerHTML = `
                <div class="py-8 text-center">
                    <p class="text-sm text-slate-400">Nothing has happened yet</p>
                    <p class="text-xs text-slate-500 mt-1">Sends, opens, and clicks appear here the moment they occur.</p>
                </div>`;
            return;
        }

        if (countEl) countEl.textContent = `${activities.length} recent`;
        feedEl.innerHTML = activities.map(activityRow).join('');
        initLucide();
    } catch (err) {
        console.warn('Activity feed error:', err);
    }
}

async function renderDeliverabilityChart() {
    const canvas = document.getElementById('deliverabilityChart');
    if (!canvas) return;

    try {
        const res = await fetch('/api/dashboard/chart');
        if (!res.ok) return;
        const data = await res.json();

        // An empty account shows an empty state, never an invented curve.
        const emptyEl = document.getElementById('chart-empty');
        if (emptyEl) emptyEl.classList.toggle('hidden', data.has_data !== false);
        canvas.style.visibility = data.has_data === false ? 'hidden' : 'visible';

        if (App.chartInstance) App.chartInstance.destroy();

        const series = data.datasets || {};
        const line = (label, key, color, fill) => ({
            label,
            data: series[key] || [],
            borderColor: color,
            backgroundColor: fill || 'transparent',
            fill: Boolean(fill),
            tension: 0.35,
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBorderWidth: 2,
            pointHoverBackgroundColor: '#0f172a',
            pointHoverBorderColor: color,
        });

        App.chartInstance = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                labels: data.labels || [],
                datasets: [
                    line('Delivered', 'delivered', '#2ea043', 'rgba(46, 160, 67, 0.10)'),
                    line('Opened', 'opened', '#a371f7'),
                    line('Clicked', 'clicked', '#388bfd'),
                    line('Failed', 'failed', '#f85149'),
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        align: 'end',
                        labels: {
                            color: '#8b949e',
                            boxWidth: 8,
                            boxHeight: 8,
                            usePointStyle: true,
                            pointStyle: 'circle',
                            padding: 16,
                            font: { family: 'Plus Jakarta Sans', size: 11 },
                        },
                    },
                    tooltip: {
                        backgroundColor: '#111824',
                        borderColor: '#28374d',
                        borderWidth: 1,
                        titleColor: '#f0f6fc',
                        bodyColor: '#c9d1d9',
                        padding: 10,
                        cornerRadius: 8,
                        displayColors: true,
                        usePointStyle: true,
                    },
                },
                scales: {
                    x: {
                        border: { display: false },
                        grid: { display: false },
                        ticks: { color: '#6e7681', font: { size: 10 }, maxRotation: 0, autoSkipPadding: 16 },
                    },
                    y: {
                        beginAtZero: true,
                        border: { display: false },
                        grid: { color: 'rgba(255, 255, 255, 0.05)' },
                        ticks: { color: '#6e7681', font: { size: 10 }, precision: 0 },
                    },
                },
            },
        });
    } catch (err) {
        console.warn('Chart render error:', err);
    }
}

// ==========================================================================
// 2. Quick Mass Broadcast / Send to Customers
// ==========================================================================
function setBroadcastAudienceMode(mode) {
    App.audienceMode = mode;
    const btnPaste = document.getElementById('tab-btn-paste-emails');
    const btnList = document.getElementById('tab-btn-list-emails');
    const modePaste = document.getElementById('broadcast-mode-paste');
    const modeList = document.getElementById('broadcast-mode-list');

    if (mode === 'paste') {
        btnPaste.classList.add('bg-indigo-600', 'text-white');
        btnPaste.classList.remove('text-slate-400');
        btnList.classList.remove('bg-indigo-600', 'text-white');
        btnList.classList.add('text-slate-400');
        modePaste.classList.remove('hidden');
        modeList.classList.add('hidden');
    } else {
        btnList.classList.add('bg-indigo-600', 'text-white');
        btnList.classList.remove('text-slate-400');
        btnPaste.classList.remove('bg-indigo-600', 'text-white');
        btnPaste.classList.add('text-slate-400');
        modeList.classList.remove('hidden');
        modePaste.classList.add('hidden');
    }
    updateBroadcastEmailCount();
}

function setBroadcastTimingMode(mode) {
    App.broadcastTimingMode = mode;
    const btnNow = document.getElementById('btn-timing-now');
    const btnSchedule = document.getElementById('btn-timing-schedule');
    const wrap = document.getElementById('broadcast-schedule-wrap');
    const input = document.getElementById('broadcast-schedule-datetime');
    const launchBtn = document.getElementById('btn-launch-broadcast');

    if (mode === 'now') {
        if (btnNow) {
            btnNow.className = 'px-2.5 py-1 rounded-md text-white bg-indigo-600 font-semibold transition-all cursor-pointer';
        }
        if (btnSchedule) {
            btnSchedule.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-white transition-all cursor-pointer';
        }
        if (wrap) {
            wrap.classList.add('hidden');
        }
        if (launchBtn) {
            launchBtn.innerHTML = '<i data-lucide="send" class="w-4 h-4"></i><span>Send to All Now</span>';
            launchBtn.className = 'px-5 py-2.5 rounded-xl btn-dark-green text-white text-xs font-extrabold shadow-lg shadow-emerald-500/25 flex items-center justify-center gap-2 transition-all whitespace-nowrap flex-shrink-0 cursor-pointer';
            initLucide();
        }
    } else {
        if (btnSchedule) {
            btnSchedule.className = 'px-2.5 py-1 rounded-md text-white bg-indigo-600 font-semibold transition-all cursor-pointer';
        }
        if (btnNow) {
            btnNow.className = 'px-2.5 py-1 rounded-md text-slate-400 hover:text-white transition-all cursor-pointer';
        }
        if (wrap) {
            wrap.classList.remove('hidden');
        }
        if (input && (!input.value || new Date(input.value).getTime() <= Date.now())) {
            const nextHour = new Date(Date.now() + 3600000);
            const tzOffset = nextHour.getTimezoneOffset() * 60000;
            input.value = (new Date(nextHour.getTime() - tzOffset)).toISOString().slice(0, 16);
        }
        if (launchBtn) {
            launchBtn.innerHTML = '<i data-lucide="clock" class="w-4 h-4"></i><span>Schedule Broadcast</span>';
            launchBtn.className = 'px-5 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-extrabold shadow-lg shadow-purple-500/25 flex items-center justify-center gap-2 transition-all whitespace-nowrap flex-shrink-0 cursor-pointer';
            initLucide();
        }
    }
}

function parseEmailsFromString(text) {
    if (!text) return [];
    const normalized = text.replace(/[\r\n;]+/g, ',');
    const tokens = normalized.split(',').map(t => t.trim()).filter(t => t.length > 0);
    const valid = [];
    const seen = new Set();
    const emailRegex = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;

    for (let token of tokens) {
        let email = token;
        let name = '';
        if (token.includes('<') && token.includes('>')) {
            const parts = token.split('<');
            name = parts[0].replace(/"/g, '').trim();
            email = parts[1].split('>')[0].trim();
        }
        email = email.toLowerCase().replace(/"/g, '').trim();
        if (emailRegex.test(email) && !seen.has(email)) {
            seen.add(email);
            valid.push({ email, name: name || email.split('@')[0] });
        }
    }
    return valid;
}

function updateBroadcastEmailCount() {
    const badge = document.getElementById('broadcast-recipient-count-badge');
    if (!badge) return;

    if (App.audienceMode === 'paste') {
        const text = document.getElementById('broadcast-raw-emails')?.value || '';
        const parsed = parseEmailsFromString(text);
        badge.innerText = `${parsed.length} Customer${parsed.length === 1 ? '' : 's'}`;
        badge.className = parsed.length > 0 
            ? 'px-2.5 py-0.5 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
            : 'px-2.5 py-0.5 rounded-full text-xs font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30';
    } else {
        const select = document.getElementById('broadcast-list-select');
        const selectedVal = select?.value || 'all';
        if (selectedVal === 'all') {
            badge.innerText = `${App.subscribers.length} Customers (All)`;
        } else {
            const targetList = App.lists.find(l => l.id === selectedVal);
            badge.innerText = `${targetList ? targetList.subscriber_count : 0} Customers`;
        }
        badge.className = 'px-2.5 py-0.5 rounded-full text-xs font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30';
    }
}

function loadSampleCustomerEmails() {
    const textarea = document.getElementById('broadcast-raw-emails');
    if (!textarea) return;
    textarea.value = `alexandra.chen@techcorp.io\n"Michael Scott" <michael@dundermifflin.com>\nsarah.connor@cyberdyne.org\njohn.doe@startup.ai, clara.oswald@spacefleet.org\nvip.client@globex.com`;
    updateBroadcastEmailCount();
    showToast('Loaded 6 sample customer emails', 'info');
}

function initCustomPlaceholders() {
    try {
        const stored = localStorage.getItem('bitmail_custom_placeholders');
        if (stored) {
            const parsed = JSON.parse(stored);
            if (Array.isArray(parsed)) {
                parsed.forEach(tag => {
                    const clean = String(tag).trim().toLowerCase().replace(/[^a-z0-9_]/g, '_');
                    if (clean && !App.customPlaceholders.includes(clean)) {
                        App.customPlaceholders.push(clean);
                    }
                });
            }
        }
    } catch (e) {
        console.warn('Failed to load custom placeholders from localStorage', e);
    }
    renderAllPlaceholderChips();
}

function saveCustomPlaceholders() {
    try {
        localStorage.setItem('bitmail_custom_placeholders', JSON.stringify(App.customPlaceholders));
    } catch (e) {
        console.warn('Failed to save custom placeholders', e);
    }
}

async function fetchAvailablePlaceholders() {
    try {
        const res = await fetch('/api/subscribers/placeholders');
        if (!res.ok) return;
        const data = await safeJson(res);
        let changed = false;
        if (data && Array.isArray(data.custom_fields)) {
            data.custom_fields.forEach(tag => {
                const clean = String(tag).trim().toLowerCase().replace(/[^a-z0-9_]/g, '_');
                if (clean && !App.customPlaceholders.includes(clean)) {
                    App.customPlaceholders.push(clean);
                    changed = true;
                }
            });
        }
        if (changed) {
            saveCustomPlaceholders();
            renderAllPlaceholderChips();
        }
    } catch (err) {
        console.warn('Placeholder discovery error:', err);
    }
}

function renderAllPlaceholderChips() {
    renderPlaceholderChips('broadcast-merge-tags', 'broadcast');
    renderPlaceholderChips('studio-merge-tags', 'studio');
    initLucide();
}

const HTML_TABLE_PRESETS = {
    invoice: `<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin: 20px 0; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; font-family: Arial, sans-serif; font-size: 14px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
    <thead>
        <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0; text-align: left;">
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Invoice #</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Description</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Due Date</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; text-align: right;">Amount</th>
        </tr>
    </thead>
    <tbody>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 14px 16px; color: #64748b; font-family: Courier, monospace; font-size: 13px;">{{invoice_no}}</td>
            <td style="padding: 14px 16px; color: #1e293b; font-weight: 600;">Monthly Services / Plan</td>
            <td style="padding: 14px 16px; color: #64748b;">{{due_date}}</td>
            <td style="padding: 14px 16px; color: #0f172a; font-weight: 700; text-align: right;">{{amount_due}}</td>
        </tr>
    </tbody>
    <tfoot>
        <tr style="background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
            <td colspan="3" style="padding: 12px 16px; text-align: right; color: #475569; font-weight: 600;">Total Amount Due:</td>
            <td style="padding: 12px 16px; text-align: right; color: #4f46e5; font-size: 16px; font-weight: 800;">{{amount_due}}</td>
        </tr>
    </tfoot>
</table>`,

    order: `<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin: 20px 0; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; font-family: Arial, sans-serif; font-size: 14px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
    <thead>
        <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0; text-align: left;">
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Order ID</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Product Item</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; text-align: center;">Qty</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px; text-align: right;">Total</th>
        </tr>
    </thead>
    <tbody>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 14px 16px; color: #64748b; font-family: Courier, monospace; font-size: 13px;">{{order_id}}</td>
            <td style="padding: 14px 16px; color: #1e293b; font-weight: 600;">{{product_name}}</td>
            <td style="padding: 14px 16px; color: #64748b; text-align: center;">1</td>
            <td style="padding: 14px 16px; color: #0f172a; font-weight: 700; text-align: right;">{{order_total}}</td>
        </tr>
    </tbody>
    <tfoot>
        <tr style="background-color: #f8fafc; border-top: 1px solid #e2e8f0;">
            <td colspan="3" style="padding: 12px 16px; text-align: right; color: #475569; font-weight: 600;">Order Total:</td>
            <td style="padding: 12px 16px; text-align: right; color: #10b981; font-size: 16px; font-weight: 800;">{{order_total}}</td>
        </tr>
    </tfoot>
</table>`,

    specs: `<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin: 20px 0; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; font-family: Arial, sans-serif; font-size: 14px; overflow: hidden;">
    <tbody>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 12px 16px; width: 35%; background-color: #f8fafc; font-weight: 600; color: #475569; border-right: 1px solid #f1f5f9;">Account ID</td>
            <td style="padding: 12px 16px; color: #0f172a; font-family: Courier, monospace; font-weight: 600;">{{account_id}}</td>
        </tr>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 12px 16px; width: 35%; background-color: #f8fafc; font-weight: 600; color: #475569; border-right: 1px solid #f1f5f9;">Subscription Plan</td>
            <td style="padding: 12px 16px; color: #4f46e5; font-weight: 700;">{{plan_name}}</td>
        </tr>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 12px 16px; width: 35%; background-color: #f8fafc; font-weight: 600; color: #475569; border-right: 1px solid #f1f5f9;">Renewal Date</td>
            <td style="padding: 12px 16px; color: #64748b;">{{renewal_date}}</td>
        </tr>
        <tr>
            <td style="padding: 12px 16px; width: 35%; background-color: #f8fafc; font-weight: 600; color: #475569; border-right: 1px solid #f1f5f9;">Support Tier</td>
            <td style="padding: 12px 16px; color: #10b981; font-weight: 600;">{{support_tier}}</td>
        </tr>
    </tbody>
</table>`,

    custom: `<table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin: 20px 0; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; font-family: Arial, sans-serif; font-size: 14px; overflow: hidden;">
    <thead>
        <tr style="background-color: #f8fafc; border-bottom: 2px solid #e2e8f0; text-align: left;">
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Item</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Details</th>
            <th style="padding: 12px 16px; color: #475569; font-size: 11px; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Status</th>
        </tr>
    </thead>
    <tbody>
        <tr style="border-bottom: 1px solid #f1f5f9;">
            <td style="padding: 14px 16px; color: #1e293b; font-weight: 600;">Service Item</td>
            <td style="padding: 14px 16px; color: #4f46e5; font-family: Courier, monospace;">{{item_code}}</td>
            <td style="padding: 14px 16px; color: #10b981; font-weight: 700;">Active</td>
        </tr>
        <tr>
            <td style="padding: 14px 16px; color: #1e293b; font-weight: 600;">Account Description</td>
            <td style="padding: 14px 16px; color: #4f46e5; font-family: Courier, monospace;">{{item_notes}}</td>
            <td style="padding: 14px 16px; color: #64748b;">Pending</td>
        </tr>
    </tbody>
</table>`
};

function openInsertTableModal(context = 'broadcast') {
    App.currentInsertTableContext = context;
    setInsertTablePreset('invoice');
    openModal('modal-insert-table');
}

function setInsertTablePreset(presetKey) {
    if (!HTML_TABLE_PRESETS[presetKey]) presetKey = 'invoice';
    App.currentInsertTablePreset = presetKey;

    ['invoice', 'order', 'specs', 'custom'].forEach(k => {
        const card = document.getElementById(`insert-table-card-${k}`);
        if (card) {
            if (k === presetKey) {
                card.classList.add('border-purple-500/50', 'bg-purple-950/20');
                card.classList.remove('border-white/10');
            } else {
                card.classList.remove('border-purple-500/50', 'bg-purple-950/20');
                card.classList.add('border-white/10');
            }
        }
    });

    renderInsertTablePreview();
}

function renderInsertTablePreview() {
    const box = document.getElementById('insert-table-preview-box');
    if (!box) return;
    const tplHtml = HTML_TABLE_PRESETS[App.currentInsertTablePreset] || '';
    box.innerHTML = tplHtml;
}

function confirmInsertHtmlTable() {
    const tableHtml = HTML_TABLE_PRESETS[App.currentInsertTablePreset];
    if (!tableHtml) {
        showToast('No table preset selected', 'warning');
        return;
    }

    const context = App.currentInsertTableContext || 'broadcast';
    let target = null;
    if (App.lastFocusedInput && (
        (context === 'studio' && (App.lastFocusedInput.id === 'studio-tpl-html')) ||
        (context === 'broadcast' && (App.lastFocusedInput.id === 'broadcast-html'))
    )) {
        target = App.lastFocusedInput;
    } else {
        target = context === 'studio' ? document.getElementById('studio-tpl-html') : document.getElementById('broadcast-html');
    }

    if (!target) {
        showToast('Could not find composer text area', 'error');
        return;
    }

    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    const val = target.value;
    target.value = val.substring(0, start) + "\n\n" + tableHtml + "\n\n" + val.substring(end);
    target.focus();
    const newPos = start + tableHtml.length + 4;
    target.setSelectionRange(newPos, newPos);
    target.dispatchEvent(new Event('input', { bubbles: true }));

    closeModal('modal-insert-table');
    showToast('Table inserted into message composer!', 'success');
}

async function loadTablePlaceholders(listId, context = 'broadcast') {
    if (!listId || listId === 'all') {
        if (context === 'broadcast') App.currentBroadcastTablePlaceholders = [];
        else App.currentStudioTablePlaceholders = [];
        renderPlaceholderChips(context === 'broadcast' ? 'broadcast-merge-tags' : 'studio-merge-tags', context);
        return;
    }

    try {
        const res = await fetch(`/api/subscribers/placeholders?list_id=${encodeURIComponent(listId)}`);
        if (res.ok) {
            const data = await res.json();
            const cols = Array.isArray(data.table_placeholders) ? data.table_placeholders : [];
            if (context === 'broadcast') {
                App.currentBroadcastTablePlaceholders = cols;
            } else {
                App.currentStudioTablePlaceholders = cols;
            }

            // Also integrate any list custom fields into App.customPlaceholders
            if (Array.isArray(data.custom_fields)) {
                let addedAny = false;
                data.custom_fields.forEach(f => {
                    const clean = String(f).trim().toLowerCase().replace(/[^a-z0-9_]/g, '_');
                    if (clean && !App.customPlaceholders.includes(clean)) {
                        App.customPlaceholders.push(clean);
                        addedAny = true;
                    }
                });
                if (addedAny) saveCustomPlaceholders();
            }

            renderPlaceholderChips(context === 'broadcast' ? 'broadcast-merge-tags' : 'studio-merge-tags', context);
        }
    } catch (e) {
        console.warn('Failed to load table placeholders for list', listId, e);
    }
}

function handleBroadcastAudienceChange() {
    updateBroadcastEmailCount();
    const select = document.getElementById('broadcast-list-select');
    const selectedVal = select ? select.value : 'all';
    loadTablePlaceholders(selectedVal, 'broadcast');
}

function renderPlaceholderChips(containerId, context) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const standardTags = [
        { key: 'first_name', label: '{{first_name}}' },
        { key: 'email', label: '{{email}}' },
        { key: 'company', label: '{{company}}' },
        { key: 'unsubscribe_url', label: '{{unsubscribe}}' }
    ];

    let html = '';

    // Check for active table column placeholders for this context
    const tableCols = context === 'studio' ? (App.currentStudioTablePlaceholders || []) : (App.currentBroadcastTablePlaceholders || []);
    if (tableCols && tableCols.length > 0) {
        html += `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-bold text-purple-300 bg-purple-950/80 border border-purple-500/30 uppercase tracking-wide">
            <i data-lucide="table" class="w-3 h-3 text-purple-400"></i> Table:
        </span>`;
        tableCols.forEach(col => {
            html += `<button type="button" onclick="insertMergeTag('{{${col}}}', '${context}')" class="px-2 py-0.5 rounded bg-purple-900/60 hover:bg-purple-800 text-[11px] font-mono text-purple-200 border border-purple-500/30 transition-colors cursor-pointer shadow-xs" title="Insert Table Column {{${col}}}">📁 {{${col}}}</button>`;
        });
        html += `<span class="w-px h-3.5 bg-white/10 mx-1 self-center"></span>`;
    }

    standardTags.forEach(t => {
        html += `<button type="button" onclick="insertMergeTag('{{${t.key}}}', '${context}')" class="px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-[11px] font-mono text-indigo-300 border border-white/5 transition-colors cursor-pointer" title="Insert {{${t.key}}}">${t.label}</button>`;
    });

    if (App.customPlaceholders && App.customPlaceholders.length > 0) {
        App.customPlaceholders.forEach(tag => {
            // Avoid duplicate rendering if tag is already shown in tableCols
            if (tableCols && tableCols.includes(tag)) return;
            html += `<span class="inline-flex items-center rounded bg-indigo-950/60 border border-indigo-500/30 text-indigo-300 text-[11px] font-mono group shadow-xs">
                <button type="button" onclick="insertMergeTag('{{${tag}}}', '${context}')" class="px-2 py-0.5 hover:text-white transition-colors cursor-pointer" title="Insert {{${tag}}}">{{${tag}}}</button>
                <button type="button" onclick="removeCustomPlaceholder('${tag}', event)" class="pr-1.5 pl-0.5 py-0.5 text-slate-400 hover:text-rose-400 transition-colors cursor-pointer" title="Remove custom placeholder">&times;</button>
            </span>`;
        });
    }

    container.innerHTML = html;
    if (window.lucide) lucide.createIcons();
}

function insertMergeTag(tag, context = 'broadcast') {
    let target = null;

    if (App.lastFocusedInput && (
        (context === 'studio' && (App.lastFocusedInput.id === 'studio-tpl-html' || App.lastFocusedInput.id === 'studio-tpl-subject')) ||
        (context === 'broadcast' && (App.lastFocusedInput.id === 'broadcast-html' || App.lastFocusedInput.id === 'broadcast-subject'))
    )) {
        target = App.lastFocusedInput;
    } else {
        target = context === 'studio' ? document.getElementById('studio-tpl-html') : document.getElementById('broadcast-html');
    }

    if (!target) return;

    const start = target.selectionStart ?? target.value.length;
    const end = target.selectionEnd ?? target.value.length;
    const val = target.value;
    target.value = val.substring(0, start) + tag + val.substring(end);
    target.focus();
    const newPos = start + tag.length;
    target.setSelectionRange(newPos, newPos);
    target.dispatchEvent(new Event('input', { bubbles: true }));
}

function insertBroadcastTag(tag) {
    insertMergeTag(tag, 'broadcast');
}

async function promptAddCustomPlaceholder(context = 'broadcast') {
    const raw = prompt('Enter a new placeholder tag name (e.g. phone, address, points, order_id):');
    if (!raw) return;

    const clean = raw.trim().toLowerCase().replace(/[{}]/g, '').replace(/[^a-z0-9_]/g, '_');
    if (!clean) {
        showToast('Invalid placeholder name.', 'warning');
        return;
    }

    const standard = ['first_name', 'last_name', 'email', 'company', 'unsubscribe', 'unsubscribe_url'];
    if (standard.includes(clean)) {
        insertMergeTag(`{{${clean}}}`, context);
        showToast(`Inserted standard placeholder {{${clean}}}`, 'info');
        return;
    }

    if (!App.customPlaceholders.includes(clean)) {
        App.customPlaceholders.push(clean);
        saveCustomPlaceholders();
        renderAllPlaceholderChips();
        showToast(`Created custom placeholder {{${clean}}}!`, 'success');
    }

    insertMergeTag(`{{${clean}}}`, context);
}

function removeCustomPlaceholder(tag, event) {
    if (event) {
        event.stopPropagation();
        event.preventDefault();
    }
    App.customPlaceholders = App.customPlaceholders.filter(t => t !== tag);
    saveCustomPlaceholders();
    renderAllPlaceholderChips();
    showToast(`Removed custom tag {{${tag}}}`, 'info');
}


function loadBroadcastTemplatePreset(type) {
    const textarea = document.getElementById('broadcast-html');
    const subjectInput = document.getElementById('broadcast-subject');
    if (!textarea) return;

    if (type === 'product') {
        if (subjectInput) subjectInput.value = 'Exciting Product Update for {{first_name}}! 🚀';
        textarea.value = `<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b;">
    <h2 style="color: #4f46e5;">Hello {{first_name}},</h2>
    <p>We are excited to announce major improvements to your account platform.</p>
    <div style="background: #f8fafc; padding: 18px; border-radius: 8px; border-left: 4px solid #4f46e5; margin: 16px 0;">
        <h4 style="margin: 0 0 8px 0; color: #0f172a;">What's New:</h4>
        <ul style="margin: 0; padding-left: 20px; color: #334155;">
            <li>High-throughput concurrent email dispatching</li>
            <li>Real-time Open and Link Click tracking telemetry</li>
            <li>Dedicated Email Storage Vault with full .EML forensic archiving</li>
        </ul>
    </div>
    <p><a href="https://example.com/login" style="display: inline-block; padding: 12px 24px; background: #4f46e5; color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: bold;">Log in to Your Dashboard</a></p>
    <p style="color: #64748b; font-size: 12px; margin-top: 24px;">Sent with Bitmail.</p>
</div>`;
    } else if (type === 'newsletter') {
        if (subjectInput) subjectInput.value = 'Weekly Insights Digest #42 for {{company}}';
        textarea.value = `<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b;">
    <h1 style="color: #1e1b4b; font-size: 22px;">Weekly Industry Digest</h1>
    <p style="color: #64748b;">Curated strategies for {{first_name}} at {{company}}</p>
    <hr style="border: 0; border-top: 1px solid #e2e8f0; margin: 20px 0;" />
    <h3 style="color: #4f46e5;">Top Story: Scaling Infrastructure to 10M Messages/Day</h3>
    <p style="color: #334155; line-height: 1.6;">Discover how asynchronous queue workers and token-bucket governors prevent ISP throttling and maintain 99.8% inbox deliverability rates.</p>
    <p><a href="https://example.com/article" style="color: #4f46e5; font-weight: bold;">Read Full Analysis →</a></p>
</div>`;
    } else if (type === 'invoice') {
        if (subjectInput) subjectInput.value = 'Invoice {{invoice_no}} from Our Team';
        textarea.value = `<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b; line-height: 1.6;">
    <h2 style="color: #4f46e5; margin-bottom: 8px;">Hello {{first_name}},</h2>
    <p>Please find your itemized invoice details below for your recent billing cycle with {{company}}.</p>
    
    ${HTML_TABLE_PRESETS.invoice}
    
    <p>Please review and let us know if you have any questions or require updated billing details.</p>
    <p><a href="https://example.com/pay" style="display: inline-block; padding: 12px 24px; background: #4f46e5; color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: bold;">Pay Invoice Online</a></p>
    <p style="color: #64748b; font-size: 12px; margin-top: 24px;">Thank you for your business!</p>
</div>`;
    } else if (type === 'order') {
        if (subjectInput) subjectInput.value = 'Your Order Confirmation #{{order_id}} 📦';
        textarea.value = `<div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #1e293b; line-height: 1.6;">
    <h2 style="color: #059669; margin-bottom: 8px;">Thank You for Your Order, {{first_name}}! 🎉</h2>
    <p>Your order <strong>#{{order_id}}</strong> has been confirmed and is being processed for shipment.</p>
    
    ${HTML_TABLE_PRESETS.order}
    
    <p>You can track the live delivery status of your package using your tracking number once dispatched.</p>
    <p><a href="https://example.com/track" style="display: inline-block; padding: 12px 24px; background: #059669; color: #ffffff; text-decoration: none; border-radius: 6px; font-weight: bold;">Track Your Order</a></p>
    <p style="color: #64748b; font-size: 12px; margin-top: 24px;">Sent with Bitmail.</p>
</div>`;
    }
    showToast('Loaded template preset into composer', 'info');
}

function populateBroadcastDropdowns() {
    // Populate Quick Broadcast List Dropdown
    const listSelect = document.getElementById('broadcast-list-select');
    if (listSelect) {
        listSelect.innerHTML = `<option value="all">⭐ All Active Customers (${App.subscribers.length})</option>` +
            App.lists.map(l => `<option value="${l.id}">${escapeHtml(l.name)} (${l.subscriber_count} contacts)</option>`).join('');
    }

    // Populate CSV Import Target Group Dropdown
    const csvListSelect = document.getElementById('modal-csv-list-select');
    if (csvListSelect) {
        csvListSelect.innerHTML = `<option value="">All Customers (No specific group)</option>` +
            App.lists.map(l => `<option value="${l.id}">Group: ${escapeHtml(l.name)} (${l.subscriber_count} contacts)</option>`).join('');
    }

    // Populate Add/Edit Customer Group Dropdown
    const subListSelect = document.getElementById('modal-sub-list-select');
    if (subListSelect) {
        subListSelect.innerHTML = `<option value="">No specific group</option>` +
            App.lists.map(l => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join('');
    }

    // Populate Customer Table Filter Dropdown
    const filterListSelect = document.getElementById('subscriber-filter-list');
    if (filterListSelect) {
        const curVal = filterListSelect.value || 'all';
        filterListSelect.innerHTML = `<option value="all">📁 All Groups / Lists</option>` +
            App.lists.map(l => `<option value="${l.id}" ${curVal === l.id ? 'selected' : ''}>📁 ${escapeHtml(l.name)} (${l.subscriber_count})</option>`).join('');
    }

    // Populate Template Studio Table Dropdown
    const studioTableSelect = document.getElementById('studio-table-select');
    if (studioTableSelect) {
        const curStudioVal = studioTableSelect.value || '';
        studioTableSelect.innerHTML = `<option value="">Table: Standard Tags</option>` +
            App.lists.map(l => `<option value="${l.id}" ${curStudioVal === l.id ? 'selected' : ''}>${escapeHtml(l.name)}</option>`).join('');
    }

    // Update Manage Groups counts in Customers tab header and modal badge
    const btnCount = document.getElementById('manage-groups-btn-count');
    if (btnCount) btnCount.textContent = App.lists.length;
    const badgeCount = document.getElementById('manage-groups-count-badge');
    if (badgeCount) badgeCount.textContent = `${App.lists.length} Group${App.lists.length === 1 ? '' : 's'}`;


    // Populate SMTP Relay Dropdown
    const smtpSelect = document.getElementById('broadcast-smtp-select');
    if (smtpSelect) {
        if (App.smtpConfigs.length === 0) {
            smtpSelect.innerHTML = `<option value="">Default Mail Cluster (Active Relay)</option>`;
        } else {
            smtpSelect.innerHTML = App.smtpConfigs.map(s => {
                const label = `${escapeHtml(s.name)} (${s.host}:${s.port}) ${s.is_default ? '⭐ Default' : ''} ${s.is_sandbox ? '[Sandbox]' : ''}`;
                return `<option value="${s.id}" ${s.is_default ? 'selected' : ''}>${label}</option>`;
            }).join('');
        }
    }
    updateSmtpStatusHint();
}

function updateSmtpStatusHint() {
    const hintEl = document.getElementById('broadcast-smtp-hint');
    if (!hintEl) return;

    const smtpSelect = document.getElementById('broadcast-smtp-select');
    const selectedId = smtpSelect?.value;
    const selectedConfig = App.smtpConfigs.find(s => s.id === selectedId) || App.smtpConfigs.find(s => s.is_default) || App.smtpConfigs[0];

    const host = selectedConfig ? (selectedConfig.host || '').toLowerCase() : 'sandbox';
    const isSandbox = selectedConfig ? (selectedConfig.is_sandbox || host === 'sandbox' || host === '127.0.0.1' || host === 'localhost') : true;

    if (isSandbox) {
        hintEl.className = "mt-2 p-2.5 rounded-xl text-[11px] bg-amber-500/10 border border-amber-500/30 text-amber-300 flex items-center justify-between transition-all";
        hintEl.innerHTML = `
            <div class="flex items-center gap-1.5">
                <i data-lucide="alert-triangle" class="w-4 h-4 shrink-0 text-amber-400"></i>
                <span><strong>Sandbox Simulator:</strong> Emails are saved in Storage Vault only (no external dispatch).</span>
            </div>
            <button type="button" onclick="openGmailConnectModal()" class="px-2.5 py-1 rounded-lg bg-red-600 hover:bg-red-500 text-white font-bold text-[10px] shrink-0 flex items-center gap-1 shadow">
                <i data-lucide="mail" class="w-3 h-3"></i> Connect Gmail →
            </button>
        `;
    } else {
        hintEl.className = "mt-2 p-2.5 rounded-xl text-[11px] bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 flex items-center justify-between transition-all";
        hintEl.innerHTML = `
            <div class="flex items-center gap-1.5">
                <i data-lucide="shield-check" class="w-4 h-4 shrink-0 text-emerald-400"></i>
                <span><strong>Live Delivery Active:</strong> Dispatches directly to real recipient inboxes via <code>${selectedConfig.host}:${selectedConfig.port}</code> and archives in Vault.</span>
            </div>
        `;
    }
    initLucide();
}

async function sendBroadcastTestPreview() {
    const testEmail = await promptDialog(
        'Enter the recipient address for the test preview send.',
        'my-test-email@company.com',
        { title: 'Send test preview', confirmText: 'Send test' }
    );
    if (testEmail === null) return;
    if (!testEmail.includes('@')) {
        showToast('That does not look like a valid email address.', 'warning');
        return;
    }

    const subject = document.getElementById('broadcast-subject')?.value || 'Test Preview';
    const html = document.getElementById('broadcast-html')?.value || '<p>Test Message</p>';
    const senderName = document.getElementById('broadcast-sender-name')?.value || 'Bitmail';
    const senderEmail = document.getElementById('broadcast-sender-email')?.value || 'team@bitmail.io';
    const smtpId = document.getElementById('broadcast-smtp-select')?.value || null;

    showToast(`Dispatching test preview to ${testEmail}...`, 'info');

    try {
        const res = await fetch('/api/v1/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                recipient_email: testEmail,
                recipient_name: 'Test Reviewer',
                subject: subject,
                body_html: html,
                sender_name: senderName,
                sender_email: senderEmail,
                smtp_config_id: smtpId,
                merge_variables: {
                    first_name: 'Test',
                    last_name: 'User',
                    email: testEmail,
                    company: 'Acme Testing Corp'
                },
                track_opens: true,
                track_clicks: true
            })
        });

        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast(`✓ Test preview sent successfully! Storage ID: ${data.sent_email_id}`, 'success');
            await fetchVaultEmails();
            await fetchDashboardStats();
        } else {
            showToast(`Test failed: ${data.detail || data.error || data.message || 'Check SMTP server settings'}`, 'error');
        }
    } catch (err) {
        showToast(`Test send error: ${err.message}`, 'error');
    }
}

function openLivePreviewModal(source = 'broadcast') {
    App.previewSource = source;
    const badge = document.getElementById('preview-source-badge');
    if (badge) {
        badge.textContent = source === 'studio' ? 'Template Studio' : 'Quick Broadcast';
    }
    openModal('modal-live-preview');
    refreshLivePreview();
}

function setPreviewDevice(device) {
    const wrapper = document.getElementById('preview-viewport-wrapper');
    const btnDesk = document.getElementById('preview-btn-desktop');
    const btnMob = document.getElementById('preview-btn-mobile');
    const btnFull = document.getElementById('preview-btn-full');

    const inactiveClass = 'px-2.5 py-1 rounded text-xs font-semibold text-slate-400 hover:text-white flex items-center gap-1 transition-all';
    const activeClass = 'px-2.5 py-1 rounded text-xs font-semibold bg-indigo-600 text-white flex items-center gap-1 transition-all';

    if (btnDesk) btnDesk.className = device === 'desktop' ? activeClass : inactiveClass;
    if (btnMob) btnMob.className = device === 'mobile' ? activeClass : inactiveClass;
    if (btnFull) btnFull.className = device === 'full' ? activeClass : inactiveClass;

    if (!wrapper) return;

    if (device === 'mobile') {
        wrapper.className = 'w-[375px] max-w-[375px] h-full bg-white rounded-3xl shadow-2xl border-4 border-slate-700 overflow-hidden transition-all duration-200';
    } else if (device === 'full') {
        wrapper.className = 'w-full max-w-none h-full bg-white rounded-xl shadow-2xl border border-slate-700/50 overflow-hidden transition-all duration-200';
    } else {
        wrapper.className = 'w-full max-w-[640px] h-full bg-white rounded-xl shadow-2xl border border-slate-700/50 overflow-hidden transition-all duration-200';
    }
}

async function refreshLivePreview() {
    const isStudio = App.previewSource === 'studio';
    const subjectEl = isStudio ? document.getElementById('studio-tpl-subject') : document.getElementById('broadcast-subject');
    const htmlEl = isStudio ? document.getElementById('studio-tpl-html') : document.getElementById('broadcast-html');

    const rawSubject = subjectEl?.value || (isStudio ? 'Welcome {{first_name}} to Bitmail!' : 'Important Update for {{first_name}}');
    const rawHtml = htmlEl?.value || '<div style="font-family: Arial, sans-serif; padding: 20px;"><h3>Hello {{first_name}}</h3><p>Previewing your email message.</p></div>';

    // Rich sample variables for preview interpolation
    const sampleContext = {
        first_name: 'Alexandra',
        last_name: 'Chen',
        name: 'Alexandra Chen',
        email: 'alexandra.chen@bitnade.com',
        company: 'Bitnade Technologies',
        unsubscribe: 'https://bitnade.com/unsubscribe?token=sample_demo_token',
        unsubscribe_url: 'https://bitnade.com/unsubscribe?token=sample_demo_token'
    };

    // Auto-populate values for discovered or user-defined custom placeholders
    if (App.customPlaceholders && Array.isArray(App.customPlaceholders)) {
        App.customPlaceholders.forEach(tag => {
            const formatted = tag.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            sampleContext[tag] = `[${formatted}]`;
        });
    }

    try {
        const res = await fetch('/api/templates/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                subject_template: rawSubject,
                body_template: rawHtml,
                context: sampleContext
            })
        });

        const data = await safeJson(res);
        const subjText = document.getElementById('preview-subject-text');
        if (subjText) {
            subjText.textContent = data.rendered_subject || rawSubject;
        }

        const tagsEl = document.getElementById('preview-detected-tags');
        if (tagsEl) {
            if (data.detected_tags && data.detected_tags.length > 0) {
                tagsEl.textContent = data.detected_tags.map(t => `{{${t}}}`).join(', ');
            } else {
                tagsEl.textContent = 'None detected';
            }
        }

        const iframe = document.getElementById('live-preview-iframe');
        if (iframe) {
            iframe.srcdoc = data.rendered_body || rawHtml;
        }
    } catch (err) {
        console.warn('Live preview render error:', err);
        const subjText = document.getElementById('preview-subject-text');
        if (subjText) subjText.textContent = rawSubject;
        const iframe = document.getElementById('live-preview-iframe');
        if (iframe) iframe.srcdoc = rawHtml;
    }
}

function handleRateLimitChange(selectEl) {
    const wrap = document.getElementById('broadcast-rate-limit-custom-wrap');
    const input = document.getElementById('broadcast-rate-limit-custom');
    if (!wrap) return;
    if (selectEl && selectEl.value === 'custom') {
        wrap.classList.remove('hidden');
        wrap.classList.add('flex');
        if (input) {
            input.focus();
            if (!input.value) input.value = '75';
        }
    } else {
        wrap.classList.add('hidden');
        wrap.classList.remove('flex');
    }
}

function getSelectedRateLimit() {
    const select = document.getElementById('broadcast-rate-limit');
    if (!select) return 25;
    if (select.value === 'custom') {
        const customInput = document.getElementById('broadcast-rate-limit-custom');
        const val = parseInt(customInput?.value || '25', 10);
        return Math.max(1, Math.min(1000, isNaN(val) ? 25 : val));
    }
    const val = parseInt(select.value || '25', 10);
    return isNaN(val) ? 25 : val;
}

async function launchQuickBroadcast() {
    const subject = document.getElementById('broadcast-subject')?.value?.trim();
    const html = document.getElementById('broadcast-html')?.value?.trim();
    const senderName = document.getElementById('broadcast-sender-name')?.value?.trim() || 'Bitmail Team';
    const senderEmail = document.getElementById('broadcast-sender-email')?.value?.trim() || 'team@bitmail.io';
    const smtpId = document.getElementById('broadcast-smtp-select')?.value || null;
    const rateLimit = getSelectedRateLimit();
    const trackOpens = document.getElementById('broadcast-track-opens')?.checked ?? true;
    const trackClicks = document.getElementById('broadcast-track-clicks')?.checked ?? true;

    if (!subject) {
        showToast('Please enter an email subject line.', 'warning');
        return;
    }
    if (!html) {
        showToast('Please enter message HTML content.', 'warning');
        return;
    }

    let payload = {
        subject: subject,
        body_html: html,
        sender_name: senderName,
        sender_email: senderEmail,
        smtp_config_id: smtpId,
        rate_limit_per_second: rateLimit,
        track_opens: trackOpens,
        track_clicks: trackClicks
    };

    let scheduledIso = null;
    const isScheduled = App.broadcastTimingMode === 'schedule';
    if (isScheduled) {
        const dtVal = document.getElementById('broadcast-schedule-datetime')?.value;
        if (!dtVal) {
            showToast('Please select a scheduled date and time.', 'warning');
            return;
        }
        const parsedDate = new Date(dtVal);
        if (isNaN(parsedDate.getTime()) || parsedDate.getTime() <= Date.now()) {
            showToast('Scheduled time must be in the future.', 'warning');
            return;
        }
        scheduledIso = parsedDate.toISOString();
        payload.scheduled_at = scheduledIso;
    }

    if (App.audienceMode === 'paste') {
        const rawText = document.getElementById('broadcast-raw-emails')?.value || '';
        const parsed = parseEmailsFromString(rawText);
        if (parsed.length === 0) {
            showToast('Please paste at least one valid customer email address.', 'warning');
            return;
        }
        payload.recipients_text = rawText;
    } else {
        const listSelect = document.getElementById('broadcast-list-select');
        payload.list_id = listSelect?.value || 'all';
    }

    // Confirmation prompt
    const confirmMsg = isScheduled
        ? `Schedule broadcast "${subject}" for ${new Date(scheduledIso).toLocaleString()}?`
        : `Launch broadcast "${subject}" to your target customer recipients?`;
    const confirmDetail = isScheduled
        ? 'The automated campaign scheduler will dispatch this broadcast when the scheduled time arrives.'
        : 'Every message is delivered with rate limiting and archived in the Storage Vault.';

    const confirmed = await confirmDialog(confirmMsg, {
        title: isScheduled ? 'Confirm scheduled broadcast' : 'Confirm broadcast launch',
        detail: confirmDetail,
        confirmText: isScheduled ? 'Schedule broadcast' : 'Launch broadcast'
    });
    if (!confirmed) return;

    const btn = document.getElementById('btn-launch-broadcast');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/campaigns/quick-broadcast', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await safeJson(res);
        if (!res.ok || !data.success) {
            showToast(`Broadcast failed: ${data.detail || data.message || 'Unknown error'}`, 'error');
            return;
        }

        if (data.status === 'scheduled') {
            showToast(`📅 Broadcast scheduled for ${new Date(data.scheduled_at || scheduledIso).toLocaleString()}!`, 'success');
            await fetchCampaigns();
            return;
        }

        showToast(`🚀 Broadcast launched to ${data.total_recipients} customers!`, 'success');
        
        // Open live monitoring console
        startLiveBroadcastMonitoring(data.campaign_id, data.total_recipients);
    } catch (err) {
        showToast(`Broadcast exception: ${err.message}`, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

function startLiveBroadcastMonitoring(campaignId, totalRecipients) {
    const consoleEl = document.getElementById('broadcast-live-console');
    const logEl = document.getElementById('broadcast-log-terminal');
    const progBar = document.getElementById('broadcast-progress-bar');
    const progText = document.getElementById('broadcast-progress-text');
    const statusBadge = document.getElementById('broadcast-status-badge');

    if (consoleEl) consoleEl.classList.remove('hidden');
    if (logEl) logEl.innerHTML = `<div class="text-emerald-400">[${new Date().toLocaleTimeString()}] Broadcast ${campaignId} initialized for ${totalRecipients} customer recipients.</div>`;

    App.activeBroadcast = { campaignId, totalRecipients };

    if (App.broadcastPollInterval) clearInterval(App.broadcastPollInterval);

    App.broadcastPollInterval = setInterval(async () => {
        try {
            const res = await fetch(`/api/campaigns/${campaignId}`);
            if (!res.ok) return;
            const camp = await res.json();

            const sent = camp.sent_count || 0;
            const total = camp.total_recipients || totalRecipients || 1;
            const pct = Math.min(100, Math.round((sent / total) * 100));

            if (progBar) progBar.style.width = `${pct}%`;
            if (progText) progText.innerText = `${sent} / ${total} dispatched (${pct}%)`;

            if (logEl) {
                const logEntry = document.createElement('div');
                logEntry.className = 'text-slate-300';
                logEntry.innerHTML = `<span class="text-slate-500">[${new Date().toLocaleTimeString()}]</span> Dispatched batch progress: ${sent}/${total} • Stored in Vault`;
                logEl.appendChild(logEntry);
                logEl.scrollTop = logEl.scrollHeight;
            }

            if (camp.status === 'completed' || camp.status === 'cancelled' || sent >= total) {
                clearInterval(App.broadcastPollInterval);
                if (statusBadge) {
                    statusBadge.innerText = camp.status === 'cancelled' ? 'Cancelled' : 'Completed';
                    statusBadge.className = camp.status === 'cancelled' 
                        ? 'px-2.5 py-0.5 rounded-full text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30'
                        : 'px-2.5 py-0.5 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
                }
                const btn = document.getElementById('btn-launch-broadcast');
                if (btn) btn.disabled = false;
                showToast(`✓ Broadcast ${campaignId} execution finished! All messages archived.`, 'success');
                await refreshAllData();
            }
        } catch (err) {
            console.warn('Poll broadcast error:', err);
        }
    }, 1000);
}

async function pauseActiveBroadcast() {
    if (!App.activeBroadcast) return;
    try {
        await fetch(`/api/campaigns/${App.activeBroadcast.campaignId}/pause`, { method: 'POST' });
        showToast('Broadcast execution paused', 'info');
    } catch (err) {
        showToast('Pause error: ' + err.message, 'error');
    }
}

async function cancelActiveBroadcast() {
    if (!App.activeBroadcast) return;
    const confirmed = await confirmDialog('Cancel the remaining dispatch queue?', {
        title: 'Cancel broadcast',
        detail: 'Messages already sent cannot be recalled.',
        confirmText: 'Cancel dispatch',
        cancelText: 'Keep sending',
        danger: true
    });
    if (!confirmed) return;
    try {
        await fetch(`/api/campaigns/${App.activeBroadcast.campaignId}/cancel`, { method: 'POST' });
        showToast('Broadcast dispatch cancelled', 'warning');
    } catch (err) {
        showToast('Cancel error: ' + err.message, 'error');
    }
}

// ==========================================================================
// 3. Email Storage Vault
// ==========================================================================
async function fetchVaultEmails(query = '') {
    try {
        const url = query ? `/api/storage/emails?search=${encodeURIComponent(query)}&limit=100` : '/api/storage/emails?limit=100';
        const res = await fetch(url);
        if (!res.ok) return;
        const data = await res.json();
        App.vaultEmails = data.emails || data.items || (Array.isArray(data) ? data : []);

        filterVaultEmails();
        
        // Update summary disk
        const sumRes = await fetch('/api/storage/summary');
        if (sumRes.ok) {
            const summary = await sumRes.json();
            const diskEl = document.getElementById('vault-summary-disk');
            if (diskEl) diskEl.innerText = `Archived: ${summary.total_archived || App.vaultEmails.length} emails (${summary.disk_size_human || '0.1 MB'})`;
        }
    } catch (err) {
        console.warn('Vault fetch error:', err);
    }
}

function renderVaultTable(rows = App.vaultEmails) {
    const tbody = document.getElementById('vault-table-body');
    if (!tbody) return;

    pruneSelection('vault');

    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-xs text-slate-500">No emails stored in Vault yet. Dispatched emails will be archived here automatically.</td></tr>`;
        renderBulkBar('vault');
        return;
    }

    tbody.innerHTML = rows.map(item => {
        const statusClass = item.status === 'delivered' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' :
            item.status === 'simulated' ? 'bg-indigo-500/20 text-indigo-300 border-indigo-500/30' :
            item.status === 'sent' ? 'bg-blue-500/20 text-blue-300 border-blue-500/30' :
            'bg-rose-500/20 text-rose-300 border-rose-500/30';

        return `
            <tr class="hover:bg-slate-900/50 transition-colors border-b border-white/5 text-xs">
                ${selectionCheckboxCell('vault', item.id)}
                <td class="py-3 px-4">
                    <div class="font-semibold text-white">${escapeHtml(item.recipient || item.recipient_email)}</div>
                    <div class="text-[11px] text-slate-400">${escapeHtml(item.recipient_name || item.recipientName || 'Customer')}</div>
                </td>
                <td class="py-3 px-4">
                    <div class="font-medium text-slate-200 truncate max-w-xs">${escapeHtml(item.subject)}</div>
                    <div class="text-[11px] text-indigo-400 truncate">${escapeHtml(item.campaign_name || item.campaignName || 'Direct / Broadcast')}</div>
                </td>
                <td class="py-3 px-4">
                    <span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase border ${statusClass}">${item.status}</span>
                </td>
                <td class="py-3 px-4 text-slate-400 font-mono text-[11px]">
                    ${formatDate(item.sent_at || item.sentAt || item.created_at)}
                </td>
                <td class="py-3 px-4">
                    <div class="flex items-center gap-2 text-[11px]">
                        <span class="${(item.open_count || 0) > 0 ? 'text-purple-300 font-semibold' : 'text-slate-500'}">Opens: ${item.open_count || 0}</span>
                        <span class="text-slate-600">•</span>
                        <span class="${(item.click_count || 0) > 0 ? 'text-blue-300 font-semibold' : 'text-slate-500'}">Clicks: ${item.click_count || 0}</span>
                    </div>
                </td>
                <td class="py-3 px-4 text-right">
                    <div class="flex items-center justify-end gap-2">
                        <button onclick="inspectVaultEmail('${item.id}')" class="px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 text-[11px] font-semibold transition-colors">
                            Inspect
                        </button>
                        <a href="/api/storage/emails/${item.id}/eml" download="${item.id}.eml" class="p-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-white/5 transition-colors" title="Download RFC .EML file">
                            <i data-lucide="download" class="w-3.5 h-3.5"></i>
                        </a>
                        <button onclick="deleteVaultEmail('${item.id}')" class="p-1 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 transition-colors" title="Delete archived email">
                            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');

    renderBulkBar('vault');
    initLucide();
}

async function deleteVaultEmail(emailId) {
    const item = App.vaultEmails.find(e => e.id === emailId);
    await deleteOne('vault', emailId,
        `Delete the archived email to ${item ? (item.recipient || item.recipient_email) : 'this recipient'}? The stored .eml file is removed from disk too.`);
}

function filterVaultEmails() {
    const q = document.getElementById('vault-search-input')?.value || '';
    const status = document.getElementById('vault-filter-status')?.value || 'all';
    
    let filtered = App.vaultEmails;
    if (status !== 'all') {
        filtered = filtered.filter(e => (e.status || '').toLowerCase() === status.toLowerCase());
    }
    if (q) {
        const lower = q.toLowerCase();
        filtered = filtered.filter(e => 
            (e.recipient_email || e.recipient || '').toLowerCase().includes(lower) ||
            (e.subject || '').toLowerCase().includes(lower) ||
            (e.id || '').toLowerCase().includes(lower)
        );
    }
    
    const tbody = document.getElementById('vault-table-body');
    if (!tbody) return;
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-6 text-xs text-slate-500">No matching emails found in Vault.</td></tr>`;
        renderBulkBar('vault');
        return;
    }
    renderVaultTable(filtered);
}

async function inspectVaultEmail(storageId) {
    try {
        const res = await fetch(`/api/storage/emails/${storageId}`);
        if (!res.ok) return;
        const email = await res.json();

        const subjEl = document.getElementById('vault-modal-subject');
        const recipEl = document.getElementById('vault-modal-recipient');
        const sentEl = document.getElementById('vault-modal-sent-at');
        const badgeEl = document.getElementById('vault-modal-status-badge');
        const iframe = document.getElementById('vault-preview-iframe');
        const headersEl = document.getElementById('vault-content-headers');
        const metaEl = document.getElementById('vault-content-metadata');
        const downloadBtn = document.getElementById('vault-modal-download-eml');

        if (subjEl) subjEl.innerText = email.subject || 'No Subject';
        if (recipEl) recipEl.innerText = `To: ${email.recipient_name ? email.recipient_name + ' <' + email.recipient_email + '>' : email.recipient_email}`;
        if (sentEl) sentEl.innerText = `Sent: ${formatDate(email.sent_at || email.created_at)}`;
        if (badgeEl) {
            badgeEl.innerText = email.status;
            badgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold ' + (email.status === 'delivered' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-indigo-500/20 text-indigo-300');
        }

        if (iframe) iframe.src = `/api/storage/emails/${storageId}/rendered`;
        if (headersEl) headersEl.innerText = JSON.stringify(email.headers || {}, null, 2);
        if (metaEl) metaEl.innerText = JSON.stringify(email.metadata || email.variables || {}, null, 2);

        if (downloadBtn) {
            downloadBtn.onclick = () => {
                window.location.href = `/api/storage/emails/${storageId}/eml`;
            };
        }

        setVaultModalTab('preview');
        openModal('modal-email-detail');
        initLucide();
    } catch (err) {
        showToast('Failed to inspect email: ' + err.message, 'error');
    }
}

function setVaultModalTab(tab) {
    const btnPrev = document.getElementById('tab-btn-vault-preview');
    const btnHead = document.getElementById('tab-btn-vault-headers');
    const btnMeta = document.getElementById('tab-btn-vault-metadata');

    const conPrev = document.getElementById('vault-content-preview');
    const conHead = document.getElementById('vault-content-headers');
    const conMeta = document.getElementById('vault-content-metadata');

    [btnPrev, btnHead, btnMeta].forEach(b => {
        b?.classList.remove('text-indigo-400', 'border-b-2', 'border-indigo-500');
        b?.classList.add('text-slate-400');
    });

    [conPrev, conHead, conMeta].forEach(c => c?.classList.add('hidden'));

    if (tab === 'preview') {
        btnPrev?.classList.add('text-indigo-400', 'border-b-2', 'border-indigo-500');
        conPrev?.classList.remove('hidden');
    } else if (tab === 'headers') {
        btnHead?.classList.add('text-indigo-400', 'border-b-2', 'border-indigo-500');
        conHead?.classList.remove('hidden');
    } else if (tab === 'metadata') {
        btnMeta?.classList.add('text-indigo-400', 'border-b-2', 'border-indigo-500');
        conMeta?.classList.remove('hidden');
    }
}

// ==========================================================================
// 4. Customers & Subscriber Management
// ==========================================================================
async function fetchSubscribers() {
    try {
        const res = await fetch('/api/subscribers?limit=200');
        if (!res.ok) return;
        const data = await res.json();
        App.subscribers = Array.isArray(data) ? data : (data.items || data.subscribers || []);

        const listsRes = await fetch('/api/lists');
        if (listsRes.ok) {
            App.lists = await listsRes.json();
        }

        filterSubscribers();
        populateBroadcastDropdowns();
    } catch (err) {
        console.warn('Subscribers fetch error:', err);
    }
}

function renderSubscribersTable(rows = App.subscribers) {
    const tbody = document.getElementById('subscribers-table-body');
    if (!tbody) return;

    pruneSelection('subscribers');

    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center py-8 text-xs text-slate-500">No customer contacts in database yet. Click "Paste Multiple Emails" or "Add Customer" to add some!</td></tr>`;
        renderBulkBar('subscribers');
        return;
    }

    tbody.innerHTML = rows.map(sub => {
        let tagsArr = [];
        try {
            if (typeof sub.tags === 'string') tagsArr = JSON.parse(sub.tags);
            else if (Array.isArray(sub.tags)) tagsArr = sub.tags;
        } catch (e) {}

        const fullName = `${sub.first_name || ''} ${sub.last_name || ''}`.trim() || sub.email.split('@')[0];
        const statusClass = sub.status === 'active' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : 'bg-rose-500/20 text-rose-300 border-rose-500/30';

        // Render Group / List memberships
        const groupBadges = (sub.lists || []).map(lid => {
            const listObj = App.lists.find(l => l.id === lid);
            return listObj ? `<span class="px-2 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/25 text-[10px] font-semibold flex items-center gap-1" title="Customer Group / List">📁 ${escapeHtml(listObj.name)}</span>` : '';
        }).filter(Boolean).join('');

        // Render Custom Attributes / Placeholders
        let customBadges = '';
        try {
            const cf = typeof sub.custom_fields === 'string' ? JSON.parse(sub.custom_fields || '{}') : (sub.custom_fields || {});
            customBadges = Object.entries(cf).map(([k, v]) =>
                `<span class="px-2 py-0.5 rounded bg-indigo-950/60 text-indigo-300 border border-indigo-500/25 text-[10px] font-mono" title="Custom Attribute / Placeholder: {{${escapeHtml(k)}}}">&#123;&#123;${escapeHtml(k)}&#125;&#125;: <strong class="text-white">${escapeHtml(String(v))}</strong></span>`
            ).join('');
        } catch (e) {}

        const tagsBadges = tagsArr.map(t => `<span class="px-2 py-0.5 rounded bg-slate-800 text-[10px] text-slate-300 border border-white/5">${escapeHtml(t)}</span>`).join('');
        const attributesContent = (groupBadges || customBadges || tagsBadges) ? `${groupBadges}${customBadges}${tagsBadges}` : `<span class="text-slate-600 text-[11px]">—</span>`;

        return `
            <tr class="hover:bg-slate-900/50 transition-colors border-b border-white/5 text-xs">
                ${selectionCheckboxCell('subscribers', sub.id)}
                <td class="py-3 px-4">
                    <div class="font-bold text-white">${escapeHtml(sub.email)}</div>
                    <div class="text-[11px] text-slate-400">${escapeHtml(fullName)}</div>
                </td>
                <td class="py-3 px-4">
                    <div class="flex items-center gap-1.5 flex-wrap">
                        ${attributesContent}
                    </div>
                </td>
                <td class="py-3 px-4">
                    <span class="px-2.5 py-0.5 rounded-full text-[10px] font-bold uppercase border ${statusClass}">${sub.status || 'active'}</span>
                </td>
                <td class="py-3 px-4 text-slate-400 text-[11px]">
                    ${formatDate(sub.created_at)}
                </td>
                <td class="py-3 px-4 text-right">
                    <div class="flex items-center justify-end gap-1.5">
                        <button onclick="openEditSubscriberModal('${sub.id}')" class="p-1.5 rounded-lg bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300 border border-indigo-500/20 transition-colors" title="Edit customer">
                            <i data-lucide="pencil" class="w-3.5 h-3.5"></i>
                        </button>
                        <button onclick="deleteSubscriber('${sub.id}')" class="p-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 transition-colors" title="Delete customer">
                            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');

    renderBulkBar('subscribers');
    initLucide();
}

function openBulkPasteModal() {
    openModal('modal-bulk-paste');
}

async function submitBulkCustomerEmails() {
    const rawText = document.getElementById('modal-bulk-emails-text')?.value || '';
    const tagsInput = document.getElementById('modal-bulk-tags')?.value || '';

    if (!rawText.trim()) {
        showToast('Please paste at least one customer email address.', 'warning');
        return;
    }

    const tags = tagsInput.split(',').map(t => t.trim()).filter(t => t.length > 0);

    try {
        const res = await fetch('/api/subscribers/bulk-text', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw_text: rawText, tags: tags })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ ${data.message}`, 'success');
            closeModal('modal-bulk-paste');
            document.getElementById('modal-bulk-emails-text').value = '';
            await fetchSubscribers();
            await fetchDashboardStats();
        } else {
            showToast(data.message || 'Failed to import emails', 'error');
        }
    } catch (err) {
        showToast('Import error: ' + err.message, 'error');
    }
}

function toggleCsvDestMode() {
    const isNew = document.getElementById('csv-dest-new')?.checked;
    const listSelect = document.getElementById('modal-csv-list-select');
    const newNameInput = document.getElementById('modal-csv-new-list-name');
    
    if (listSelect && newNameInput) {
        if (isNew) {
            listSelect.disabled = true;
            listSelect.classList.add('opacity-40', 'cursor-not-allowed');
            newNameInput.disabled = false;
            newNameInput.classList.remove('opacity-40', 'cursor-not-allowed');
            newNameInput.focus();
        } else {
            listSelect.disabled = false;
            listSelect.classList.remove('opacity-40', 'cursor-not-allowed');
            newNameInput.disabled = true;
            newNameInput.classList.add('opacity-40', 'cursor-not-allowed');
        }
    }
}

function openCsvImportModal() {
    const fileEl = document.getElementById('modal-csv-file');
    if (fileEl) fileEl.value = '';
    const newNameInput = document.getElementById('modal-csv-new-list-name');
    if (newNameInput) newNameInput.value = '';
    const existingRadio = document.getElementById('csv-dest-existing');
    if (existingRadio) existingRadio.checked = true;
    populateBroadcastDropdowns();
    toggleCsvDestMode();
    openModal('modal-csv-import');
}

async function submitCsvImport() {
    const fileEl = document.getElementById('modal-csv-file');
    const file = fileEl?.files?.[0];
    if (!file) {
        showToast('Choose a CSV file first.', 'warning');
        return;
    }

    const form = new FormData();
    form.append('file', file);
    form.append('update_duplicates', 'true');

    const isNew = document.getElementById('csv-dest-new')?.checked;
    if (isNew) {
        const newName = document.getElementById('modal-csv-new-list-name')?.value?.trim();
        if (!newName) {
            showToast('Please enter a name for the new customer table.', 'warning');
            return;
        }
        form.append('new_list_name', newName);
    } else {
        const listSelect = document.getElementById('modal-csv-list-select');
        if (listSelect && listSelect.value) {
            form.append('list_id', listSelect.value);
        }
    }

    showToast(`Importing ${file.name}...`, 'info');

    try {
        const res = await fetch('/api/subscribers/import-csv', { method: 'POST', body: form });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || 'CSV import failed', 'error');
            return;
        }

        const destInfo = data.list_name 
            ? ` into new table "${data.list_name}"`
            : (data.list_id ? ' into selected table' : '');

        showToast(`Imported ${data.added_count} new, updated ${data.updated_count}${destInfo}.`,
            data.failed_count ? 'warning' : 'success');

        // Automatically detect, register, and display custom placeholder tags from CSV multi-column import
        if (data.custom_fields_detected && Array.isArray(data.custom_fields_detected) && data.custom_fields_detected.length > 0) {
            let newlyAdded = 0;
            data.custom_fields_detected.forEach(tag => {
                const cleanTag = String(tag).trim().toLowerCase().replace(/[^a-z0-9_]/g, '_');
                if (cleanTag && !App.customPlaceholders.includes(cleanTag)) {
                    App.customPlaceholders.push(cleanTag);
                    newlyAdded++;
                }
            });
            if (newlyAdded > 0) {
                saveCustomPlaceholders();
                renderAllPlaceholderChips();
                showToast(`✓ Discovered ${newlyAdded} new placeholder(s): ${data.custom_fields_detected.map(t => '{{' + t + '}}').join(', ')}`, 'info');
            }
        }

        closeModal('modal-csv-import');

        // Refresh lists if a new table was created
        const listsRes = await fetch('/api/lists');
        if (listsRes.ok) {
            App.lists = await listsRes.json();
            populateBroadcastDropdowns();
            renderManageGroupsList();
        }

        await fetchSubscribers();
        await fetchDashboardStats();
    } catch (err) {
        showToast('CSV import error: ' + err.message, 'error');
    }
}

const TABLE_PRESETS = {
    ecommerce: {
        name: 'E-Commerce Orders',
        desc: 'Customer purchase orders with tracking and item details',
        columns: ['order_id', 'product_name', 'quantity', 'order_total', 'tracking_no']
    },
    billing: {
        name: 'Invoices & Billing',
        desc: 'Client invoice notifications and payment schedules',
        columns: ['invoice_no', 'due_date', 'amount_due', 'payment_status', 'billing_period']
    },
    leads: {
        name: 'B2B Sales Leads',
        desc: 'Qualified leads, accounts, and contact designations',
        columns: ['company_name', 'job_title', 'phone_number', 'lead_source', 'deal_size']
    },
    events: {
        name: 'Event Attendees',
        desc: 'Conferences, webinars, and RSVP ticket management',
        columns: ['event_name', 'ticket_tier', 'event_date', 'seat_number', 'qr_code']
    },
    membership: {
        name: 'SaaS Memberships',
        desc: 'Active subscriptions, license tiers, and renewal dates',
        columns: ['account_id', 'plan_name', 'billing_interval', 'renewal_date', 'support_tier']
    },
    custom: {
        name: '',
        desc: '',
        columns: []
    }
};

function applyTableTemplate(presetKey) {
    const preset = TABLE_PRESETS[presetKey];
    if (!preset) return;

    ['ecommerce', 'billing', 'leads', 'events', 'membership', 'custom'].forEach(k => {
        const card = document.getElementById(`tpl-card-${k}`);
        if (card) {
            if (k === presetKey) {
                card.classList.add('border-purple-500/50', 'bg-purple-950/20');
                card.classList.remove('border-white/10');
            } else {
                card.classList.remove('border-purple-500/50', 'bg-purple-950/20');
                card.classList.add('border-white/10');
            }
        }
    });

    const nameInput = document.getElementById('new-group-name');
    const descInput = document.getElementById('new-group-desc');
    if (preset.name && nameInput) nameInput.value = preset.name;
    if (preset.desc && descInput) descInput.value = preset.desc;

    App.newTableColumns = [...preset.columns];
    renderNewTableColumns();
}

function renderNewTableColumns() {
    const container = document.getElementById('new-table-columns-chips');
    if (!container) return;

    if (!App.newTableColumns || App.newTableColumns.length === 0) {
        container.innerHTML = `<span class="text-xs text-slate-500 italic p-1">No custom columns added yet. Type a column name below or pick a template above.</span>`;
        return;
    }

    container.innerHTML = App.newTableColumns.map(col => `
        <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-purple-950/80 border border-purple-500/30 text-purple-200 text-xs font-mono shadow-xs">
            <span>&#123;&#123;${col}&#125;&#125;</span>
            <button type="button" onclick="removeTableColumn('${col}')" class="text-slate-400 hover:text-rose-400 transition-colors cursor-pointer" title="Remove column">&times;</button>
        </span>
    `).join('');
}

function addNewTableColumnChip() {
    const input = document.getElementById('new-table-col-input');
    if (!input) return;
    const raw = input.value.trim();
    if (!raw) return;

    const clean = raw.toLowerCase().replace(/[{}]/g, '').replace(/[^a-z0-9_]/g, '_');
    if (!clean) {
        showToast('Invalid column name', 'warning');
        return;
    }

    if (!App.newTableColumns) App.newTableColumns = [];
    if (!App.newTableColumns.includes(clean)) {
        App.newTableColumns.push(clean);
        renderNewTableColumns();
        input.value = '';
    } else {
        showToast(`Column "${clean}" already exists in table`, 'info');
    }
}

function removeTableColumn(col) {
    if (!App.newTableColumns) return;
    App.newTableColumns = App.newTableColumns.filter(c => c !== col);
    renderNewTableColumns();
}

function openCreateGroupModal() {
    const input = document.getElementById('new-group-name');
    const desc = document.getElementById('new-group-desc');
    const colInput = document.getElementById('new-table-col-input');
    if (input) input.value = '';
    if (desc) desc.value = '';
    if (colInput) colInput.value = '';
    App.newTableColumns = [];
    applyTableTemplate('billing');
    openModal('modal-create-group');
    if (input) setTimeout(() => input.focus(), 60);
}

async function handleCreateGroupSubmit(e) {
    if (e) e.preventDefault();
    const name = document.getElementById('new-group-name')?.value?.trim();
    const desc = document.getElementById('new-group-desc')?.value?.trim();
    if (!name) {
        showToast('Please enter a group / table name.', 'warning');
        return;
    }
    const btn = document.getElementById('btn-create-group-submit');
    if (btn) btn.disabled = true;

    try {
        const res = await fetch('/api/lists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                name, 
                description: desc || '',
                schema_fields: App.newTableColumns || []
            })
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(`Failed to create table: ${data.detail || 'Unknown error'}`, 'error');
            return;
        }
        showToast(`Customer table "${name}" created with ${App.newTableColumns ? App.newTableColumns.length : 0} placeholder columns!`, 'success');
        closeModal('modal-create-group');
        
        // Refresh lists and dropdowns across UI
        const listsRes = await fetch('/api/lists');
        if (listsRes.ok) {
            App.lists = await listsRes.json();
            populateBroadcastDropdowns();
            renderManageGroupsList();
        }
        await fetchSubscribers();
    } catch (err) {
        showToast(`Error creating table: ${err.message}`, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function openManageGroupsModal() {
    try {
        const res = await fetch('/api/lists');
        if (res.ok) {
            App.lists = await res.json();
            populateBroadcastDropdowns();
        }
    } catch (e) {
        console.error('Failed to fetch lists:', e);
    }
    renderManageGroupsList();
    openModal('modal-manage-groups');
}

function renderManageGroupsList() {
    const container = document.getElementById('manage-groups-list');
    if (!container) return;

    const countBadge = document.getElementById('manage-groups-count-badge');
    if (countBadge) {
        countBadge.textContent = `${App.lists.length} Group${App.lists.length === 1 ? '' : 's'}`;
    }

    if (!App.lists || App.lists.length === 0) {
        container.innerHTML = `
            <div class="py-12 text-center text-slate-500">
                <i data-lucide="folders" class="w-12 h-12 mx-auto text-slate-600 mb-3 opacity-60"></i>
                <p class="text-sm font-medium text-slate-300">No customer groups yet</p>
                <p class="text-xs text-slate-500 mt-1 max-w-sm mx-auto">Create targeted customer lists to organize audience segments and run dedicated campaigns.</p>
                <button type="button" onclick="openCreateGroupModal()" class="mt-4 px-4 py-2 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold inline-flex items-center gap-1.5 transition-all cursor-pointer">
                    <i data-lucide="plus" class="w-4 h-4"></i> Create First Group
                </button>
            </div>
        `;
        if (window.lucide) lucide.createIcons();
        return;
    }

    container.innerHTML = App.lists.map(list => {
        const count = list.subscriber_count || 0;
        const safeName = escapeHtml(list.name || 'Unnamed Group');
        const safeDesc = escapeHtml(list.description || 'No description provided');
        const listId = list.id;

        let schemaCols = [];
        try {
            if (Array.isArray(list.schema_fields)) schemaCols = list.schema_fields;
            else if (typeof list.schema_fields === 'string') schemaCols = JSON.parse(list.schema_fields || '[]');
        } catch(e) {}

        const schemaBadges = schemaCols.length > 0
            ? `<div class="flex items-center gap-1 flex-wrap mt-1.5">
                 <span class="text-[10px] text-purple-400 font-semibold flex items-center gap-0.5"><i data-lucide="columns" class="w-2.5 h-2.5"></i> Schema:</span>
                 ${schemaCols.slice(0, 5).map(c => `<span class="px-1.5 py-0.5 rounded bg-purple-950/60 text-purple-300 border border-purple-500/25 text-[10px] font-mono">&#123;&#123;${escapeHtml(c)}&#125;&#125;</span>`).join('')}
                 ${schemaCols.length > 5 ? `<span class="text-[10px] text-purple-400 font-mono">+${schemaCols.length - 5} more</span>` : ''}
               </div>`
            : '';

        return `
            <div class="p-4 rounded-xl bg-slate-900/80 border border-white/5 hover:border-purple-500/30 transition-all flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div class="flex items-start gap-3 min-w-0">
                    <div class="w-9 h-9 rounded-lg bg-purple-500/10 text-purple-400 border border-purple-500/20 flex items-center justify-center shrink-0 mt-0.5">
                        <i data-lucide="folder" class="w-4 h-4"></i>
                    </div>
                    <div class="min-w-0">
                        <div class="flex items-center gap-2 flex-wrap">
                            <h4 class="text-sm font-semibold text-white truncate">${safeName}</h4>
                            <span class="px-2 py-0.5 rounded-full text-[10px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/30 whitespace-nowrap">
                                ${count} contact${count === 1 ? '' : 's'}
                            </span>
                        </div>
                        <p class="text-xs text-slate-400 mt-0.5 line-clamp-1">${safeDesc}</p>
                        ${schemaBadges}
                    </div>
                </div>
                <div class="flex items-center gap-1.5 shrink-0 self-end sm:self-center">
                    <button type="button" onclick="viewGroupCustomers('${listId}')" class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white text-xs font-medium border border-white/5 transition-all flex items-center gap-1 cursor-pointer" title="View contacts in this group">
                        <i data-lucide="users" class="w-3.5 h-3.5 text-cyan-400"></i> View
                    </button>
                    <button type="button" onclick="openEditGroupModal('${listId}')" class="px-2.5 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white text-xs font-medium border border-white/5 transition-all flex items-center gap-1 cursor-pointer" title="Rename or edit description">
                        <i data-lucide="edit-3" class="w-3.5 h-3.5 text-amber-400"></i> Rename
                    </button>
                    <button type="button" onclick="deleteCustomerGroup('${listId}', '${safeName.replace(/'/g, "\\'")}')" class="p-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20 transition-all cursor-pointer" title="Delete group">
                        <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                    </button>
                </div>
            </div>
        `;
    }).join('');

    if (window.lucide) lucide.createIcons();
}

function viewGroupCustomers(listId) {
    closeModal('modal-manage-groups');
    switchTab('subscribers');
    const filterSelect = document.getElementById('subscriber-filter-list');
    if (filterSelect) {
        filterSelect.value = listId;
        filterSubscribers();
    }
}

function openEditGroupModal(listId) {
    const group = App.lists.find(l => l.id === listId);
    if (!group) return;

    const idInput = document.getElementById('edit-group-id');
    const nameInput = document.getElementById('edit-group-name');
    const descInput = document.getElementById('edit-group-desc');

    if (idInput) idInput.value = group.id;
    if (nameInput) nameInput.value = group.name || '';
    if (descInput) descInput.value = group.description || '';

    openModal('modal-edit-group');
    if (nameInput) setTimeout(() => nameInput.focus(), 60);
}

async function handleEditGroupSubmit(e) {
    if (e) e.preventDefault();
    const id = document.getElementById('edit-group-id')?.value;
    const name = document.getElementById('edit-group-name')?.value?.trim();
    const desc = document.getElementById('edit-group-desc')?.value?.trim();

    if (!id || !name) {
        showToast('Please enter a group name.', 'warning');
        return;
    }

    try {
        const res = await fetch(`/api/lists/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description: desc || '' })
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(`Failed to update group: ${data.detail || 'Unknown error'}`, 'error');
            return;
        }

        showToast(`Group "${name}" updated successfully!`, 'success');
        closeModal('modal-edit-group');

        // Refresh lists
        const listsRes = await fetch('/api/lists');
        if (listsRes.ok) {
            App.lists = await listsRes.json();
            populateBroadcastDropdowns();
            renderManageGroupsList();
        }
        await fetchSubscribers();
    } catch (err) {
        showToast(`Error updating group: ${err.message}`, 'error');
    }
}

async function deleteCustomerGroup(listId, groupName) {
    if (!confirm(`Are you sure you want to delete the group "${groupName}"?\n\nCustomer contacts in this group will NOT be deleted; they will simply no longer belong to this group.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/lists/${listId}`, {
            method: 'DELETE'
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(`Failed to delete group: ${data.detail || 'Unknown error'}`, 'error');
            return;
        }

        showToast(`Group "${groupName}" deleted successfully!`, 'success');

        // Refresh lists
        const listsRes = await fetch('/api/lists');
        if (listsRes.ok) {
            App.lists = await listsRes.json();
            populateBroadcastDropdowns();
            renderManageGroupsList();
        }
        await fetchSubscribers();
    } catch (err) {
        showToast(`Error deleting group: ${err.message}`, 'error');
    }
}

function onSubListSelectionChanged() {
    const listSelect = document.getElementById('modal-sub-list-select');
    const section = document.getElementById('modal-sub-table-schema-section');
    const inputsContainer = document.getElementById('modal-sub-table-schema-inputs');
    if (!listSelect || !section || !inputsContainer) return;

    const listId = listSelect.value;
    if (!listId) {
        section.classList.add('hidden');
        inputsContainer.innerHTML = '';
        return;
    }

    const list = App.lists.find(l => l.id === listId);
    let schema = [];
    if (list && list.schema_fields) {
        if (Array.isArray(list.schema_fields)) schema = list.schema_fields;
        else if (typeof list.schema_fields === 'string') {
            try { schema = JSON.parse(list.schema_fields); } catch (e) {}
        }
    }

    if (schema.length === 0) {
        section.classList.add('hidden');
        inputsContainer.innerHTML = '';
        return;
    }

    section.classList.remove('hidden');
    inputsContainer.innerHTML = schema.map(col => `
        <div>
            <label class="block text-[11px] font-mono text-purple-300 mb-1 flex items-center justify-between">
                <span>&#123;&#123;${escapeHtml(col)}&#125;&#125;</span>
                <span class="text-[9px] text-slate-500 font-sans">Column</span>
            </label>
            <input type="text" data-schema-col="${escapeHtml(col)}" class="schema-col-input w-full px-2.5 py-1.5 bg-slate-900 border border-white/10 rounded-lg text-xs text-white focus:outline-none focus:border-purple-500 font-mono" placeholder="Value for ${escapeHtml(col)}">
        </div>
    `).join('');
    if (window.lucide) lucide.createIcons();
}

function addSubCustomFieldRow(key = '', val = '') {
    const container = document.getElementById('modal-sub-custom-fields-list');
    if (!container) return;

    const cleanKey = key.trim().toLowerCase().replace(/[{}]/g, '').replace(/[^a-z0-9_]/g, '_');
    const row = document.createElement('div');
    row.className = 'sub-custom-field-row flex items-center gap-2 bg-slate-900/60 p-2 rounded-xl border border-white/5';
    row.innerHTML = `
        <div class="w-1/3 min-w-0">
            <input type="text" placeholder="e.g. phone" value="${escapeHtml(key)}" oninput="updateKeyPlaceholderBadge(this)" class="custom-field-key w-full px-2.5 py-1.5 bg-slate-900 border border-white/10 rounded-lg text-xs text-indigo-300 font-mono focus:outline-none focus:border-indigo-500">
        </div>
        <div class="flex-1 min-w-0">
            <input type="text" placeholder="Value..." value="${escapeHtml(String(val))}" class="custom-field-val w-full px-2.5 py-1.5 bg-slate-900 border border-white/10 rounded-lg text-xs text-white focus:outline-none focus:border-indigo-500">
        </div>
        <span class="custom-field-badge px-2 py-1 rounded bg-indigo-950/70 text-indigo-300 text-[10px] font-mono border border-indigo-500/25 shrink-0 hidden sm:inline-block">
            &#123;&#123;${cleanKey || 'tag'}&#125;&#125;
        </span>
        <button type="button" onclick="this.closest('.sub-custom-field-row').remove()" class="p-1 text-slate-400 hover:text-rose-400 cursor-pointer" title="Remove placeholder">
            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
        </button>
    `;
    container.appendChild(row);
    if (window.lucide) lucide.createIcons();
}

function updateKeyPlaceholderBadge(input) {
    const row = input.closest('.sub-custom-field-row');
    if (!row) return;
    const badge = row.querySelector('.custom-field-badge');
    if (!badge) return;
    const cleanKey = input.value.trim().toLowerCase().replace(/[{}]/g, '').replace(/[^a-z0-9_]/g, '_');
    badge.textContent = `{{${cleanKey || 'tag'}}}`;
}

function openAddSubscriberModal() {
    App.editing.subscriber = null;
    setText('modal-sub-title', 'Add Customer Contact');
    setText('modal-sub-submit', 'Save Customer');
    setText('btn-sub-submit', 'Save Customer');
    setValue('modal-sub-email', '');
    setValue('modal-sub-first-name', '');
    setValue('modal-sub-last-name', '');
    setValue('modal-sub-status', 'active');
    
    // Ensure dropdown options are loaded
    populateBroadcastDropdowns();
    setValue('modal-sub-list-select', '');

    // Reset table schema inputs & custom fields list
    const customList = document.getElementById('modal-sub-custom-fields-list');
    if (customList) customList.innerHTML = '';
    onSubListSelectionChanged();

    openModal('modal-add-subscriber');
}

function openEditSubscriberModal(subId) {
    const sub = App.subscribers.find(s => s.id === subId);
    if (!sub) {
        showToast('Customer record is no longer loaded. Refresh and try again.', 'warning');
        return;
    }
    App.editing.subscriber = subId;
    setText('modal-sub-title', 'Edit Customer Contact');
    setText('modal-sub-submit', 'Save Changes');
    setText('btn-sub-submit', 'Save Changes');
    setValue('modal-sub-email', sub.email || '');
    setValue('modal-sub-first-name', sub.first_name || '');
    setValue('modal-sub-last-name', sub.last_name || '');
    setValue('modal-sub-status', sub.status || 'active');
    
    // Ensure dropdown options are loaded
    populateBroadcastDropdowns();
    const assignedList = (sub.lists && sub.lists[0]) || '';
    setValue('modal-sub-list-select', assignedList);
    onSubListSelectionChanged();

    // Reset custom fields list
    const customList = document.getElementById('modal-sub-custom-fields-list');
    if (customList) customList.innerHTML = '';

    // Parse custom fields
    let cf = {};
    try {
        cf = typeof sub.custom_fields === 'string' ? JSON.parse(sub.custom_fields || '{}') : (sub.custom_fields || {});
    } catch (e) {}

    // Populate schema inputs if present
    const schemaInputs = document.querySelectorAll('.schema-col-input');
    const handledCols = new Set();
    schemaInputs.forEach(input => {
        const col = input.getAttribute('data-schema-col');
        if (col && cf[col] !== undefined) {
            input.value = cf[col];
            handledCols.add(col);
        }
    });

    // Any remaining custom fields become key-value rows
    Object.entries(cf).forEach(([k, v]) => {
        if (!handledCols.has(k)) {
            addSubCustomFieldRow(k, v);
        }
    });

    openModal('modal-add-subscriber');
}

async function submitAddSubscriber() {
    const email = document.getElementById('modal-sub-email')?.value?.trim();
    const first = document.getElementById('modal-sub-first-name')?.value?.trim();
    const last = document.getElementById('modal-sub-last-name')?.value?.trim();
    const status = document.getElementById('modal-sub-status')?.value || 'active';
    const listSelect = document.getElementById('modal-sub-list-select');
    const selectedList = listSelect ? listSelect.value : null;

    if (!email || !email.includes('@')) {
        showToast('Please enter a valid email address.', 'warning');
        return;
    }

    // Collect custom fields from Schema Inputs and Custom Key-Value Rows
    const customFields = {};

    // 1. Schema inputs
    const schemaInputs = document.querySelectorAll('.schema-col-input');
    schemaInputs.forEach(input => {
        const col = input.getAttribute('data-schema-col');
        if (col) {
            const val = input.value.trim();
            if (val) customFields[col] = val;
        }
    });

    // 2. Custom field rows
    const customRows = document.querySelectorAll('.sub-custom-field-row');
    customRows.forEach(row => {
        const keyInput = row.querySelector('.custom-field-key');
        const valInput = row.querySelector('.custom-field-val');
        if (keyInput && valInput) {
            const rawKey = keyInput.value.trim();
            const cleanKey = rawKey.toLowerCase().replace(/[{}]/g, '').replace(/[^a-z0-9_]/g, '_');
            const val = valInput.value.trim();
            if (cleanKey && val) {
                customFields[cleanKey] = val;
            }
        }
    });

    const editingId = App.editing.subscriber;

    try {
        const res = await fetch(editingId ? `/api/subscribers/${editingId}` : '/api/subscribers', {
            method: editingId ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                email: email,
                first_name: first || null,
                last_name: last || null,
                status: status,
                custom_fields: customFields,
                list_ids: selectedList ? [selectedList] : []
            })
        });

        const data = await safeJson(res);
        if (res.ok) {
            showToast(editingId ? `Customer ${email} updated.` : `Customer ${email} added successfully.`, 'success');
            App.editing.subscriber = null;
            closeModal('modal-add-subscriber');
            await fetchSubscribers();
            await fetchDashboardStats();

            // Discover new placeholders into App.customPlaceholders
            let addedAny = false;
            Object.keys(customFields).forEach(k => {
                if (!App.customPlaceholders.includes(k)) {
                    App.customPlaceholders.push(k);
                    addedAny = true;
                }
            });
            if (addedAny) {
                saveCustomPlaceholders();
                renderAllPlaceholderChips();
            }
        } else {
            showToast(data.detail || 'Failed to save customer', 'error');
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

async function deleteSubscriber(subId) {
    const sub = App.subscribers.find(s => s.id === subId);
    await deleteOne('subscribers', subId,
        `Remove ${sub ? sub.email : 'this customer contact'} from your customer database?`);
}

function filterSubscribers() {
    const q = document.getElementById('subscriber-search-input')?.value?.toLowerCase() || '';
    const status = document.getElementById('subscriber-filter-status')?.value || 'all';
    const listFilter = document.getElementById('subscriber-filter-list')?.value || 'all';

    let filtered = App.subscribers;
    if (status !== 'all') {
        filtered = filtered.filter(s => (s.status || 'active').toLowerCase() === status.toLowerCase());
    }
    if (listFilter !== 'all') {
        filtered = filtered.filter(s => Array.isArray(s.lists) && s.lists.includes(listFilter));
    }
    if (q) {
        filtered = filtered.filter(s => {
            const email = (s.email || '').toLowerCase();
            const fn = (s.first_name || '').toLowerCase();
            const ln = (s.last_name || '').toLowerCase();
            const tags = String(s.tags || '').toLowerCase();
            const custom = String(typeof s.custom_fields === 'object' ? JSON.stringify(s.custom_fields) : (s.custom_fields || '')).toLowerCase();
            return email.includes(q) || fn.includes(q) || ln.includes(q) || tags.includes(q) || custom.includes(q);
        });
    }

    const tbody = document.getElementById('subscribers-table-body');
    if (!tbody) return;
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center py-6 text-xs text-slate-500">No customers match the filter.</td></tr>`;
        renderBulkBar('subscribers');
        return;
    }
    renderSubscribersTable(filtered);
}

// ==========================================================================
// 5. Campaigns Management
// ==========================================================================
async function fetchCampaigns() {
    try {
        const res = await fetch('/api/campaigns');
        if (!res.ok) return;
        App.campaigns = await res.json();

        const badge = document.getElementById('nav-campaigns-badge');
        if (badge) badge.innerText = App.campaigns.length;

        renderCampaignsTable();
    } catch (err) {
        console.warn('Campaigns fetch error:', err);
    }
}

function renderCampaignsTable(rows = App.campaigns) {
    const tbody = document.getElementById('campaigns-table-body');
    if (!tbody) return;

    pruneSelection('campaigns');

    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-xs text-slate-500">No mass campaigns yet. Click "Send to Customers" or "Create Campaign" to create one.</td></tr>`;
        renderBulkBar('campaigns');
        return;
    }

    tbody.innerHTML = rows.map(camp => {
        const sent = camp.sent_count || 0;
        const total = camp.total_recipients || 1;
        const pct = Math.min(100, Math.round((sent / total) * 100));

        const statusClass = camp.status === 'completed' ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' :
            camp.status === 'sending' ? 'bg-amber-500/20 text-amber-300 border-amber-500/30' :
            camp.status === 'scheduled' ? 'bg-purple-500/20 text-purple-300 border-purple-500/30' :
            'bg-slate-500/20 text-slate-300 border-white/10';

        return `
            <tr class="hover:bg-slate-900/50 transition-colors border-b border-white/5 text-xs">
                ${selectionCheckboxCell('campaigns', camp.id)}
                <td class="py-3 px-4">
                    <div class="font-bold text-white">${escapeHtml(camp.name || camp.subject)}</div>
                    <div class="text-[11px] text-slate-400 truncate max-w-xs">${escapeHtml(camp.subject)}</div>
                </td>
                <td class="py-3 px-4 font-semibold text-slate-300">
                    ${camp.total_recipients} Customers
                </td>
                <td class="py-3 px-4">
                    <div class="flex items-center gap-2">
                        <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase border ${statusClass}">
                            ${camp.status === 'scheduled' ? '<i data-lucide="clock" class="w-3 h-3 inline mr-1"></i>' : ''}${camp.status}
                        </span>
                        <span class="text-[11px] text-slate-400 font-mono">${pct}%</span>
                    </div>
                    ${camp.status === 'scheduled' && camp.scheduled_at ? `
                        <div class="text-[11px] text-purple-300/90 font-mono mt-1 flex items-center gap-1" title="Scheduled UTC: ${escapeHtml(camp.scheduled_at)}">
                            <i data-lucide="calendar" class="w-3 h-3 text-purple-400"></i> ${formatScheduleDate(camp.scheduled_at)}
                        </div>
                    ` : `
                        <div class="w-24 bg-slate-800 rounded-full h-1 mt-1 overflow-hidden">
                            <div class="bg-indigo-500 h-full" style="width: ${pct}%"></div>
                        </div>
                    `}
                </td>
                <td class="py-3 px-4 text-[11px] text-slate-300">
                    ${camp.delivered_count || camp.sent_count || 0} / ${camp.total_recipients}
                </td>
                <td class="py-3 px-4 text-[11px]">
                    <span class="text-purple-300 font-medium">Opens: ${camp.open_count || 0}</span> • 
                    <span class="text-blue-300 font-medium">Clicks: ${camp.click_count || 0}</span>
                </td>
                <td class="py-3 px-4 text-right">
                    <div class="flex items-center justify-end gap-2">
                        ${camp.status === 'draft' ? `
                            <button onclick="launchCampaignDirect('${camp.id}')" class="px-2.5 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-slate-950 font-bold text-[11px]">Launch</button>
                        ` : ''}
                        ${camp.status === 'scheduled' ? `
                            <button onclick="launchCampaignDirect('${camp.id}')" class="px-2.5 py-1 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-slate-950 font-bold text-[11px]" title="Launch immediately">Launch Now</button>
                            <button onclick="rescheduleCampaign('${camp.id}')" class="p-1.5 rounded-lg bg-purple-500/10 hover:bg-purple-500/20 text-purple-300 border border-purple-500/20" title="Reschedule delivery time">
                                <i data-lucide="calendar-clock" class="w-3.5 h-3.5"></i>
                            </button>
                            <button onclick="unscheduleCampaign('${camp.id}')" class="p-1.5 rounded-lg bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 border border-amber-500/20" title="Cancel schedule (revert to draft)">
                                <i data-lucide="calendar-x" class="w-3.5 h-3.5"></i>
                            </button>
                        ` : ''}
                        <button onclick="switchTab('vault')" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-white/5 text-[11px]">Vault</button>
                        <button onclick="openEditCampaignModal('${camp.id}')" class="p-1.5 rounded-lg bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300 border border-indigo-500/20" title="Rename / edit campaign">
                            <i data-lucide="pencil" class="w-3.5 h-3.5"></i>
                        </button>
                        <button onclick="deleteCampaign('${camp.id}')" class="p-1.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/20" title="Delete campaign">
                            <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');

    renderBulkBar('campaigns');
    initLucide();
}

function formatScheduleDate(dtStr) {
    if (!dtStr) return '--';
    try {
        let str = String(dtStr).trim();
        if (!str.endsWith('Z') && !str.includes('+')) {
            str = str.replace(' ', 'T') + 'Z';
        }
        const d = new Date(str);
        if (isNaN(d.getTime())) return dtStr;
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch (_) {
        return dtStr;
    }
}

async function rescheduleCampaign(campId) {
    const camp = App.campaigns.find(c => c.id === campId);
    if (!camp) return;

    let defaultVal = '';
    if (camp.scheduled_at) {
        try {
            let str = String(camp.scheduled_at).trim();
            if (!str.endsWith('Z') && !str.includes('+')) str = str.replace(' ', 'T') + 'Z';
            const d = new Date(str);
            if (!isNaN(d.getTime())) {
                const tzOffset = d.getTimezoneOffset() * 60000;
                defaultVal = (new Date(d.getTime() - tzOffset)).toISOString().slice(0, 16);
            }
        } catch (_) {}
    }
    if (!defaultVal) {
        const nextHour = new Date(Date.now() + 3600000);
        const tzOffset = nextHour.getTimezoneOffset() * 60000;
        defaultVal = (new Date(nextHour.getTime() - tzOffset)).toISOString().slice(0, 16);
    }

    const newTime = await promptDialog('Select the new delivery date and time (local time):', defaultVal, {
        title: 'Reschedule Campaign',
        detail: `Campaign: ${camp.name || camp.subject}`,
        confirmText: 'Save Schedule',
        input: { type: 'datetime-local', value: defaultVal }
    });

    if (!newTime) return;

    const parsedDate = new Date(newTime);
    if (isNaN(parsedDate.getTime()) || parsedDate.getTime() <= Date.now()) {
        showToast('Scheduled time must be in the future.', 'warning');
        return;
    }

    try {
        const res = await fetch(`/api/campaigns/${campId}/schedule`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scheduled_at: parsedDate.toISOString() })
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || data.message || 'Failed to reschedule campaign', 'error');
            return;
        }
        showToast(`Campaign rescheduled for ${parsedDate.toLocaleString()}!`, 'success');
        await fetchCampaigns();
    } catch (err) {
        showToast(`Error: ${err.message}`, 'error');
    }
}

async function unscheduleCampaign(campId) {
    const camp = App.campaigns.find(c => c.id === campId);
    const confirmed = await confirmDialog(
        `Cancel scheduled dispatch for "${camp ? (camp.name || camp.subject) : campId}"? It will be reverted to draft status.`,
        {
            title: 'Cancel Scheduled Broadcast',
            confirmText: 'Unschedule',
            danger: true
        }
    );
    if (!confirmed) return;

    try {
        const res = await fetch(`/api/campaigns/${campId}/unschedule`, {
            method: 'POST'
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || data.message || 'Failed to unschedule campaign', 'error');
            return;
        }
        showToast('Campaign unscheduled and moved to draft.', 'success');
        await fetchCampaigns();
    } catch (err) {
        showToast(`Error: ${err.message}`, 'error');
    }
}

async function deleteCampaign(campId) {
    const camp = App.campaigns.find(c => c.id === campId);
    const running = camp && ['sending', 'queued', 'paused'].includes(camp.status);
    await deleteOne('campaigns', campId,
        `Delete campaign "${camp ? (camp.name || camp.subject) : campId}"?${running ? ' Its running dispatch queue will be cancelled first.' : ''}`);
}

// Campaign edit is deliberately limited to the two fields that are safe to
// change after creation. Recipients and body are locked in at launch time.
async function openEditCampaignModal(campId) {
    const camp = App.campaigns.find(c => c.id === campId);
    if (!camp) {
        showToast('Campaign is no longer loaded. Refresh and try again.', 'warning');
        return;
    }

    const name = await promptDialog('Campaign name', camp.name || '', { title: 'Edit campaign' });
    if (name === null) return;
    const subject = await promptDialog('Subject line', camp.subject || '', { title: 'Edit campaign' });
    if (subject === null) return;

    if (!name.trim() || !subject.trim()) {
        showToast('Name and subject cannot be empty.', 'warning');
        return;
    }

    try {
        const res = await fetch(`/api/campaigns/${campId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), subject: subject.trim() })
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || 'Failed to update campaign', 'error');
            return;
        }
        showToast('Campaign updated.', 'success');
        await fetchCampaigns();
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

// The broadcast composer IS the campaign builder; the header button used to
// call a function that never existed, so it did nothing at all.
function openNewCampaignModal() {
    switchTab('broadcast');
    document.getElementById('broadcast-subject')?.focus();
}

async function launchCampaignDirect(campId) {
    try {
        const res = await fetch(`/api/campaigns/${campId}/launch`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            showToast('Campaign launched successfully!', 'success');
            await fetchCampaigns();
        } else {
            showToast(data.message || 'Launch error', 'error');
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

// ==========================================================================
// 6. Templates
// ==========================================================================
async function fetchTemplates() {
    try {
        const res = await fetch('/api/templates');
        if (!res.ok) return;
        App.templates = await res.json();
        renderTemplatePresets();
    } catch (err) {
        console.warn('Templates fetch error:', err);
    }
}

function renderTemplatePresets() {
    const listEl = document.getElementById('template-starter-list');
    if (!listEl) return;

    if (App.templates.length === 0) {
        listEl.innerHTML = `<div class="p-3 text-[11px] text-slate-500">No saved templates yet. Compose one and hit Save.</div>`;
        renderBulkBar('templates');
        return;
    }

    pruneSelection('templates');

    listEl.innerHTML = App.templates.map(tpl => `
        <div class="group p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800 border ${App.editing.template === tpl.id ? 'border-indigo-500/60' : 'border-white/5'} hover:border-indigo-500/40 transition-all flex items-start gap-2">
            <input type="checkbox" class="row-select mt-1" data-kind="templates" data-id="${tpl.id}"
                   ${isSelected('templates', tpl.id) ? 'checked' : ''}
                   onchange="toggleRowSelection('templates', '${tpl.id}', this.checked)"
                   aria-label="Select template">
            <div onclick="selectStudioTemplate('${tpl.id}')" class="flex-1 min-w-0 cursor-pointer">
                <div class="text-xs font-bold text-white truncate">${escapeHtml(tpl.name)}</div>
                <div class="text-[11px] text-slate-400 truncate mt-0.5">${escapeHtml(tpl.subject || '')}</div>
            </div>
            <button onclick="deleteTemplate('${tpl.id}')" class="p-1 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 opacity-0 group-hover:opacity-100 transition-opacity" title="Delete template">
                <i data-lucide="trash-2" class="w-3 h-3"></i>
            </button>
        </div>
    `).join('');

    renderBulkBar('templates');
    initLucide();
}

async function deleteTemplate(tplId) {
    const tpl = App.templates.find(t => t.id === tplId);
    const removed = await deleteOne('templates', tplId, `Delete template "${tpl ? tpl.name : tplId}"?`);
    if (removed && App.editing.template === tplId) newStudioTemplate();
}

// Clears the studio back to "new template" mode so the next save creates a
// record instead of overwriting whichever template was last clicked.
function newStudioTemplate() {
    App.editing.template = null;
    setValue('studio-tpl-name', '');
    setValue('studio-tpl-subject', '');
    setValue('studio-tpl-html', '');
    setText('studio-save-label', 'Save Template');
    renderTemplatePresets();
}

function selectStudioTemplate(tplId) {
    const tpl = App.templates.find(t => t.id === tplId);
    if (!tpl) return;

    App.editing.template = tplId;
    setValue('studio-tpl-name', tpl.name);
    setValue('studio-tpl-subject', tpl.subject || '');
    setValue('studio-tpl-html', tpl.body_html || '');
    setText('studio-save-label', 'Update Template');
    renderTemplatePresets();

    showToast(`Editing template "${tpl.name}"`, 'info');
}

async function saveStudioTemplate() {
    const name = document.getElementById('studio-tpl-name')?.value?.trim();
    const subject = document.getElementById('studio-tpl-subject')?.value?.trim();
    const html = document.getElementById('studio-tpl-html')?.value?.trim();

    if (!name || !html) {
        showToast('Please provide a template name and HTML content.', 'warning');
        return;
    }

    // A template loaded into the studio is updated in place; previously every
    // save created a duplicate record.
    const editingId = App.editing.template;

    try {
        const res = await fetch(editingId ? `/api/templates/${editingId}` : '/api/templates', {
            method: editingId ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, subject, body_html: html })
        });

        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || 'Failed to save template', 'error');
            return;
        }
        if (!editingId && data.id) App.editing.template = data.id;
        showToast(editingId ? `Template "${name}" updated!` : `Template "${name}" saved!`, 'success');
        setText('studio-save-label', 'Update Template');
        await fetchTemplates();
    } catch (err) {
        showToast('Failed to save template: ' + err.message, 'error');
    }
}

// ==========================================================================
// 7. Mail Server & SMTP Management
// ==========================================================================
async function fetchSmtpConfigs() {
    try {
        const res = await fetch('/api/smtp');
        if (!res.ok) return;
        App.smtpConfigs = await res.json();

        renderSmtpProfiles();
        populateBroadcastDropdowns();

        const defaultRelay = App.smtpConfigs.find(s => s.is_default) || App.smtpConfigs[0];
        const sideRelay = document.getElementById('sidebar-active-relay');
        if (sideRelay && defaultRelay) {
            sideRelay.innerText = `${defaultRelay.name} (${defaultRelay.host})`;
        }
    } catch (err) {
        console.warn('SMTP fetch error:', err);
    }
}

function renderSmtpProfiles() {
    const listEl = document.getElementById('smtp-profiles-list');
    if (!listEl) return;

    if (App.smtpConfigs.length === 0) {
        listEl.innerHTML = `<div class="col-span-3 text-center py-8 text-xs text-slate-500">No mail servers configured. Click "+ Add Mail Server" or a quick preset above.</div>`;
        return;
    }

    listEl.innerHTML = App.smtpConfigs.map(smtp => `
        <div class="glass-panel p-5 rounded-2xl border ${smtp.is_default ? 'border-amber-500/40 bg-amber-950/10' : 'border-white/10'} space-y-3">
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <div class="p-2 rounded-xl ${smtp.is_sandbox ? 'bg-emerald-500/20 text-emerald-300' : 'bg-indigo-500/20 text-indigo-300'}">
                        <i data-lucide="${smtp.is_sandbox ? 'shield' : 'server'}" class="w-4 h-4"></i>
                    </div>
                    <div>
                        <h4 class="text-xs font-bold text-white truncate">${escapeHtml(smtp.name)}</h4>
                        <p class="text-[11px] text-slate-400 font-mono">${escapeHtml(smtp.host)}:${smtp.port}</p>
                    </div>
                </div>
                ${smtp.is_default ? '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">DEFAULT</span>' : ''}
            </div>

            <div class="text-[11px] text-slate-400 space-y-1">
                <div>Security: <span class="text-slate-200 font-semibold">${smtp.use_tls ? 'STARTTLS (587)' : (smtp.use_ssl ? 'SSL (465)' : 'Plain')}</span></div>
                <div>User: <span class="text-slate-200 font-mono">${escapeHtml(smtp.username || 'None')}</span></div>
            </div>

            <div class="flex items-center justify-between pt-2 border-t border-white/5">
                <button onclick="testConfiguredSmtp('${smtp.id}')" class="px-3 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-300">
                    Test Probe
                </button>
                <div class="flex items-center gap-1">
                    ${!smtp.is_default ? `<button onclick="setDefaultSmtp('${smtp.id}')" class="px-2.5 py-1 rounded-lg bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 text-xs font-semibold">Make Default</button>` : ''}
                    <button onclick="openEditSmtpModal('${smtp.id}')" class="p-1 rounded-lg bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300" title="Edit mail server"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
                    <button onclick="deleteSmtp('${smtp.id}')" class="p-1 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-400" title="Delete mail server"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
                </div>
            </div>
        </div>
    `).join('');

    initLucide();
}

function openAddSmtpModal() {
    App.editing.smtp = null;
    setText('modal-smtp-title', 'Configure Mail Server (SMTP Relay)');
    setText('modal-smtp-submit', 'Save Mail Server');
    setText('btn-smtp-submit', 'Save Mail Server');
    setValue('modal-smtp-name', '');
    setValue('modal-smtp-host', '');
    setValue('modal-smtp-port', '587');
    setValue('modal-smtp-user', '');
    setValue('modal-smtp-pass', '');
    const tls = document.getElementById('modal-smtp-tls');
    const ssl = document.getElementById('modal-smtp-ssl');
    const def = document.getElementById('modal-smtp-default');
    if (tls) tls.checked = true;
    if (ssl) ssl.checked = false;
    if (def) def.checked = true;
    openModal('modal-add-smtp');
}

function openEditSmtpModal(smtpId) {
    const cfg = App.smtpConfigs.find(c => c.id === smtpId);
    if (!cfg) {
        showToast('Mail server profile is no longer loaded. Refresh and try again.', 'warning');
        return;
    }
    App.editing.smtp = smtpId;
    setText('modal-smtp-title', `Edit Mail Server - ${cfg.name}`);
    setText('modal-smtp-submit', 'Save Changes');
    setText('btn-smtp-submit', 'Save Changes');
    setValue('modal-smtp-name', cfg.name || '');
    setValue('modal-smtp-host', cfg.host || '');
    setValue('modal-smtp-port', cfg.port || 587);
    setValue('modal-smtp-user', cfg.username || '');
    // The API never returns stored secrets; blank means "keep the existing one".
    setValue('modal-smtp-pass', '');
    const passEl = document.getElementById('modal-smtp-pass');
    if (passEl) passEl.placeholder = cfg.has_password ? 'Unchanged - type to replace' : 'No password set';
    const tls = document.getElementById('modal-smtp-tls');
    const ssl = document.getElementById('modal-smtp-ssl');
    const def = document.getElementById('modal-smtp-default');
    if (tls) tls.checked = !!cfg.use_tls;
    if (ssl) ssl.checked = !!cfg.use_ssl;
    if (def) def.checked = !!cfg.is_default;
    openModal('modal-add-smtp');
}

function openGmailConnectModal() {
    openModal('modal-gmail-connect');
}

async function submitGmailConnect() {
    const email = document.getElementById('gmail-email-input')?.value?.trim();
    const appPassword = document.getElementById('gmail-app-password-input')?.value?.trim();
    const senderName = document.getElementById('gmail-sender-name-input')?.value?.trim() || 'Bitmail Sender';
    const isDefault = document.getElementById('gmail-set-default')?.checked ?? true;

    if (!email || !email.includes('@')) {
        showToast('Please enter your valid Gmail or Google Workspace email address.', 'warning');
        return;
    }
    if (!appPassword || appPassword.length < 8) {
        showToast('Please enter your 16-character Google App Password.', 'warning');
        return;
    }

    const btn = document.getElementById('btn-submit-gmail-connect');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span>Verifying with Google...</span>`;
    }

    showToast('Performing live handshake with smtp.gmail.com:587...', 'info');

    try {
        const res = await fetch('/api/smtp/gmail-connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                email: email,
                app_password: appPassword,
                sender_name: senderName,
                is_default: isDefault
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ Gmail Connected: ${data.message}`, 'success');
            closeModal('modal-gmail-connect');
            
            // Auto-populate broadcast sender
            const senderNameInput = document.getElementById('broadcast-sender-name');
            const senderEmailInput = document.getElementById('broadcast-sender-email');
            if (senderNameInput && senderName) senderNameInput.value = senderName;
            if (senderEmailInput && email) senderEmailInput.value = email;

            await fetchSmtpConfigs();
            populateBroadcastDropdowns();
        } else {
            showToast(data.detail || data.message || 'Gmail verification failed', 'error');
        }
    } catch (err) {
        showToast('Connection error: ' + err.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i data-lucide="shield-check" class="w-3.5 h-3.5"></i><span>Connect & Verify Gmail</span>`;
            initLucide();
        }
    }
}

function openBrevoConnectModal() {
    openModal('modal-brevo-connect');
}

async function submitBrevoConnect() {
    const email = document.getElementById('brevo-email-input')?.value?.trim();
    const key = document.getElementById('brevo-key-input')?.value?.trim();
    const isDefault = document.getElementById('brevo-set-default')?.checked ?? true;

    if (!email || !email.includes('@')) {
        showToast('Please enter your Brevo account login email address.', 'warning');
        return;
    }
    if (!key || key.length < 8) {
        showToast('Please enter your Brevo SMTP Master Key.', 'warning');
        return;
    }

    const btn = document.getElementById('btn-submit-brevo-connect');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span>Verifying with Brevo...</span>`;
    }

    showToast('Performing live handshake with smtp-relay.brevo.com:587...', 'info');

    try {
        const res = await fetch('/api/smtp/brevo-connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                login_email: email,
                smtp_key: key,
                is_default: isDefault
            })
        });

        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast(`✓ Brevo Connected: ${data.message}`, 'success');
            closeModal('modal-brevo-connect');

            // Auto-populate broadcast sender
            const senderEmailInput = document.getElementById('broadcast-sender-email');
            if (senderEmailInput && email) senderEmailInput.value = email;

            await fetchSmtpConfigs();
            populateBroadcastDropdowns();
        } else {
            showToast(data.detail || data.message || 'Brevo verification failed', 'error');
        }
    } catch (err) {
        showToast('Connection error: ' + err.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `<i data-lucide="shield-check" class="w-3.5 h-3.5"></i><span>Connect & Verify Brevo</span>`;
            initLucide();
        }
    }
}

function presetSmtp(type) {
    if (type === 'gmail') {
        openGmailConnectModal();
        return;
    }
    if (type === 'brevo') {
        openBrevoConnectModal();
        return;
    }

    openModal('modal-add-smtp');
    const nameEl = document.getElementById('modal-smtp-name');
    const hostEl = document.getElementById('modal-smtp-host');
    const portEl = document.getElementById('modal-smtp-port');
    const userEl = document.getElementById('modal-smtp-user');
    const tlsEl = document.getElementById('modal-smtp-tls');
    const sslEl = document.getElementById('modal-smtp-ssl');

    if (type === 'outlook') {
        nameEl.value = 'Microsoft 365 / Outlook';
        hostEl.value = 'smtp.office365.com';
        portEl.value = 587;
        tlsEl.checked = true;
        sslEl.checked = false;
    } else if (type === 'sendgrid') {
        nameEl.value = 'SendGrid SMTP';
        hostEl.value = 'smtp.sendgrid.net';
        portEl.value = 587;
        userEl.value = 'apikey';
        tlsEl.checked = true;
        sslEl.checked = false;
    } else if (type === 'ses') {
        nameEl.value = 'Amazon SES Relay';
        hostEl.value = 'email-smtp.us-east-1.amazonaws.com';
        portEl.value = 587;
        tlsEl.checked = true;
        sslEl.checked = false;
    } else if (type === 'brevo') {
        nameEl.value = 'Brevo (Sendinblue)';
        hostEl.value = 'smtp-relay.brevo.com';
        portEl.value = 587;
        tlsEl.checked = true;
        sslEl.checked = false;
    } else if (type === 'sandbox') {
        nameEl.value = 'Local Sandbox Test Mode';
        hostEl.value = 'sandbox';
        portEl.value = 1025;
        tlsEl.checked = false;
        sslEl.checked = false;
        showToast('Sandbox mode active: messages simulated & archived with zero external auth', 'info');
    }
}

async function submitAddSmtpProfile() {
    const name = document.getElementById('modal-smtp-name')?.value?.trim();
    const host = document.getElementById('modal-smtp-host')?.value?.trim();
    const port = parseInt(document.getElementById('modal-smtp-port')?.value || '587', 10);
    const user = document.getElementById('modal-smtp-user')?.value?.trim();
    const pass = document.getElementById('modal-smtp-pass')?.value || '';
    const tls = document.getElementById('modal-smtp-tls')?.checked ?? true;
    const ssl = document.getElementById('modal-smtp-ssl')?.checked ?? false;
    const isDefault = document.getElementById('modal-smtp-default')?.checked ?? false;

    if (!name || !host) {
        showToast('Please provide a profile name and SMTP host.', 'warning');
        return;
    }

    const editingId = App.editing.smtp;
    const payload = {
        name: name,
        host: host,
        port: port,
        username: user || null,
        use_tls: tls,
        use_ssl: ssl,
        is_default: isDefault,
        is_sandbox: host.toLowerCase() === 'sandbox' || host === '127.0.0.1'
    };
    // On edit an empty password field means "leave the stored secret alone".
    if (pass || !editingId) payload.password = pass || null;

    try {
        const res = await fetch(editingId ? `/api/smtp/${editingId}` : '/api/smtp', {
            method: editingId ? 'PUT' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await safeJson(res);
        if (res.ok) {
            showToast(editingId ? `Mail server "${name}" updated.` : `Mail server profile "${name}" created!`, 'success');
            App.editing.smtp = null;
            closeModal('modal-add-smtp');
            await fetchSmtpConfigs();
        } else {
            showToast(data.detail || 'Failed to save SMTP profile', 'error');
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

async function testModalSmtpConnection() {
    const host = document.getElementById('modal-smtp-host')?.value?.trim() || '127.0.0.1';
    const port = parseInt(document.getElementById('modal-smtp-port')?.value || '587', 10);
    const user = document.getElementById('modal-smtp-user')?.value?.trim();
    const pass = document.getElementById('modal-smtp-pass')?.value || '';
    const tls = document.getElementById('modal-smtp-tls')?.checked ?? true;
    const ssl = document.getElementById('modal-smtp-ssl')?.checked ?? false;

    showToast(`Testing connectivity to ${host}:${port}...`, 'info');

    try {
        const res = await fetch('/api/smtp/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                host: host,
                port: port,
                username: user || null,
                password: pass || null,
                use_tls: tls,
                use_ssl: ssl,
                test_recipient: 'probe-test@example.com'
            })
        });

        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ Connection Verified: ${data.message} (${data.latency_ms}ms)`, 'success');
        } else {
            showToast(`Connection failed: ${data.message || 'Check host, port, or app password'}`, 'error');
        }
    } catch (err) {
        showToast('Test probe exception: ' + err.message, 'error');
    }
}

async function testConfiguredSmtp(smtpId) {
    showToast('Running SMTP diagnostic handshake...', 'info');
    try {
        const res = await fetch('/api/smtp/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ smtp_config_id: smtpId, test_recipient: 'probe@example.com' })
        });
        const data = await res.json();
        if (res.ok && data.success) {
            showToast(`✓ Server Verified: ${data.message} (${data.latency_ms}ms)`, 'success');
        } else {
            showToast(`Failed: ${data.message}`, 'error');
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

async function setDefaultSmtp(smtpId) {
    try {
        await fetch(`/api/smtp/${smtpId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_default: true })
        });
        showToast('Default mail server updated.', 'success');
        await fetchSmtpConfigs();
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

async function deleteSmtp(smtpId) {
    const cfg = App.smtpConfigs.find(c => c.id === smtpId);
    await deleteOne('smtp', smtpId,
        `Delete mail server "${cfg ? cfg.name : smtpId}"? Campaigns still pointing at it will fall back to the default relay.`);
}

// ==========================================================================
// Modal Utilities & Helpers
// ==========================================================================
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('hidden');
        modal.querySelectorAll('input, select, textarea, button[type="submit"]').forEach(el => {
            el.disabled = false;
        });
    }
    initLucide();
    suppressUnwantedAutofill();
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('hidden');
        modal.querySelectorAll('input, select, textarea, button[type="submit"]').forEach(el => {
            el.disabled = true;
        });
    }
}

// ==========================================================================
// Custom Confirmation / Prompt Dialogs
// window.confirm() and window.prompt() are suppressed by ad blockers, by
// Chrome's "prevent this page from creating additional dialogs" checkbox, and
// inside sandboxed iframes. When that happened a destructive action silently
// became a no-op, which is why deletes appeared broken. These dialogs are our
// own DOM, so they always run. Native <dialog> gives Esc, focus trapping and a
// top-layer backdrop for free.
// ==========================================================================
let _uiDialogEl = null;

function uiDialog(opts) {
    const {
        title = 'Please confirm',
        message = '',
        detail = '',
        confirmText = 'Confirm',
        cancelText = 'Cancel',
        danger = false,
        input = null
    } = opts || {};

    if (!_uiDialogEl) {
        _uiDialogEl = document.createElement('dialog');
        _uiDialogEl.id = 'ui-dialog-element';
        _uiDialogEl.className = 'ui-dialog';
        // Clicking the backdrop counts as cancel.
        _uiDialogEl.addEventListener('click', (e) => {
            if (e.target === _uiDialogEl) {
                _uiDialogEl.returnValue = '';
                _uiDialogEl.close();
            }
        });
        document.body.appendChild(_uiDialogEl);
    }

    const dlg = _uiDialogEl;
    dlg.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;max-width:100vw;max-height:100vh;margin:0;padding:1rem;border:none;background:transparent;display:flex;align-items:center;justify-content:center;z-index:99999;box-sizing:border-box;';

    // A second dialog while one is open would overwrite its markup and resolve
    // both promises from one answer - e.g. a double-clicked delete button
    // firing two DELETE requests. Ignore the newcomer instead.
    if (dlg.open) return Promise.resolve(input ? null : false);

    const confirmClass = danger
        ? 'bg-rose-600 hover:bg-rose-500 text-white'
        : 'bg-indigo-600 hover:bg-indigo-500 text-white';

    const inputHtml = input ? `
        <input id="ui-dialog-input" type="${input.type || 'text'}"
               value="${escapeHtml(input.value || '')}"
               placeholder="${escapeHtml(input.placeholder || '')}"
               class="w-full px-3 py-2 bg-slate-900 border border-white/10 rounded-xl text-xs text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500">
    ` : '';

    dlg.innerHTML = `
        <form method="dialog" class="glass-panel rounded-2xl border border-white/10 p-6 space-y-4 shadow-2xl bg-slate-950/95 max-w-md w-full" style="margin:auto;max-width:28rem;width:calc(100% - 2rem);">
            <div class="flex items-start gap-3">
                <div class="p-2 rounded-xl ${danger ? 'bg-rose-500/15 text-rose-400' : 'bg-indigo-500/15 text-indigo-300'} shrink-0">
                    <i data-lucide="${danger ? 'alert-triangle' : 'help-circle'}" class="w-4 h-4"></i>
                </div>
                <div class="space-y-1 min-w-0">
                    <h3 class="text-sm font-bold text-white">${escapeHtml(title)}</h3>
                    <p class="text-xs text-slate-300 leading-relaxed">${escapeHtml(message)}</p>
                    ${detail ? `<p class="text-[11px] text-slate-500 leading-relaxed">${escapeHtml(detail)}</p>` : ''}
                </div>
            </div>
            ${inputHtml}
            <div class="flex items-center justify-end gap-2 pt-3 border-t border-white/5">
                <button type="button" data-ui-dialog-cancel class="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-300">${escapeHtml(cancelText)}</button>
                <button type="submit" value="confirm" data-autofocus class="px-5 py-2 rounded-xl text-xs font-bold ${confirmClass}">${escapeHtml(confirmText)}</button>
            </div>
        </form>
    `;

    dlg.querySelector('[data-ui-dialog-cancel]')?.addEventListener('click', () => {
        dlg.returnValue = '';
        dlg.close();
    });

    initLucide();
    dlg.returnValue = '';
    dlg.showModal();

    const field = dlg.querySelector('#ui-dialog-input');
    if (field) {
        field.focus();
        field.select();
    } else {
        dlg.querySelector('[data-autofocus]')?.focus();
    }

    return new Promise((resolve) => {
        dlg.addEventListener('close', function onClose() {
            dlg.removeEventListener('close', onClose);
            dlg.style.display = 'none';
            const confirmed = dlg.returnValue === 'confirm';
            if (!input) return resolve(confirmed);
            resolve(confirmed ? (dlg.querySelector('#ui-dialog-input')?.value ?? '') : null);
        });
    });
}

// Drop-in replacements. confirmDialog resolves true/false, promptDialog
// resolves the typed string or null when cancelled - same contract as the
// natives they replace, minus the blocking and the blockability.
function confirmDialog(message, opts = {}) {
    return uiDialog({ message, ...opts });
}

function promptDialog(message, defaultValue = '', opts = {}) {
    return uiDialog({
        title: 'Input required',
        message,
        confirmText: 'Continue',
        input: { value: defaultValue },
        ...opts
    });
}

// ==========================================================================
// Multi-Select & Mass Actions
// One engine drives every table that supports selection, so the select-all
// checkbox, the action bar and the bulk request stay consistent.
// ==========================================================================
const BULK_KINDS = {
    subscribers: {
        resource: 'subscribers',
        noun: 'customer contact',
        items: () => App.subscribers,
        refresh: () => fetchSubscribers()
    },
    vault: {
        resource: 'emails',
        noun: 'archived email',
        items: () => App.vaultEmails,
        refresh: () => fetchVaultEmails()
    },
    campaigns: {
        resource: 'campaigns',
        noun: 'campaign',
        items: () => App.campaigns,
        refresh: () => fetchCampaigns()
    },
    smtp: {
        resource: 'smtp',
        noun: 'mail server profile',
        items: () => App.smtpConfigs,
        refresh: () => fetchSmtpConfigs()
    },
    templates: {
        resource: 'templates',
        noun: 'template',
        items: () => App.templates,
        refresh: () => fetchTemplates()
    }
};

function selectionSet(kind) {
    if (!App.selected[kind]) App.selected[kind] = new Set();
    return App.selected[kind];
}

function isSelected(kind, id) {
    return selectionSet(kind).has(id);
}

function toggleRowSelection(kind, id, checked) {
    const set = selectionSet(kind);
    if (checked) set.add(id); else set.delete(id);
    renderBulkBar(kind);
}

// Select-all applies to the rows currently rendered, not the whole dataset -
// selecting rows hidden by a filter is how people delete the wrong records.
function toggleSelectAll(kind, checked) {
    const set = selectionSet(kind);
    document.querySelectorAll(`.row-select[data-kind="${kind}"]`).forEach(cb => {
        cb.checked = checked;
        if (checked) set.add(cb.dataset.id); else set.delete(cb.dataset.id);
    });
    renderBulkBar(kind);
}

function clearSelection(kind) {
    selectionSet(kind).clear();
    const master = document.getElementById(`select-all-${kind}`);
    if (master) {
        master.checked = false;
        master.indeterminate = false;
    }
    document.querySelectorAll(`.row-select[data-kind="${kind}"]`).forEach(cb => { cb.checked = false; });
    renderBulkBar(kind);
}

// Drops ids that no longer exist (deleted elsewhere, or filtered out of the
// dataset) so the counter can never promise more than we can actually delete.
function pruneSelection(kind) {
    const cfg = BULK_KINDS[kind];
    if (!cfg) return;
    const live = new Set((cfg.items() || []).map(i => i.id));
    const set = selectionSet(kind);
    set.forEach(id => { if (!live.has(id)) set.delete(id); });
}

function renderBulkBar(kind) {
    const bar = document.getElementById(`bulk-bar-${kind}`);
    const count = selectionSet(kind).size;
    if (bar) {
        bar.classList.toggle('hidden', count === 0);
        const label = document.getElementById(`bulk-count-${kind}`);
        if (label) label.innerText = String(count);
    }

    const master = document.getElementById(`select-all-${kind}`);
    if (master) {
        const boxes = document.querySelectorAll(`.row-select[data-kind="${kind}"]`);
        const checked = document.querySelectorAll(`.row-select[data-kind="${kind}"]:checked`).length;
        master.checked = boxes.length > 0 && checked === boxes.length;
        master.indeterminate = checked > 0 && checked < boxes.length;
    }
}

function selectionCheckboxCell(kind, id) {
    return `<td class="py-3 px-4 w-8">
        <input type="checkbox" class="row-select" data-kind="${kind}" data-id="${id}"
               ${isSelected(kind, id) ? 'checked' : ''}
               onchange="toggleRowSelection('${kind}', '${id}', this.checked)"
               aria-label="Select row">
    </td>`;
}

async function bulkDeleteSelected(kind) {
    const cfg = BULK_KINDS[kind];
    if (!cfg) return;

    const ids = [...selectionSet(kind)];
    if (ids.length === 0) {
        showToast('Nothing selected.', 'warning');
        return;
    }

    const plural = ids.length === 1 ? cfg.noun : `${cfg.noun}s`;
    const ok = await confirmDialog(`Permanently delete ${ids.length} ${plural}?`, {
        title: 'Confirm mass delete',
        detail: 'This cannot be undone.',
        confirmText: `Delete ${ids.length}`,
        danger: true
    });
    if (!ok) return;

    try {
        const res = await fetch('/api/bulk-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resource: cfg.resource, ids })
        });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || 'Mass delete failed', 'error');
            return;
        }
        showToast(data.message || `Deleted ${data.deleted} records.`,
            data.failed_count ? 'warning' : 'success');
        if (data.failed_count) {
            console.warn('[bulk-delete] failures:', data.failed);
        }
        clearSelection(kind);
        await cfg.refresh();
        await fetchDashboardStats();
    } catch (err) {
        showToast('Mass delete error: ' + err.message, 'error');
    }
}

// Single-record delete goes through the same confirm + error surfacing path.
async function deleteOne(kind, id, message) {
    const cfg = BULK_KINDS[kind];
    if (!cfg) return false;

    const ok = await confirmDialog(message || `Delete this ${cfg.noun}?`, {
        title: 'Confirm delete',
        detail: 'This cannot be undone.',
        confirmText: 'Delete',
        danger: true
    });
    if (!ok) return false;

    try {
        const res = await fetch(`${DELETE_ENDPOINTS[kind]}/${encodeURIComponent(id)}`, { method: 'DELETE' });
        const data = await safeJson(res);
        if (!res.ok) {
            showToast(data.detail || `Failed to delete ${cfg.noun}`, 'error');
            return false;
        }
        selectionSet(kind).delete(id);
        showToast(data.message || `${cfg.noun} deleted.`, 'success');
        await cfg.refresh();
        await fetchDashboardStats();
        return true;
    } catch (err) {
        showToast('Delete error: ' + err.message, 'error');
        return false;
    }
}

const DELETE_ENDPOINTS = {
    subscribers: '/api/subscribers',
    vault: '/api/storage/emails',
    campaigns: '/api/campaigns',
    smtp: '/api/smtp',
    templates: '/api/templates'
};

function handleGlobalSearch(e) {
    if (e.key === 'Enter') {
        const q = e.target.value.trim();
        if (!q) return;
        switchTab('vault');
        const vaultInput = document.getElementById('vault-search-input');
        if (vaultInput) {
            vaultInput.value = q;
            filterVaultEmails();
        }
    }
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    const bgClass = type === 'success' ? 'bg-emerald-950/90 border-emerald-500/40 text-emerald-200' :
        type === 'error' ? 'bg-rose-950/90 border-rose-500/40 text-rose-200' :
        type === 'warning' ? 'bg-amber-950/90 border-amber-500/40 text-amber-200' :
        'bg-slate-900/90 border-indigo-500/40 text-indigo-200';

    toast.className = `p-3.5 px-4 rounded-xl border backdrop-blur-md shadow-2xl text-xs font-semibold flex items-center gap-2 transform transition-all duration-300 translate-y-2 opacity-0 pointer-events-auto ${bgClass}`;
    toast.innerHTML = `<span>${escapeHtml(message)}</span>`;

    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => {
        toast.classList.remove('translate-y-2', 'opacity-0');
    });

    // Auto remove
    setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-x-4');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function formatDate(isoStr) {
    if (!isoStr) return '--';
    try {
        const d = new Date(isoStr);
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch (e) {
        return isoStr;
    }
}

function formatTimeAgo(isoStr) {
    if (!isoStr) return '';
    try {
        const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
        if (diff < 60) return 'Just now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    } catch (e) {
        return '';
    }
}

// ==========================================================================
// Direct Scan-to-Login & Mobile Device Linking Controllers
// ==========================================================================
let currentScanSession = null;
let scanCountdownInterval = null;
let scanStatusPollInterval = null;

async function openScanLoginModal() {
    openModal('modal-scan-login');

    const wrapper = document.getElementById('scan-qr-svg-wrapper');
    const statusText = document.getElementById('scan-status-text');
    const timerText = document.getElementById('scan-timer-text');
    const openLinkBtn = document.getElementById('scan-open-link-btn');

    if (wrapper) {
        wrapper.innerHTML = `
            <div class="text-xs text-slate-500 font-mono flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-purple-500 animate-ping"></span> Generating Live QR...
            </div>
        `;
    }
    if (statusText) statusText.innerText = 'Initializing scan session...';

    clearInterval(scanCountdownInterval);
    clearInterval(scanStatusPollInterval);

    try {
        const res = await fetch('/api/auth/scan/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_info: 'Desktop Chrome / Linux' })
        });

        const data = await safeJson(res);
        if (res.ok && data.success) {
            currentScanSession = data;
            
            // Insert SVG QR Code
            if (wrapper) {
                wrapper.innerHTML = data.qr_svg;
                // Add styling to svg
                const svgEl = wrapper.querySelector('svg');
                if (svgEl) {
                    svgEl.setAttribute('width', '100%');
                    svgEl.setAttribute('height', '100%');
                }
            }

            if (statusText) statusText.innerText = 'Waiting for phone scan...';
            if (openLinkBtn) openLinkBtn.href = data.scan_url;

            // Start 5-minute countdown
            let secondsLeft = data.expires_in_seconds || 300;
            const updateTimerDisplay = () => {
                const m = Math.floor(secondsLeft / 60);
                const s = secondsLeft % 60;
                if (timerText) timerText.innerText = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            };
            updateTimerDisplay();

            scanCountdownInterval = setInterval(() => {
                secondsLeft--;
                if (secondsLeft <= 0) {
                    clearInterval(scanCountdownInterval);
                    clearInterval(scanStatusPollInterval);
                    if (statusText) statusText.innerHTML = '<span class="text-rose-400">QR Expired. Click to refresh.</span>';
                    if (timerText) timerText.innerText = '00:00';
                } else {
                    updateTimerDisplay();
                }
            }, 1000);

            // Fallback polling every 3s in addition to instant WebSocket event
            scanStatusPollInterval = setInterval(async () => {
                if (document.getElementById('modal-scan-login')?.classList.contains('hidden')) {
                    clearInterval(scanStatusPollInterval);
                    return;
                }
                try {
                    const statusRes = await fetch(`/api/auth/scan/session/${data.session_id}/status?token=${encodeURIComponent(data.token)}`);
                    if (statusRes.ok) {
                        const sData = await statusRes.json();
                        if (sData.is_approved) {
                            clearInterval(scanStatusPollInterval);
                            clearInterval(scanCountdownInterval);
                            handleWebSocketEvent({
                                type: 'scan_auth_approved',
                                data: {
                                    session_id: sData.session_id,
                                    email: sData.user_email,
                                    name: sData.user_name,
                                    auth_token: sData.auth_token
                                }
                            });
                        }
                    }
                } catch (e) {
                    console.debug('Poll check error:', e);
                }
            }, 3000);

        } else {
            showToast('Failed to create scan session: ' + (data.detail || 'Unknown error'), 'error');
        }
    } catch (err) {
        showToast('Error generating scan QR: ' + err.message, 'error');
    }
}

async function simulateScanApproval() {
    if (!currentScanSession || !currentScanSession.session_id) {
        showToast('Please open the scan modal first.', 'warning');
        return;
    }

    const testEmail = await promptDialog(
        'Enter the email address to simulate instant phone authorization for.',
        'admin@bitnade.com',
        { title: 'Simulate scan approval', confirmText: 'Approve' }
    );
    if (!testEmail || !testEmail.includes('@')) return;

    showToast(`Simulating instant phone scan approval for ${testEmail}...`, 'info');

    try {
        const res = await fetch(`/api/auth/scan/simulate-approval/${currentScanSession.session_id}?email=${encodeURIComponent(testEmail)}`, {
            method: 'POST'
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast(`✓ Phone scan simulated successfully! Authenticated as ${testEmail}`, 'success');
        } else {
            showToast('Simulation failed: ' + (data.detail || 'Unknown error'), 'error');
        }
    } catch (err) {
        showToast('Simulation error: ' + err.message, 'error');
    }
}

// ==========================================================================
// Authentication & User Session Management
// ==========================================================================

function handleSessionExpired() {
    localStorage.removeItem('bitmail_token');
    document.cookie = 'bitmail_token=; Max-Age=0; path=/;';
    App.currentUser = null;
    showToast('Your session has expired. Please sign in.', 'warning');
    openLoginModal();
}

async function checkAuthSession() {
    const token = localStorage.getItem('bitmail_token');
    if (!token) {
        openLoginModal();
        return false;
    }

    try {
        const res = await fetch('/api/auth/me');
        if (res.ok) {
            const user = await res.json();
            App.currentUser = user;
            updateUserDisplay(user);
            closeLoginModal();
            return true;
        } else {
            handleSessionExpired();
            return false;
        }
    } catch (err) {
        console.warn('Auth validation error:', err);
        openLoginModal();
        return false;
    }
}

function updateUserDisplay(user) {
    if (!user) return;
    const name = user.name || user.username || user.email;
    const initial = name.charAt(0).toUpperCase();

    const avatarEl = document.getElementById('header-user-avatar');
    if (avatarEl) avatarEl.innerText = initial;

    const labelEl = document.getElementById('header-user-label');
    if (labelEl) labelEl.innerText = name;

    const sublabelEl = document.getElementById('header-user-sublabel');
    if (sublabelEl) {
        sublabelEl.innerText = (user.role === 'admin' ? 'Administrator' : 'Session Active');
    }

    const dropName = document.getElementById('dropdown-user-name');
    if (dropName) dropName.innerText = name;

    const dropEmail = document.getElementById('dropdown-user-email');
    if (dropEmail) dropEmail.innerText = user.email;

    const dropRole = document.getElementById('dropdown-user-role');
    if (dropRole) dropRole.innerText = (user.role === 'admin' ? 'Admin Session' : 'Member Session');
}

function openLoginModal() {
    const modal = document.getElementById('modal-auth-login');
    if (modal) {
        modal.classList.remove('hidden');
        modal.querySelectorAll('input, select, textarea, button[type="submit"]').forEach(el => {
            el.disabled = false;
        });
    }
    switchAuthTab('password');
}

function closeLoginModal() {
    const modal = document.getElementById('modal-auth-login');
    if (modal) {
        modal.classList.add('hidden');
        modal.querySelectorAll('input, select, textarea, button[type="submit"]').forEach(el => {
            el.disabled = true;
        });
    }
}

function switchAuthTab(tab) {
    const btnPass = document.getElementById('auth-tab-btn-password');
    const btnScan = document.getElementById('auth-tab-btn-scan');
    const tabPass = document.getElementById('auth-tab-password');
    const tabScan = document.getElementById('auth-tab-scan');

    if (tab === 'password') {
        if (btnPass) {
            btnPass.className = 'flex-1 py-1.5 rounded-lg text-xs font-bold transition-all bg-indigo-600 text-white shadow';
        }
        if (btnScan) {
            btnScan.className = 'flex-1 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white transition-all';
        }
        if (tabPass) tabPass.classList.remove('hidden');
        if (tabScan) tabScan.classList.add('hidden');
    } else {
        if (btnScan) {
            btnScan.className = 'flex-1 py-1.5 rounded-lg text-xs font-bold transition-all bg-indigo-600 text-white shadow';
        }
        if (btnPass) {
            btnPass.className = 'flex-1 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white transition-all';
        }
        if (tabPass) tabPass.classList.add('hidden');
        if (tabScan) tabScan.classList.remove('hidden');
        initAuthScanQR();
    }
}

async function handlePasswordLogin(event) {
    if (event) event.preventDefault();
    const loginInput = document.getElementById('login-input-identity');
    const passInput = document.getElementById('login-input-password');
    const rememberInput = document.getElementById('login-remember-me');
    const errAlert = document.getElementById('login-error-alert');
    const submitBtn = document.getElementById('login-submit-btn');

    const login = loginInput?.value?.trim();
    const password = passInput?.value;
    const rememberMe = rememberInput?.checked ?? true;

    if (!login || !password) {
        if (errAlert) {
            errAlert.innerText = 'Please enter your login and password.';
            errAlert.classList.remove('hidden');
        }
        return;
    }

    if (errAlert) errAlert.classList.add('hidden');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin"></span> Authenticating...';
    }

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login, password, remember_me: rememberMe })
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            localStorage.setItem('bitmail_token', data.token);
            document.cookie = `bitmail_token=${data.token}; path=/; max-age=${rememberMe ? 2592000 : 86400}; SameSite=Lax`;
            App.currentUser = data.user;
            updateUserDisplay(data.user);
            closeLoginModal();
            showToast(`✓ Welcome back, ${data.user.name || data.user.email}!`, 'success');

            // Connect realtime telemetry & refresh all dashboard panels
            initWebSocket();
            await refreshAllData();
        } else {
            if (errAlert) {
                errAlert.innerText = data.detail || 'Invalid email/username or password.';
                errAlert.classList.remove('hidden');
            }
        }
    } catch (err) {
        if (errAlert) {
            errAlert.innerText = 'Connection error: ' + err.message;
            errAlert.classList.remove('hidden');
        }
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i data-lucide="log-in" class="w-4 h-4"></i><span>Unlock Platform</span>';
            initLucide();
        }
    }
}

function fillDemoAdminCredentials() {
    const loginInput = document.getElementById('login-input-identity');
    const passInput = document.getElementById('login-input-password');
    if (loginInput) loginInput.value = 'admin@bitmail.com';
    if (passInput) passInput.value = 'admin123';
    const errAlert = document.getElementById('login-error-alert');
    if (errAlert) errAlert.classList.add('hidden');
}

function togglePasswordVisibility(inputId, btn) {
    const input = document.getElementById(inputId);
    if (!input) return;
    if (input.type === 'password') {
        input.type = 'text';
        btn.innerHTML = '<i data-lucide="eye-off" class="w-4 h-4"></i>';
    } else {
        input.type = 'password';
        btn.innerHTML = '<i data-lucide="eye" class="w-4 h-4"></i>';
    }
    initLucide();
}

function toggleUserMenu(e) {
    if (e) e.stopPropagation();
    const menu = document.getElementById('user-dropdown-menu');
    if (menu) menu.classList.toggle('hidden');
}

document.addEventListener('click', (e) => {
    const menu = document.getElementById('user-dropdown-menu');
    const btn = document.getElementById('header-user-btn');
    if (menu && !menu.classList.contains('hidden')) {
        if (!menu.contains(e.target) && !btn?.contains(e.target)) {
            menu.classList.add('hidden');
        }
    }
});

async function logoutUser() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (e) {
        console.warn('Logout API error:', e);
    }
    localStorage.removeItem('bitmail_token');
    document.cookie = 'bitmail_token=; Max-Age=0; path=/;';
    App.currentUser = null;
    const menu = document.getElementById('user-dropdown-menu');
    if (menu) menu.classList.add('hidden');
    showToast('You have been signed out.', 'info');
    openLoginModal();
}

function openChangePasswordModal() {
    const menu = document.getElementById('user-dropdown-menu');
    if (menu) menu.classList.add('hidden');
    openModal('modal-change-password');
    const curr = document.getElementById('cp-current-password');
    if (curr) curr.value = '';
    const newP = document.getElementById('cp-new-password');
    if (newP) newP.value = '';
    const conf = document.getElementById('cp-confirm-password');
    if (conf) conf.value = '';
    document.getElementById('cp-error-alert')?.classList.add('hidden');
}

async function handleChangePassword(e) {
    if (e) e.preventDefault();
    const curr = document.getElementById('cp-current-password')?.value;
    const newP = document.getElementById('cp-new-password')?.value;
    const conf = document.getElementById('cp-confirm-password')?.value;
    const errEl = document.getElementById('cp-error-alert');

    if (newP !== conf) {
        if (errEl) {
            errEl.innerText = 'New passwords do not match.';
            errEl.classList.remove('hidden');
        }
        return;
    }

    try {
        const res = await fetch('/api/auth/change-password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ current_password: curr, new_password: newP })
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            closeModal('modal-change-password');
            showToast('✓ Password updated successfully!', 'success');
        } else {
            if (errEl) {
                errEl.innerText = data.detail || 'Failed to update password.';
                errEl.classList.remove('hidden');
            }
        }
    } catch (err) {
        if (errEl) {
            errEl.innerText = 'Error: ' + err.message;
            errEl.classList.remove('hidden');
        }
    }
}

let authScanSession = null;
let authScanTimer = null;
let authScanPoll = null;

async function initAuthScanQR() {
    const wrapper = document.getElementById('scan-qr-svg-wrapper-auth');
    const statusText = document.getElementById('scan-status-text-auth');
    const timerText = document.getElementById('scan-timer-text-auth');

    if (wrapper) {
        wrapper.innerHTML = `
            <div class="text-xs text-slate-500 font-mono flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-purple-500 animate-ping"></span> Generating Live QR...
            </div>
        `;
    }
    if (statusText) statusText.innerText = 'Initializing scan session...';

    clearInterval(authScanTimer);
    clearInterval(authScanPoll);

    try {
        const res = await fetch('/api/auth/scan/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_info: 'Desktop Browser (Bitmail Auth)' })
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            authScanSession = data;
            if (wrapper) {
                wrapper.innerHTML = data.qr_svg;
                const svgEl = wrapper.querySelector('svg');
                if (svgEl) {
                    svgEl.setAttribute('width', '100%');
                    svgEl.setAttribute('height', '100%');
                }
            }
            if (statusText) statusText.innerText = 'Waiting for phone scan...';

            let secondsLeft = data.expires_in_seconds || 300;
            const updateTimer = () => {
                const m = Math.floor(secondsLeft / 60);
                const s = secondsLeft % 60;
                if (timerText) timerText.innerText = `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
            };
            updateTimer();

            authScanTimer = setInterval(() => {
                secondsLeft--;
                if (secondsLeft <= 0) {
                    clearInterval(authScanTimer);
                    clearInterval(authScanPoll);
                    if (statusText) statusText.innerHTML = '<span class="text-rose-400">QR Expired. Click to refresh.</span>';
                } else {
                    updateTimer();
                }
            }, 1000);

            authScanPoll = setInterval(async () => {
                if (document.getElementById('modal-auth-login')?.classList.contains('hidden')) {
                    clearInterval(authScanPoll);
                    return;
                }
                try {
                    const sRes = await fetch(`/api/auth/scan/session/${data.session_id}/status?token=${encodeURIComponent(data.token)}`);
                    if (sRes.ok) {
                        const sData = await sRes.json();
                        if (sData.is_approved && sData.auth_token) {
                            clearInterval(authScanPoll);
                            clearInterval(authScanTimer);
                            handleWebSocketEvent({
                                type: 'scan_auth_approved',
                                data: {
                                    session_id: sData.session_id,
                                    email: sData.user_email,
                                    name: sData.user_name,
                                    auth_token: sData.auth_token
                                }
                            });
                        }
                    }
                } catch (e) {}
            }, 2500);
        }
    } catch (err) {
        showToast('Error generating QR: ' + err.message, 'error');
    }
}

async function simulateAuthScanApproval() {
    if (!authScanSession || !authScanSession.session_id) {
        showToast('Please wait for QR code to generate.', 'warning');
        return;
    }
    showToast('Simulating phone QR scan authorization...', 'info');
    try {
        const res = await fetch(`/api/auth/scan/simulate-approval/${authScanSession.session_id}?email=admin@bitmail.com`, {
            method: 'POST'
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast('✓ Mobile scan simulated successfully!', 'success');
        } else {
            showToast('Simulation error: ' + (data.detail || 'Unknown error'), 'error');
        }
    } catch (err) {
        showToast('Simulation failed: ' + err.message, 'error');
    }
}

// ==========================================================================
// 8. System & Dispatch Logs Console
// ==========================================================================
async function fetchLogs() {
    const levelSelect = document.getElementById('logs-level-filter');
    const sourceSelect = document.getElementById('logs-source-filter');
    const searchInput = document.getElementById('logs-search-input');

    const level = levelSelect ? levelSelect.value : 'all';
    const source = sourceSelect ? sourceSelect.value : 'all';
    const search = searchInput ? searchInput.value.trim() : '';

    const params = new URLSearchParams();
    if (level && level !== 'all') params.append('level', level);
    if (source && source !== 'all') params.append('source', source);
    if (search) params.append('search', search);
    params.append('limit', '250');

    try {
        const res = await fetch(`/api/logs?${params.toString()}`);
        if (!res.ok) return;
        const data = await res.json();

        App.logs = data.logs || [];

        // Update KPIs
        const totalEl = document.getElementById('logs-kpi-total');
        const errorsEl = document.getElementById('logs-kpi-errors');
        const warningsEl = document.getElementById('logs-kpi-warnings');
        const infoEl = document.getElementById('logs-kpi-info');
        const badgeEl = document.getElementById('nav-logs-badge');

        if (totalEl) totalEl.innerText = data.stats?.total ?? App.logs.length;
        if (errorsEl) errorsEl.innerText = data.stats?.errors ?? 0;
        if (warningsEl) warningsEl.innerText = data.stats?.warnings ?? 0;
        if (infoEl) infoEl.innerText = data.stats?.info ?? 0;
        if (badgeEl) badgeEl.innerText = data.stats?.total ?? App.logs.length;

        renderLogsTerminal();
    } catch (err) {
        console.warn('Failed to fetch system logs:', err);
    }
}

function renderLogsTerminal() {
    const container = document.getElementById('logs-container');
    const visibleCountEl = document.getElementById('logs-visible-count');
    if (!container) return;

    if (visibleCountEl) {
        visibleCountEl.innerText = `Showing ${App.logs.length} events`;
    }

    if (!App.logs || App.logs.length === 0) {
        container.innerHTML = `
            <div class="text-center py-16 text-slate-500 text-xs font-mono">
                <i data-lucide="terminal" class="w-8 h-8 mx-auto mb-2 text-slate-600 opacity-60"></i>
                No log entries match the selected filters.
            </div>
        `;
        initLucide();
        return;
    }

    container.innerHTML = App.logs.map(log => formatLogLine(log)).join('');

    const autoScroll = document.getElementById('logs-auto-scroll');
    if (autoScroll && autoScroll.checked) {
        container.scrollTop = container.scrollHeight;
    }

    initLucide();
}

function formatLogLine(log) {
    const lvl = (log.level || 'INFO').toUpperCase();
    const src = (log.source || 'system').toLowerCase();

    let levelClass = 'bg-slate-700/50 text-slate-300 border-white/10';
    let textClass = 'text-slate-200';

    if (lvl === 'ERROR' || lvl === 'CRITICAL') {
        levelClass = 'bg-rose-500/20 text-rose-300 border-rose-500/30';
        textClass = 'text-rose-200 font-semibold';
    } else if (lvl === 'WARNING' || lvl === 'WARN') {
        levelClass = 'bg-amber-500/20 text-amber-300 border-amber-500/30';
        textClass = 'text-amber-200';
    } else if (lvl === 'INFO') {
        levelClass = 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30';
        textClass = 'text-slate-200';
    } else if (lvl === 'DEBUG') {
        levelClass = 'bg-slate-800 text-slate-400 border-white/5';
        textClass = 'text-slate-400';
    }

    let sourceClass = 'text-slate-300 bg-slate-800 border-white/10';
    if (src === 'queue') sourceClass = 'text-indigo-300 bg-indigo-500/10 border-indigo-500/20';
    else if (src === 'scheduler') sourceClass = 'text-purple-300 bg-purple-500/10 border-purple-500/20';
    else if (src === 'smtp') sourceClass = 'text-amber-300 bg-amber-500/10 border-amber-500/20';
    else if (src === 'auth' || src === 'security') sourceClass = 'text-cyan-300 bg-cyan-500/10 border-cyan-500/20';
    else if (src === 'storage') sourceClass = 'text-emerald-300 bg-emerald-500/10 border-emerald-500/20';
    else if (src === 'tracking') sourceClass = 'text-blue-300 bg-blue-500/10 border-blue-500/20';

    const hasDetails = log.details && Object.keys(log.details).length > 0;
    const detailsHtml = hasDetails ? `
        <details class="mt-1 text-[11px] text-slate-400">
            <summary class="cursor-pointer hover:text-indigo-300 transition-colors inline-flex items-center gap-1 select-none">
                <span>View payload details</span>
            </summary>
            <pre class="mt-1 p-2 rounded-lg bg-slate-900 border border-white/5 overflow-x-auto text-[10px] text-slate-300 leading-tight">${escapeHtml(JSON.stringify(log.details, null, 2))}</pre>
        </details>
    ` : '';

    return `
        <div class="p-2 rounded-lg hover:bg-slate-900/60 border border-white/5 transition-all text-xs font-mono flex flex-col gap-1">
            <div class="flex items-center flex-wrap gap-2 text-[11px]">
                <span class="text-slate-500 select-none">[${escapeHtml(log.timestamp || '')}]</span>
                <span class="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase border ${levelClass}">${escapeHtml(lvl)}</span>
                <span class="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase border ${sourceClass}">${escapeHtml(src)}</span>
                <span class="text-slate-400 text-[10px] truncate max-w-xs" title="${escapeHtml(log.logger || '')}">(${escapeHtml(log.logger || '')})</span>
            </div>
            <div class="${textClass} break-words pl-1 leading-relaxed">
                ${escapeHtml(log.message || '')}
            </div>
            ${detailsHtml}
        </div>
    `;
}

function handleIncomingSystemLog(logEntry) {
    if (!logEntry || App.logsStreamPaused) return;

    // Check filter criteria
    const levelSelect = document.getElementById('logs-level-filter');
    const sourceSelect = document.getElementById('logs-source-filter');
    const searchInput = document.getElementById('logs-search-input');

    const curLevel = levelSelect ? levelSelect.value : 'all';
    const curSource = sourceSelect ? sourceSelect.value : 'all';
    const curSearch = searchInput ? searchInput.value.trim().toLowerCase() : '';

    const entryLevel = (logEntry.level || 'INFO').toLowerCase();
    const entrySource = (logEntry.source || 'system').toLowerCase();

    if (curLevel !== 'all' && entryLevel !== curLevel.toLowerCase()) return;
    if (curSource !== 'all' && entrySource !== curSource.toLowerCase()) return;
    if (curSearch) {
        const text = `${logEntry.message} ${logEntry.logger} ${logEntry.source}`.toLowerCase();
        if (!text.includes(curSearch)) return;
    }

    // Add to current in-memory view
    App.logs.unshift(logEntry);
    if (App.logs.length > 500) App.logs.pop();

    // Increment KPIs
    const totalEl = document.getElementById('logs-kpi-total');
    const badgeEl = document.getElementById('nav-logs-badge');
    if (totalEl) totalEl.innerText = parseInt(totalEl.innerText || '0', 10) + 1;
    if (badgeEl) badgeEl.innerText = parseInt(badgeEl.innerText || '0', 10) + 1;

    if (entryLevel === 'error' || entryLevel === 'critical') {
        const errorsEl = document.getElementById('logs-kpi-errors');
        if (errorsEl) errorsEl.innerText = parseInt(errorsEl.innerText || '0', 10) + 1;
    } else if (entryLevel === 'warning' || entryLevel === 'warn') {
        const warnEl = document.getElementById('logs-kpi-warnings');
        if (warnEl) warnEl.innerText = parseInt(warnEl.innerText || '0', 10) + 1;
    } else if (entryLevel === 'info') {
        const infoEl = document.getElementById('logs-kpi-info');
        if (infoEl) infoEl.innerText = parseInt(infoEl.innerText || '0', 10) + 1;
    }

    // Prepend into container if visible
    const container = document.getElementById('logs-container');
    if (container) {
        const placeholder = container.querySelector('.text-center');
        if (placeholder) placeholder.remove();

        const div = document.createElement('div');
        div.innerHTML = formatLogLine(logEntry);
        if (div.firstElementChild) {
            container.insertBefore(div.firstElementChild, container.firstChild);
        }

        const visibleCountEl = document.getElementById('logs-visible-count');
        if (visibleCountEl) {
            visibleCountEl.innerText = `Showing ${App.logs.length} events`;
        }

        const autoScroll = document.getElementById('logs-auto-scroll');
        if (autoScroll && autoScroll.checked) {
            container.scrollTop = 0;
        }
    }
}

function handleLogsFilterChange() {
    clearTimeout(App.logsFilterDebounce);
    App.logsFilterDebounce = setTimeout(() => {
        fetchLogs();
    }, 250);
}

function toggleLogsStreamPause() {
    App.logsStreamPaused = !App.logsStreamPaused;
    const icon = document.getElementById('logs-pause-icon');
    const text = document.getElementById('logs-pause-text');
    const status = document.getElementById('logs-stream-status');

    if (App.logsStreamPaused) {
        if (text) text.innerText = 'Resume Stream';
        if (icon) icon.setAttribute('data-lucide', 'play');
        if (status) {
            status.innerText = 'PAUSED';
            status.parentElement.className = 'flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs font-semibold';
        }
        showToast('Telemetry stream paused.', 'info');
    } else {
        if (text) text.innerText = 'Pause Stream';
        if (icon) icon.setAttribute('data-lucide', 'pause');
        if (status) {
            status.innerText = 'LIVE STREAM';
            status.parentElement.className = 'flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-xs font-semibold';
        }
        showToast('Telemetry stream resumed.', 'success');
        fetchLogs();
    }
    initLucide();
}

async function clearSystemLogsBuffer() {
    const confirmed = await confirmDialog(
        'Clear all recorded system logs from the telemetry buffer?',
        {
            title: 'Clear System Logs',
            detail: 'This will purge all cached dispatch events and application logs in memory.',
            confirmText: 'Clear Buffer',
            danger: true
        }
    );
    if (!confirmed) return;

    try {
        const res = await fetch('/api/logs', { method: 'DELETE' });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast(`✓ ${data.message || 'Log buffer cleared.'}`, 'success');
            App.logs = [];
            renderLogsTerminal();
            const totalEl = document.getElementById('logs-kpi-total');
            const errorsEl = document.getElementById('logs-kpi-errors');
            const warningsEl = document.getElementById('logs-kpi-warnings');
            const infoEl = document.getElementById('logs-kpi-info');
            const badgeEl = document.getElementById('nav-logs-badge');
            if (totalEl) totalEl.innerText = '0';
            if (errorsEl) errorsEl.innerText = '0';
            if (warningsEl) warningsEl.innerText = '0';
            if (infoEl) infoEl.innerText = '0';
            if (badgeEl) badgeEl.innerText = '0';
        } else {
            showToast(data.detail || 'Failed to clear logs', 'error');
        }
    } catch (err) {
        showToast('Error clearing logs: ' + err.message, 'error');
    }
}

function exportLogsFile(format = 'text') {
    const url = `/api/logs/export?format=${encodeURIComponent(format)}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = format === 'json' ? 'bitmail-system-logs.json' : 'bitmail-system.log';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast(`Downloading system logs (${format.toUpperCase()})...`, 'info');
}

async function emitDiagnosticTestLog() {
    const msg = await promptDialog(
        'Enter diagnostic message to log:',
        `Manual diagnostic test trace at ${new Date().toLocaleTimeString()}`,
        { title: 'Emit Diagnostic Log Event' }
    );
    if (!msg || !msg.trim()) return;

    try {
        const res = await fetch('/api/logs/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                level: 'info',
                message: msg.trim(),
                source: 'system'
            })
        });
        const data = await safeJson(res);
        if (res.ok && data.success) {
            showToast('✓ Diagnostic log event emitted.', 'success');
            await fetchLogs();
        } else {
            showToast(data.detail || 'Failed to emit test log', 'error');
        }
    } catch (err) {
        showToast('Error: ' + err.message, 'error');
    }
}

// ==========================================================================
// Deliverability & DNS Authenticator Controller
// ==========================================================================

async function copyElementText(elementId, btn) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const text = el.innerText || el.textContent;
    try {
        await navigator.clipboard.writeText(text);
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = '✓ Copied';
            setTimeout(() => { btn.innerHTML = originalHtml; }, 2000);
        }
        showToast('✓ Copied to clipboard', 'success');
    } catch (e) {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        if (btn) {
            const originalHtml = btn.innerHTML;
            btn.innerHTML = '✓ Copied';
            setTimeout(() => { btn.innerHTML = originalHtml; }, 2000);
        }
        showToast('✓ Copied to clipboard', 'success');
    }
}

function initDeliverabilityPanel() {
    initLucide();
    if (!App.deliverability.lastDiagnostic) {
        const domainInput = document.getElementById('dns-check-domain');
        if (domainInput && domainInput.value.trim()) {
            runDnsDiagnostic(false);
        }
    }
}

function setDnsProbeDomain(domain) {
    const domainInput = document.getElementById('dns-check-domain');
    if (domainInput) {
        domainInput.value = domain;
        runDnsDiagnostic(true);
    }
}

async function runDnsDiagnostic(force = true) {
    const domainInput = document.getElementById('dns-check-domain');
    const selectorInput = document.getElementById('dns-check-selector');
    const btn = document.getElementById('btn-run-dns-check');
    const searchIcon = document.getElementById('icon-dns-search');
    const searchText = document.getElementById('text-dns-search');

    if (!domainInput) return;
    const domain = domainInput.value.trim();
    if (!domain) {
        showToast('Please enter a domain name to probe.', 'warning');
        return;
    }
    const selector = selectorInput ? selectorInput.value.trim() : '';

    if (btn) btn.disabled = true;
    if (searchText) searchText.textContent = 'Querying DNS...';
    if (searchIcon) {
        searchIcon.setAttribute('data-lucide', 'loader-2');
        searchIcon.classList.add('animate-spin');
        initLucide();
    }

    try {
        const res = await fetch('/api/deliverability/dns-check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ domain: domain, dkim_selector: selector || null })
        });
        const data = await safeJson(res);

        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || 'DNS diagnostic probe failed.', 'error');
            return;
        }

        const diag = data.result;
        App.deliverability.lastDiagnostic = diag;
        renderDnsDiagnosticResults(diag);
        showToast(`✓ Live DNS probe completed for ${diag.domain} (Score: ${diag.score}/100 Grade ${diag.grade})`, 'success');
    } catch (err) {
        showToast('DNS query network error: ' + err.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
        if (searchText) searchText.textContent = 'Query Live DNS';
        if (searchIcon) {
            searchIcon.setAttribute('data-lucide', 'search');
            searchIcon.classList.remove('animate-spin');
            initLucide();
        }
    }
}

function renderDnsDiagnosticResults(diag) {
    const container = document.getElementById('dns-score-container');
    if (!container) return;
    container.classList.remove('hidden');

    const scoreVal = document.getElementById('dns-score-value');
    const scoreGrade = document.getElementById('dns-score-grade');
    const scoreBadge = document.getElementById('dns-score-badge');
    const scoreTitle = document.getElementById('dns-score-title');
    const scoreDesc = document.getElementById('dns-score-desc');
    const domainTag = document.getElementById('dns-score-domain-tag');
    const timestampTag = document.getElementById('dns-score-timestamp');

    if (scoreVal) scoreVal.textContent = diag.score;
    if (scoreGrade) scoreGrade.textContent = `GRADE ${diag.grade}`;
    if (domainTag) domainTag.textContent = diag.domain;
    if (timestampTag) timestampTag.textContent = new Date(diag.checked_at).toLocaleTimeString();

    if (scoreBadge) {
        scoreBadge.className = 'w-16 h-16 rounded-2xl flex flex-col items-center justify-center shrink-0 border ';
        if (diag.score >= 90) {
            scoreBadge.classList.add('bg-emerald-500/20', 'border-emerald-500/30');
            if (scoreVal) scoreVal.className = 'text-2xl font-black text-emerald-400 font-mono';
            if (scoreGrade) scoreGrade.className = 'text-[10px] font-bold text-emerald-300 tracking-wider';
            if (scoreTitle) scoreTitle.textContent = 'Excellent Deliverability Configuration';
            if (scoreDesc) scoreDesc.textContent = 'Domain meets Google & Yahoo 2024 bulk sender rules. Inboxes will trust outgoing messages.';
        } else if (diag.score >= 70) {
            scoreBadge.classList.add('bg-amber-500/20', 'border-amber-500/30');
            if (scoreVal) scoreVal.className = 'text-2xl font-black text-amber-400 font-mono';
            if (scoreGrade) scoreGrade.className = 'text-[10px] font-bold text-amber-300 tracking-wider';
            if (scoreTitle) scoreTitle.textContent = 'Moderate Deliverability – Improvements Advised';
            if (scoreDesc) scoreDesc.textContent = 'One or more authentication records are weak or missing. Messages may land in Spam.';
        } else {
            scoreBadge.classList.add('bg-rose-500/20', 'border-rose-500/30');
            if (scoreVal) scoreVal.className = 'text-2xl font-black text-rose-400 font-mono';
            if (scoreGrade) scoreGrade.className = 'text-[10px] font-bold text-rose-300 tracking-wider';
            if (scoreTitle) scoreTitle.textContent = 'Critical Deliverability Issues Detected';
            if (scoreDesc) scoreDesc.textContent = 'Missing essential authentication (SPF / DMARC). Outgoing mail risks direct rejection.';
        }
    }

    const spfSummary = document.getElementById('dns-summary-spf');
    const dmarcSummary = document.getElementById('dns-summary-dmarc');
    const dkimSummary = document.getElementById('dns-summary-dkim');
    const mxSummary = document.getElementById('dns-summary-mx');

    if (spfSummary) {
        const pass = diag.spf.status === 'pass' || diag.spf.status === 'softfail';
        spfSummary.className = `px-2.5 py-1 rounded-lg text-xs font-semibold border ${pass ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400' : 'bg-rose-500/10 border-rose-500/20 text-rose-400'}`;
        spfSummary.textContent = `SPF: ${diag.spf.status.toUpperCase()}`;
    }
    if (dmarcSummary) {
        const pass = diag.dmarc.status === 'pass' || diag.dmarc.status === 'warning';
        dmarcSummary.className = `px-2.5 py-1 rounded-lg text-xs font-semibold border ${pass ? 'bg-indigo-500/10 border-indigo-500/20 text-indigo-400' : 'bg-rose-500/10 border-rose-500/20 text-rose-400'}`;
        dmarcSummary.textContent = `DMARC: ${diag.dmarc.policy ? diag.dmarc.policy.toUpperCase() : diag.dmarc.status.toUpperCase()}`;
    }
    if (dkimSummary) {
        const pass = diag.dkim.status === 'pass';
        dkimSummary.className = `px-2.5 py-1 rounded-lg text-xs font-semibold border ${pass ? 'bg-purple-500/10 border-purple-500/20 text-purple-400' : 'bg-amber-500/10 border-amber-500/20 text-amber-400'}`;
        dkimSummary.textContent = `DKIM: ${pass ? 'FOUND' : 'MISSING'}`;
    }
    if (mxSummary) {
        const pass = diag.mx.status === 'pass';
        mxSummary.className = `px-2.5 py-1 rounded-lg text-xs font-semibold border ${pass ? 'bg-amber-500/10 border-amber-500/20 text-amber-400' : 'bg-rose-500/10 border-rose-500/20 text-rose-400'}`;
        mxSummary.textContent = `MX: ${diag.mx.records && diag.mx.records.length ? `${diag.mx.records.length} ACTIVE` : 'NONE'}`;
    }

    // Pillar 1: SPF
    const badgeSpf = document.getElementById('badge-dns-spf');
    const detailSpf = document.getElementById('detail-dns-spf');
    const recordBoxSpf = document.getElementById('record-box-spf');
    const recordTextSpf = document.getElementById('record-text-spf');
    if (badgeSpf) {
        badgeSpf.textContent = diag.spf.status.toUpperCase();
        badgeSpf.className = `px-2 py-0.5 rounded-md text-[11px] font-bold ${
            diag.spf.status === 'pass' ? 'bg-emerald-500/20 text-emerald-300' :
            (diag.spf.status === 'softfail' ? 'bg-amber-500/20 text-amber-300' : 'bg-rose-500/20 text-rose-300')
        }`;
    }
    if (detailSpf) detailSpf.textContent = diag.spf.details || 'SPF record check complete.';
    if (recordBoxSpf && recordTextSpf) {
        if (diag.spf.record) {
            recordTextSpf.textContent = diag.spf.record;
            recordBoxSpf.classList.remove('hidden');
        } else {
            recordBoxSpf.classList.add('hidden');
        }
    }

    // Pillar 2: DMARC
    const badgeDmarc = document.getElementById('badge-dns-dmarc');
    const detailDmarc = document.getElementById('detail-dns-dmarc');
    const recordBoxDmarc = document.getElementById('record-box-dmarc');
    const recordTextDmarc = document.getElementById('record-text-dmarc');
    if (badgeDmarc) {
        const pol = diag.dmarc.policy ? `p=${diag.dmarc.policy}` : diag.dmarc.status.toUpperCase();
        badgeDmarc.textContent = pol;
        badgeDmarc.className = `px-2 py-0.5 rounded-md text-[11px] font-bold ${
            diag.dmarc.status === 'pass' ? 'bg-emerald-500/20 text-emerald-300' :
            (diag.dmarc.status === 'warning' ? 'bg-amber-500/20 text-amber-300' : 'bg-rose-500/20 text-rose-300')
        }`;
    }
    if (detailDmarc) {
        let msg = diag.dmarc.details || 'DMARC record check complete.';
        if (diag.dmarc.meets_2024_bulk_requirements) {
            msg += ' (Meets Google/Yahoo 2024 bulk sender rules)';
        }
        detailDmarc.textContent = msg;
    }
    if (recordBoxDmarc && recordTextDmarc) {
        if (diag.dmarc.record) {
            recordTextDmarc.textContent = diag.dmarc.record;
            recordBoxDmarc.classList.remove('hidden');
        } else {
            recordBoxDmarc.classList.add('hidden');
        }
    }

    // Pillar 3: DKIM
    const badgeDkim = document.getElementById('badge-dns-dkim');
    const detailDkim = document.getElementById('detail-dns-dkim');
    const recordBoxDkim = document.getElementById('record-box-dkim');
    const recordTextDkim = document.getElementById('record-text-dkim');
    const dkimQueryLabel = document.getElementById('dkim-query-label');
    if (badgeDkim) {
        badgeDkim.textContent = diag.dkim.status === 'pass' ? 'ACTIVE' : diag.dkim.status.toUpperCase();
        badgeDkim.className = `px-2 py-0.5 rounded-md text-[11px] font-bold ${
            diag.dkim.status === 'pass' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-amber-500/20 text-amber-300'
        }`;
    }
    if (detailDkim) detailDkim.textContent = diag.dkim.details || 'DKIM signature check complete.';
    if (dkimQueryLabel && diag.dkim.selector) {
        dkimQueryLabel.textContent = `Selector (${diag.dkim.selector}):`;
    }
    if (recordBoxDkim && recordTextDkim) {
        if (diag.dkim.record) {
            recordTextDkim.textContent = diag.dkim.record;
            recordBoxDkim.classList.remove('hidden');
        } else {
            recordBoxDkim.classList.add('hidden');
        }
    }

    // Pillar 4: MX
    const badgeMx = document.getElementById('badge-dns-mx');
    const detailMx = document.getElementById('detail-dns-mx');
    const recordBoxMx = document.getElementById('record-box-mx');
    if (badgeMx) {
        badgeMx.textContent = diag.mx.status.toUpperCase();
        badgeMx.className = `px-2 py-0.5 rounded-md text-[11px] font-bold ${
            diag.mx.status === 'pass' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300'
        }`;
    }
    if (detailMx) {
        let msg = diag.mx.details || 'MX record probe complete.';
        if (diag.mx.provider) msg = `Provider: ${diag.mx.provider}. ` + msg;
        detailMx.textContent = msg;
    }
    if (recordBoxMx) {
        if (diag.mx.records && diag.mx.records.length > 0) {
            recordBoxMx.innerHTML = diag.mx.records.map(r => `
                <div class="flex items-center justify-between text-[11px] font-mono p-1.5 rounded bg-black/40 border border-white/5 text-slate-300">
                    <span class="truncate">${escapeHtml(r.host)}</span>
                    <span class="px-1.5 py-0.5 rounded bg-slate-800 text-amber-400 font-bold shrink-0 ml-2">Pri ${r.priority}</span>
                </div>
            `).join('');
            recordBoxMx.classList.remove('hidden');
        } else {
            recordBoxMx.classList.add('hidden');
        }
    }

    // Recommended Fixes
    const recSection = document.getElementById('dns-recommendations-section');
    const recList = document.getElementById('dns-recommendations-list');
    if (recSection && recList) {
        if (diag.recommended_records && diag.recommended_records.length > 0) {
            recSection.classList.remove('hidden');
            recList.innerHTML = diag.recommended_records.map((rec, idx) => `
                <div class="p-3 rounded-xl bg-slate-950/70 border border-amber-500/20 space-y-2">
                    <div class="flex items-center justify-between">
                        <div class="flex items-center gap-2">
                            <span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-bold text-[10px] font-mono">${escapeHtml(rec.type)}</span>
                            <span class="text-xs font-semibold text-white font-mono">${escapeHtml(rec.full_name || rec.name)}</span>
                        </div>
                        <button type="button" onclick="copyElementText('rec-val-${idx}', this)" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-cyan-400 text-xs font-medium border border-white/10 transition-colors flex items-center gap-1 cursor-pointer">
                            <i data-lucide="copy" class="w-3 h-3"></i> Copy Record
                        </button>
                    </div>
                    <div id="rec-val-${idx}" class="p-2 rounded bg-black/60 border border-white/5 text-[11px] font-mono text-slate-300 break-all select-all">${escapeHtml(rec.value)}</div>
                    <div class="text-[11px] text-slate-400">${escapeHtml(rec.purpose)}</div>
                </div>
            `).join('');
        } else {
            recSection.classList.add('hidden');
            recList.innerHTML = '';
        }
    }

    initLucide();
}

// Single Email Validator
async function validateSingleEmail() {
    const input = document.getElementById('email-single-input');
    const btn = document.getElementById('btn-validate-single');

    if (!input) return;
    const email = input.value.trim();
    if (!email) {
        showToast('Please enter an email address to verify.', 'warning');
        return;
    }

    if (btn) btn.disabled = true;
    try {
        const res = await fetch('/api/deliverability/validate-email', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: email })
        });
        const data = await safeJson(res);

        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || 'Email validation failed.', 'error');
            return;
        }

        const v = data.result;
        renderSingleEmailResult(v);
        showToast(`Email evaluated: ${v.status.toUpperCase()}`, v.status === 'valid' ? 'success' : (v.status === 'risky' ? 'warning' : 'error'));
    } catch (err) {
        showToast('Validation request error: ' + err.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

function renderSingleEmailResult(v) {
    const resultBox = document.getElementById('email-single-result');
    if (!resultBox) return;
    resultBox.classList.remove('hidden');

    const emailTag = document.getElementById('single-res-email');
    const badge = document.getElementById('single-res-badge');
    const syntaxTag = document.getElementById('single-res-syntax');
    const burnerTag = document.getElementById('single-res-burner');
    const mxTag = document.getElementById('single-res-mx');
    const mxList = document.getElementById('single-res-mx-list');
    const reasonsTag = document.getElementById('single-res-reasons');

    if (emailTag) emailTag.textContent = v.email;
    if (badge) {
        if (v.status === 'valid') {
            badge.textContent = 'DELIVERABLE';
            badge.className = 'px-2 py-0.5 rounded-md text-[11px] font-bold bg-emerald-500/20 text-emerald-300';
        } else if (v.status === 'risky') {
            badge.textContent = 'RISKY BURNER';
            badge.className = 'px-2 py-0.5 rounded-md text-[11px] font-bold bg-amber-500/20 text-amber-300';
        } else {
            badge.textContent = 'INVALID / UNREACHABLE';
            badge.className = 'px-2 py-0.5 rounded-md text-[11px] font-bold bg-rose-500/20 text-rose-300';
        }
    }

    if (syntaxTag) {
        syntaxTag.textContent = v.syntax_valid ? 'Valid RFC 5322' : 'Syntax Error';
        syntaxTag.className = v.syntax_valid ? 'font-medium text-emerald-400' : 'font-medium text-rose-400';
    }

    if (burnerTag) {
        burnerTag.textContent = v.is_disposable ? 'Burner / Disposable Detected!' : 'Clean (Not Disposable)';
        burnerTag.className = v.is_disposable ? 'font-bold text-amber-400' : 'font-medium text-emerald-400';
    }

    if (mxTag) {
        mxTag.textContent = v.has_mx ? 'Active MX Host Found' : 'No MX Records';
        mxTag.className = v.has_mx ? 'font-medium text-emerald-400' : 'font-medium text-rose-400';
    }

    if (mxList) {
        if (v.mx_records && v.mx_records.length > 0) {
            mxList.innerHTML = `<span class="text-slate-500 text-[10px]">Resolved Exchangers:</span>` +
                v.mx_records.map(r => `<div>• ${escapeHtml(r.host)} (Pri ${r.priority})</div>`).join('');
            mxList.classList.remove('hidden');
        } else {
            mxList.classList.add('hidden');
        }
    }

    if (reasonsTag) {
        if (v.reasons && v.reasons.length > 0) {
            reasonsTag.textContent = 'Note: ' + v.reasons.join(' | ');
            reasonsTag.classList.remove('hidden');
        } else {
            reasonsTag.classList.add('hidden');
        }
    }
}

function testBurnerSampleEmail() {
    const input = document.getElementById('email-single-input');
    if (input) {
        input.value = 'throwaway-lead@mailinator.com';
        validateSingleEmail();
    }
}

// Batch Email List Cleaner
function updateBatchInputCount() {
    const textarea = document.getElementById('email-batch-input');
    const badge = document.getElementById('batch-input-count-badge');
    if (!textarea || !badge) return;

    const raw = textarea.value.trim();
    if (!raw) {
        badge.textContent = '0 Emails';
        return;
    }
    const items = raw.split(/[\r\n,;]+/).filter(x => x.trim().length > 0);
    badge.textContent = `${items.length} Emails`;
}

function clearBatchInput() {
    const textarea = document.getElementById('email-batch-input');
    if (textarea) textarea.value = '';
    updateBatchInputCount();
    const resBox = document.getElementById('batch-results-container');
    if (resBox) resBox.classList.add('hidden');
}

function loadSampleBatchEmails() {
    const textarea = document.getElementById('email-batch-input');
    if (textarea) {
        textarea.value = [
            'alex.contact@gmail.com',
            '"Sarah Tech" <sarah@bitnade.com>',
            'temporary-burner@mailinator.com',
            'support@outlook.com',
            'spambox-user@tempmail.com',
            'invalid-syntax@@broken..com',
            'nonexistent-domain-4982734982.net'
        ].join('\n');
        updateBatchInputCount();
        showToast('Sample mixed email batch loaded.', 'info');
    }
}

async function validateBatchEmails() {
    const textarea = document.getElementById('email-batch-input');
    const btn = document.getElementById('btn-validate-batch');
    const text = document.getElementById('text-validate-batch');

    if (!textarea) return;
    const raw = textarea.value.trim();
    if (!raw) {
        showToast('Please paste recipient emails to clean.', 'warning');
        return;
    }

    const emailList = raw.split(/[\r\n,;]+/).map(x => x.trim()).filter(x => x.length > 0);
    if (emailList.length === 0) {
        showToast('No valid entries detected in input.', 'warning');
        return;
    }

    if (btn) btn.disabled = true;
    if (text) text.textContent = `Cleaning ${emailList.length} emails...`;

    try {
        const res = await fetch('/api/deliverability/validate-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ emails: emailList })
        });
        const data = await safeJson(res);

        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || 'Batch verification failed.', 'error');
            return;
        }

        const b = data.result;
        App.deliverability.batchResults = b.results || [];
        App.deliverability.lastBatchCleaned = (b.results || []).filter(r => r.status === 'valid').map(r => r.email);

        renderBatchResults(b);
        showToast(`✓ Batch verified in ${b.duration_seconds}s: ${b.deliverable_count}/${b.total} clean (${b.deliverable_percent}%)`, 'success');
    } catch (err) {
        showToast('Batch verification error: ' + err.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
        if (text) text.textContent = 'Clean & Verify List';
    }
}

function renderBatchResults(b) {
    const container = document.getElementById('batch-results-container');
    if (!container) return;
    container.classList.remove('hidden');

    const totalTag = document.getElementById('batch-kpi-total');
    const deliverableTag = document.getElementById('batch-kpi-deliverable');
    const burnerTag = document.getElementById('batch-kpi-burner');
    const invalidTag = document.getElementById('batch-kpi-invalid');

    if (totalTag) totalTag.textContent = b.total;
    if (deliverableTag) deliverableTag.textContent = `${b.deliverable_count} (${b.deliverable_percent}%)`;
    if (burnerTag) burnerTag.textContent = b.disposable_count || b.risky_count;
    if (invalidTag) invalidTag.textContent = b.invalid_count;

    const countAll = document.getElementById('count-filter-all');
    const countValid = document.getElementById('count-filter-valid');
    const countRisky = document.getElementById('count-filter-risky');
    const countInvalid = document.getElementById('count-filter-invalid');

    if (countAll) countAll.textContent = b.total;
    if (countValid) countValid.textContent = b.deliverable_count;
    if (countRisky) countRisky.textContent = b.disposable_count || b.risky_count;
    if (countInvalid) countInvalid.textContent = b.invalid_count;

    filterBatchResults('all');
}

function filterBatchResults(filter) {
    App.deliverability.batchFilter = filter;

    ['all', 'valid', 'risky', 'invalid'].forEach(f => {
        const btn = document.getElementById(`btn-batch-filter-${f}`);
        if (btn) {
            if (f === filter) {
                btn.className = 'px-2.5 py-1 rounded-lg text-xs font-semibold bg-indigo-600 text-white cursor-pointer';
            } else {
                btn.className = 'px-2.5 py-1 rounded-lg text-xs font-semibold bg-slate-800 text-slate-300 hover:text-white cursor-pointer';
            }
        }
    });

    const tbody = document.getElementById('batch-results-tbody');
    if (!tbody) return;

    let rows = App.deliverability.batchResults || [];
    if (filter === 'valid') {
        rows = rows.filter(r => r.status === 'valid');
    } else if (filter === 'risky') {
        rows = rows.filter(r => r.status === 'risky' || r.is_disposable);
    } else if (filter === 'invalid') {
        rows = rows.filter(r => r.status === 'invalid');
    }

    if (rows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-slate-500 font-sans text-xs">No records match the current filter.</td></tr>`;
        return;
    }

    tbody.innerHTML = rows.map(r => {
        let badgeHtml = '';
        if (r.status === 'valid') {
            badgeHtml = '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold text-[10px]">Deliverable</span>';
        } else if (r.status === 'risky') {
            badgeHtml = '<span class="px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 font-semibold text-[10px]">Burner Domain</span>';
        } else {
            badgeHtml = '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 font-semibold text-[10px]">Invalid / Dead</span>';
        }

        const dispHtml = r.is_disposable 
            ? '<span class="text-amber-400 font-bold">Burner!</span>' 
            : '<span class="text-slate-400">Clean</span>';

        const mxHtml = r.has_mx 
            ? `<span class="text-emerald-400">${r.mx_records && r.mx_records.length ? `${r.mx_records.length} MX` : 'Yes'}</span>` 
            : '<span class="text-rose-400 font-bold">No MX</span>';

        const reason = (r.reasons && r.reasons.length) ? r.reasons[0] : (r.status === 'valid' ? 'RFC 5322 Compliant' : '--');

        return `
            <tr class="hover:bg-white/5 transition-colors">
                <td class="py-2 px-3 text-white max-w-[200px] truncate">${escapeHtml(r.email)}</td>
                <td class="py-2 px-3">${badgeHtml}</td>
                <td class="py-2 px-3 font-sans">${dispHtml}</td>
                <td class="py-2 px-3">${mxHtml}</td>
                <td class="py-2 px-3 text-slate-400 font-sans text-[11px] truncate max-w-[220px]" title="${escapeHtml(reason)}">${escapeHtml(reason)}</td>
            </tr>
        `;
    }).join('');
}

function copyDeliverableEmails() {
    const list = App.deliverability.lastBatchCleaned || [];
    if (list.length === 0) {
        showToast('No deliverable emails to copy.', 'warning');
        return;
    }
    const text = list.join('\n');
    navigator.clipboard.writeText(text).then(() => {
        showToast(`✓ Copied ${list.length} deliverable email(s) to clipboard!`, 'success');
    }).catch(() => {
        showToast(`Failed to copy to clipboard`, 'error');
    });
}

function exportCleanedEmailCsv() {
    const list = App.deliverability.batchResults || [];
    if (list.length === 0) {
        showToast('No batch data to export.', 'warning');
        return;
    }

    let csvContent = 'data:text/csv;charset=utf-8,Email,Status,Is_Disposable,Has_MX,Domain,Reasons\n';
    list.forEach(r => {
        const row = [
            `"${(r.email || '').replace(/"/g, '""')}"`,
            r.status,
            r.is_disposable ? 'true' : 'false',
            r.has_mx ? 'true' : 'false',
            `"${(r.domain || '').replace(/"/g, '""')}"`,
            `"${((r.reasons || []).join('; ')).replace(/"/g, '""')}"`
        ];
        csvContent += row.join(',') + '\n';
    });

    const encodedUri = encodeURI(csvContent);
    const link = document.createElement('a');
    link.setAttribute('href', encodedUri);
    link.setAttribute('download', `bitmail-deliverability-export-${Date.now()}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    showToast(`✓ Exported ${list.length} email records to CSV`, 'success');
}

// Quick Broadcast Pre-flight Scan
async function runBroadcastPreflightCheck() {
    const banner = document.getElementById('broadcast-preflight-banner');
    const content = document.getElementById('broadcast-preflight-content');
    const rawInput = document.getElementById('broadcast-raw-emails');
    const btnScan = document.getElementById('btn-broadcast-preflight');

    if (!rawInput) return;
    const raw = rawInput.value.trim();
    if (!raw) {
        showToast('Enter recipient emails in Step 1 first before running pre-flight scan.', 'warning');
        return;
    }

    const emailList = raw.split(/[\r\n,;]+/).map(x => x.trim()).filter(x => x.length > 0);
    if (emailList.length === 0) {
        showToast('No recipients found to scan.', 'warning');
        return;
    }

    if (btnScan) {
        btnScan.disabled = true;
        btnScan.innerHTML = '<i data-lucide="loader-2" class="w-3 h-3 animate-spin"></i> Scanning...';
        initLucide();
    }

    try {
        const res = await fetch('/api/deliverability/validate-batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ emails: emailList })
        });
        const data = await safeJson(res);

        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || 'Pre-flight scan failed.', 'error');
            return;
        }

        const b = data.result;
        App.deliverability.lastBroadcastCleaned = (b.results || []).filter(r => r.status === 'valid').map(r => r.email);

        if (banner && content) {
            banner.classList.remove('hidden');
            if (b.deliverable_count === b.total) {
                content.innerHTML = `<span class="text-emerald-400 font-bold">✓ 100% Deliverable!</span> All ${b.total} recipients verified with clean RFC 5322 syntax, no burner domains, and active MX hosts.`;
            } else {
                const riskyCount = (b.disposable_count || 0) + (b.invalid_count || 0);
                content.innerHTML = `<span class="text-amber-400 font-bold">⚠ ${riskyCount} risky / invalid recipients detected</span> out of ${b.total}: 
                    <span class="text-emerald-400 font-medium">${b.deliverable_count} Deliverable</span>, 
                    <span class="text-amber-400 font-medium">${b.disposable_count} Disposable Burners</span>, 
                    <span class="text-rose-400 font-medium">${b.invalid_count} Invalid / No-MX</span>. 
                    Click "Clean Recipient List" to purge bounce risks automatically.`;
            }
        }
        showToast(`Pre-flight scan complete: ${b.deliverable_count}/${b.total} clean.`, b.deliverable_count === b.total ? 'success' : 'warning');
    } catch (err) {
        showToast('Pre-flight scan error: ' + err.message, 'error');
    } finally {
        if (btnScan) {
            btnScan.disabled = false;
            btnScan.innerHTML = '<i data-lucide="shield-check" class="w-3 h-3"></i> Pre-flight Scan';
            initLucide();
        }
    }
}

function applyBroadcastCleanedRecipients() {
    const rawInput = document.getElementById('broadcast-raw-emails');
    const banner = document.getElementById('broadcast-preflight-banner');
    const cleaned = App.deliverability.lastBroadcastCleaned || [];

    if (!rawInput) return;
    if (cleaned.length === 0) {
        showToast('No clean recipients to apply.', 'warning');
        return;
    }

    rawInput.value = cleaned.join('\n');
    updateBroadcastEmailCount();
    if (banner) banner.classList.add('hidden');
    showToast(`✓ Applied clean recipient list (${cleaned.length} verified addresses).`, 'success');
}

