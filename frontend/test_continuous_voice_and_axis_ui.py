"""Unit tests for Axis Bank-inspired banking UI refinements and continuous Hey Vaani voice interaction."""

import unittest
from pathlib import Path

from frontend.components.fintech_ui import (
    format_inr,
    render_balance_hero_html,
    render_transaction_card_html,
)
from frontend.components.wake_word import render_wake_word_listener


class TestAxisBankingUI(unittest.TestCase):
    def setUp(self):
        self.css_path = Path(__file__).parent / "assets" / "styles.css"
        self.assertTrue(self.css_path.exists(), "styles.css must exist")
        self.css = self.css_path.read_text(encoding="utf-8")

    def test_axis_bank_inspired_tokens(self):
        """Verify clean corporate banking palette: Axis burgundy, deep navy, clean white cards."""
        # Axis burgundy accent and deep navy typography
        self.assertIn("#97144D", self.css)
        self.assertIn("#0F172A", self.css)
        self.assertIn("--bg-card: #FFFFFF", self.css)
        self.assertIn("--border-light: #E2E8F0", self.css)

    def test_balance_hero_responsive_and_clean_design(self):
        """Verify balance hero uses responsive typography and executive styling."""
        html = render_balance_hero_html(
            balance=124500.50,
            account_number="VS1000001234",
            customer_name="Arjun Sharma",
            is_visible=True,
            monthly_inflow=85000.0,
        )
        self.assertIn("VAANISHIELD SECURE BANKING", html)
        self.assertIn("₹1,24,500.50", html)
        self.assertIn("Arjun Sharma", html)
        self.assertIn("RuPay", html)

        # CSS checks for hero balance text wrapping and font scaling
        self.assertIn(".hero-balance-amount", self.css)
        self.assertIn("clamp(1.75rem", self.css)
        self.assertIn("word-break: break-word", self.css)

    def test_responsive_workspace_overflow_and_metrics_protection(self):
        """Verify responsive workspace prevents clipping and squeezes on open copilot."""
        self.assertIn(".st-key-banking-workspace-copilot-open", self.css)
        self.assertIn("calc(100% - var(--copilot-dock-reserve))", self.css)

        # Metric value & label wrapping
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stMetricValue\"]", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stMetricLabel\"]", self.css)

        # Cards and columns reflow
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stHorizontalBlock\"]", self.css)
        self.assertIn("flex-wrap: wrap !important", self.css)


