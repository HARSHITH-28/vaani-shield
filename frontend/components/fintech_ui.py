"""Reusable design-token components and fintech UI renderers for VaaniShield 2.0.

Provides modern, light-theme fintech components with royal indigo and emerald accents.
All returned HTML strings are strictly stripped of leading indentation via textwrap.dedent
to ensure Streamlit markdown parses them as raw HTML rather than indented code blocks.
"""

from __future__ import annotations

import html
import re
import textwrap


def format_inr(amount: float | int | str | None) -> str:
    """Format an amount as Indian Rupee (e.g. ₹1,42,500.00)."""
    if amount is None or amount == "":
        return "₹0.00"
    try:
        val = float(amount)
    except (ValueError, TypeError):
        return f"₹{amount}"

    is_neg = val < 0
    val = abs(val)
    parts = f"{val:.2f}".split(".")
    integer_part = parts[0]
    decimal_part = parts[1]

    if len(integer_part) > 3:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        formatted_int = ",".join(groups) + "," + last_three
    else:
        formatted_int = integer_part

    sign = "- " if is_neg else ""
    return f"{sign}₹{formatted_int}.{decimal_part}"


def get_category_icon(category: str | None) -> str:
    """Return a modern contextual icon for transaction categories."""
    if not category:
        return "💳"
    cat = str(category).lower()
    if any(k in cat for k in ("shop", "retail", "store", "dmart", "amazon", "flipkart")):
        return "🛍️"
    if any(k in cat for k in ("food", "dine", "dining", "restaur", "zomato", "swiggy", "cafe")):
        return "☕"
    if any(k in cat for k in ("util", "bill", "electric", "water", "gas", "recharge", "broadband")):
        return "⚡"
    if any(k in cat for k in ("travel", "flight", "train", "uber", "ola", "transport", "fuel", "petrol")):
        return "🚗"
    if any(k in cat for k in ("salary", "payroll", "wage", "income", "credit", "interest")):
        return "💰"
    if any(k in cat for k in ("health", "med", "pharmacy", "doctor", "hospital")):
        return "🏥"
    if any(k in cat for k in ("entertain", "movie", "netflix", "prime", "music", "game")):
        return "🎬"
    if any(k in cat for k in ("invest", "sip", "mutual", "stock", "deposit")):
        return "📈"
    if any(k in cat for k in ("transfer", "upi", "p2p", "neft", "imps", "rtgs")):
        return "⇄"
    return "💳"


def render_balance_hero_html(
    balance: float,
    account_number: str = "VS1000001234",
    account_type: str = "SAVINGS",
    customer_name: str = "Arjun Sharma",
    is_visible: bool = True,
    monthly_inflow: float = 85000.0,
) -> str:
    """Render a premium royal indigo digital banking balance hero card without leading indentation."""
    formatted_bal = format_inr(balance) if is_visible else "₹ ••••••••"
    masked_acc = f"•••• •••• •••• {account_number[-4:]}" if len(account_number) >= 4 else account_number
    safe_name = html.escape(customer_name)
    safe_type = html.escape(account_type.upper())
    inflow_text = format_inr(monthly_inflow) if is_visible else "₹ •••••"

    raw_html = f"""
<div class="fintech-hero-card">
  <div class="hero-card-glow"></div>
  <div class="hero-card-inner">
    <div class="hero-card-top">
      <div class="hero-brand-tag">
        <span class="hero-bank-icon">🛡️</span>
        <div>
          <span class="hero-bank-title">VAANISHIELD SECURE BANKING</span>
          <span class="hero-account-badge">{safe_type} ACCOUNT · ACTIVE</span>
        </div>
      </div>
      <div class="hero-chip-icon">
        <div class="emv-chip"></div>
      </div>
    </div>
    <div class="hero-balance-section">
      <span class="hero-balance-label">TOTAL AVAILABLE BALANCE</span>
      <div class="hero-balance-row">
        <h1 class="hero-balance-amount">{formatted_bal}</h1>
      </div>
      <span class="hero-account-num">{masked_acc}</span>
    </div>
    <div class="hero-card-bottom">
      <div class="hero-holder-col">
        <span class="hero-meta-label">PRIMARY ACCOUNT HOLDER</span>
        <span class="hero-meta-val">{safe_name}</span>
      </div>
      <div class="hero-inflow-col">
        <span class="hero-meta-label">MONTHLY INFLOW</span>
        <span class="hero-inflow-val">+{inflow_text}</span>
      </div>
      <div class="hero-network-col">
        <span class="hero-rupay-tag">RuPay <i>SELECT</i></span>
      </div>
    </div>
  </div>
</div>
"""
    return textwrap.dedent(raw_html).strip()


