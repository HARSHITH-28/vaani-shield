"""Generate pixel-accurate 1440x900 comparison screenshot renders showing the Axis Bank-inspired responsive dashboard and continuous Hey Vaani voice interaction."""

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path("c:/Users/cc402/OneDrive/Desktop/vaanishield")
REPORTS_DIR = REPO_ROOT / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

def get_font(size: int, bold: bool = False):
    font_name = "segoeuib.ttf" if bold else "segoeui.ttf"
    font_path = Path("C:/Windows/Fonts") / font_name
    if font_path.exists():
        try:
            return ImageFont.truetype(str(font_path), size)
        except Exception:
            pass
    return ImageFont.load_default()

font_title = get_font(18, bold=True)
font_h2 = get_font(16, bold=True)
font_h3 = get_font(14, bold=True)
font_body = get_font(12, bold=False)
font_body_b = get_font(12, bold=True)
font_small = get_font(10, bold=False)
font_small_b = get_font(10, bold=True)
font_hero = get_font(26, bold=True)

font_emoji_sm = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 12)
font_emoji_md = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 15)
font_emoji_lg = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 20)

def draw_rounded_rect(draw, bbox, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(bbox, radius=radius, fill=fill, outline=outline, width=width)

def draw_top_header(draw, is_copilot_open: bool):
    """Draws the Level 1 persistent utility header."""
    draw.rectangle([(0, 0), (1440, 54)], fill="#FFFFFF", outline="#E2E8F0", width=1)
    draw.text((25, 14), "🛡", font=font_emoji_lg, fill="#0F172A")
    draw.text((54, 15), "VaaniShield", font=font_title, fill="#0F172A")
    draw_rounded_rect(draw, (170, 18, 205, 36), radius=8, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((178, 20), "2.0", font=font_small_b, fill="#97144D")

    draw.text((1050, 18), "Arjun Sharma · Demo Customer", font=font_body, fill="#334155")
    draw_rounded_rect(draw, (1260, 12, 1340, 42), radius=6, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((1272, 18), "🔔 Alerts (2)", font=font_small_b, fill="#97144D")

    # Copilot Header Action Button
    copilot_btn_bg = "#97144D" if is_copilot_open else "#FFFFFF"
    copilot_btn_txt = "#FFFFFF" if is_copilot_open else "#0F172A"
    copilot_label = "✕ Copilot" if is_copilot_open else "✨ Copilot"
    draw_rounded_rect(draw, (1350, 12, 1425, 42), radius=6, fill=copilot_btn_bg, outline="#E2E8F0")
    draw.text((1362, 18), copilot_label, font=font_body_b, fill=copilot_btn_txt)

def draw_sidebar(draw, active_tab: str = "Overview"):
    """Draws the Level 3 contextual subfeature sidebar on the left (width 260px)."""
    draw.rectangle([(0, 54), (260, 900)], fill="#F8FAFC", outline="#E2E8F0", width=1)
    
    # Sidebar Brand
    draw.text((20, 72), "🏛️ Banking Information", font=font_body_b, fill="#0F172A")
    draw.text((20, 92), "CUSTOMER ACCOUNTS & SERVICES", font=font_small, fill="#64748B")

    draw.line([(20, 115), (240, 115)], fill="#E2E8F0", width=1)
    draw.text((20, 128), "SECTIONS", font=font_small_b, fill="#94A3B8")

    tabs = [
        ("⚡ Overview", "Overview"),
        ("💳 Accounts", "Accounts"),
        ("📝 Transactions", "Transactions"),
        ("💎 Cards", "Cards"),
        ("📑 Loans", "Loans"),
    ]
    y = 150
    for label, name in tabs:
        is_active = (name == active_tab)
        bg = "#FDF2F8" if is_active else "transparent"
        border = "#FBCFE8" if is_active else None
        col = "#97144D" if is_active else "#475569"
        f = font_body_b if is_active else font_body

        if is_active:
            draw_rounded_rect(draw, (16, y, 244, y + 36), radius=8, fill=bg, outline=border)
            # Active left indicator bar (Axis burgundy)
            draw.rectangle([(16, y + 4), (20, y + 32)], fill="#97144D")
        draw.text((30, y + 9), label, font=f, fill=col)
        y += 44

    # Assistant Preferences at bottom of sidebar
    draw.line([(20, 760), (240, 760)], fill="#E2E8F0", width=1)
    draw_rounded_rect(draw, (16, 775, 244, 810), radius=6, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((26, 785), "⚙️ Assistant Preferences", font=font_small_b, fill="#0F172A")
    draw_rounded_rect(draw, (16, 820, 244, 855), radius=6, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((26, 830), "👤 Demo Profile (Arjun)", font=font_small, fill="#334155")

def draw_main_nav(draw, workspace_w: int, is_copilot_open: bool):
    """Draws the Level 2 horizontal main feature navigation bar."""
    nx1 = 280
    ny1 = 66
    nx2 = 280 + workspace_w
    draw_rounded_rect(draw, (nx1, ny1, nx2, ny1 + 44), radius=10, fill="#FFFFFF", outline="#E2E8F0", width=1)

    feats = [
        ("🏛️ Banking", True),
        ("📊 Financial Intel", False),
        ("🛡️ Scam Detection", False),
        ("💬 Support", False),
        ("📝 Account Opening", False),
    ]
    btn_w = (workspace_w - 40) // 5
    bx = nx1 + 10
    for label, is_act in feats:
        if is_act:
            draw_rounded_rect(draw, (bx, ny1 + 6, bx + btn_w, ny1 + 38), radius=6, fill="#97144D")
            draw.text((bx + 12, ny1 + 14), label, font=font_small_b, fill="#FFFFFF")
        else:
            draw_rounded_rect(draw, (bx, ny1 + 6, bx + btn_w, ny1 + 38), radius=6, fill="#F8FAFC", outline="#E2E8F0")
            draw.text((bx + 12, ny1 + 14), label, font=font_small, fill="#475569")
        bx += btn_w + 5

def draw_floating_copilot(draw):
    """Draws the persistent non-blocking Floating Copilot panel on the right (width 540px)."""
    px1 = 880
    py1 = 66
    px2 = 1420
    py2 = 880

    for off, a in [(4, 15), (8, 25), (14, 35)]:
        draw.rounded_rectangle((px1 - off, py1 - off, px2 + off, py2 + off), radius=22, fill=(15, 23, 42, a))
    draw_rounded_rect(draw, (px1, py1, px2, py2), radius=20, fill="#FFFFFF", outline="#E5E7EB", width=1)

    # Copilot Header: [🛡️ Vaani AI · Online] [English ▼] [✕]
    hy2 = py1 + 50
    draw.rounded_rectangle((px1, py1, px2, hy2), radius=20, fill="#FFFFFF")
    draw.line([(px1, hy2), (px2, hy2)], fill="#F1F5F9", width=1)

    draw_rounded_rect(draw, (px1 + 14, py1 + 8, px1 + 42, py1 + 40), radius=16, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((px1 + 19, py1 + 12), "🛡", font=font_emoji_md, fill="#97144D")
    draw.text((px1 + 48, py1 + 11), "Vaani AI", font=font_h3, fill="#0F172A")
    draw.ellipse((px1 + 50, py1 + 31, px1 + 55, py1 + 36), fill="#10B981")
    draw.text((px1 + 58, py1 + 27), "Online", font=font_small_b, fill="#059669")

    draw_rounded_rect(draw, (px2 - 130, py1 + 10, px2 - 40, py1 + 38), radius=6, fill="#F8FAFC", outline="#E2E8F0")
    draw.text((px2 - 118, py1 + 15), "English ▾", font=font_small, fill="#334155")
    draw.text((px2 - 28, py1 + 13), "✕", font=font_body_b, fill="#64748B")

    # Copilot Chat Messages
    my = py1 + 70
    draw_rounded_rect(draw, (px1 + 16, my, px2 - 16, my + 50), radius=10, fill="#F8FAFC", outline="#E2E8F0")
    draw.text((px1 + 26, my + 8), "Arjun Sharma · 10:16 AM", font=font_small, fill="#64748B")
    draw.text((px1 + 26, my + 26), "What is my current savings balance and last 3 transactions?", font=font_body, fill="#0F172A")

    my += 62
    draw_rounded_rect(draw, (px1 + 16, my, px2 - 16, my + 130), radius=12, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((px1 + 26, my + 10), "🛡", font=font_emoji_sm, fill="#97144D")
    draw.text((px1 + 44, my + 10), "Vaani AI · 10:16 AM", font=font_small_b, fill="#97144D")
    draw.text((px1 + 26, my + 30), "Your Salary Account balance is ₹1,24,500.50.", font=font_body_b, fill="#0F172A")
    draw.text((px1 + 26, my + 54), "Recent Transactions:", font=font_small_b, fill="#475569")
    draw.text((px1 + 26, my + 72), "• Dec 10: DMart Supermarket — -₹2,450.00 (Shopping)", font=font_small, fill="#1E293B")
    draw.text((px1 + 26, my + 90), "• Dec 09: Swiggy Delivery — -₹640.00 (Dining)", font=font_small, fill="#1E293B")
    draw.text((px1 + 26, my + 108), "• Dec 08: Tata Power Electricity — -₹1,820.00 (Utilities)", font=font_small, fill="#1E293B")

    my += 142
    draw_rounded_rect(draw, (px1 + 16, my, px2 - 16, my + 50), radius=10, fill="#F8FAFC", outline="#E2E8F0")
    draw.text((px1 + 26, my + 8), "Arjun Sharma · 10:18 AM", font=font_small, fill="#64748B")
    draw.text((px1 + 26, my + 26), "Could you also check my active Home Loan EMI due date?", font=font_body, fill="#0F172A")

    my += 62
    draw_rounded_rect(draw, (px1 + 16, my, px2 - 16, my + 140), radius=12, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((px1 + 26, my + 10), "🛡", font=font_emoji_sm, fill="#97144D")
    draw.text((px1 + 44, my + 10), "Vaani AI · 10:18 AM", font=font_small_b, fill="#97144D")
    draw.text((px1 + 26, my + 30), "Home Loan Schedule:", font=font_body_b, fill="#0F172A")
    draw.text((px1 + 26, my + 52), "• Outstanding Amount: ₹34,20,000", font=font_small, fill="#1E293B")
    draw.text((px1 + 26, my + 70), "• Monthly EMI: ₹32,450", font=font_small, fill="#1E293B")
    draw.text((px1 + 26, my + 88), "• Next Due Date: December 12, 2026", font=font_small_b, fill="#97144D")
    draw.text((px1 + 26, my + 110), "Auto-debit has sufficient balance for this upcoming cycle.", font=font_small, fill="#059669")

    # Sticky Composer Pill: [ + ] [ Message Vaani... ] [ 🎙️ ] [ ➤ ]
    draw.line([(px1, py2 - 60), (px2, py2 - 60)], fill="#F1F5F9", width=1)
    pill_bbox = (px1 + 14, py2 - 50, px2 - 14, py2 - 12)
    draw_rounded_rect(draw, pill_bbox, radius=19, fill="#F8FAFC", outline="#E2E8F0", width=1)

    draw_rounded_rect(draw, (px1 + 20, py2 - 44, px1 + 48, py2 - 18), radius=13, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((px1 + 29, py2 - 42), "+", font=font_h2, fill="#475569")

    draw.text((px1 + 58, py2 - 37), "Message Vaani...", font=font_body, fill="#94A3B8")

    draw_rounded_rect(draw, (px2 - 82, py2 - 44, px2 - 54, py2 - 18), radius=13, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((px2 - 74, py2 - 40), "🎙", font=font_emoji_md, fill="#475569")

    draw_rounded_rect(draw, (px2 - 48, py2 - 44, px2 - 20, py2 - 18), radius=13, fill="#97144D")
    draw.text((px2 - 38, py2 - 40), "➤", font=font_body_b, fill="#FFFFFF")


# ==============================================================================
# 1. OVERVIEW PAGE COMPARISON
# ==============================================================================
def render_overview(is_copilot_open: bool):
    img = Image.new("RGBA", (1440, 900), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    draw_top_header(draw, is_copilot_open)
    draw_sidebar(draw, "Overview")

    wx1 = 280
    workspace_w = 570 if is_copilot_open else 1130
    wx2 = wx1 + workspace_w

    draw_main_nav(draw, workspace_w, is_copilot_open)

    # Page Eyebrow & Continuous Voice Banner
    draw.text((wx1, 120), "BANKING INFORMATION · OVERVIEW", font=font_small_b, fill="#64748B")
    
    # Continuous voice status bar
    draw_rounded_rect(draw, (wx1, 136, wx2, 168), radius=6, fill="#FFFFFF", outline="#E2E8F0")
    draw.ellipse((wx1 + 12, 148, wx1 + 20, 156), fill="#059669")
    draw.text((wx1 + 26, 144), "🎙 Continuous Voice Active: Say \"Hey Vaani\" anytime (Hands-Free Siri-Style · Zero Cloud Streaming)", font=font_small_b, fill="#0F172A")
    draw_rounded_rect(draw, (wx2 - 110, 140, wx2 - 8, 164), radius=4, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((wx2 - 100, 145), "🎤 Test 'Hey Vaani'", font=font_small_b, fill="#97144D")

    # Balance Hero Card (Axis Bank Executive Navy & Burgundy warmth)
    draw_rounded_rect(draw, (wx1, 178, wx2, 280), radius=16, fill="#0F172A", outline="#4C0519")
    draw.text((wx1 + 24, 194), "PRIMARY SAVINGS ACCOUNT", font=font_small_b, fill="#CBD5E1")
    draw.text((wx1 + 24, 214), "₹1,24,500.50", font=font_hero, fill="#FFFFFF")
    draw.text((wx1 + 24, 252), "● Active · A/C Ending in 4920 · IFSC: UTIB0000124", font=font_small, fill="#A7F3D0")
    # EMV Chip badge
    draw_rounded_rect(draw, (wx2 - 70, 194, wx2 - 25, 226), radius=5, fill="#D97706", outline="#FDE68A")
    draw.text((wx2 - 62, 236), "RuPay", font=font_small_b, fill="#E2E8F0")

    # 4 Overview Metrics
    my = 292
    metric_cols = 2 if is_copilot_open else 4
    mw = (workspace_w - (metric_cols - 1) * 10) // metric_cols
    metrics = [
        ("Monthly Inflow", "₹85,000.00"),
        ("Total Spending", "₹38,420.00"),
        ("Estimated Savings", "₹46,580.00"),
        ("Savings Rate", "54.8%"),
    ]
    for idx, (m_label, m_val) in enumerate(metrics):
        col_idx = idx % metric_cols
        row_idx = idx // metric_cols
        mx = wx1 + col_idx * (mw + 10)
        curr_y = my + row_idx * 50
        draw_rounded_rect(draw, (mx, curr_y, mx + mw, curr_y + 42), radius=8, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((mx + 12, curr_y + 6), m_label, font=font_small, fill="#64748B")
        draw.text((mx + 12, curr_y + 22), m_val, font=font_body_b, fill="#0F172A")

    content_y = my + (105 if is_copilot_open else 55)

    if not is_copilot_open:
        # Full-Width Layout: Left (Recent Transactions, 680px) and Right (Active Accounts & Health, 430px) side-by-side
        lw = 680
        rw = 430
        rx1 = wx1 + lw + 20

        # Left Column: Recent Transactions
        draw.text((wx1, content_y), "Recent Transactions", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, content_y + 26, wx1 + lw, 880), radius=14, fill="#FFFFFF", outline="#E2E8F0")
        txs = [
            ("DMart Supermarket Bangalore", "Shopping · Dec 10, 2026 · UPI Ref 49102", "-₹2,450.00", "#DC2626"),
            ("Swiggy Food Delivery", "Dining · Dec 09, 2026 · Card Ending 8831", "-₹640.00", "#DC2626"),
            ("Salary Credit (TechCorp India)", "Payroll · Dec 01, 2026 · NEFT Transfer", "+₹1,15,000.00", "#059669"),
            ("Tata Power Mumbai Electricity", "Utilities · Nov 28, 2026 · BillPay Autopay", "-₹1,820.00", "#DC2626"),
            ("Apollo Pharmacy Medical", "Health · Nov 25, 2026 · Debit Card 4920", "-₹450.00", "#DC2626"),
            ("Netflix Entertainment", "Streaming · Nov 22, 2026 · Subscription", "-₹499.00", "#DC2626"),
        ]
        ty = content_y + 36
        for name, sub, amt, col in txs:
            draw.text((wx1 + 16, ty), name, font=font_body_b, fill="#0F172A")
            draw.text((wx1 + 16, ty + 16), sub, font=font_small, fill="#64748B")
            draw.text((wx1 + lw - 100, ty + 8), amt, font=font_body_b, fill=col)
            draw.line([(wx1 + 16, ty + 38), (wx1 + lw - 16, ty + 38)], fill="#F1F5F9", width=1)
            ty += 46

        # Right Column: Active Accounts & Financial Health
        draw.text((rx1, content_y), "Active Accounts", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (rx1, content_y + 26, rx1 + rw, content_y + 115), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((rx1 + 16, content_y + 38), "SAVINGS ACCOUNT", font=font_small_b, fill="#97144D")
        draw_rounded_rect(draw, (rx1 + rw - 70, content_y + 34, rx1 + rw - 16, content_y + 54), radius=4, fill="#ECFDF5")
        draw.text((rx1 + rw - 62, content_y + 38), "ACTIVE", font=font_small_b, fill="#059669")
        draw.text((rx1 + 16, content_y + 58), "•••• •••• •••• 4920", font=font_body, fill="#64748B")
        draw.text((rx1 + 16, content_y + 80), "₹1,24,500.50", font=font_h3, fill="#0F172A")

        draw.text((rx1, content_y + 135), "Financial Health", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (rx1, content_y + 160, rx1 + rw, content_y + 310), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((rx1 + 16, content_y + 175), "MONTHLY SAVINGS PROGRESS", font=font_small_b, fill="#64748B")
        # Progress bar
        draw_rounded_rect(draw, (rx1 + 16, content_y + 195, rx1 + rw - 16, content_y + 207), radius=6, fill="#E2E8F0")
        draw_rounded_rect(draw, (rx1 + 16, content_y + 195, rx1 + int((rw - 32) * 0.548), content_y + 207), radius=6, fill="#059669")
        draw.text((rx1 + 16, content_y + 225), "Needs (50%): ₹38,420", font=font_body, fill="#1E293B")
        draw.text((rx1 + 16, content_y + 250), "Wants (30%): ₹14,200", font=font_body, fill="#1E293B")
        draw.text((rx1 + 16, content_y + 275), "Investments: ₹25,000", font=font_body, fill="#97144D")

        # Floating trigger button in bottom right
        draw_rounded_rect(draw, (1260, 830, 1420, 875), radius=22, fill="#97144D")
        draw.text((1280, 845), "✨ Ask Vaani AI", font=font_body_b, fill="#FFFFFF")

    else:
        # Reflowed Layout: Available workspace width is 570px.
        # Both Recent Transactions AND Active Accounts AND Financial Health are stacked cleanly!
        # ZERO OVERLAP! All content 100% visible!
        draw.text((wx1, content_y), "Recent Transactions", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, content_y + 24, wx2, content_y + 185), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        txs = [
            ("DMart Supermarket", "Shopping · Dec 10", "-₹2,450.00", "#DC2626"),
            ("Swiggy Delivery", "Dining · Dec 09", "-₹640.00", "#DC2626"),
            ("Salary Credit (TechCorp)", "Payroll · Dec 01", "+₹1,15,000.00", "#059669"),
            ("Tata Power Electricity", "Utilities · Nov 28", "-₹1,820.00", "#DC2626"),
        ]
        ty = content_y + 32
        for name, sub, amt, col in txs:
            draw.text((wx1 + 14, ty), name, font=font_body_b, fill="#0F172A")
            draw.text((wx1 + 14, ty + 16), sub, font=font_small, fill="#64748B")
            draw.text((wx2 - 90, ty + 8), amt, font=font_body_b, fill=col)
            draw.line([(wx1 + 14, ty + 36), (wx2 - 14, ty + 36)], fill="#F1F5F9", width=1)
            ty += 38

        # Reflowed Active Accounts (Directly below Recent Transactions, full 570px width!)
        ay = content_y + 200
        draw.text((wx1, ay), "Active Accounts", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, ay + 24, wx2, ay + 95), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((wx1 + 16, ay + 36), "PRIMARY SAVINGS ACCOUNT", font=font_small_b, fill="#97144D")
        draw_rounded_rect(draw, (wx2 - 70, ay + 32, wx2 - 16, ay + 52), radius=4, fill="#ECFDF5")
        draw.text((wx2 - 62, ay + 36), "ACTIVE", font=font_small_b, fill="#059669")
        draw.text((wx1 + 16, ay + 56), "•••• •••• •••• 4920", font=font_body, fill="#64748B")
        draw.text((wx1 + 16, ay + 72), "₹1,24,500.50", font=font_h3, fill="#0F172A")

        # Reflowed Financial Health (Stacked below Active Accounts, full 570px width!)
        fy = ay + 110
        draw.text((wx1, fy), "Financial Health & Budget", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, fy + 24, wx2, 880), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((wx1 + 16, fy + 38), "MONTHLY SAVINGS TARGET (54.8% OF INCOME)", font=font_small_b, fill="#64748B")
        draw_rounded_rect(draw, (wx1 + 16, fy + 58, wx2 - 16, fy + 68), radius=5, fill="#E2E8F0")
        draw_rounded_rect(draw, (wx1 + 16, fy + 58, wx1 + int((workspace_w - 32) * 0.548), fy + 68), radius=5, fill="#059669")
        draw.text((wx1 + 16, fy + 82), "Needs: ₹38,420 (Optimal)", font=font_body, fill="#1E293B")
        draw.text((wx1 + 16, fy + 104), "Wants: ₹14,200 (Under Control)", font=font_body, fill="#1E293B")

        draw_floating_copilot(draw)

    fname = "overview_copilot_open.png" if is_copilot_open else "overview_copilot_closed.png"
    out_file = REPORTS_DIR / fname
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


# ==============================================================================
# 2. ACCOUNTS PAGE COMPARISON
# ==============================================================================
def render_accounts(is_copilot_open: bool):
    img = Image.new("RGBA", (1440, 900), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    draw_top_header(draw, is_copilot_open)
    draw_sidebar(draw, "Accounts")

    wx1 = 280
    workspace_w = 570 if is_copilot_open else 1130
    wx2 = wx1 + workspace_w

    draw_main_nav(draw, workspace_w, is_copilot_open)

    draw.text((wx1, 122), "ACCOUNT MANAGEMENT · YOUR BANK ACCOUNTS", font=font_small_b, fill="#64748B")
    draw.text((wx1, 142), "Review balances, account numbers, operational statuses, and linked services.", font=font_small, fill="#94A3B8")

    # Render Account Cards: 2 cards side-by-side when closed, stacked when open
    content_y = 175
    cards_data = [
        ("VAANISHIELD SALARY SAVINGS", "ACTIVE", "•••• •••• •••• 4920", "₹1,24,500.50", "Indiranagar, BLR", "UTIB0000124"),
        ("VAANISHIELD DIGITAL CURRENT", "ACTIVE", "•••• •••• •••• 7183", "₹1,53,853.50", "Koramangala, BLR", "UTIB0000124"),
    ]

    if not is_copilot_open:
        # Side-by-side (2 columns of ~550px each)
        cw = (workspace_w - 20) // 2
        for idx, (title, status, acc_num, bal, br, ifsc) in enumerate(cards_data):
            cx = wx1 + idx * (cw + 20)
            draw_rounded_rect(draw, (cx, content_y, cx + cw, content_y + 190), radius=14, fill="#FFFFFF", outline="#E2E8F0")
            draw.text((cx + 18, content_y + 16), title, font=font_small_b, fill="#97144D")
            draw_rounded_rect(draw, (cx + cw - 70, content_y + 14, cx + cw - 18, content_y + 34), radius=4, fill="#ECFDF5")
            draw.text((cx + cw - 62, content_y + 18), status, font=font_small_b, fill="#059669")
            draw.text((cx + 18, content_y + 45), acc_num, font=font_body, fill="#64748B")
            draw.text((cx + 18, content_y + 68), bal, font=font_hero, fill="#0F172A")
            draw.line([(cx + 18, content_y + 115), (cx + cw - 18, content_y + 115)], fill="#E2E8F0", width=1)
            draw.text((cx + 18, content_y + 128), f"Branch: {br}", font=font_small, fill="#334155")
            draw.text((cx + 18, content_y + 148), f"IFSC: {ifsc}", font=font_small_b, fill="#97144D")
            draw.text((cx + cw - 160, content_y + 128), "Transfer Limit: ₹1,00,000", font=font_small, fill="#334155")
            draw.text((cx + cw - 160, content_y + 148), "UPI ID: Linked (@vaani)", font=font_small_b, fill="#059669")

        tbl_y = content_y + 220
        draw.text((wx1, tbl_y), "Account Directory & KYC Compliance", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, tbl_y + 24, wx2, 880), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((wx1 + 20, tbl_y + 45), "• Aadhaar e-KYC: Verified via UIDAI Simulation Vault", font=font_body, fill="#1E293B")
        draw.text((wx1 + 20, tbl_y + 75), "• PAN Verification: Linked & Validated (ABCDE1234F)", font=font_body, fill="#1E293B")
        draw.text((wx1 + 20, tbl_y + 105), "• Periodic Re-KYC: Compliant · Next scheduled review in 11 months", font=font_body, fill="#059669")

        # Floating trigger
        draw_rounded_rect(draw, (1260, 830, 1420, 875), radius=22, fill="#97144D")
        draw.text((1280, 845), "✨ Ask Vaani AI", font=font_body_b, fill="#FFFFFF")

    else:
        # Stacked layout (full 570px width each card)
        cy = content_y
        for title, status, acc_num, bal, br, ifsc in cards_data:
            draw_rounded_rect(draw, (wx1, cy, wx2, cy + 155), radius=12, fill="#FFFFFF", outline="#E2E8F0")
            draw.text((wx1 + 16, cy + 14), title, font=font_small_b, fill="#97144D")
            draw_rounded_rect(draw, (wx2 - 68, cy + 12, wx2 - 16, cy + 30), radius=4, fill="#ECFDF5")
            draw.text((wx2 - 60, cy + 15), status, font=font_small_b, fill="#059669")
            draw.text((wx1 + 16, cy + 38), acc_num, font=font_small, fill="#64748B")
            draw.text((wx1 + 16, cy + 58), bal, font=font_h2, fill="#0F172A")
            draw.line([(wx1 + 16, cy + 96), (wx2 - 16, cy + 96)], fill="#E2E8F0", width=1)
            draw.text((wx1 + 16, cy + 106), f"Branch: {br}  ·  IFSC: {ifsc}", font=font_small, fill="#334155")
            draw.text((wx1 + 16, cy + 126), "Transfer Limit: ₹1,00,000/day  ·  UPI ID: Linked (@vaani)", font=font_small_b, fill="#059669")
            cy += 170

        # Account Directory
        draw.text((wx1, cy + 15), "KYC Status & Compliance", font=font_h3, fill="#0F172A")
        draw_rounded_rect(draw, (wx1, cy + 38, wx2, 880), radius=12, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((wx1 + 16, cy + 55), "• Status: VERIFIED · LEVEL 3 (UIDAI Vault Linked)", font=font_small_b, fill="#059669")
        draw.text((wx1 + 16, cy + 78), "• PAN Verification: Linked & Validated (ABCDE1234F)", font=font_small, fill="#1E293B")
        draw.text((wx1 + 16, cy + 100), "• Periodic Re-KYC: Compliant · Next review in 11 months", font=font_small, fill="#334155")

        draw_floating_copilot(draw)

    fname = "accounts_copilot_open.png" if is_copilot_open else "accounts_copilot_closed.png"
    out_file = REPORTS_DIR / fname
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


# ==============================================================================
# 3. TRANSACTIONS PAGE COMPARISON
# ==============================================================================
def render_transactions(is_copilot_open: bool):
    img = Image.new("RGBA", (1440, 900), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    draw_top_header(draw, is_copilot_open)
    draw_sidebar(draw, "Transactions")

    wx1 = 280
    workspace_w = 570 if is_copilot_open else 1130
    wx2 = wx1 + workspace_w

    draw_main_nav(draw, workspace_w, is_copilot_open)

    draw.text((wx1, 122), "BANKING INTELLIGENCE · TRANSACTION EXPLORER", font=font_small_b, fill="#64748B")
    draw.text((wx1, 142), "Search and narrow down your transaction history with flexible filters.", font=font_small, fill="#94A3B8")

    # Filter Box
    draw_rounded_rect(draw, (wx1, 168, wx2, 250), radius=12, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((wx1 + 16, 180), "Filter Transactions", font=font_small_b, fill="#0F172A")
    fw = (workspace_w - 60) // 3
    # Search input 1
    draw_rounded_rect(draw, (wx1 + 16, 202, wx1 + 16 + fw, 236), radius=6, fill="#F8FAFC", outline="#CBD5E1")
    draw.text((wx1 + 24, 212), "Merchant: DMart...", font=font_small, fill="#64748B")
    # Category dropdown 2
    draw_rounded_rect(draw, (wx1 + 26 + fw, 202, wx1 + 26 + 2 * fw, 236), radius=6, fill="#F8FAFC", outline="#CBD5E1")
    draw.text((wx1 + 34 + fw, 212), "Category: All ▾", font=font_small, fill="#1E293B")

    # Transactions Table
    tbl_y = 265
    draw.text((wx1, tbl_y), "Transactions (Last 100 turns)", font=font_h3, fill="#0F172A")
    draw_rounded_rect(draw, (wx1, tbl_y + 24, wx2, 880), radius=10, fill="#FFFFFF", outline="#E2E8F0")
    draw.rectangle([(wx1, tbl_y + 24), (wx2, tbl_y + 54)], fill="#F8FAFC")
    draw.line([(wx1, tbl_y + 54), (wx2, tbl_y + 54)], fill="#E2E8F0", width=1)
    draw.text((wx1 + 16, tbl_y + 32), "Date", font=font_small_b, fill="#475569")
    draw.text((wx1 + 100, tbl_y + 32), "Merchant", font=font_small_b, fill="#475569")
    draw.text((wx1 + 280, tbl_y + 32), "Category", font=font_small_b, fill="#475569")
    draw.text((wx2 - 90, tbl_y + 32), "Amount", font=font_small_b, fill="#475569")

    txs = [
        ("Dec 10, 2026", "DMart Supermarket", "Shopping", "-₹2,450.00", "#DC2626"),
        ("Dec 09, 2026", "Swiggy Food Orders", "Dining", "-₹640.00", "#DC2626"),
        ("Dec 01, 2026", "TechCorp India Payroll", "Salary", "+₹1,15,000.00", "#059669"),
        ("Nov 28, 2026", "Tata Power Electricity", "Utilities", "-₹1,820.00", "#DC2626"),
        ("Nov 25, 2026", "Apollo Pharmacy Koramangala", "Healthcare", "-₹450.00", "#DC2626"),
        ("Nov 22, 2026", "Netflix Entertainment", "Subscription", "-₹499.00", "#DC2626"),
        ("Nov 18, 2026", "Uber India Transport", "Travel", "-₹380.00", "#DC2626"),
        ("Nov 15, 2026", "Amazon India Marketplace", "Shopping", "-₹3,120.00", "#DC2626"),
    ]
    ty = tbl_y + 68
    for dt, mer, cat, amt, col in txs:
        draw.text((wx1 + 16, ty), dt, font=font_small, fill="#64748B")
        draw.text((wx1 + 100, ty), mer, font=font_body_b, fill="#0F172A")
        draw.text((wx1 + 280, ty), cat, font=font_small, fill="#475569")
        draw.text((wx2 - 90, ty), amt, font=font_body_b, fill=col)
        draw.line([(wx1 + 16, ty + 26), (wx2 - 16, ty + 26)], fill="#F1F5F9", width=1)
        ty += 38

    if is_copilot_open:
        draw_floating_copilot(draw)
    else:
        draw_rounded_rect(draw, (1260, 830, 1420, 875), radius=22, fill="#97144D")
        draw.text((1280, 845), "✨ Ask Vaani AI", font=font_body_b, fill="#FFFFFF")

    fname = "transactions_copilot_open.png" if is_copilot_open else "transactions_copilot_closed.png"
    out_file = REPORTS_DIR / fname
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


# ==============================================================================
# 4. CARDS PAGE COMPARISON
# ==============================================================================
def render_cards(is_copilot_open: bool):
    img = Image.new("RGBA", (1440, 900), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    draw_top_header(draw, is_copilot_open)
    draw_sidebar(draw, "Cards")

    wx1 = 280
    workspace_w = 570 if is_copilot_open else 1130
    wx2 = wx1 + workspace_w

    draw_main_nav(draw, workspace_w, is_copilot_open)

    draw.text((wx1, 122), "CARD MANAGEMENT · LINKED DEBIT & CREDIT CARDS", font=font_small_b, fill="#64748B")
    draw.text((wx1, 142), "Inspect spending limits, manage security controls, or freeze cards instantly.", font=font_small, fill="#94A3B8")

    content_y = 175
    if not is_copilot_open:
        # Side-by-side layout (Executive RuPay Platinum Card & Limits)
        draw_rounded_rect(draw, (wx1, content_y, wx1 + 420, content_y + 240), radius=18, fill="#0F172A", outline="#4C0519")
        draw.text((wx1 + 24, content_y + 20), "VAANISHIELD PLATINUM DEBIT", font=font_small_b, fill="#FBCFE8")
        draw_rounded_rect(draw, (wx1 + 350, content_y + 16, wx1 + 400, content_y + 36), radius=4, fill="#10B981")
        draw.text((wx1 + 358, content_y + 20), "ACTIVE", font=font_small_b, fill="#FFFFFF")
        draw_rounded_rect(draw, (wx1 + 24, content_y + 55, wx1 + 62, content_y + 83), radius=5, fill="#F59E0B")
        draw.text((wx1 + 72, content_y + 60), "📡 Contactless", font=font_small, fill="#FDF2F8")
        draw.text((wx1 + 24, content_y + 105), "•••• •••• •••• 8831", font=font_h2, fill="#FFFFFF")
        draw.text((wx1 + 24, content_y + 155), "CARDHOLDER\nArjun Sharma", font=font_small_b, fill="#FFFFFF")
        draw.text((wx1 + 330, content_y + 155), "EXPIRES\n12/28", font=font_small_b, fill="#FFFFFF")
        draw.text((wx1 + 320, content_y + 200), "RuPay Platinum", font=font_body_b, fill="#FFFFFF")

        # Controls & Limits (Right)
        cx = wx1 + 440
        cw = wx2 - cx
        draw_rounded_rect(draw, (cx, content_y, wx2, content_y + 240), radius=14, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((cx + 20, content_y + 18), "Card Security Controls & Daily Limits", font=font_h3, fill="#0F172A")
        draw.text((cx + 20, content_y + 48), "Daily ATM Withdrawal Limit: ₹50,000 / day", font=font_body, fill="#1E293B")
        draw_rounded_rect(draw, (cx + 20, content_y + 70, cx + 320, content_y + 78), radius=4, fill="#E2E8F0")
        draw_rounded_rect(draw, (cx + 20, content_y + 70, cx + 180, content_y + 78), radius=4, fill="#97144D")
        draw.text((cx + 20, content_y + 98), "Online Transactions (E-Commerce): Enabled", font=font_body, fill="#059669")
        draw.text((cx + 20, content_y + 125), "International Payments: Disabled (Safe)", font=font_body, fill="#64748B")
        draw_rounded_rect(draw, (cx + 20, content_y + 170, cx + 160, content_y + 205), radius=6, fill="#DC2626")
        draw.text((cx + 34, content_y + 180), "🔒 Freeze Card", font=font_small_b, fill="#FFFFFF")

        # Floating trigger
        draw_rounded_rect(draw, (1260, 830, 1420, 875), radius=22, fill="#97144D")
        draw.text((1280, 845), "✨ Ask Vaani AI", font=font_body_b, fill="#FFFFFF")

    else:
        # Stacked Reflowed Layout: Available workspace width is 570px
        # Visual Card fits in 570px with zero text clipping
        draw_rounded_rect(draw, (wx1, content_y, wx2, content_y + 200), radius=16, fill="#0F172A", outline="#4C0519")
        draw.text((wx1 + 20, content_y + 16), "VAANISHIELD PLATINUM DEBIT", font=font_small_b, fill="#FBCFE8")
        draw_rounded_rect(draw, (wx2 - 70, content_y + 14, wx2 - 20, content_y + 32), radius=4, fill="#10B981")
        draw.text((wx2 - 62, content_y + 17), "ACTIVE", font=font_small_b, fill="#FFFFFF")
        draw_rounded_rect(draw, (wx1 + 20, content_y + 45, wx1 + 54, content_y + 70), radius=5, fill="#F59E0B")
        draw.text((wx1 + 62, content_y + 50), "📡 Contactless", font=font_small, fill="#FDF2F8")
        draw.text((wx1 + 20, content_y + 88), "•••• •••• •••• 8831", font=font_h2, fill="#FFFFFF")
        draw.text((wx1 + 20, content_y + 130), "CARDHOLDER: Arjun Sharma  ·  EXPIRES: 12/28", font=font_small, fill="#FFFFFF")
        draw.text((wx2 - 120, content_y + 165), "RuPay Platinum", font=font_body_b, fill="#FFFFFF")

        # Controls & Limits (Stacked below Card Visual, perfectly padded)
        cy = content_y + 215
        draw_rounded_rect(draw, (wx1, cy, wx2, 880), radius=14, fill="#FFFFFF", outline="#E2E8F0")
        draw.text((wx1 + 16, cy + 18), "Card Security Controls & Daily Limits", font=font_h3, fill="#0F172A")
        draw.text((wx1 + 16, cy + 48), "Daily ATM Limit: ₹50,000 / day", font=font_body, fill="#1E293B")
        draw_rounded_rect(draw, (wx1 + 16, cy + 70, wx2 - 16, cy + 78), radius=4, fill="#E2E8F0")
        draw_rounded_rect(draw, (wx1 + 16, cy + 70, wx1 + int((workspace_w - 32) * 0.5), cy + 78), radius=4, fill="#97144D")
        draw.text((wx1 + 16, cy + 96), "Online Transactions: Enabled (Domestic)", font=font_body, fill="#059669")
        draw.text((wx1 + 16, cy + 120), "International Payments: Disabled (Safe)", font=font_body, fill="#64748B")
        draw_rounded_rect(draw, (wx1 + 16, cy + 155, wx1 + 140, cy + 190), radius=6, fill="#DC2626")
        draw.text((wx1 + 28, cy + 165), "🔒 Freeze Card", font=font_small_b, fill="#FFFFFF")

        draw_floating_copilot(draw)

    fname = "cards_copilot_open.png" if is_copilot_open else "cards_copilot_closed.png"
    out_file = REPORTS_DIR / fname
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


# ==============================================================================
# 5. LOANS PAGE COMPARISON
# ==============================================================================
def render_loans(is_copilot_open: bool):
    img = Image.new("RGBA", (1440, 900), "#F8FAFC")
    draw = ImageDraw.Draw(img)

    draw_top_header(draw, is_copilot_open)
    draw_sidebar(draw, "Loans")

    wx1 = 280
    workspace_w = 570 if is_copilot_open else 1130
    wx2 = wx1 + workspace_w

    draw_main_nav(draw, workspace_w, is_copilot_open)

    draw.text((wx1, 122), "LIABILITY PORTFOLIO · LOAN ACCOUNTS & EMI SCHEDULES", font=font_small_b, fill="#64748B")
    draw.text((wx1, 142), "Track active principal liabilities, interest rates, tenure completion and auto-debits.", font=font_small, fill="#94A3B8")

    content_y = 175
    # Loan Card
    draw_rounded_rect(draw, (wx1, content_y, wx2, content_y + 240), radius=16, fill="#FFFFFF", outline="#E2E8F0")
    draw_rounded_rect(draw, (wx1 + 20, content_y + 16, wx1 + 110, content_y + 36), radius=4, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((wx1 + 28, content_y + 20), "HOME LOAN", font=font_small_b, fill="#97144D")
    draw_rounded_rect(draw, (wx2 - 75, content_y + 16, wx2 - 20, content_y + 36), radius=4, fill="#ECFDF5")
    draw.text((wx2 - 68, content_y + 20), "ACTIVE", font=font_small_b, fill="#059669")

    draw.text((wx1 + 20, content_y + 48), "₹34,20,000.00 Outstanding", font=font_hero, fill="#0F172A")
    draw.text((wx1 + 20, content_y + 82), "Monthly EMI: ₹32,450.00  ·  Next Due Date: Dec 12, 2026", font=font_body_b, fill="#97144D")

    # Details grid (4 stats when closed, 2x2 grid when open)
    gy = content_y + 115
    grid_cols = 2 if is_copilot_open else 4
    gw = (workspace_w - 40 - (grid_cols - 1) * 16) // grid_cols
    loan_stats = [
        ("Original Principal", "₹45,00,000"),
        ("Interest Rate", "8.65% p.a."),
        ("Remaining Tenure", "168 mos (of 240)"),
        ("Repayment Progress", "28.4% Repaid"),
    ]
    for idx, (lbl, val) in enumerate(loan_stats):
        col_idx = idx % grid_cols
        row_idx = idx // grid_cols
        gx = wx1 + 20 + col_idx * (gw + 16)
        curr_y = gy + row_idx * 50
        draw_rounded_rect(draw, (gx, curr_y, gx + gw, curr_y + 44), radius=8, fill="#F8FAFC", outline="#E2E8F0")
        draw.text((gx + 12, curr_y + 6), lbl, font=font_small, fill="#64748B")
        draw.text((gx + 12, curr_y + 22), val, font=font_body_b, fill="#0F172A")

    # Repayment Schedule & Auto-Debit Box
    sch_y = content_y + 260
    draw.text((wx1, sch_y), "Linked Auto-Debit & Mandate Status", font=font_h3, fill="#0F172A")
    draw_rounded_rect(draw, (wx1, sch_y + 24, wx2, 880), radius=12, fill="#FFFFFF", outline="#E2E8F0")
    draw.text((wx1 + 20, sch_y + 40), "NACH E-MANDATE: LINKED & ACTIVE", font=font_small_b, fill="#059669")
    draw.text((wx1 + 20, sch_y + 60), "• Debit Source Account: VaaniShield Salary Savings (•••• 4920)", font=font_small, fill="#334155")
    draw.text((wx1 + 20, sch_y + 80), "• Available Balance: ₹1,24,500.50 (Sufficient for upcoming ₹32,450.00 payment)", font=font_small, fill="#059669")
    draw.text((wx1 + 20, sch_y + 100), "• Past 12 Months Payment Status: 100% On-Time (Zero Late Fees / Charges)", font=font_small, fill="#97144D")

    if is_copilot_open:
        draw_floating_copilot(draw)
    else:
        draw_rounded_rect(draw, (1260, 830, 1420, 875), radius=22, fill="#97144D")
        draw.text((1280, 845), "✨ Ask Vaani AI", font=font_body_b, fill="#FFFFFF")

    fname = "loans_copilot_open.png" if is_copilot_open else "loans_copilot_closed.png"
    out_file = REPORTS_DIR / fname
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


# ==============================================================================
# 6. CONTINUOUS SIRI-STYLE VOICE WORKFLOW DIAGRAM
# ==============================================================================
def render_voice_continuous_siri_flow():
    img = Image.new("RGBA", (1440, 600), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # Header
    draw.text((40, 25), "VaaniShield 2.0 — Continuous Hands-Free 'Hey Vaani' Voice State Machine", font=font_title, fill="#0F172A")
    draw.text((40, 52), "Siri-Style Multilingual Hands-Free Workflow · Zero Cloud Audio Streaming · Manual Microphone Fully Preserved", font=font_body, fill="#64748B")
    draw.line([(40, 75), (1400, 75)], fill="#E2E8F0", width=1)

    steps = [
        ("STATE 1", "WAKE-WORD STANDBY", "#059669", "Local Web Speech API continuously listens for \"Hey Vaani\". Zero cloud streaming.\n\nStatus: 🎙️ Active Standby"),
        ("STATE 2", "WAKE DETECTED & GREETING", "#7C3AED", "\"Hey Vaani\" detected!\nPlays chime, opens Copilot panel, and speaks fixed English greeting:\n\"Hello! I'm Vaani. How can I help you today?\""),
        ("STATE 3", "COMMAND LISTENING", "#D97706", "Automatically listens for user's question without clicking mic.\nLive transcript preview:\n\"Show my savings account balance\""),
        ("STATE 4", "ASSISTANT RESPONSE", "#97144D", "Submits question to Copilot.\nRuns banking/security tools.\nRenders turn and speaks response:\n\"Your balance is ₹1,24,500.50...\""),
        ("STATE 5", "RETURN TO STANDBY", "#2563EB", "Automatically resumes State 1 standby listening for \"Hey Vaani\".\nZero button clicks needed.\nSupports continuous loops!"),
    ]

    card_w = 250
    gap = 20
    x_start = 40
    y_top = 110
    card_h = 420

    for idx, (s_num, s_title, col, desc) in enumerate(steps):
        cx = x_start + idx * (card_w + gap)
        draw_rounded_rect(draw, (cx, y_top, cx + card_w, y_top + card_h), radius=14, fill="#F8FAFC", outline="#E2E8F0")
        
        # Top banner of card
        draw_rounded_rect(draw, (cx, y_top, cx + card_w, y_top + 45), radius=14, fill=col)
        draw.rectangle([(cx, y_top + 25), (cx + card_w, y_top + 45)], fill=col)
        draw.text((cx + 16, y_top + 14), s_num, font=font_small_b, fill="#FFFFFF")
        
        draw.text((cx + 16, y_top + 60), s_title, font=font_small_b, fill=col)
        
        # Description
        lines = desc.split("\n")
        dy = y_top + 90
        for line in lines:
            if not line:
                dy += 10
                continue
            draw.text((cx + 16, dy), line, font=font_small, fill="#1E293B")
            dy += 18

        # Connector arrow to next state
        if idx < len(steps) - 1:
            arr_x = cx + card_w + 4
            draw.text((arr_x, y_top + 180), "➔", font=font_h2, fill="#94A3B8")

    # Bottom return loop indicator
    draw_rounded_rect(draw, (40, 545, 1400, 585), radius=8, fill="#FDF2F8", outline="#FBCFE8")
    draw.text((60, 558), "🔁 Continuous Hands-Free Cycle: Hey Vaani ➔ Fixed Greeting ➔ Spoken Question ➔ Answer & Speak ➔ Automatically Returns to Standby ➔ Ready for Next \"Hey Vaani\"", font=font_body_b, fill="#97144D")

    out_file = REPORTS_DIR / "voice_siri_continuous_flow.png"
    img.save(out_file, "PNG")
    print(f"Saved: {out_file}")


if __name__ == "__main__":
    for is_open in [False, True]:
        render_overview(is_open)
        render_accounts(is_open)
        render_transactions(is_open)
        render_cards(is_open)
        render_loans(is_open)
    render_voice_continuous_siri_flow()
    print("All comparison and voice flow renders generated successfully!")