class TestContinuousVoiceInteraction(unittest.TestCase):
    def setUp(self):
        self.wake_path = Path(__file__).parent / "components" / "wake_word.py"
        self.assertTrue(self.wake_path.exists(), "wake_word.py must exist")
        self.code = self.wake_path.read_text(encoding="utf-8")

    def test_continuous_siri_voice_state_machine_present(self):
        """Verify full 5-state voice interaction flow in browser Web Speech engine."""
        # State 1: Standby
        self.assertIn("STANDBY", self.code)
        self.assertIn("startWakeStandby", self.code)

        # State 2: Wake Detected & Fixed English Greeting
        self.assertIn("onWakePhraseDetected", self.code)
        self.assertIn("speakGreeting", self.code)
        self.assertIn("Hello! I'm Vaani. How can I help you today?", self.code)

        # State 3: Command Listening (automatic capture without mic click)
        self.assertIn("startCommandListening", self.code)
        self.assertIn("COMMAND_LISTENING", self.code)

        # State 4: Submit command to Copilot
        self.assertIn("submitCommandToCopilot", self.code)
        self.assertIn("st-key-copilot-composer-pill", self.code)

        # State 5: Return to Standby for repeated hands-free loop
        self.assertIn("onAssistantResponding", self.code)

    def test_manual_mic_protection_and_singleton(self):
        """Verify manual microphone input is protected and singleton manager is used."""
        # Detect manual voice tray to avoid microphone conflict
        self.assertIn("st-key-copilot-voice-tray", self.code)
        self.assertIn("parentWin.__vaaniVoiceManager", self.code)
        self.assertIn("stopAllRecognitions", self.code)
        self.assertIn("btn_manual_wake_trigger", self.code)

    def test_javascript_template_interpolation_preservation(self):
        """Regression test: verify JavaScript ${currentText} and ${questionText} are preserved and not evaluated by Python."""
        from unittest.mock import MagicMock, patch
        from frontend.components import wake_word

        mock_st = MagicMock()
        mock_cols = (MagicMock(), MagicMock())
        mock_st.columns.return_value = mock_cols
        state = {
            "wake_word_enabled": True,
            "assistant_open": False,
            "customer": {"display_name": "Arjun Sharma"},
            "language_hint": "en-IN",
        }

        captured_html = []
        with patch.object(wake_word.components, "html", side_effect=lambda html, **kw: captured_html.append(html)):
            # Must execute without raising NameError: name 'currentText' is not defined
            wake_word.render_wake_word_listener(mock_st, state)

        self.assertEqual(len(captured_html), 1, "Expected components.html to be called once")
        rendered = captured_html[0]

        # Verify JavaScript template string interpolation expressions are preserved verbatim in browser output
        self.assertIn("${currentText}", rendered)
        self.assertIn("${questionText}", rendered)
        self.assertIn('`🎙️ "${currentText}"`', rendered)
        self.assertIn('`✨ Asking Vaani AI: "${questionText}"`', rendered)

    def test_voice_command_listening_transition_and_rerun_resilience(self):
        """Verify reliable transition to command listening, silence timer, and cross-rerun session handling."""
        self.assertIn("__vaaniVoicePendingState", self.code)
        self.assertIn("START_COMMAND_LISTENING", self.code)
        self.assertIn("silenceTimer", self.code)
        self.assertIn("finalizeAndSubmit", self.code)
        self.assertIn("__vaaniActiveUtterance", self.code)
        self.assertIn("nativeSetter", self.code)
        self.assertIn("HTMLTextAreaElement.prototype", self.code)
        self.assertIn("bootstrapParentVoiceEngine", self.code)
        self.assertIn("stChatMessageAvatarUser", self.code)

    def test_full_siri_voice_lifecycle_in_js_engine(self):
        """Execute rendered JS state machine in Node.js to verify all 11 hands-free lifecycle requirements."""
        import json
        import re
        import shutil
        import subprocess
        from unittest.mock import MagicMock, patch
        from frontend.components import wake_word

        if not shutil.which("node"):
            self.skipTest("Node.js not available")

        mock_st = MagicMock()
        mock_st.columns.return_value = (MagicMock(), MagicMock())
        state = {
            "wake_word_enabled": True,
            "assistant_open": False,
            "customer": {"display_name": "Arjun Sharma"},
            "language_hint": "en-IN",
        }

        captured_html = []
        with patch.object(wake_word.components, "html", side_effect=lambda html, **kw: captured_html.append(html)):
            wake_word.render_wake_word_listener(mock_st, state)

        rendered = captured_html[0]
        match = re.search(r"<script>([\s\S]*?)</script>", rendered)
        self.assertIsNotNone(match, "Expected <script> block in wake_word HTML")
        js_script = match.group(1)

        node_harness = r"""
const assert = require('assert');

// Simulated fast-forward timer queue for deterministic lifecycle execution
let nowMs = 10000;
let timerIdSeq = 1;
const timers = new Map();
const intervals = new Map();

Date.now = () => nowMs;

function customSetTimeout(fn, delay) {
  const id = timerIdSeq++;
  timers.set(id, { fn, due: nowMs + (delay || 0) });
  return id;
}
function customClearTimeout(id) {
  timers.delete(id);
}
function customSetInterval(fn, delay) {
  const id = timerIdSeq++;
  intervals.set(id, { fn, delay: Math.max(10, delay || 10), due: nowMs + (delay || 10) });
  return id;
}
function customClearInterval(id) {
  intervals.delete(id);
}

function advanceTime(ms) {
  const target = nowMs + ms;
  while (nowMs < target) {
    let nextDue = target;
    for (const t of timers.values()) {
      if (t.due < nextDue) nextDue = t.due;
    }
    for (const iv of intervals.values()) {
      if (iv.due < nextDue) nextDue = iv.due;
    }
    nowMs = nextDue;
    for (const [id, t] of Array.from(timers.entries())) {
      if (t.due <= nowMs) {
        timers.delete(id);
        t.fn();
      }
    }
    for (const [id, iv] of Array.from(intervals.entries())) {
      if (iv.due <= nowMs && intervals.has(id)) {
        iv.due = nowMs + iv.delay;
        iv.fn();
      }
    }
  }
}

// Mock DOM elements in parentDoc and child iframe
const elementsById = {
  'vaani-voice-msg': { textContent: '', style: {}, dataset: {}, addEventListener: () => {} },
  'vaani-voice-pulse': { textContent: '', style: {}, dataset: {}, addEventListener: () => {} },
  'vaani-voice-badge': { textContent: '', style: {}, dataset: {}, addEventListener: () => {} },
  'vaani-voice-container': { textContent: '', style: {}, dataset: {}, addEventListener: () => {} },
};

const statusTextEl = { innerHTML: '⏳ Starting Voice Standby...' };
const statusDotEl = { style: { background: '#F59E0B' } };
const statusBarEl = {
  dataset: {},
  addEventListener: () => {},
  querySelector: (sel) => {
    if (sel === '.wake-word-text') return statusTextEl;
    if (sel === '.wake-word-dot') return statusDotEl;
    return null;
  }
};

let copilotOpen = false;
let copilotToggleClicks = 0;
let manualVoiceTrayOpen = false;
let submittedQuestions = [];
let chatMessages = [];
let activeRecognitions = [];
let allRecognitions = [];
let spokenGreetings = [];

const textareaEl = {
  value: '',
  dataset: {},
  focus: function() { parentDoc.activeElement = this; },
  blur: function() { if (parentDoc.activeElement === this) parentDoc.activeElement = null; },
  addEventListener: () => {},
  dispatchEvent: function(evt) {
    if (evt && evt.type === 'keydown' && evt.key === 'Enter' && evt.ctrlKey) {
      if (this.value.trim()) {
        submittedQuestions.push(this.value.trim());
      }
    }
    return true;
  }
};

function HTMLTextAreaElement() {}
Object.defineProperty(HTMLTextAreaElement.prototype, 'value', {
  set: function(v) { this._val = v; textareaEl.value = v; },
  get: function() { return this._val || ''; }
});

const toggleBtn = {
  textContent: '✨ Vaani Copilot',
  click: () => {
    copilotToggleClicks++;
    copilotOpen = true;
  }
};

const sendBtn = {
  click: () => {
    if (textareaEl.value.trim()) {
      submittedQuestions.push(textareaEl.value.trim());
    }
  }
};

const childDoc = {
  getElementById: (id) => elementsById[id] || null
};

const parentDoc = {
  activeElement: null,
  addEventListener: () => {},
  getElementById: (id) => elementsById[id] || null,
  querySelectorAll: (sel) => {
    if (sel === 'iframe') return [{ contentDocument: childDoc }];
    if (sel === '[data-testid="stChatMessage"]') return chatMessages;
    if (sel === 'button') return [toggleBtn];
    return [];
  },
  querySelector: (sel) => {
    if (sel === '.wake-word-status-bar') return statusBarEl;
    if (sel === '.st-key-floating-assistant-panel') return copilotOpen ? {} : null;
    if (sel === '.st-key-header-copilot-toggle button') return toggleBtn;
    if (sel === '.st-key-copilot-composer-pill') return copilotOpen ? { querySelector: (s) => s === 'textarea' ? textareaEl : null } : null;
    if (sel === '.st-key-copilot-composer-pill textarea' || sel === 'textarea') return copilotOpen ? textareaEl : null;
    if (sel === '.st-key-copilot_send_button button') return copilotOpen ? sendBtn : null;
    if (sel === '.st-key-copilot-voice-tray') return manualVoiceTrayOpen ? {} : null;
    if (sel === '[data-testid="stSpinner"]') return null;
    return null;
  }
};

class MockSpeechRecognition {
  constructor() {
    this.continuous = false;
    this.interimResults = false;
    this.lang = 'en-IN';
    this.running = false;
    this.aborted = false;
    allRecognitions.push(this);
  }
  start() {
    this.running = true;
    activeRecognitions.push(this);
    if (this.onstart) this.onstart();
  }
  stop() {
    this.running = false;
    activeRecognitions = activeRecognitions.filter(r => r !== this);
    if (this.onend) this.onend();
  }
  abort() {
    this.running = false;
    this.aborted = true;
    activeRecognitions = activeRecognitions.filter(r => r !== this);
  }
}

class MockUtterance {
  constructor(text) {
    this.text = text;
    this.rate = 1.0;
    this.pitch = 1.0;
    this.lang = 'en-IN';
  }
}

const synth = {
  speaking: false,
  pending: false,
  paused: false,
  getVoices: () => [{ lang: 'en-IN', name: 'Microsoft Neerja' }],
  cancel: function() { this.speaking = false; },
  speak: function(u) {
    spokenGreetings.push(u.text);
    this.speaking = true;
    if (u.onstart) u.onstart();
    customSetTimeout(() => {
      this.speaking = false;
      if (u.onend) u.onend();
    }, 300);
  }
};

const parentWin = {
  document: parentDoc,
  setTimeout: customSetTimeout,
  clearTimeout: customClearTimeout,
  setInterval: customSetInterval,
  clearInterval: customClearInterval,
  SpeechRecognition: MockSpeechRecognition,
  webkitSpeechRecognition: MockSpeechRecognition,
  speechSynthesis: synth,
  SpeechSynthesisUtterance: MockUtterance,
  HTMLTextAreaElement: HTMLTextAreaElement,
  Event: function(type, opts) { this.type = type; Object.assign(this, opts); },
  KeyboardEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  FocusEvent: function(type, opts) { this.type = type; Object.assign(this, opts); },
  eval: function(code) { return eval(code); }
};
parentWin.parent = parentWin;

// Simulate child iframe window distinct from parentWin (parentWin !== window)
const childWin = {
  parent: parentWin,
  document: childDoc
};
global.window = childWin;
global.document = childDoc;

// Execute the actual wake_word.py script inside child iframe context (which delegates to parentWin.eval)
const scriptCode = __SCRIPT_CODE__;
(function(window, document) { eval(scriptCode); })(childWin, childDoc);

const mgr = parentWin.__vaaniVoiceManager;
assert(mgr, 'VoiceManager singleton must be created on parentWin');

// 1. Verify Standby listening actually starts and updates status bar truthfully after onstart
assert.strictEqual(mgr.state, 'STANDBY');
assert.strictEqual(mgr.recognitionRunning, true);
assert.strictEqual(activeRecognitions.length, 1, 'Exactly one recognition session may exist');
assert(statusTextEl.innerHTML.includes('Continuous Voice Active'), 'Status bar must show Continuous Voice Active once onstart fires');

// Verify rerun survival across tab switch (calling script again does not abort running standby)
const firstStandbyRec = mgr.wakeRecognition;
eval(scriptCode);
assert.strictEqual(mgr.wakeRecognition, firstStandbyRec, 'Standby recognition must survive Streamlit rerun without aborting');
assert.strictEqual(activeRecognitions.length, 1);

// 2. Simulate saying "Hey Vaani" while Copilot is closed
firstStandbyRec.onresult({
  resultIndex: 0,
  results: [{ isFinal: true, 0: { transcript: 'Hey Vaani' } }]
});

// 3. Verify Copilot opens automatically and standby recognition stopped before greeting
assert.strictEqual(copilotOpen, true, 'Copilot must open automatically when closed');
assert.strictEqual(copilotToggleClicks, 1);
assert.strictEqual(firstStandbyRec.running, false, 'Standby recognition must stop cleanly before greeting');

// 4. Verify fixed greeting plays exactly once
assert.strictEqual(mgr.state, 'GREETING');
assert.strictEqual(spokenGreetings.length, 1);
assert.strictEqual(spokenGreetings[0], "Hello! I'm Vaani. How can I help you today?");

// 5. Advance time for greeting TTS (300ms) + 400ms echo buffer -> Command Listening starts
advanceTime(850);
assert.strictEqual(mgr.state, 'COMMAND_LISTENING', 'Must transition to COMMAND_LISTENING after greeting finishes');
assert.strictEqual(activeRecognitions.length, 1, 'Command recognition must start automatically without mic click');
const cmdRec1 = mgr.commandRecognition;
assert(cmdRec1, 'commandRecognition instance must be active');

// 6. Simulate spoken question with interim and final results
cmdRec1.onresult({
  resultIndex: 0,
  results: [{ isFinal: false, 0: { transcript: 'What is my' } }]
});
assert.strictEqual(textareaEl.value, 'What is my', 'Interim transcript must mirror live into Copilot composer');

cmdRec1.onresult({
  resultIndex: 0,
  results: [
    { isFinal: true, 0: { transcript: 'What is my' } },
    { isFinal: true, 0: { transcript: 'account balance?' } }
  ]
});
assert.strictEqual(textareaEl.value, 'What is my account balance?');

// A browser recognition interruption after speech must not submit early.
cmdRec1.running = false;
activeRecognitions = activeRecognitions.filter(r => r !== cmdRec1);
cmdRec1.onend();
assert.strictEqual(submittedQuestions.length, 0, 'Recognition end alone must not submit an incomplete turn');
advanceTime(300);
assert.strictEqual(cmdRec1.running, true, 'Command recognition should resume after an interruption');

// 7. Advance 1800ms silence timer + 150ms React commit -> Query submitted EXACTLY ONCE
advanceTime(2000);
assert.strictEqual(submittedQuestions.length, 1, 'Spoken query must be submitted exactly once');
assert.strictEqual(submittedQuestions[0], 'What is my account balance?');
assert.strictEqual(mgr.state, 'ASSISTANT_RESPONDING');

// First Streamlit renders the user message (stChatMessageAvatarUser) — manager must NOT mistake it for assistant!
chatMessages.push({
  getAttribute: () => 'user',
  querySelector: (s) => s.includes('stChatMessageAvatarUser') ? {} : null
});
advanceTime(400);
assert.strictEqual(mgr.state, 'ASSISTANT_RESPONDING', 'Must keep waiting while only user message is rendered');

// 8. Assistant message renders with <audio> element (Gnani TTS)
let audioEndedCb = null;
const mockAudioEl = {
  paused: false,
  ended: false,
  currentTime: 0.5,
  addEventListener: (evt, cb) => {
    if (evt === 'ended') audioEndedCb = cb;
  },
  play: () => Promise.resolve()
};
chatMessages.push({
  getAttribute: () => 'assistant',
  querySelector: (s) => {
    if (s.includes('stChatMessageAvatarAssistant')) return {};
    if (s === 'audio') return mockAudioEl;
    return null;
  }
});

advanceTime(800);
assert(audioEndedCb, 'Must attach ended listener to assistant <audio> playback');

// 9. Assistant audio finishes -> returns to STANDBY automatically
mockAudioEl.ended = true;
audioEndedCb();
advanceTime(600);
assert.strictEqual(mgr.state, 'STANDBY', 'Must return to STANDBY after assistant audio finishes');
assert.strictEqual(mgr.recognitionRunning, true);
assert.strictEqual(activeRecognitions.length, 1);

// 10. Second "Hey Vaani" turn without page refresh!
advanceTime(2600); // pass cooldown
const secondStandbyRec = mgr.wakeRecognition;
secondStandbyRec.onresult({
  resultIndex: 0,
  results: [{ isFinal: true, 0: { transcript: 'Hey Vaani' } }]
});
assert.strictEqual(spokenGreetings.length, 2, 'Second Hey Vaani must play greeting');
advanceTime(850);
assert.strictEqual(mgr.state, 'COMMAND_LISTENING', 'Second Hey Vaani must enter COMMAND_LISTENING');
mgr.commandRecognition.onresult({
  resultIndex: 0,
  results: [{ isFinal: true, 0: { transcript: 'Show recent transactions' } }]
});
advanceTime(2000);
assert.strictEqual(submittedQuestions.length, 2, 'Second spoken command must be submitted');
assert.strictEqual(submittedQuestions[1], 'Show recent transactions');

// Complete second assistant turn
const mockAudioEl2 = {
  paused: false,
  ended: true,
  currentTime: 1.0,
  addEventListener: () => {},
  play: () => Promise.resolve()
};
chatMessages.push({
  getAttribute: () => 'assistant',
  querySelector: (s) => s === 'audio' ? mockAudioEl2 : (s.includes('stChatMessageAvatarAssistant') ? {} : null)
});
advanceTime(1500);
assert.strictEqual(mgr.state, 'STANDBY', 'Must return to STANDBY after second turn');

// 11. Manual microphone tray opens -> pauses wake-word standby, and resumes when closed
manualVoiceTrayOpen = true;
advanceTime(500);
assert.strictEqual(mgr.state, 'MANUAL_MIC', 'Opening manual mic tray must pause wake-word standby');
assert.strictEqual(activeRecognitions.length, 0, 'No Web Speech recognition may run while manual mic is open');

manualVoiceTrayOpen = false;
advanceTime(500);
assert.strictEqual(mgr.state, 'STANDBY', 'Closing manual mic tray must resume wake-word standby');
assert.strictEqual(activeRecognitions.length, 1);

// 12. Disabling wake-word stops everything cleanly
mgr.stopAll();
assert.strictEqual(mgr.state, 'DISABLED');
assert.strictEqual(activeRecognitions.length, 0);

console.log('ALL_11_VOICE_LIFECYCLE_CHECKS_PASSED');
"""
        node_harness = node_harness.replace("__SCRIPT_CODE__", json.dumps(js_script))
        res = subprocess.run(
            ["node", "-"], input=node_harness, capture_output=True,
            text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Node.js voice lifecycle verification failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}",
        )
        self.assertIn("ALL_11_VOICE_LIFECYCLE_CHECKS_PASSED", res.stdout)