def render_stepper_html(steps: list[str], current_index: int) -> str:
    """Render a modern 4-step fintech onboarding progress stepper without leading indentation."""
    items = []
    total = len(steps)
    for idx, name in enumerate(steps):
        is_completed = idx < current_index
        is_active = idx == current_index
        status_cls = "completed" if is_completed else ("active" if is_active else "upcoming")
        num_display = "✓" if is_completed else str(idx + 1)
        safe_name = html.escape(name)
        connector = "<div class='stepper-line'></div>" if idx < total - 1 else ""

        item_html = f"""
<div class="stepper-item {status_cls}">
  <div class="stepper-bubble">{num_display}</div>
  <div class="stepper-text">
    <span class="stepper-step-num">STEP 0{idx+1}</span>
    <span class="stepper-label">{safe_name}</span>
  </div>
  {connector}
</div>
"""
        items.append(textwrap.dedent(item_html).strip())

    joined = "".join(items)
    return textwrap.dedent(f"<div class='fintech-stepper'>{joined}</div>").strip()


def render_transaction_card_html(
    merchant: str,
    amount: float | str,
    date_str: str,
    category: str = "General",
    status: str = "SUCCESS",
    tx_type: str = "DEBIT",
    ref_id: str | None = None,
) -> str:
    """Render a single readable transaction card with category icon and status without leading indentation."""
    icon = get_category_icon(category)
    safe_merchant = html.escape(str(merchant or "Unknown Merchant"))
    safe_cat = html.escape(str(category or "General"))
    safe_status = html.escape(str(status or "SUCCESS")).upper()
    safe_date = html.escape(str(date_str or ""))

    is_credit = str(tx_type).upper() == "CREDIT"
    amt_class = "amount-credit" if is_credit else "amount-debit"
    sign_prefix = "+ " if is_credit else "- "

    try:
        amt_val = float(amount)
        formatted_amount = f"{sign_prefix}{format_inr(abs(amt_val))}"
    except (ValueError, TypeError):
        formatted_amount = f"{sign_prefix}₹{amount}"

    status_pill_cls = "status-pill-success"
    if safe_status in ("FAILED", "DECLINED"):
        status_pill_cls = "status-pill-error"
    elif safe_status in ("PENDING", "PROCESSING"):
        status_pill_cls = "status-pill-warning"

    ref_html = f"<span class='tx-ref-id'>Ref: {html.escape(ref_id[:16])}...</span>" if ref_id else ""

    raw_html = f"""
<div class="fintech-tx-row">
  <div class="tx-left">
    <div class="tx-icon-badge">{icon}</div>
    <div class="tx-details">
      <strong class="tx-merchant-name">{safe_merchant}</strong>
      <div class="tx-sub-meta">
        <span class="tx-date-text">{safe_date}</span>
        <span class="tx-dot-sep">•</span>
        <span class="tx-category-tag">{safe_cat}</span>
        {f"<span class='tx-dot-sep'>•</span>{ref_html}" if ref_html else ""}
      </div>
    </div>
  </div>
  <div class="tx-right">
    <span class="tx-amount {amt_class}">{formatted_amount}</span>
    <span class="tx-status-pill {status_pill_cls}">{safe_status}</span>
  </div>
</div>
"""
    return textwrap.dedent(raw_html).strip()


def render_compact_assistant_tx_html(
    merchant: str,
    amount: str | float,
    date_str: str = "",
    category: str = "",
    status: str = "SUCCESS",
    tx_type: str = "DEBIT",
) -> str:
    """Render a compact transaction card optimized for narrow chat panels without leading indentation."""
    icon = get_category_icon(category)
    safe_m = html.escape(str(merchant or "Merchant"))
    safe_c = html.escape(str(category or "General"))
    safe_d = html.escape(str(date_str or ""))
    safe_s = html.escape(str(status or "SUCCESS")).upper()

    is_credit = str(tx_type).upper() == "CREDIT"
    sign = "+" if is_credit else "-"
    amt_cls = "credit" if is_credit else "debit"

    try:
        amt_num = float(amount)
        formatted_amount = f"{sign}{format_inr(abs(amt_num))}"
    except (ValueError, TypeError):
        formatted_amount = f"{sign}₹{amount}"

    raw_html = f"""
<div class="chat-tx-card">
  <div class="chat-tx-top">
    <div class="chat-tx-merchant">
      <span class="chat-tx-icon">{icon}</span>
      <span class="chat-tx-name">{safe_m}</span>
    </div>
    <span class="chat-tx-amt {amt_cls}">{formatted_amount}</span>
  </div>
  <div class="chat-tx-bottom">
    <span class="chat-tx-meta">{safe_d} {f'· {safe_c}' if safe_c else ''}</span>
    <span class="chat-tx-badge status-{safe_s.lower()}">{safe_s}</span>
  </div>
</div>
"""
    return textwrap.dedent(raw_html).strip()


