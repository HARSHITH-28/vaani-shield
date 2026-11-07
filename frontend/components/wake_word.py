"""Browser-side continuous hands-free voice interaction & local wake-word engine.

Implements a robust Siri-style voice state machine in the top-level parent window realm:
  - DISABLED             - Wake-word disabled in Preferences; all recognition instances stopped.
  - STANDBY              - Local browser Web Speech API listening for "Hey Vaani" (zero cloud streaming).
  - WAKE_DETECTED        - Safely stops standby, opens Copilot if closed, prepares greeting.
  - GREETING             - Plays fixed English greeting once:
                           "Hello! I'm Vaani. How can I help you today?"
  - COMMAND_LISTENING    - Automatically listens for user's question with live transcript
                           (Kannada, English, Hindi, code-mixed) without clicking mic.
  - SUBMITTING           - Submits recognized question once through existing Copilot composer pipeline.
  - ASSISTANT_RESPONDING - Waits for assistant response & TTS playback completion, then returns to STANDBY.

Strictly protects manual microphone functionality and prevents recognition self-triggering or duplicate sessions.
"""

from __future__ import annotations

import json
import streamlit.components.v1 as components


def render_wake_word_listener(st, state):
    """Render the browser-side hands-free voice engine and truthful visual status indicator."""
    wake_enabled = bool(state.get("wake_word_enabled", True))
    is_dock_open = bool(state.get("assistant_open"))

    display_name = (
        state.get("customer", {}).get("display_name")
        or state.get("customer_profile", {}).get("name")
        or "Arjun Sharma"
    )
    json_name = json.dumps(display_name)
    current_lang = str(state.get("language_hint") or "auto")
    auto_trigger = bool(
        state.pop("wake_triggered_greeting", False)
        or state.pop("wake_activated", False)
    )

    # Status indicator in Streamlit (updated dynamically by parent-realm VoiceManager)
    col_indicator, col_action = st.columns([4.2, 1.8])
    with col_indicator:
        if not wake_enabled:
            st.markdown(
                """
                <div class="wake-word-status-bar standby">
                  <div class="wake-word-indicator">
                    <span class="wake-word-dot" style="background:#94A3B8;"></span>
                    <span class="wake-word-text">🎙️ <b>Wake-Word Paused</b> (Manual Mic Active)</span>
                  </div>
                  <small class="wake-word-sub">Enable in sidebar for hands-free <i>"Hey Vaani"</i></small>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"""
                <div class="wake-word-status-bar {'active' if not is_dock_open else 'connected'}">
                  <div class="wake-word-indicator">
                    <span class="wake-word-dot" style="background:#F59E0B;"></span>
                    <span class="wake-word-text">⏳ <b>Starting Voice Standby...</b> Say <i>"Hey Vaani"</i> once active</span>
                  </div>
                  <small class="wake-word-sub">Hands-Free Siri-Style · Zero Cloud Audio Streaming · Manual Mic Available</small>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with col_action:
        # Simulation / activation button to trigger and test the complete wake-word -> greeting -> command flow
        btn_label = "🎤 Test 'Hey Vaani'" if not is_dock_open else "🎤 Trigger Voice Turn"
        if st.button(btn_label, key="btn_manual_wake_trigger", help="Simulate hands-free 'Hey Vaani' greeting and question capture", use_container_width=True):
            state["assistant_open"] = True
            state["wake_activated"] = True
            state["wake_triggered_greeting"] = True
            st.rerun()

    if not wake_enabled:
        # If wake word is disabled, inject cleanup script to ensure any background recognition instance terminates cleanly
        cleanup_html = """
        <script>
          (function() {
            const p = window.parent || window;
            if (p.__vaaniVoiceManager) {
              try { p.__vaaniVoiceManager.stopAll(); } catch(e) {}
            }
          })();
        </script>
        """
        components.html(cleanup_html, height=0)
        return

    # Browser-side JavaScript Web Speech Continuous Voice State Machine
    # Executed inside window.parent's global realm so Streamlit iframe updates never abort active sessions.
    html_code = f"""
    <div id="vaani-voice-container" style="
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 11.5px;
      color: #1E293B;
      background: #FFFFFF;
      border: 1px solid #E2E8F0;
      border-radius: 8px;
      padding: 5px 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
      margin-top: 2px;
      cursor: pointer;
    " title="Click to arm or restart local 'Hey Vaani' voice standby">
      <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
        <span id="vaani-voice-pulse" style="width: 8px; height: 8px; border-radius: 50%; background: #F59E0B; display: inline-block;"></span>
        <span id="vaani-voice-msg" style="font-weight: 500; color: #334155;">⏳ Starting local voice standby...</span>
      </div>
      <div style="display: flex; align-items: center; gap: 6px;">
        <span id="vaani-voice-badge" style="font-size: 10px; font-weight: 700; color: #97144D; background: #FDF2F8; border: 1px solid #FBCFE8; padding: 2px 6px; border-radius: 4px; text-transform: uppercase;">Arming Mic</span>
      </div>
    </div>

    <script>
      (function() {{
        const parentWin = window.parent || window;
        const parentDoc = parentWin.document || document;
        const runtimeConfig = {{
          userLanguage: {json.dumps(current_lang)},
          customerName: {json_name},
          autoTrigger: {json.dumps(auto_trigger)},
          isDockOpen: {json.dumps(is_dock_open)}
        }};

        function bootstrapParentVoiceEngine(parentWin, parentDoc, cfg) {{
          const pSetTimeout = parentWin.setTimeout.bind(parentWin);
          const pClearTimeout = parentWin.clearTimeout.bind(parentWin);
          const pSetInterval = parentWin.setInterval.bind(parentWin);
          const pClearInterval = parentWin.clearInterval.bind(parentWin);

          const SR = parentWin.SpeechRecognition || parentWin.webkitSpeechRecognition;
          const userLanguage = (cfg && cfg.userLanguage) || 'auto';
          const customerName = (cfg && cfg.customerName) || 'Arjun Sharma';
          const autoTrigger = Boolean(cfg && cfg.autoTrigger);

          const FIXED_GREETING = "Hello! I'm Vaani. How can I help you today?";
          // Welcome back, ${{customerName}}! What can I help you with?
          const COOLDOWN_MS = 2500;
          const LISTENING_TIMEOUT_MS = 8500;

          // -------------------------------------------------------------------
          // Cross-Iframe & Parent Status Bar Live DOM Element Lookup
          // -------------------------------------------------------------------
          function getDOMElements() {{
            let msgEl = null, pulseEl = null, badgeEl = null, containerEl = null;
            try {{
              const iframes = parentDoc.querySelectorAll('iframe');
              for (let i = 0; i < iframes.length; i++) {{
                const doc = iframes[i].contentDocument || (iframes[i].contentWindow && iframes[i].contentWindow.document);
                if (doc) {{
                  const m = doc.getElementById('vaani-voice-msg');
                  if (m) {{
                    msgEl = m;
                    pulseEl = doc.getElementById('vaani-voice-pulse');
                    badgeEl = doc.getElementById('vaani-voice-badge');
                    containerEl = doc.getElementById('vaani-voice-container');
                  }}
                }}
              }}
            }} catch (e) {{}}
            const statusBar = parentDoc.querySelector('.wake-word-status-bar');
            const statusTextEl = statusBar ? statusBar.querySelector('.wake-word-text') : null;
            const statusDotEl = statusBar ? statusBar.querySelector('.wake-word-dot') : null;
            return {{
              msgEl: msgEl || parentDoc.getElementById('vaani-voice-msg'),
              pulseEl: pulseEl || parentDoc.getElementById('vaani-voice-pulse'),
              badgeEl: badgeEl || parentDoc.getElementById('vaani-voice-badge'),
              containerEl: containerEl || parentDoc.getElementById('vaani-voice-container'),
              statusBar: statusBar,
              statusTextEl: statusTextEl,
              statusDotEl: statusDotEl
            }};
          }}

          // -------------------------------------------------------------------
          // Independent Scroll, Manual Mic Monitor & Enter-to-Submit Manager
          // -------------------------------------------------------------------
          function setupCopilotScrollAndComposer() {{
            try {{
              if (!parentWin.__vaaniChatScrollState) {{
                parentWin.__vaaniChatScrollState = {{
                  msgCount: 0,
                  scrollTop: 0,
                  userScrolledUp: false
                }};
              }}
              const scrollState = parentWin.__vaaniChatScrollState;
              const scrollEl = parentDoc.querySelector('.st-key-copilot-chat-scroll-area');

              if (scrollEl) {{
                const wrapper = scrollEl.parentElement;
                if (wrapper && wrapper.getAttribute('data-testid') === 'stLayoutWrapper') {{
                  wrapper.style.setProperty('flex', '1 1 0%', 'important');
                  wrapper.style.setProperty('min-height', '0', 'important');
                  wrapper.style.setProperty('max-height', '100%', 'important');
                  wrapper.style.setProperty('height', '100%', 'important');
                  wrapper.style.setProperty('overflow', 'hidden', 'important');
                  wrapper.style.setProperty('display', 'flex', 'important');
                  wrapper.style.setProperty('flex-direction', 'column', 'important');
                }}

                if (!scrollEl.dataset.scrollBound) {{
                  scrollEl.dataset.scrollBound = 'true';
                  scrollEl.addEventListener('scroll', function() {{
                    const distFromBottom = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
                    scrollState.userScrolledUp = distFromBottom > 65;
                    scrollState.scrollTop = scrollEl.scrollTop;
                  }}, {{ passive: true }});
                }}

                const currentMsgCount = scrollEl.querySelectorAll('[data-testid="stChatMessage"]').length;
                if (currentMsgCount > scrollState.msgCount) {{
                  scrollState.msgCount = currentMsgCount;
                  scrollState.userScrolledUp = false;
                  pSetTimeout(() => {{
                    scrollEl.scrollTop = scrollEl.scrollHeight;
                    scrollState.scrollTop = scrollEl.scrollTop;
                  }}, 60);
                }} else if (currentMsgCount === scrollState.msgCount && scrollState.userScrolledUp) {{
                  if (Math.abs(scrollEl.scrollTop - scrollState.scrollTop) > 10) {{
                    scrollEl.scrollTop = scrollState.scrollTop;
                  }}
                }} else {{
                  scrollState.msgCount = currentMsgCount;
                }}
              }}

              // Bind Enter-to-Submit on Copilot composer textarea
              const pill = parentDoc.querySelector('.st-key-copilot-composer-pill');
              const area = pill ? pill.querySelector('textarea') : null;
              if (area && !area.dataset.vaaniEnterBound) {{
                area.dataset.vaaniEnterBound = 'true';
                area.addEventListener('keydown', function(e) {{
                  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.isComposing) {{
                    e.preventDefault();
                    const ctrlEnter = new parentWin.KeyboardEvent('keydown', {{
                      key: 'Enter',
                      code: 'Enter',
                      keyCode: 13,
                      which: 13,
                      ctrlKey: true,
                      metaKey: true,
                      bubbles: true,
                      cancelable: true
                    }});
                    area.dispatchEvent(ctrlEnter);
                  }}
                }});
              }}

              // Keep VoiceManager UI and manual mic mutual exclusion synced across reruns
              const mgr = parentWin.__vaaniVoiceManager;
              if (mgr && mgr.active) {{
                const manualTray = parentDoc.querySelector('.st-key-copilot-voice-tray');
                if (manualTray && (mgr.state === 'STANDBY' || mgr.state === 'COMMAND_LISTENING')) {{
                  mgr.stopAllRecognitions();
                  mgr.updateUI('MANUAL_MIC', '🎙️ Manual recording active in Copilot', '#6366F1');
                }} else if (!manualTray && mgr.state === 'MANUAL_MIC') {{
                  mgr.startWakeStandby(true);
                }} else {{
                  mgr.syncUI();
                }}
              }}
            }} catch (err) {{}}
          }}

          setupCopilotScrollAndComposer();
          if (!parentWin.__vaaniScrollInterval) {{
            parentWin.__vaaniScrollInterval = pSetInterval(setupCopilotScrollAndComposer, 450);
          }}

          function playChime() {{
            try {{
              const AudioCtx = parentWin.AudioContext || parentWin.webkitAudioContext;
              if (!AudioCtx) return;
              const ctx = new AudioCtx();
              const osc = ctx.createOscillator();
              const gain = ctx.createGain();
              osc.connect(gain);
              gain.connect(ctx.destination);
              osc.type = 'sine';
              const now = ctx.currentTime;
              osc.frequency.setValueAtTime(523.25, now);        // C5
              osc.frequency.setValueAtTime(659.25, now + 0.08);  // E5
              osc.frequency.setValueAtTime(783.99, now + 0.16);  // G5
              gain.gain.setValueAtTime(0.14, now);
              gain.gain.exponentialRampToValueAtTime(0.001, now + 0.36);
              osc.start(now);
              osc.stop(now + 0.36);
              pSetTimeout(() => {{
                try {{ if (typeof ctx.close === 'function') ctx.close(); }} catch (e) {{}}
              }}, 500);
            }} catch(e) {{
              console.log("Audio chime feedback unavailable:", e);
            }}
          }}

          // -------------------------------------------------------------------
          // Parent-Realm Persistent Singleton Voice State Machine
          // -------------------------------------------------------------------
          let manager = parentWin.__vaaniVoiceManager;

          if (!manager || typeof manager.configure !== 'function') {{
            if (manager && typeof manager.stopAllRecognitions === 'function') {{
              try {{ manager.stopAllRecognitions(); }} catch(e) {{}}
            }}

            manager = {{
              state: 'STANDBY', // 'DISABLED', 'STANDBY', 'WAKE_DETECTED', 'GREETING', 'COMMAND_LISTENING', 'SUBMITTING', 'PROCESSING', 'ASSISTANT_RESPONDING'
              wakeRecognition: null,
              commandRecognition: null,
              responsePollTimer: null,
              standbyRestartTimer: null,
              recognitionRunning: false,
              consecutiveQuickEnds: 0,
              lastTriggerTime: 0,
              permissionBlocked: false,
              active: true,
              userLanguage: userLanguage,
              customerName: customerName,
              lastMessage: '⏳ Starting local voice standby...',
              lastColor: '#F59E0B',

              configure: function(newCfg) {{
                if (newCfg) {{
                  if (newCfg.userLanguage) this.userLanguage = newCfg.userLanguage;
                  if (newCfg.customerName) this.customerName = newCfg.customerName;
                }}
                this.active = true;

                // Bind click handler on voice container to allow manual one-click re-arming
                const {{ containerEl, statusBar }} = getDOMElements();
                const bindManualRearm = (el) => {{
                  if (el && !el.dataset.vaaniRearmBound) {{
                    el.dataset.vaaniRearmBound = 'true';
                    el.addEventListener('click', () => {{
                      this.permissionBlocked = false;
                      this.consecutiveQuickEnds = 0;
                      if (this.state === 'STANDBY' || this.state === 'PERMISSION_BLOCKED' || this.state === 'DISABLED') {{
                        this.startWakeStandby(true);
                      }}
                    }});
                  }}
                }};
                bindManualRearm(containerEl);
                bindManualRearm(statusBar);

                if (newCfg && newCfg.autoTrigger) {{
                  pSetTimeout(() => {{
                    this.onWakePhraseDetected();
                  }}, 150);
                  return;
                }}

                // Check if we have an active voice pending state across a rerun (e.g. Copilot just opened)
                if (parentWin.__vaaniVoicePendingState && (Date.now() - parentWin.__vaaniVoicePendingState.time < 6500)) {{
                  const pending = parentWin.__vaaniVoicePendingState;
                  delete parentWin.__vaaniVoicePendingState;
                  if (pending.action === 'START_COMMAND_LISTENING' && this.state !== 'GREETING' && this.state !== 'COMMAND_LISTENING') {{
                    this.waitForCopilotAndStartListening();
                    return;
                  }}
                }}

                const activeTurnStates = ['WAKE_DETECTED', 'GREETING', 'COMMAND_LISTENING', 'SUBMITTING', 'PROCESSING', 'ASSISTANT_RESPONDING'];
                if (activeTurnStates.includes(this.state)) {{
                  this.syncUI();
                  return;
                }}

                // If standby recognition is ALREADY actively running in window.parent, keep it running across reruns!
                if (this.state === 'STANDBY' && this.recognitionRunning && this.wakeRecognition) {{
                  this.syncUI();
                  return;
                }}

                // Start standby if not currently running
                this.startWakeStandby(false);
              }},

              updateUI: function(stateName, message, color) {{
                this.state = stateName;
                if (message) this.lastMessage = message;
                if (color) this.lastColor = color;
                this.syncUI();
              }},

              syncUI: function() {{
                const {{ msgEl, pulseEl, badgeEl, statusTextEl, statusDotEl }} = getDOMElements();
                if (msgEl && this.lastMessage) msgEl.textContent = this.lastMessage;
                if (pulseEl) pulseEl.style.background = this.lastColor || '#059669';
                if (badgeEl) {{
                  const badgeLabels = {{
                    'DISABLED': 'Wake-Word Disabled',
                    'STANDBY': this.recognitionRunning ? 'Hands-Free Standby' : 'Arming Mic',
                    'WAKE_DETECTED': 'Wake Detected',
                    'GREETING': 'Greeting Playing',
                    'COMMAND_LISTENING': 'Listening...',
                    'SUBMITTING': 'Submitting Command',
                    'PROCESSING': 'Submitting Command',
                    'ASSISTANT_RESPONDING': 'Assistant Responding',
                    'PERMISSION_BLOCKED': 'Mic Blocked',
                    'UNSUPPORTED': 'Web Speech Unavailable',
                    'MANUAL_MIC': 'Manual Mic Active'
                  }};
                  badgeEl.textContent = badgeLabels[this.state] || this.state.replace('_', ' ');
                }}

                // Truthfully synchronize the main Streamlit top status bar
                if (statusTextEl) {{
                  if (this.state === 'STANDBY' && this.recognitionRunning) {{
                    statusTextEl.innerHTML = '🎙️ <b>Continuous Voice Active:</b> Say <i>"Hey Vaani"</i>';
                  }} else if (this.state === 'STANDBY' && !this.recognitionRunning) {{
                    statusTextEl.innerHTML = '⏳ <b>Voice Standby Arming:</b> Click anywhere if browser awaits interaction';
                  }} else if (this.state === 'WAKE_DETECTED' || this.state === 'GREETING') {{
                    statusTextEl.innerHTML = '🗣️ <b>Vaani Greeting:</b> <i>"Hello! I\\\'m Vaani. How can I help you today?"</i>';
                  }} else if (this.state === 'COMMAND_LISTENING') {{
                    statusTextEl.innerHTML = '👂 <b>Listening for Question:</b> Speak now (hands-free)';
                  }} else if (this.state === 'SUBMITTING' || this.state === 'PROCESSING') {{
                    statusTextEl.innerHTML = '✨ <b>Submitting Question:</b> Sending to Vaani Copilot...';
                  }} else if (this.state === 'ASSISTANT_RESPONDING') {{
                    statusTextEl.innerHTML = '🔊 <b>Vaani Responding:</b> Speaking answer, standby will resume...';
                  }} else if (this.state === 'PERMISSION_BLOCKED') {{
                    statusTextEl.innerHTML = '⚠️ <b>Microphone Permission Blocked:</b> Allow mic in browser address bar';
                  }} else if (this.state === 'UNSUPPORTED') {{
                    statusTextEl.innerHTML = '⚠️ <b>Web Speech API Unavailable:</b> Use manual mic 🎙️ in Copilot';
                  }} else if (this.state === 'MANUAL_MIC') {{
                    statusTextEl.innerHTML = '🎙️ <b>Wake-Word Paused:</b> Manual recording active in Copilot';
                  }} else if (this.state === 'DISABLED') {{
                    statusTextEl.innerHTML = '🎙️ <b>Wake-Word Paused</b> (Manual Mic Active)';
                  }}
                }}
                if (statusDotEl) {{
                  statusDotEl.style.background = this.lastColor || '#059669';
                }}
              }},

              openCopilotIfClosed: function() {{
                try {{
                  const panel = parentDoc.querySelector('.st-key-floating-assistant-panel') ||
                                parentDoc.querySelector('.st-key-floating-assistant-dock');
                  if (panel) return false;

                  const toggleBtn = parentDoc.querySelector('.st-key-header-copilot-toggle button') ||
                                    parentDoc.querySelector('.st-key-floating-assistant-toggle button') ||
                                    parentDoc.querySelector('button[key="header-copilot-toggle"]') ||
                                    Array.from(parentDoc.querySelectorAll('button')).find(b => {{
                                      const txt = (b.textContent || '').trim();
                                      return (txt.includes('Copilot') || txt.includes('Ask Vaani')) && !txt.includes('✕');
                                    }});
                  if (toggleBtn && !toggleBtn.textContent.includes('✕')) {{
                    parentWin.__vaaniVoicePendingState = {{ action: 'START_COMMAND_LISTENING', time: Date.now() }};
                    toggleBtn.click();
                    return true;
                  }}
                }} catch(err) {{
                  console.warn("Could not trigger Copilot toggle button:", err);
                }}
                return false;
              }},

              // STATE 1: WAKE-WORD STANDBY
              startWakeStandby: function(forceRestart) {{
                if (!this.active || this.permissionBlocked) return;

                if (this.standbyRestartTimer) {{
                  pClearTimeout(this.standbyRestartTimer);
                  this.standbyRestartTimer = null;
                }}

                // Check if manual mic tray is currently open in Copilot
                const manualTray = parentDoc.querySelector('.st-key-copilot-voice-tray');
                if (manualTray) {{
                  this.stopAllRecognitions();
                  this.updateUI('MANUAL_MIC', '🎙️ Manual recording active in Copilot', '#6366F1');
                  this.standbyRestartTimer = pSetTimeout(() => {{ this.startWakeStandby(false); }}, 1500);
                  return;
                }}

                // Do not abort an already healthy running standby recognition session unless forced
                if (!forceRestart && this.state === 'STANDBY' && this.recognitionRunning && this.wakeRecognition) {{
                  this.syncUI();
                  return;
                }}

                this.stopAllRecognitions();

                if (!SR) {{
                  this.updateUI('UNSUPPORTED', 'Web Speech API not supported in this browser (use manual mic 🎙️)', '#94A3B8');
                  return;
                }}

                // Truthful initial state before onstart fires
                this.recognitionRunning = false;
                this.updateUI('STANDBY', '⏳ Arming local microphone for "Hey Vaani"...', '#F59E0B');

                try {{
                  const rec = new SR();
                  rec.continuous = true;
                  rec.interimResults = true;
                  rec.lang = 'en-IN';
                  let sessionStartTime = Date.now();

                  rec.onstart = () => {{
                    sessionStartTime = Date.now();
                    this.recognitionRunning = true;
                    this.consecutiveQuickEnds = 0;
                    if (this.state === 'STANDBY') {{
                      this.updateUI('STANDBY', '🎙️ Standby: Say "Hey Vaani" anytime', '#059669');
                    }}
                  }};

                  rec.onresult = (event) => {{
                    if (this.state !== 'STANDBY') return;
                    for (let i = event.resultIndex || 0; i < event.results.length; ++i) {{
                      const res = event.results[i];
                      const phrase = ((res && res[0] && res[0].transcript) || '').toLowerCase().trim();
                      if (
                        phrase.includes("hey vaani") ||
                        phrase.includes("hey vani") ||
                        phrase.includes("hey bani") ||
                        phrase.includes("hey wani") ||
                        phrase.includes("hi vaani") ||
                        phrase.includes("hi vani") ||
                        phrase.includes("hello vaani") ||
                        phrase.includes("vaani") ||
                        phrase.includes("vani") ||
                        phrase.includes("vaanishield") ||
                        phrase.includes("वानी") ||
                        phrase.includes("वाणी") ||
                        phrase.includes("ವಾಣಿ")
                      ) {{
                        this.onWakePhraseDetected();
                        break;
                      }}
                    }}
                  }};

                  rec.onerror = (event) => {{
                    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {{
                      this.permissionBlocked = true;
                      this.recognitionRunning = false;
                      this.updateUI('PERMISSION_BLOCKED', '⚠️ Microphone permission blocked. Allow mic in Edge address bar.', '#DC2626');
                    }} else if (event.error === 'audio-capture') {{
                      this.permissionBlocked = true;
                      this.recognitionRunning = false;
                      this.updateUI('PERMISSION_BLOCKED', '⚠️ No microphone detected. Check audio input device.', '#DC2626');
                    }} else if (event.error !== 'no-speech' && event.error !== 'aborted') {{
                      console.warn("Wake recognition notice:", event.error);
                    }}
                  }};

                  rec.onend = () => {{
                    this.recognitionRunning = false;
                    if (this.permissionBlocked) {{
                      // Stop restart loop if microphone permission is denied
                      return;
                    }}
                    if (this.state === 'STANDBY' && this.active && !this.permissionBlocked) {{
                      const sessionDuration = Date.now() - sessionStartTime;
                      if (sessionDuration < 300) {{
                        this.consecutiveQuickEnds = (this.consecutiveQuickEnds || 0) + 1;
                      }} else {{
                        this.consecutiveQuickEnds = 0;
                      }}

                      if (this.consecutiveQuickEnds >= 4) {{
                        // Browser requires user gesture before allowing microphone session
                        this.updateUI('STANDBY', '👆 Click anywhere on dashboard to activate "Hey Vaani" mic', '#D97706');
                        return;
                      }}

                      if (this.standbyRestartTimer) pClearTimeout(this.standbyRestartTimer);
                      this.standbyRestartTimer = pSetTimeout(() => {{
                        if (this.state === 'STANDBY' && this.active && !this.permissionBlocked && !this.recognitionRunning) {{
                          try {{
                            this.startWakeStandby(true);
                          }} catch(e) {{}}
                        }}
                      }}, 400);
                    }}
                  }};

                  this.wakeRecognition = rec;
                  rec.start();
                }} catch (e) {{
                  this.recognitionRunning = false;
                  this.updateUI('STANDBY', '👆 Click anywhere on dashboard to activate "Hey Vaani" mic', '#D97706');
                  console.error("Failed to start wake recognition:", e);
                }}
              }},

              // STATE 2: WAKE PHRASE DETECTED
              onWakePhraseDetected: function() {{
                const now = Date.now();
                if (now - this.lastTriggerTime < COOLDOWN_MS) {{
                  return;
                }}
                this.lastTriggerTime = now;
                this.stopAllRecognitions();

                this.updateUI('WAKE_DETECTED', '✨ "Hey Vaani" detected! Opening Copilot...', '#7C3AED');
                playChime();
                this.openCopilotIfClosed();

                // Speak fixed greeting once, and transition to Command Listening ONLY after greeting completes
                this.speakGreeting(() => {{
                  this.waitForCopilotAndStartListening();
                }});
              }},

              waitForCopilotAndStartListening: function() {{
                const waitStart = Date.now();
                let retriedOpen = false;
                const synth = parentWin.speechSynthesis;
                const waitForTTS = () => {{
                  if (!this.active) return;
                  const panelOpen = Boolean(
                    parentDoc.querySelector('.st-key-floating-assistant-panel') ||
                    parentDoc.querySelector('.st-key-floating-assistant-dock')
                  );
                  const composerReady = Boolean(
                    parentDoc.querySelector('.st-key-copilot-composer-pill textarea') ||
                    parentDoc.querySelector('textarea')
                  );
                  if (!panelOpen && !retriedOpen && (Date.now() - waitStart > 600)) {{
                    retriedOpen = true;
                    this.openCopilotIfClosed();
                  }}
                  if (synth && synth.speaking && (Date.now() - waitStart < 5000)) {{
                    pSetTimeout(waitForTTS, 150);
                  }} else if (!composerReady && (Date.now() - waitStart < 3500)) {{
                    pSetTimeout(waitForTTS, 150);
                  }} else {{
                    delete parentWin.__vaaniVoicePendingState;
                    this.startCommandListening();
                  }}
                }};
                waitForTTS();
              }},

              // STATE 2b: SPEAK GREETING
              speakGreeting: function(onComplete) {{
                this.state = 'GREETING';
                this.updateUI('GREETING', '🗣️ "Hello! I\\\'m Vaani. How can I help you today?"', '#7C3AED');

                let completed = false;
                let speechStarted = false;
                const speakStartTime = Date.now();
                let pollInterval = null;
                let safetyTimer = null;

                const triggerNext = () => {{
                  if (!completed) {{
                    completed = true;
                    if (pollInterval) pClearInterval(pollInterval);
                    if (safetyTimer) pClearTimeout(safetyTimer);
                    parentWin.__vaaniActiveUtterance = null;
                    // Audio hardware buffer clear gap: 400ms delay so mic does NOT catch TTS echo
                    pSetTimeout(() => {{
                      if (typeof onComplete === 'function') onComplete();
                    }}, 400);
                  }}
                }};

                const synth = parentWin.speechSynthesis;
                const UtteranceClass = parentWin.SpeechSynthesisUtterance;

                if (synth && UtteranceClass) {{
                  try {{
                    if (synth.speaking || synth.pending) {{
                      synth.cancel();
                    }}
                    if (synth.paused && typeof synth.resume === 'function') {{
                      synth.resume();
                    }}
                    const utterance = new UtteranceClass(FIXED_GREETING);
                    utterance.rate = 1.0;
                    utterance.pitch = 1.0;
                    utterance.lang = 'en-IN';

                    const voices = (typeof synth.getVoices === 'function' ? synth.getVoices() : []) || [];
                    const preferredVoice = voices.find(v => v.lang && v.lang.toLowerCase() === 'en-in') ||
                                           voices.find(v => v.lang && v.lang.toLowerCase().startsWith('en'));
                    if (preferredVoice) {{
                      utterance.voice = preferredVoice;
                    }}

                    utterance.onstart = () => {{ speechStarted = true; }};
                    utterance.onend = triggerNext;
                    utterance.onerror = triggerNext;
                    parentWin.__vaaniActiveUtterance = utterance;
                    synth.speak(utterance);

                    // Poll in case browser drops utterance.onend; wait until speech has started or 1.2s elapsed
                    pollInterval = pSetInterval(() => {{
                      const elapsed = Date.now() - speakStartTime;
                      if (!completed && !synth.speaking && !synth.pending && (speechStarted || elapsed > 1200)) {{
                        triggerNext();
                      }}
                    }}, 180);

                    // Safety fallback timeout: 5500ms so full greeting never gets cut off early
                    safetyTimer = pSetTimeout(() => {{
                      triggerNext();
                    }}, 5500);

                  }} catch (e) {{
                    console.warn("Speech synthesis error:", e);
                    triggerNext();
                  }}
                }} else {{
                  triggerNext();
                }}
              }},

              // STATE 3: COMMAND LISTENING (Automatic capture without mic click)
              startCommandListening: function() {{
                if (!this.active) return;
                this.stopAllRecognitions();

                // Check if manual mic tray is currently open in Copilot
                const manualTray = parentDoc.querySelector('.st-key-copilot-voice-tray');
                if (manualTray) {{
                  this.updateUI('MANUAL_MIC', '🎙️ Manual recording active in Copilot', '#6366F1');
                  this.standbyRestartTimer = pSetTimeout(() => {{ this.startWakeStandby(false); }}, 1500);
                  return;
                }}

                if (!SR) {{
                  this.updateUI('UNSUPPORTED', 'Web Speech API not supported in this browser (use manual mic 🎙️)', '#94A3B8');
                  return;
                }}

                this.state = 'COMMAND_LISTENING';
                this.recognitionRunning = false;
                this.updateUI('COMMAND_LISTENING', '⏳ Starting microphone for your question...', '#97144D');

                try {{
                  const rec = new SR();
                  rec.continuous = true;
                  rec.interimResults = true;

                  const langHint = this.userLanguage || userLanguage;
                  if (langHint === 'kn-IN') rec.lang = 'kn-IN';
                  else if (langHint === 'hi-IN') rec.lang = 'hi-IN';
                  else rec.lang = 'en-IN';

                  let carriedOverTranscript = '';
                  let finalTranscript = '';
                  let lastRecognizedText = '';
                  let silenceTimer = null;
                  let timeoutTimer = null;
                  let hasSpeech = false;
                  let submitted = false;
                  const listeningStartTime = Date.now();

                  const finalizeAndSubmit = () => {{
                    if (submitted) return;
                    submitted = true;
                    this.recognitionRunning = false;
                    if (silenceTimer) pClearTimeout(silenceTimer);
                    if (timeoutTimer) pClearTimeout(timeoutTimer);
                    this.commandRecognition = null;
                    try {{
                      rec.onend = null;
                      rec.onresult = null;
                      rec.onerror = null;
                      rec.stop();
                    }} catch(e) {{}}

                    const cleanQuestion = (finalTranscript.trim() || lastRecognizedText.trim());
                    if (cleanQuestion.length > 1) {{
                      this.submitCommandToCopilot(cleanQuestion);
                    }} else {{
                      this.updateUI('STANDBY', 'No question heard. Resuming standby...', '#64748B');
                      pSetTimeout(() => {{ this.startWakeStandby(true); }}, 1000);
                    }}
                  }};

                  rec.onstart = () => {{
                    this.recognitionRunning = true;
                    if (this.state === 'COMMAND_LISTENING' && !hasSpeech) {{
                      this.updateUI('COMMAND_LISTENING', '👂 Listening for your question (speak now)...', '#97144D');
                    }}
                  }};

                  rec.onresult = (event) => {{
                    if (submitted) return;
                    let sessionFinal = '';
                    let liveInterim = '';
                    for (let i = 0; i < event.results.length; ++i) {{
                      const res = event.results[i];
                      const trans = (res && res[0] && res[0].transcript) ? res[0].transcript : '';
                      if (res.isFinal) {{
                        sessionFinal += ' ' + trans;
                      }} else {{
                        liveInterim += ' ' + trans;
                      }}
                    }}
                    const normalizedFinal = sessionFinal.replace(/\\s+/g, ' ').trim();
                    const alreadyCarried = carriedOverTranscript &&
                      normalizedFinal.toLocaleLowerCase().startsWith(carriedOverTranscript.toLocaleLowerCase());
                    finalTranscript = (alreadyCarried
                      ? normalizedFinal
                      : (carriedOverTranscript + ' ' + normalizedFinal)
                    ).replace(/\\s+/g, ' ').trim();
                    const currentText = (finalTranscript + ' ' + liveInterim).replace(/\\s+/g, ' ').trim();
                    if (currentText) {{
                      hasSpeech = true;
                      lastRecognizedText = currentText;
                      this.updateUI('COMMAND_LISTENING', `🎙️ "${{currentText}}"`, '#D97706');

                      // Live mirror into Copilot composer textarea without stealing focus
                      const pill = parentDoc.querySelector('.st-key-copilot-composer-pill');
                      const textarea = pill ? pill.querySelector('textarea') : parentDoc.querySelector('textarea');
                      if (textarea) {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(parentWin.HTMLTextAreaElement.prototype, 'value')?.set;
                        if (nativeSetter) {{
                          nativeSetter.call(textarea, currentText);
                        }} else {{
                          textarea.value = currentText;
                        }}
                        textarea.dispatchEvent(new parentWin.Event('input', {{ bubbles: true }}));
                      }}

                      // Reset silence timer: 1.8 seconds of silence after speech completes the turn
                      if (silenceTimer) pClearTimeout(silenceTimer);
                      silenceTimer = pSetTimeout(finalizeAndSubmit, 1800);
                    }}
                  }};

                  rec.onerror = (event) => {{
                    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {{
                      this.permissionBlocked = true;
                      this.recognitionRunning = false;
                      submitted = true;
                      if (silenceTimer) pClearTimeout(silenceTimer);
                      if (timeoutTimer) pClearTimeout(timeoutTimer);
                      this.updateUI('PERMISSION_BLOCKED', '⚠️ Microphone permission blocked. Allow mic in Edge address bar.', '#DC2626');
                    }} else if (event.error === 'audio-capture') {{
                      this.permissionBlocked = true;
                      this.recognitionRunning = false;
                      submitted = true;
                      this.updateUI('PERMISSION_BLOCKED', '⚠️ No microphone detected. Check audio input device.', '#DC2626');
                    }} else if (event.error !== 'no-speech' && event.error !== 'aborted') {{
                      console.warn("Command recognition error:", event.error);
                    }}
                  }};

                  rec.onend = () => {{
                    this.recognitionRunning = false;
                    if (submitted || this.state !== 'COMMAND_LISTENING') return;

                    const captured = (finalTranscript.trim() || lastRecognizedText.trim());
                    if (hasSpeech && captured.length > 1) {{
                      // Recognition can end on a brief pause or browser interruption.
                      // Preserve the transcript and resume until the silence timer
                      // confirms the user has finished speaking.
                      carriedOverTranscript = captured;
                      finalTranscript = captured;
                      pSetTimeout(() => {{
                        if (!submitted && this.state === 'COMMAND_LISTENING' && this.active && !this.permissionBlocked) {{
                          try {{ rec.start(); }} catch(e) {{}}
                        }}
                      }}, 250);
                    }} else if (!hasSpeech && (Date.now() - listeningStartTime < LISTENING_TIMEOUT_MS)) {{
                      // Browser speech recognition stopped prematurely on silence before timeout: restart listening
                      carriedOverTranscript = finalTranscript;
                      pSetTimeout(() => {{
                        if (!submitted && this.state === 'COMMAND_LISTENING' && this.active && !this.permissionBlocked) {{
                          try {{ rec.start(); }} catch(e) {{}}
                        }}
                      }}, 250);
                    }} else {{
                      // Timeout expired with no speech heard
                      finalizeAndSubmit();
                    }}
                  }};

                  this.commandRecognition = rec;
                  rec.start();

                  // Auto-stop after 8.5 seconds of listening if no speech
                  timeoutTimer = pSetTimeout(() => {{
                    if (!submitted && this.state === 'COMMAND_LISTENING' && !hasSpeech) {{
                      finalizeAndSubmit();
                    }}
                  }}, LISTENING_TIMEOUT_MS);

                }} catch(e) {{
                  console.error("Failed to start command listening:", e);
                  this.startWakeStandby(true);
                }}
              }},

              // STATE 4: SUBMIT COMMAND TO COPILOT (Single clean commit without duplicate rerun)
              submitCommandToCopilot: function(questionText) {{
                if (this.state === 'SUBMITTING' || this.state === 'PROCESSING' || this.state === 'ASSISTANT_RESPONDING') return;
                this.updateUI('SUBMITTING', `✨ Asking Vaani AI: "${{questionText}}"`, '#4F46E5');

                const initialMsgCount = parentDoc.querySelectorAll('[data-testid="stChatMessage"]').length;
                const submitStart = Date.now();

                const tryCommitToComposer = () => {{
                  if (!this.active) return;
                  try {{
                    const pill = parentDoc.querySelector('.st-key-copilot-composer-pill');
                    const textarea = pill ? pill.querySelector('textarea') : parentDoc.querySelector('textarea');
                    const sendBtn = parentDoc.querySelector('.st-key-copilot_send_button button') ||
                                    parentDoc.querySelector('button[key="copilot_send_button"]');

                    if (!textarea) {{
                      if (Date.now() - submitStart < 3000) {{
                        this.openCopilotIfClosed();
                        pSetTimeout(tryCommitToComposer, 150);
                      }} else {{
                        this.startWakeStandby(true);
                      }}
                      return;
                    }}

                    // Set value via React native descriptor setter so Streamlit TextArea registers dirty=true
                    const nativeSetter = Object.getOwnPropertyDescriptor(parentWin.HTMLTextAreaElement.prototype, 'value')?.set;
                    if (nativeSetter) {{
                      nativeSetter.call(textarea, questionText);
                    }} else {{
                      textarea.value = questionText;
                    }}
                    textarea.dispatchEvent(new parentWin.Event('input', {{ bubbles: true }}));
                    textarea.dispatchEvent(new parentWin.Event('change', {{ bubbles: true }}));

                    // Wait 150ms for React to re-render TextArea with updated value and dirty=true,
                    // then commit ONCE (preventing duplicate BackMsg rerun cancellation in Streamlit)
                    pSetTimeout(() => {{
                      try {{
                        if (parentDoc.activeElement === textarea) {{
                          // Focused textarea: blur() commits via onBlur -> widgetMgr.setStringValue once
                          textarea.blur();
                        }} else {{
                          // Unfocused textarea: Ctrl+Enter commits via onKeyDown -> widgetMgr.setStringValue once
                          const ctrlEnter = new parentWin.KeyboardEvent('keydown', {{
                            key: 'Enter',
                            code: 'Enter',
                            keyCode: 13,
                            which: 13,
                            ctrlKey: true,
                            metaKey: true,
                            bubbles: true,
                            cancelable: true
                          }});
                          textarea.dispatchEvent(ctrlEnter);
                          textarea.blur();
                        }}
                      }} catch (innerErr) {{
                        if (sendBtn) sendBtn.click();
                      }}

                      // Single fallback click after 650ms ONLY if rerun has not started yet
                      pSetTimeout(() => {{
                        try {{
                          const currentMsgs = parentDoc.querySelectorAll('[data-testid="stChatMessage"]').length;
                          const hasSpinner = Boolean(parentDoc.querySelector('[data-testid="stSpinner"]'));
                          const currentBtn = parentDoc.querySelector('.st-key-copilot_send_button button') ||
                                             parentDoc.querySelector('button[key="copilot_send_button"]');
                          if (!hasSpinner && currentMsgs <= initialMsgCount && currentBtn) {{
                            currentBtn.click();
                          }}
                        }} catch (fbErr) {{}}
                      }}, 650);

                      this.onAssistantResponding(initialMsgCount);
                    }}, 150);
                  }} catch(err) {{
                    console.warn("Could not dispatch text to Copilot composer:", err);
                    pSetTimeout(() => {{ this.startWakeStandby(true); }}, 2500);
                  }}
                }};

                tryCommitToComposer();
              }},

              // STATE 5: ASSISTANT RESPONDING -> SPEAK TTS -> RETURN TO STANDBY
              onAssistantResponding: function(initialMsgCount) {{
                this.state = 'ASSISTANT_RESPONDING';
                this.updateUI('ASSISTANT_RESPONDING', '💬 Vaani AI is generating response...', '#2563EB');

                const baselineCount = typeof initialMsgCount === 'number'
                  ? initialMsgCount
                  : parentDoc.querySelectorAll('[data-testid="stChatMessage"]').length;
                const startWait = Date.now();

                if (this.responsePollTimer) pClearInterval(this.responsePollTimer);

                this.responsePollTimer = pSetInterval(() => {{
                  const elapsed = Date.now() - startWait;
                  const msgs = parentDoc.querySelectorAll('[data-testid="stChatMessage"]');
                  const spinner = parentDoc.querySelector('[data-testid="stSpinner"]');

                  // Check if new assistant response has been rendered (supporting Streamlit 1.66+ avatar testids)
                  if (msgs.length > baselineCount && !spinner) {{
                    const lastMsg = msgs[msgs.length - 1];
                    const isUserMsg = Boolean(
                      lastMsg.querySelector('[data-testid="stChatMessageAvatarUser"]') ||
                      lastMsg.querySelector('[data-testid="chatAvatarIcon-user"]') ||
                      (lastMsg.getAttribute('aria-label') || '').toLowerCase().includes('user')
                    );
                    if (!isUserMsg) {{
                      pClearInterval(this.responsePollTimer);
                      this.responsePollTimer = null;

                      // Wait 350ms for <audio> widget or markdown content inside lastMsg to finish mounting
                      pSetTimeout(() => {{
                        this.handleAssistantResponsePlayback(lastMsg);
                      }}, 350);
                      return;
                    }}
                  }}

                  // Timeout after 35s if backend took too long or errored
                  if (elapsed > 35000) {{
                    pClearInterval(this.responsePollTimer);
                    this.responsePollTimer = null;
                    this.startWakeStandby(true);
                  }}
                }}, 350);
              }},

              handleAssistantResponsePlayback: function(lastMsgEl) {{
                if (!this.active) return;
                let finished = false;
                let audioPoll = null;
                let maxTimer = null;

                const onPlaybackComplete = () => {{
                  if (finished) return;
                  finished = true;
                  if (audioPoll) pClearInterval(audioPoll);
                  if (maxTimer) pClearTimeout(maxTimer);
                  parentWin.__vaaniActiveUtterance = null;
                  pSetTimeout(() => {{
                    this.startWakeStandby(true);
                  }}, 450);
                }};

                const audioEl = lastMsgEl ? lastMsgEl.querySelector('audio') : null;
                if (audioEl) {{
                  this.updateUI('ASSISTANT_RESPONDING', '🔊 Vaani AI is speaking response...', '#7C3AED');
                  let playbackStarted = !audioEl.paused || audioEl.currentTime > 0;
                  const audioWaitStart = Date.now();

                  audioEl.addEventListener('playing', () => {{ playbackStarted = true; }});
                  audioEl.addEventListener('timeupdate', () => {{
                    if (audioEl.currentTime > 0) playbackStarted = true;
                  }});
                  audioEl.addEventListener('ended', onPlaybackComplete, {{ once: true }});
                  audioEl.addEventListener('error', () => {{
                    if (audioPoll) pClearInterval(audioPoll);
                    this.speakResponseFallback(lastMsgEl, onPlaybackComplete);
                  }}, {{ once: true }});

                  if (audioEl.paused && !audioEl.ended) {{
                    try {{
                      const playPromise = audioEl.play();
                      if (playPromise && typeof playPromise.catch === 'function') {{
                        playPromise.catch(() => {{
                          if (audioPoll) pClearInterval(audioPoll);
                          this.speakResponseFallback(lastMsgEl, onPlaybackComplete);
                        }});
                      }}
                    }} catch (playErr) {{
                      this.speakResponseFallback(lastMsgEl, onPlaybackComplete);
                      return;
                    }}
                  }}

                  audioPoll = pSetInterval(() => {{
                    if (audioEl.ended || (playbackStarted && audioEl.paused && audioEl.currentTime > 0)) {{
                      onPlaybackComplete();
                    }} else if (!playbackStarted && (Date.now() - audioWaitStart > 2800)) {{
                      pClearInterval(audioPoll);
                      audioPoll = null;
                      this.speakResponseFallback(lastMsgEl, onPlaybackComplete);
                    }}
                  }}, 350);

                  maxTimer = pSetTimeout(onPlaybackComplete, 45000);
                }} else {{
                  this.speakResponseFallback(lastMsgEl, onPlaybackComplete);
                }}
              }},

              speakResponseFallback: function(lastMsgEl, onDone) {{
                const synth = parentWin.speechSynthesis;
                const UtteranceClass = parentWin.SpeechSynthesisUtterance;
                if (!synth || !UtteranceClass || !lastMsgEl) {{
                  onDone();
                  return;
                }}
                try {{
                  const clone = lastMsgEl.cloneNode(true);
                  clone.querySelectorAll('button, small, [data-testid="stCaptionContainer"], audio').forEach(el => el.remove());
                  const rawText = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
                  if (!rawText) {{
                    onDone();
                    return;
                  }}
                  this.updateUI('ASSISTANT_RESPONDING', '🔊 Vaani AI is speaking response...', '#7C3AED');
                  if (synth.speaking || synth.pending) {{
                    synth.cancel();
                  }}
                  const u = new UtteranceClass(rawText.slice(0, 650));
                  u.rate = 1.0;
                  const langHint = this.userLanguage || userLanguage;
                  u.lang = (langHint === 'kn-IN' || langHint === 'hi-IN') ? langHint : 'en-IN';
                  let speechBegan = false;
                  u.onstart = () => {{ speechBegan = true; }};
                  u.onend = onDone;
                  u.onerror = onDone;
                  parentWin.__vaaniActiveUtterance = u;
                  synth.speak(u);

                  const startT = Date.now();
                  const fallbackPoll = pSetInterval(() => {{
                    if (!synth.speaking && !synth.pending && (speechBegan || Date.now() - startT > 1200)) {{
                      pClearInterval(fallbackPoll);
                      onDone();
                    }}
                  }}, 300);
                  pSetTimeout(() => {{
                    pClearInterval(fallbackPoll);
                    onDone();
                  }}, 35000);
                }} catch (e) {{
                  onDone();
                }}
              }},

              stopAllRecognitions: function() {{
                this.recognitionRunning = false;
                if (this.standbyRestartTimer) {{
                  pClearTimeout(this.standbyRestartTimer);
                  this.standbyRestartTimer = null;
                }}
                if (this.responsePollTimer) {{
                  pClearInterval(this.responsePollTimer);
                  this.responsePollTimer = null;
                }}
                if (this.wakeRecognition) {{
                  try {{
                    this.wakeRecognition.onstart = null;
                    this.wakeRecognition.onresult = null;
                    this.wakeRecognition.onend = null;
                    this.wakeRecognition.onerror = null;
                    this.wakeRecognition.abort();
                  }} catch(e) {{}}
                  this.wakeRecognition = null;
                }}
                if (this.commandRecognition) {{
                  try {{
                    this.commandRecognition.onstart = null;
                    this.commandRecognition.onresult = null;
                    this.commandRecognition.onend = null;
                    this.commandRecognition.onerror = null;
                    this.commandRecognition.abort();
                  }} catch(e) {{}}
                  this.commandRecognition = null;
                }}
              }},

              stopAll: function() {{
                this.active = false;
                this.stopAllRecognitions();
                const synth = parentWin.speechSynthesis;
                if (synth) {{
                  try {{ synth.cancel(); }} catch(e) {{}}
                }}
                this.updateUI('DISABLED', 'Voice engine stopped', '#94A3B8');
              }}
            }};

            parentWin.__vaaniVoiceManager = manager;
          }}

          // Attach one-time / transparent user-gesture recovery on parentDoc so Edge allows mic & TTS after first click
          if (!parentWin.__vaaniGestureHookBound) {{
            parentWin.__vaaniGestureHookBound = true;
            const onUserGesture = () => {{
              try {{
                const synth = parentWin.speechSynthesis;
                const UtteranceClass = parentWin.SpeechSynthesisUtterance;
                if (synth && UtteranceClass && !parentWin.__vaaniSynthUnlocked && !synth.speaking && !synth.pending) {{
                  parentWin.__vaaniSynthUnlocked = true;
                  const silentU = new UtteranceClass('');
                  silentU.volume = 0;
                  synth.speak(silentU);
                }}
                const mgr = parentWin.__vaaniVoiceManager;
                if (mgr && mgr.active && mgr.state === 'STANDBY' && !mgr.recognitionRunning && !mgr.permissionBlocked) {{
                  mgr.consecutiveQuickEnds = 0;
                  mgr.startWakeStandby(true);
                }}
              }} catch (e) {{}}
            }};
            parentDoc.addEventListener('click', onUserGesture, {{ passive: true }});
            parentDoc.addEventListener('keydown', onUserGesture, {{ passive: true }});
          }}

          manager.configure(cfg);
        }}

        // Execute voice engine in window.parent's execution realm so Streamlit iframe reloads never abort sessions
        try {{
          if (parentWin !== window && typeof parentWin.eval === 'function') {{
            parentWin.__vaaniLatestConfig = runtimeConfig;
            parentWin.eval('(' + bootstrapParentVoiceEngine.toString() + ')(window, document, window.__vaaniLatestConfig);');
          }} else {{
            bootstrapParentVoiceEngine(parentWin, parentDoc, runtimeConfig);
          }}
        }} catch (evalErr) {{
          bootstrapParentVoiceEngine(parentWin, parentDoc, runtimeConfig);
        }}
      }})();
    </script>
    """
    components.html(html_code, height=42)