class TestTargetedFrontendRefinements(unittest.TestCase):
    def setUp(self):
        self.css_path = Path(__file__).parent / "assets" / "styles.css"
        self.assertTrue(self.css_path.exists())
        self.css = self.css_path.read_text(encoding="utf-8")

    def test_issue_1_content_overflow_and_metric_wrapping_all_tabs(self):
        """Verify metric cards, card layouts, and values wrap cleanly without clipping across all tabs."""
        # Cards page virtual card & limits reflow
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stHorizontalBlock\"]:has(.acct-status-pill)", self.css)
        self.assertIn("flex: 1 1 100% !important", self.css)

        # Overview page stacking
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stHorizontalBlock\"]:has(.acct-summary-item)", self.css)

        # Metric value & label wrapping
        self.assertIn(".st-key-banking-workspace-copilot-open div[data-testid=\"stMetricValue\"]", self.css)
        self.assertIn("white-space: normal !important", self.css)
        self.assertIn("word-break: break-word !important", self.css)
        self.assertIn("overflow-wrap: anywhere !important", self.css)

        # Global metric value override also permits wrapping (no white-space: nowrap)
        self.assertIn("div[data-testid=\"stMetricValue\"]", self.css)

        # Loans detail grid and header reflow
        self.assertIn(".st-key-banking-workspace-copilot-open .loan-card-header", self.css)
        self.assertIn(".st-key-banking-workspace-copilot-open .loan-details-grid", self.css)
        self.assertIn("repeat(2, minmax(130px, 1fr))", self.css)

        # Transactions filters reflow
        self.assertIn(".st-key-banking-workspace-copilot-open form div[data-testid=\"column\"]", self.css)

    def test_issue_2_typography_hierarchy_main_features_and_subfeatures(self):
        """Verify Level 2 main feature buttons are bold 700/800 and Level 3 sidebar items are semibold 600/700."""
        # Level 2 Horizontal Nav typography
        self.assertIn(".st-key-main-nav-container div[data-testid=\"column\"] button", self.css)
        self.assertIn("font-weight: 700 !important", self.css)
        self.assertIn(".st-key-main-nav-container div[data-testid=\"column\"] button[kind=\"primary\"]", self.css)
        self.assertIn("font-weight: 800 !important", self.css)

        # Level 3 Sidebar Subfeatures typography
        self.assertIn("[data-testid=\"stSidebar\"] div.stButton > button", self.css)
        self.assertIn("font-weight: 600 !important", self.css)
        self.assertIn("[data-testid=\"stSidebar\"] div.stButton > button[kind=\"primary\"]", self.css)
        self.assertIn("color: #97144D !important", self.css)

    def test_issue_3_popover_dropdown_layering_and_z_index(self):
        """Verify sidebar popovers render above dashboard content without clipping."""
        # Sidebar elevated z-index and overflow visible
        self.assertIn("[data-testid=\"stSidebar\"]", self.css)
        self.assertIn("z-index: 1005 !important", self.css)
        self.assertIn("overflow: visible !important", self.css)

        # BaseWeb popover layers and bodies have highest z-index
        self.assertIn("div[data-baseweb=\"popover\"]", self.css)
        self.assertIn("z-index: 999999 !important", self.css)
        self.assertIn("[data-testid=\"stSidebar\"] div[data-testid=\"stPopoverBody\"]", self.css)

    def test_sidebar_single_button_surface_no_nested_borders_or_backgrounds(self):
        """Verify sidebar navigation buttons have exactly ONE surface and reset nested descendants."""
        # Button container has padding & surface
        self.assertIn("[data-testid=\"stSidebar\"] div.stButton > button", self.css)
        self.assertIn("padding: 0.55rem 0.85rem !important", self.css)

        # Inner structural elements explicitly reset to transparent with no borders
        self.assertIn("[data-testid=\"stSidebar\"] div.stButton > button *", self.css)
        self.assertIn("background: transparent !important", self.css)
        self.assertIn("border: none !important", self.css)
        self.assertIn("box-shadow: none !important", self.css)

        # Popover trigger buttons also reset inner elements
        self.assertIn("[data-testid=\"stSidebar\"] div[data-testid=\"stPopover\"] > button *", self.css)

    def test_css_syntax_balance_and_no_orphaned_rules(self):
        """Verify CSS has perfectly balanced braces, comments, and zero orphaned rules."""
        open_b = self.css.count("{")
        close_b = self.css.count("}")
        self.assertEqual(open_b, close_b, f"CSS braces must be balanced: {open_b} open vs {close_b} close")

        open_c = self.css.count("/*")
        close_c = self.css.count("*/")
        self.assertEqual(open_c, close_c, f"CSS comments must be balanced: {open_c} open vs {close_c} close")

        # Verify orphaned declarations were repaired
        self.assertNotIn("  background: var(--primary-violet-gradient) !important;\n  border: 1px solid var(--primary-violet);\n", self.css)
        self.assertNotIn("  background: #E0E7FF !important;\n  color: var(--primary-violet-dark) !important;\n}", self.css)

    def test_hey_vaani_edge_tts_coordination_and_premature_silence_protection(self):
        """Verify TTS completion check (waitForTTS) and premature silence restart before timeout."""
        wake_path = Path(__file__).parent / "components" / "wake_word.py"
        wake_code = wake_path.read_text(encoding="utf-8")

        # TTS speaking verification before starting recognition
        self.assertIn("waitForTTS", wake_code)
        self.assertIn("synth.speaking", wake_code)

        # Silence restart logic in onend if speech hasn't been captured within timeout
        self.assertIn("LISTENING_TIMEOUT_MS", wake_code)
        self.assertIn("Date.now() - listeningStartTime < LISTENING_TIMEOUT_MS", wake_code)

        # Explicit diagnostic state badge labels
        self.assertIn("Hands-Free Standby", wake_code)
        self.assertIn("Greeting Playing", wake_code)
        self.assertIn("Listening...", wake_code)
        self.assertIn("Submitting Command", wake_code)
        self.assertIn("Mic Blocked", wake_code)

    def test_complete_10_step_voice_submission_and_tts_return_to_standby(self):
        """Verify interim transcript retention, Streamlit TextArea Ctrl+Enter commit, TTS playback, and standby return."""
        wake_path = Path(__file__).parent / "components" / "wake_word.py"
        wake_code = wake_path.read_text(encoding="utf-8")

        # Parent-bound timers survive Streamlit component iframe reloads
        self.assertIn("pSetTimeout", wake_code)
        self.assertIn("pSetInterval", wake_code)

        # Interim transcript retention so silence timer never drops spoken questions
        self.assertIn("lastRecognizedText", wake_code)
        self.assertIn("finalTranscript.trim() || lastRecognizedText.trim()", wake_code)

        # Streamlit TextArea commit via Ctrl+Enter KeyboardEvent + blur
        self.assertIn("ctrlKey: true", wake_code)
        self.assertIn("textarea.blur()", wake_code)

        # Response playback and return to standby after TTS finishes
        self.assertIn("handleAssistantResponsePlayback", wake_code)
        self.assertIn("speakResponseFallback", wake_code)
        self.assertIn("onPlaybackComplete", wake_code)

    def test_copilot_independent_conversation_scrolling_layout(self):
        """Verify Copilot conversation history scrolls independently with pinned header and sticky composer."""
        # Parent panel fixed and non-scrolling
        self.assertIn(".st-key-floating-assistant-panel", self.css)
        self.assertIn("overflow: hidden !important", self.css)
        self.assertIn("min-height: 0 !important", self.css)

        # Streamlit stLayoutWrapper around scroll area constrained with flex: 1 1 0% and min-height: 0
        self.assertIn(".st-key-floating-assistant-panel > div[data-testid=\"stLayoutWrapper\"]:has(> .st-key-copilot-chat-scroll-area)", self.css)
        self.assertIn("flex: 1 1 0% !important", self.css)

        # Scroll area has independent vertical scrollbar and overscroll containment
        self.assertIn(".st-key-copilot-chat-scroll-area", self.css)
        self.assertIn("overflow-y: auto !important", self.css)
        self.assertIn("overscroll-behavior: contain !important", self.css)

        # Child messages inside scroll area do not flex-shrink
        self.assertIn(".st-key-copilot-chat-scroll-area > div[data-testid=\"stLayoutWrapper\"]", self.css)
        self.assertIn("flex-shrink: 0 !important", self.css)

        # Smart scroll preservation across reruns in JS
        wake_path = Path(__file__).parent / "components" / "wake_word.py"
        wake_code = wake_path.read_text(encoding="utf-8")
        self.assertIn("setupCopilotScrollAndComposer", wake_code)
        self.assertIn("userScrolledUp", wake_code)


if __name__ == "__main__":
    unittest.main()