def _markdown_prose_to_html(prose: str) -> str:
    """Safely escape HTML and convert inline Markdown (**bold**, *italic*, headings, lists) into HTML."""
    rendered_lines: list[str] = []
    for raw_line in prose.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        is_bullet = False
        if re.match(r"^[-•*]\s+", line):
            is_bullet = True
            line = re.sub(r"^[-•*]\s+", "", line)
        elif re.match(r"^#{1,6}\s+", line):
            line = "**" + re.sub(r"^#{1,6}\s+", "", line).strip("*") + "**"
        escaped = html.escape(line)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", escaped)
        escaped = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", escaped)
        if is_bullet:
            rendered_lines.append(f"<div class='chat-prose-bullet'>• {escaped}</div>")
        else:
            rendered_lines.append(f"<div class='chat-prose-line'>{escaped}</div>")
    return "".join(rendered_lines)


def _strip_inline_markdown(cell: str) -> str:
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", str(cell or ""))
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+?)`", r"\1", cleaned)
    return cleaned.strip("*_ ").strip()


def parse_markdown_table_to_compact_cards(text: str) -> tuple[bool, str]:
    """Detect if text contains a markdown table representing transactions or financial rows.

    If so, convert into responsive compact cards so narrow chat windows NEVER wrap 1 letter per line.
    Returns (has_converted, rendered_html_or_original_text).
    """
    if "|" not in text or "---" not in text:
        return False, text

    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return False, text

    table_lines: list[str] = []
    in_table = False
    prefix_lines: list[str] = []
    suffix_lines: list[str] = []

    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            in_table = True
            table_lines.append(line)
        else:
            if not in_table:
                prefix_lines.append(line)
            else:
                suffix_lines.append(line)

    if len(table_lines) < 3:
        return False, text

    header_cols = [_strip_inline_markdown(c).lower() for c in table_lines[0].strip("|").split("|")]

    tx_keywords = ("date", "merchant", "amount", "category", "status", "type", "description", "balance")
    matches = sum(1 for kw in tx_keywords if any(kw in col for col in header_cols))
    if matches < 2:
        return False, text

    col_map: dict[str, int] = {}
    for idx, col in enumerate(header_cols):
        for kw in tx_keywords:
            if kw in col and kw not in col_map:
                col_map[kw] = idx

    cards_html = []
    for row_line in table_lines[2:]:
        cells = [_strip_inline_markdown(c) for c in row_line.strip("|").split("|")]
        if not any(cells):
            continue

        merchant = cells[col_map["merchant"]] if "merchant" in col_map and col_map["merchant"] < len(cells) else ""
        if not merchant and "description" in col_map and col_map["description"] < len(cells):
            merchant = cells[col_map["description"]]
        if not merchant:
            merchant = cells[0] if cells else "Transaction"

        amount = cells[col_map["amount"]] if "amount" in col_map and col_map["amount"] < len(cells) else "0"
        amount_clean = amount.replace("₹", "").replace(",", "").strip()

        date_val = cells[col_map["date"]] if "date" in col_map and col_map["date"] < len(cells) else ""
        category_val = cells[col_map["category"]] if "category" in col_map and col_map["category"] < len(cells) else ""
        status_val = cells[col_map["status"]] if "status" in col_map and col_map["status"] < len(cells) else "SUCCESS"
        tx_type_val = cells[col_map["type"]] if "type" in col_map and col_map["type"] < len(cells) else ("CREDIT" if "+" in str(amount) else "DEBIT")

        cards_html.append(
            render_compact_assistant_tx_html(
                merchant=merchant,
                amount=amount_clean,
                date_str=date_val,
                category=category_val,
                status=status_val,
                tx_type=tx_type_val,
            )
        )

    prefix_text = "\n".join(prefix_lines)
    suffix_text = "\n".join(suffix_lines)
    prefix_html = f"<div class='chat-text-prose'>{_markdown_prose_to_html(prefix_text)}</div>" if prefix_lines else ""
    cards_container = f"<div class='chat-tx-container'>{''.join(cards_html)}</div>"
    suffix_html = f"<div class='chat-text-prose'>{_markdown_prose_to_html(suffix_text)}</div>" if suffix_lines else ""

    result = f"{prefix_html}{cards_container}{suffix_html}"
    return True, textwrap.dedent(result).strip()
