#!/usr/bin/env python3
"""
Buy Money Studios @ Prauper — Public Booking Server
Public booking form + admin dashboard + engineer tracking + Intake Sheet sync.

Usage:
    python3 prauper_booking_server.py                    # localhost:8088
    python3 prauper_booking_server.py 8088 0.0.0.0       # public-facing

Data: ~/.hermes/prauper-studios/bookings/bookings.json
Intake Sheet: 11blhZ_y_TJ2UBY6eZvaGRFumwaleyyvIvdQsx2QwV8E
"""

import json
import os
import sys
import uuid
import smtplib
import ssl
import stripe
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# --- Config ---
DATA_DIR = Path("/Users/buymoney/.hermes/prauper-studios/bookings")
DATA_DIR.mkdir(parents=True, exist_ok=True)
BOOKINGS_FILE = DATA_DIR / "bookings.json"
ENGINEERS_FILE = DATA_DIR / "engineers.json"

SMTP_USER = "jackfrancis979@gmail.com"
SMTP_PASS_FILE = Path("/Users/buymoney/.config/himalaya/.gmail-pass")
NOTIFY_TO = ["emmanuel@prauper.com", "jackfrancis979@gmail.com", "buymoneyent@gmail.com"]

STUDIO_ADDRESS = "3914 Fairhill Dr, Houston, TX"
BRAND_NAME = "Buy Money Studios @ Prauper"
INTAKE_SHEET_ID = "11blhZ_y_TJ2UBY6eZvaGRFumwaleyyvIvdQsx2QwV8E"
GOOGLE_TOKEN = Path("/Users/buymoney/.hermes/google_token.json")

# --- Stripe ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# --- Data Helpers ---

def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def load_bookings():
    return load_json(BOOKINGS_FILE, {"bookings": []})

def save_bookings(data):
    save_json(BOOKINGS_FILE, data)

def load_engineers():
    return load_json(ENGINEERS_FILE, {"engineers": []})

def save_engineers(data):
    save_json(ENGINEERS_FILE, data)

def get_smtp_pass():
    if SMTP_PASS_FILE.exists():
        return SMTP_PASS_FILE.read_text().strip()
    return os.environ.get("SMTP_PASS", "")

# --- Google Sheets ---

def get_sheets_service():
    """Get Google Sheets API service."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        with open(GOOGLE_TOKEN) as f:
            t = json.load(f)

        creds = Credentials(
            token=t.get("token"),
            refresh_token=t.get("refresh_token"),
            token_uri=t.get("token_uri"),
            client_id=t.get("client_id"),
            client_secret=t.get("client_secret")
        )
        creds.refresh(Request())
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"⚠️  Google Sheets auth failed: {e}")
        return None

def read_intake_sheet():
    """Read all rows from the Recording Studio Intake sheet."""
    service = get_sheets_service()
    if not service:
        return []

    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=INTAKE_SHEET_ID,
            range="Sessions!A1:O200"
        ).execute()
        rows = result.get("values", [])
        if len(rows) <= 1:
            return []
        headers = rows[0]
        sessions = []
        for row in rows[1:]:
            if not row or not row[0]:
                continue
            session = {}
            for i, h in enumerate(headers):
                session[h] = row[i] if i < len(row) else ""
            sessions.append(session)
        return sessions
    except Exception as e:
        print(f"⚠️  Intake Sheet read failed: {e}")
        return []

def append_to_intake_sheet(row_data):
    """Append a row to the Intake Sheet."""
    service = get_sheets_service()
    if not service:
        return False

    try:
        service.spreadsheets().values().append(
            spreadsheetId=INTAKE_SHEET_ID,
            range="Sessions!A:O",
            valueInputOption="USER_ENTERED",
            body={"values": [row_data]}
        ).execute()
        print(f"✅ Appended to Intake Sheet: {row_data[:3]}...")
        return True
    except Exception as e:
        print(f"⚠️  Intake Sheet append failed: {e}")
        return False

def calc_intake_row(booking):
    """Convert a booking to an Intake Sheet row (A-O)."""
    date_str = booking["preferred_date"]
    # Format date as MM/DD/YYYY
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_str = d.strftime("%m/%d/%Y")
    except:
        pass

    artist = booking["artist_name"]
    session_type = booking.get("session_type", "Recording")
    start = booking["start_time"]
    end = booking.get("end_time", "")
    hours = booking.get("hours", 0)
    rate = 60 if "Engineer" in session_type and "No" not in session_type else 60
    total = booking.get("total_estimate", 0)
    engineer = booking.get("engineer", artist)
    eng_pay = round(total * 0.5, 2) if "No Engineer" not in session_type else 0
    prauper_cut = total - eng_pay
    stockz_share = round(prauper_cut * 0.5, 2)
    emmanuel_share = round(prauper_cut * 0.5, 2)

    return [
        date_str,           # A: Date
        artist,             # B: Artist/Client
        session_type,       # C: Session Type
        start,              # D: Start Time
        end,                # E: End Time
        f"{hours:.2f}",     # F: Hours
        f"${rate:.2f}",     # G: Rate
        f"${total:.2f}",    # H: Total Paid
        engineer,           # I: Engineer
        "",                 # J: Notes
        f"${eng_pay:.2f}",  # K: Engineer Pay
        f"${prauper_cut:.2f}",  # L: Prauper Cut
        f"${stockz_share:.2f}", # M: Stockz Share
        f"${emmanuel_share:.2f}",# N: Emmanuel Share
        ""                  # O: Paid
    ]

# --- Email ---

def send_email(subject, html_body, plain_body, to_addrs, from_addr=SMTP_USER):
    password = get_smtp_pass()
    if not password:
        print("⚠️  No SMTP password found — email not sent")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Buy Money Studios <{from_addr}>"
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(from_addr, password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
        print(f"✅ Email sent: {subject} → {to_addrs}")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False

def send_booking_notification(booking):
    artist = booking["artist_name"]
    summary = booking.get("session_summary", "")
    session_type = booking["session_type"]
    hours = booking.get("hours", 0)
    total = booking.get("total_estimate", 0)
    engineer = booking.get("engineer", "TBD")

    subject = f"🎙️ New Booking — {artist}"
    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
<h2 style="color:#1A1A1A;">New Booking Request</h2>
<table style="width:100%;border-collapse:collapse;margin:15px 0;">
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Artist</td><td style="padding:8px;border-bottom:1px solid #eee;">{artist}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Email</td><td style="padding:8px;border-bottom:1px solid #eee;">{booking.get('email','N/A')}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Phone</td><td style="padding:8px;border-bottom:1px solid #eee;">{booking.get('phone','N/A')}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Date</td><td style="padding:8px;border-bottom:1px solid #eee;">{booking['preferred_date']}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Time</td><td style="padding:8px;border-bottom:1px solid #eee;">{summary}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Session Type</td><td style="padding:8px;border-bottom:1px solid #eee;">{session_type}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Engineer</td><td style="padding:8px;border-bottom:1px solid #eee;">{engineer}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Est. Total</td><td style="padding:8px;border-bottom:1px solid #eee;">${total}</td></tr>
</table>
<p><strong>Project:</strong> {booking.get('project_details','None provided')}</p>
<p style="margin-top:20px;"><a href="http://localhost:8088/admin" style="background:#1A1A1A;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;">Review in Admin</a></p>
<p style="color:#666;font-size:12px;">— {BRAND_NAME}</p>
</body></html>"""
    plain = f"New Booking: {artist} | {booking['preferred_date']} | {summary} | {session_type} | Engineer: {engineer} | ${total}"
    return send_email(subject, html, plain, NOTIFY_TO)

def send_confirmation_email(booking):
    artist = booking["artist_name"]
    email = booking.get("email", "")
    if not email:
        return False
    summary = booking.get("session_summary", "")
    total = booking.get("total_estimate", 0)

    subject = f"✅ Booking Confirmed — {BRAND_NAME}"
    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
<h2>Booking Confirmed ✅</h2>
<p>Hi <strong>{artist}</strong>,</p>
<p>Your session at <strong>{BRAND_NAME}</strong> is confirmed!</p>
<table style="width:100%;border-collapse:collapse;margin:15px 0;">
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Date</td><td style="padding:8px;border-bottom:1px solid #eee;">{booking['preferred_date']}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Time</td><td style="padding:8px;border-bottom:1px solid #eee;">{summary}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Location</td><td style="padding:8px;border-bottom:1px solid #eee;">{STUDIO_ADDRESS}</td></tr>
<tr><td style="padding:8px;font-weight:bold;border-bottom:1px solid #eee;">Total</td><td style="padding:8px;border-bottom:1px solid #eee;">${total}</td></tr>
</table>
<p>Invoice will be sent via Shopify.</p>
<p style="color:#666;font-size:12px;">— {BRAND_NAME} · {STUDIO_ADDRESS}</p>
</body></html>"""
    plain = f"Booking Confirmed! {artist} | {booking['preferred_date']} | {summary} | ${total}"
    return send_email(subject, html, plain, [email])

# --- Pricing ---

def calc_hours(start_time, end_time):
    def to_minutes(t):
        parts = t.strip().split()
        h, m = map(int, parts[0].split(":"))
        if parts[1] == "PM" and h != 12: h += 12
        elif parts[1] == "AM" and h == 12: h = 0
        return h * 60 + m
    sm, em = to_minutes(start_time), to_minutes(end_time)
    if em <= sm: em += 24 * 60
    return (em - sm) / 60

def calc_price(session_type, hours):
    if session_type == "Hourly with Engineer":
        return round(hours * 60, 2)
    elif session_type == "No Engineer (4 Hours)":
        base = 150
        if hours <= 4: return base
        return round(base + (hours - 4) * 37.50, 2)
    elif session_type == "No Engineer (8 Hours)":
        base = 200
        if hours <= 8: return base
        return round(base + (hours - 8) * 25, 2)
    return round(hours * 60, 2)


# ============================
# PUBLIC BOOKING FORM
# ============================

BOOKING_FORM_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Buy Money Studios @ Prauper — Book a Session</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; }
  .hero { text-align: center; padding: 60px 20px 30px; }
  .hero h1 { font-size: 2rem; font-weight: 700; color: #fff; }
  .hero .sub { color: #888; margin-top: 6px; }
  .hero .addr { color: #666; font-size: 0.85rem; margin-top: 4px; }
  .container { max-width: 560px; margin: 0 auto; padding: 0 20px 60px; }
  .card { background: #141414; border: 1px solid #222; border-radius: 12px; padding: 32px; }
  label { display: block; font-size: 0.8rem; font-weight: 600; color: #999; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; margin-top: 20px; }
  label:first-child { margin-top: 0; }
  input, select, textarea { width: 100%; padding: 12px 14px; background: #1a1a1a; border: 1px solid #333; border-radius: 8px; color: #fff; font-size: 0.95rem; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: #555; }
  textarea { resize: vertical; min-height: 80px; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .preview { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 14px; margin-top: 20px; text-align: center; font-size: 1.1rem; color: #fff; font-weight: 500; }
  .preview span { color: #888; font-weight: 400; font-size: 0.85rem; display: block; margin-bottom: 4px; }
  .price-display { background: #0f2a0f; border: 1px solid #225522; border-radius: 8px; padding: 14px; margin-top: 12px; text-align: center; }
  .price-display .price-main { font-size: 1.5rem; color: #34d399; font-weight: 700; }
  .price-display .price-breakdown { font-size: 0.8rem; color: #666; margin-top: 4px; }
  .btn { display: block; width: 100%; padding: 14px; background: #fff; color: #000; border: none; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; margin-top: 28px; }
  .btn:hover { opacity: 0.85; }
  .footer { text-align: center; color: #555; font-size: 0.75rem; padding: 20px; }
  .fine-print { color: #555; font-size: 0.78rem; margin-top: 16px; line-height: 1.5; border-top: 1px solid #222; padding-top: 14px; }
  .fine-print strong { color: #888; }
  .gear-section { margin-top: 30px; }
  .gear-section h2 { font-size: 1rem; color: #fff; margin-bottom: 12px; }
  .gear-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .gear-item { background: #141414; border: 1px solid #222; border-radius: 8px; padding: 10px 12px; display: flex; align-items: flex-start; gap: 8px; }
  .gear-icon { font-size: 1.1rem; flex-shrink: 0; }
  .gear-name { font-size: 0.82rem; color: #ccc; font-weight: 500; line-height: 1.3; }
  .session-type-cards { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
  .type-card { background: #1a1a1a; border: 2px solid #333; border-radius: 10px; padding: 14px 16px; cursor: pointer; transition: all 0.2s; display: flex; justify-content: space-between; align-items: center; }
  .type-card:hover { border-color: #555; }
  .type-card.selected { border-color: #fff; background: #1f1f1f; }
  .type-card .type-name { font-weight: 600; color: #fff; font-size: 0.95rem; }
  .type-card .type-price { font-weight: 700; color: #34d399; font-size: 1.05rem; }
  .type-card .type-desc { color: #666; font-size: 0.8rem; margin-top: 2px; }
  @media (max-width: 480px) { .row { flex-direction: column; gap: 0; } .hero h1 { font-size: 1.5rem; } .gear-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="hero">
  <h1>Buy Money Studios @ Prauper</h1>
  <p class="sub">Recording Studio — Houston, TX</p>
  <p class="addr">3914 Fairhill Dr</p>
</div>
<div class="container">
  <div class="card">
    <form id="bookingForm" onsubmit="return submitBooking(event)">
      <label>Artist / Stage Name *</label>
      <input type="text" id="artist_name" required placeholder="Your name or stage name">
      <label>Email *</label>
      <input type="email" id="email" required placeholder="you@email.com">
      <div class="row">
        <div><label>Phone</label><input type="tel" id="phone" placeholder="(713) 555-0000"></div>
        <div><label>Instagram</label><input type="text" id="instagram" placeholder="@yourhandle"></div>
      </div>
      <label>Preferred Date *</label>
      <input type="date" id="preferred_date" required>
      <label>Session Type *</label>
      <div class="session-type-cards">
        <div class="type-card" onclick="selectType(this,'Hourly with Engineer')" data-type="Hourly with Engineer">
          <div><div class="type-name">Hourly with Engineer</div><div class="type-desc">Studio engineer included</div></div>
          <div class="type-price">$60/hr</div>
        </div>
        <div class="type-card" onclick="selectType(this,'No Engineer (4 Hours)')" data-type="No Engineer (4 Hours)">
          <div><div class="type-name">No Engineer — 4 Hour Block</div><div class="type-desc">BYOL · Extra hrs $37.50</div></div>
          <div class="type-price">$150</div>
        </div>
        <div class="type-card" onclick="selectType(this,'No Engineer (8 Hours)')" data-type="No Engineer (8 Hours)">
          <div><div class="type-name">No Engineer — 8 Hour Block</div><div class="type-desc">BYOL · Extra hrs $25</div></div>
          <div class="type-price">$200</div>
        </div>
      </div>
      <input type="hidden" id="session_type" required>

      <div id="engineerSection" style="display:none;margin-top:20px;">
        <label>Select Engineer *</label>
        <select id="engineer" onchange="updatePreview()">
          <option value="">Choose engineer...</option>
          <option value="MadVylan">MadVylan</option>
          <option value="Bro Dini">Bro Dini</option>
          <option value="Let Us Pick">Let Us Pick</option>
        </select>
      </div>

      <div class="row" style="margin-top:20px;">
        <div>
          <label>Start Time *</label>
          <select id="start_time" required onchange="updatePreview()">
            <option value="">Select...</option>
            <option value="11:00 AM">11:00 AM</option><option value="12:00 PM">12:00 PM</option>
            <option value="1:00 PM">1:00 PM</option><option value="2:00 PM">2:00 PM</option>
            <option value="3:00 PM">3:00 PM</option><option value="4:00 PM">4:00 PM</option>
            <option value="5:00 PM">5:00 PM</option><option value="6:00 PM">6:00 PM</option>
            <option value="7:00 PM">7:00 PM</option><option value="8:00 PM">8:00 PM</option>
            <option value="9:00 PM">9:00 PM</option><option value="10:00 PM">10:00 PM</option>
            <option value="11:00 PM">11:00 PM</option><option value="12:00 AM">12:00 AM</option>
          </select>
        </div>
        <div>
          <label>End Time *</label>
          <select id="end_time" required onchange="updatePreview()">
            <option value="">Select...</option>
            <option value="12:00 PM">12:00 PM</option><option value="1:00 PM">1:00 PM</option>
            <option value="2:00 PM">2:00 PM</option><option value="3:00 PM">3:00 PM</option>
            <option value="4:00 PM">4:00 PM</option><option value="5:00 PM">5:00 PM</option>
            <option value="6:00 PM">6:00 PM</option><option value="7:00 PM">7:00 PM</option>
            <option value="8:00 PM">8:00 PM</option><option value="9:00 PM">9:00 PM</option>
            <option value="10:00 PM">10:00 PM</option><option value="11:00 PM">11:00 PM</option>
            <option value="12:00 AM">12:00 AM</option><option value="1:00 AM">1:00 AM</option>
          </select>
        </div>
      </div>
      <div class="preview" id="preview" style="display:none;"><span>Session</span><div id="previewText"></div></div>
      <div class="price-display" id="priceDisplay" style="display:none;">
        <div class="price-main" id="priceText"></div>
        <div class="price-breakdown" id="priceBreakdown"></div>
      </div>
      <label>Tell Us About Your Project</label>
      <textarea id="project_details" placeholder="What are you working on?"></textarea>
      <div class="fine-print">
        <strong>Pricing:</strong> Hourly w/ Engineer — $60/hr. No Engineer 4-Hr — $150 + $37.50/hr extra. No Engineer 8-Hr — $200 + $25/hr extra.<br><br>
        <strong>⚠️ No Engineer:</strong> Bring your own laptop with <strong>Universal Audio drivers pre-installed</strong>. Connect to our hardware.<br><br>
        <strong>📋 Deposit:</strong> 50% non-refundable deposit required to confirm your date. Remaining balance due at session. Pay securely via Stripe (credit/debit card).<br><br>
        <strong>🚨 Studio Rules:</strong><br>
        🚫 No weapons on premises<br>
        🔨 You break it, you buy it<br>
        🧹 Leave the space as you found it<br>
        🥤 No drinks on the studio desk<br>
        🚭 Non-smoking studio · No shoes inside<br>
        ⏰ Clock starts at your <strong>scheduled time</strong>, not when you walk in. Please arrive <strong>15 minutes early</strong> to get situated.<br>
        ⚠️ <strong>No overtime</strong> — the minute the next hour starts, you owe for the next hour.<br>
        🚫 Sessions must not overlap or you will be charged a <strong>$50 fine</strong>.
      </div>
      <div class="gear-section">
        <h2>🎛️ Studio Gear</h2>
        <div class="gear-grid">
          <div class="gear-item"><span class="gear-icon">🎤</span><span class="gear-name">Neumann TLM 103 Condenser Mic</span></div>
          <div class="gear-item"><span class="gear-icon">🎤</span><span class="gear-name">Shure SM7dB Dynamic Vocal Mic</span></div>
          <div class="gear-item"><span class="gear-icon">🎚️</span><span class="gear-name">Universal Audio LA-610 MK2</span></div>
          <div class="gear-item"><span class="gear-icon">🔊</span><span class="gear-name">KRK S10.4 10" Subwoofer</span></div>
          <div class="gear-item"><span class="gear-icon">🎧</span><span class="gear-name">Universal Audio Apollo Twin</span></div>
          <div class="gear-item"><span class="gear-icon">🎧</span><span class="gear-name">Audio-Technica ATH-M50x</span></div>
          <div class="gear-item"><span class="gear-icon">📺</span><span class="gear-name">Smart TV + HDMI</span></div>
          <div class="gear-item"><span class="gear-icon">📷</span><span class="gear-name">Logitech Brio Livestreaming Camera</span></div>
        </div>
      </div>
      <button type="submit" class="btn" id="submitBtn">Pay 50% Deposit &amp; Book</button>
    </form>
  </div>
</div>
<div class="footer">© 2026 Buy Money Studios @ Prauper · 3914 Fairhill Dr, Houston, TX</div>
<script>
  document.getElementById('preferred_date').min = new Date().toISOString().split('T')[0];
  let selType = '';
  function selectType(el, t) {
    document.querySelectorAll('.type-card').forEach(c => c.classList.remove('selected'));
    el.classList.add('selected'); selType = t;
    document.getElementById('session_type').value = t;
    // Show engineer section only for hourly with engineer
    document.getElementById('engineerSection').style.display = t === 'Hourly with Engineer' ? 'block' : 'none';
    updatePreview();
  }
  function toMin(t) {
    const [h,m,p] = t.match(/(\\d+):(\\d+) (\\w+)/).slice(1);
    let hr = parseInt(h); if (p==='PM'&&hr!==12) hr+=12; else if (p==='AM'&&hr===12) hr=0;
    return hr*60+parseInt(m);
  }
  function fmtTime(mn) {
    let h24=Math.floor(mn/60)%24, mi=mn%60, p=h24>=12?'PM':'AM';
    let h12=h24>12?h24-12:h24===0?12:h24;
    return h12+':'+String(mi).padStart(2,'0')+' '+p;
  }
  function calcPrice(type, hrs) {
    if (type==='Hourly with Engineer') return {total:hrs*60, bd:'$60 × '+hrs+' hrs'};
    if (type==='No Engineer (4 Hours)') {
      if (hrs<=4) return {total:150, bd:'4-hr block'};
      return {total:150+(hrs-4)*37.5, bd:'$150 + '+(hrs-4)+' hrs × $37.50'};
    }
    if (type==='No Engineer (8 Hours)') {
      if (hrs<=8) return {total:200, bd:'8-hr block'};
      return {total:200+(hrs-8)*25, bd:'$200 + '+(hrs-8)+' hrs × $25'};
    }
    return {total:hrs*60, bd:''};
  }
  function updatePreview() {
    const s=document.getElementById('start_time').value, e=document.getElementById('end_time').value;
    const pv=document.getElementById('preview'), tx=document.getElementById('previewText');
    const pd=document.getElementById('priceDisplay'), pt=document.getElementById('priceText'), pb=document.getElementById('priceBreakdown');
    if (s&&e&&selType) {
      let sm=toMin(s), em=toMin(e); if(em<=sm)em+=1440;
      const hrs=(em-sm)/60; if(hrs<=0){pv.style.display='none';pd.style.display='none';return;}
      const warn=hrs<2?'<div style=\"color:#fbbf24;font-size:0.8rem;margin-top:4px;\">⚠️ Minimum 2 hours required</div>':'';
      tx.innerHTML=s+' – '+fmtTime(em)+' ('+hrs+' hrs)'+warn;
      const pr=calcPrice(selType,hrs);
      pt.innerHTML='$'+pr.total.toFixed(2); pb.innerHTML=pr.bd;
      pv.style.display='block'; pd.style.display='block';
    } else { pv.style.display='none'; pd.style.display='none'; }
  }
  async function submitBooking(e) {
    e.preventDefault();
    if(!selType){alert('Select a session type.');return;}
    const s=document.getElementById('start_time').value, en=document.getElementById('end_time').value;
    let sm=toMin(s),em=toMin(en); if(em<=sm)em+=1440; const hrs=(em-sm)/60;
    if(hrs<2){alert('Minimum booking is 2 hours. Please adjust your times.');return;}
    const btn=document.getElementById('submitBtn'); btn.disabled=true; btn.textContent='Submitting...';
    const pr=calcPrice(selType,hrs);
    const data={
      artist_name:document.getElementById('artist_name').value.trim(),
      email:document.getElementById('email').value.trim(),
      phone:document.getElementById('phone').value.trim(),
      instagram:document.getElementById('instagram').value.trim(),
      preferred_date:document.getElementById('preferred_date').value,
      start_time:s, end_time:en, hours:hrs, session_type:selType,
      engineer:selType==='Hourly with Engineer'?document.getElementById('engineer').value:'',
      session_summary:s+' – '+fmtTime(em)+' ('+hrs+' hrs)',
      total_estimate:pr.total,
      project_details:document.getElementById('project_details').value.trim()
    };
    try {
      // First try Stripe checkout
      const checkoutRes = await fetch('/api/create-checkout-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      const checkoutData = await checkoutRes.json();
      if(checkoutData.success && checkoutData.url){
        // Redirect to Stripe Checkout
        window.location.href = checkoutData.url;
        return;
      }
      // Fallback: if Stripe not configured, save booking without payment
      const res=await fetch('/api/bookings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
      const r=await res.json();
      if(r.success){
        document.getElementById('bookingForm').innerHTML='<div style="text-align:center;padding:40px 0;"><div style="font-size:3rem;margin-bottom:16px;">✅</div><h2 style="color:#fff;margin-bottom:8px;">Booking Request Received</h2><p style="color:#888;">Thanks '+data.artist_name+'! We will confirm shortly.</p><p style="color:#666;font-size:0.85rem;margin-top:16px;">Invoice via Shopify after confirmation.</p></div>';
      } else throw new Error(r.error||'Failed');
    } catch(err){ alert('Error: '+err.message); btn.disabled=false; btn.textContent='Pay 50% Deposit & Book'; }
  }
</script>
</body>
</html>
"""

# ============================
# ADMIN DASHBOARD
# ============================

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Buy Money Studios @ Prauper — Admin</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; }
  .header { padding: 24px 40px; border-bottom: 1px solid #222; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 1.3rem; color: #fff; }
  .header .badge { background: #222; color: #888; padding: 4px 12px; border-radius: 20px; font-size: 0.8rem; }
  .tabs { display: flex; gap: 0; padding: 0 40px; border-bottom: 1px solid #222; }
  .tab { padding: 12px 20px; cursor: pointer; color: #666; font-size: 0.85rem; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; }
  .tab:hover { color: #999; }
  .tab.active { color: #fff; border-bottom-color: #fff; }
  .content { padding: 24px 40px; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }
  .stat { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 16px; }
  .stat .num { font-size: 1.8rem; font-weight: 700; color: #fff; }
  .stat .label { color: #666; font-size: 0.75rem; margin-top: 2px; }
  .card { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 16px; margin-bottom: 10px; }
  .booking-row { display: flex; justify-content: space-between; align-items: flex-start; }
  .booking-row .info h3 { color: #fff; font-size: 0.95rem; margin-bottom: 4px; }
  .booking-row .info p { color: #888; font-size: 0.82rem; line-height: 1.5; }
  .booking-row .meta { text-align: right; min-width: 130px; }
  .status { display: inline-block; padding: 3px 8px; border-radius: 5px; font-size: 0.72rem; font-weight: 600; }
  .status-pending { background: #3d2e00; color: #fbbf24; }
  .status-confirmed { background: #003d1a; color: #34d399; }
  .status-declined { background: #3d0000; color: #f87171; }
  .status-completed { background: #1a1a3d; color: #818cf8; }
  .btn-sm { padding: 5px 12px; border: none; border-radius: 5px; font-size: 0.78rem; font-weight: 600; cursor: pointer; margin-left: 4px; }
  .btn-confirm { background: #22c55e; color: #fff; }
  .btn-decline { background: #ef4444; color: #fff; }
  .btn-sync { background: #3b82f6; color: #fff; }
  .btn-sm:hover { opacity: 0.85; }
  .empty { text-align: center; padding: 50px; color: #555; }
  /* Engineer styles */
  .eng-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .eng-card { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 20px; }
  .eng-card h3 { color: #fff; font-size: 1rem; margin-bottom: 10px; display: flex; justify-content: space-between; }
  .eng-stat { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #1a1a1a; font-size: 0.85rem; }
  .eng-stat .k { color: #888; }
  .eng-stat .v { color: #fff; font-weight: 600; }
  .eng-stat .v.money { color: #34d399; }
  .eng-stat .v.owed { color: #fbbf24; }
  .eng-add { background: #1a1a1a; border: 2px dashed #333; border-radius: 10px; padding: 20px; display: flex; align-items: center; justify-content: center; cursor: pointer; }
  .eng-add:hover { border-color: #555; }
  .section-title { font-size: 1rem; color: #fff; margin-bottom: 14px; font-weight: 600; }
  .sub { color: #666; font-size: 0.8rem; margin-top: 4px; }
  .intake-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  .intake-table th { text-align: left; padding: 8px; color: #666; font-weight: 600; border-bottom: 1px solid #222; font-size: 0.75rem; text-transform: uppercase; }
  .intake-table td { padding: 8px; border-bottom: 1px solid #1a1a1a; color: #ccc; }
  .intake-table tr:hover td { background: #1a1a1a; }
  @media (max-width: 768px) { .stats { grid-template-columns: repeat(2, 1fr); } .booking-row { flex-direction: column; } .booking-row .meta { text-align: left; margin-top: 8px; } .content { padding: 16px; } .header, .tabs { padding-left: 16px; padding-right: 16px; } }
</style>
</head>
<body>
<div class="header">
  <h1>Buy Money Studios @ Prauper</h1>
  <span class="badge" id="totalCount"></span>
</div>
<div class="tabs">
  <div class="tab active" onclick="switchTab('bookings',this)">📅 Bookings</div>
  <div class="tab" onclick="switchTab('engineers',this)">🎧 Engineers</div>
  <div class="tab" onclick="switchTab('intake',this)">📊 Intake Sheet</div>
</div>
<div class="content">
  <!-- BOOKINGS TAB -->
  <div id="tab-bookings">
    <div class="stats">
      <div class="stat"><div class="num" id="statTotal">0</div><div class="label">Total Bookings</div></div>
      <div class="stat"><div class="num" id="statPending">0</div><div class="label">Pending</div></div>
      <div class="stat"><div class="num" id="statConfirmed">0</div><div class="label">Confirmed</div></div>
      <div class="stat"><div class="num" id="statRevenue">$0</div><div class="label">Est. Revenue</div></div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:14px;">
      <div class="tab active" onclick="filterBookings('all',this)" style="border-bottom:none;padding:6px 14px;font-size:0.8rem;">All</div>
      <div class="tab" onclick="filterBookings('pending',this)" style="border-bottom:none;padding:6px 14px;font-size:0.8rem;">Pending</div>
      <div class="tab" onclick="filterBookings('confirmed',this)" style="border-bottom:none;padding:6px 14px;font-size:0.8rem;">Confirmed</div>
    </div>
    <div id="bookingsList"></div>
  </div>

  <!-- ENGINEERS TAB -->
  <div id="tab-engineers" style="display:none;">
    <div class="section-title">Engineer Earnings <span class="sub">— from Intake Sheet + Bookings</span></div>
    <div class="eng-grid" id="engGrid"></div>
    <div class="section-title" style="margin-top:24px;">Add Engineer</div>
    <div class="card" style="display:flex;gap:10px;align-items:flex-end;">
      <div style="flex:1;"><label style="margin:0 0 4px 0;font-size:0.75rem;">Name</label><input id="engName" placeholder="Engineer name"></div>
      <div style="flex:1;"><label style="margin:0 0 4px 0;font-size:0.75rem;">Email</label><input id="engEmail" placeholder="email@example.com"></div>
      <div style="flex:1;"><label style="margin:0 0 4px 0;font-size:0.75rem;">Phone</label><input id="engPhone" placeholder="(713) 555-0000"></div>
      <button class="btn-sm btn-confirm" onclick="addEngineer()" style="padding:10px 20px;">+ Add</button>
    </div>
  </div>

  <!-- INTAKE SHEET TAB -->
  <div id="tab-intake" style="display:none;">
    <div class="section-title">Recording Studio Intake <span class="sub">— synced from Google Sheets</span></div>
    <div id="intakeStatus" style="margin-bottom:12px;"></div>
    <div style="overflow-x:auto;">
      <table class="intake-table" id="intakeTable">
        <thead><tr>
          <th>Date</th><th>Artist</th><th>Type</th><th>Start</th><th>End</th><th>Hrs</th>
          <th>Rate</th><th>Total</th><th>Engineer</th><th>Notes</th><th>Eng Pay</th><th>Prauper Cut</th><th>Stockz</th><th>Emmanuel</th><th>Paid</th>
        </tr></thead>
        <tbody id="intakeBody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
  let allBookings=[], allEngineers=[], intakeSessions=[], currentFilter='all';

  async function loadAll() {
    const [bRes, eRes] = await Promise.all([
      fetch('/api/bookings').then(r=>r.json()),
      fetch('/api/engineers').then(r=>r.json())
    ]);
    allBookings = bRes.bookings || [];
    allEngineers = eRes.engineers || [];
    renderBookings(); renderEngineers();
    document.getElementById('totalCount').textContent = allBookings.length + ' booking'+(allBookings.length!==1?'s':'');
    // Load intake data for engineers tab
    loadIntake();
  }

  // ---- BOOKINGS ----
  function renderBookings() {
    const t=allBookings.length, p=allBookings.filter(b=>b.status==='pending').length;
    const c=allBookings.filter(b=>b.status==='confirmed').length;
    const rev=allBookings.filter(b=>b.status==='confirmed').reduce((s,b)=>s+(b.total_estimate||0),0);
    document.getElementById('statTotal').textContent=t;
    document.getElementById('statPending').textContent=p;
    document.getElementById('statConfirmed').textContent=c;
    document.getElementById('statRevenue').textContent='$'+rev.toLocaleString();
    const list=document.getElementById('bookingsList');
    let f=currentFilter==='all'?allBookings:allBookings.filter(b=>b.status===currentFilter);
    if(!f.length){list.innerHTML='<div class="empty">No bookings</div>';return;}
    list.innerHTML=f.map(b=>`
      <div class="card booking-row">
        <div class="info">
          <h3>${b.artist_name}</h3>
          <p>📅 ${b.preferred_date} · ⏰ ${b.session_summary||b.start_time+' – '+b.end_time}<br>
          🎤 ${b.session_type} · 💰 $${b.total_estimate||0} · 🎧 ${b.engineer||'TBD'}<br>
          ${b.email?'📧 '+b.email:''} ${b.phone?'· 📱 '+b.phone:''} ${b.instagram?'· 📸 '+b.instagram:''}
          ${b.project_details?'<br><em>"'+b.project_details.substring(0,100)+(b.project_details.length>100?'...':'')+'"</em>':''}</p>
        </div>
        <div class="meta">
          <span class="status status-${b.status}">${b.status.charAt(0).toUpperCase()+b.status.slice(1)}</span><br><br>
          ${b.status==='pending'?`
            <button class="btn-sm btn-confirm" onclick="confirmBooking('${b.id}')">✓ Confirm</button>
            <button class="btn-sm btn-decline" onclick="updateBooking('${b.id}','declined')">✕ Decline</button>
          `:''}
          ${b.status==='confirmed'&&!b.synced?`
            <button class="btn-sm btn-sync" onclick="syncToIntake('${b.id}')">📊 Sync to Intake</button>
          `:''}
          <button class="btn-sm btn-decline" style="margin-top:6px;background:#7f1d1d;" onclick="deleteBooking('${b.id}','${b.artist_name}')">🗑 Delete</button>
          <div style="color:#555;font-size:0.72rem;margin-top:6px;">${new Date(b.created_at).toLocaleDateString()}</div>
        </div>
      </div>
    `).join('');
  }

  function filterBookings(f, el) {
    currentFilter=f;
    el.parentElement.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    el.classList.add('active');
    renderBookings();
  }

  async function confirmBooking(id) {
    const b = allBookings.find(x=>x.id===id);
    if(b) {
      const eng = prompt('Assign engineer (leave blank for TBD):', b.engineer||'');
      if(eng!==null) b.engineer=eng;
    }
    await updateBooking(id,'confirmed');
  }

  async function updateBooking(id, status) {
    const b = allBookings.find(x=>x.id===id);
    await fetch('/api/bookings/'+id,{
      method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({status,engineer:b?b.engineer:''})
    });
    loadAll();
  }

  async function syncToIntake(id) {
    const r = await fetch('/api/bookings/'+id+'/sync',{method:'POST'});
    const d = await r.json();
    if(d.success) alert('✅ Synced to Intake Sheet!');
    else alert('❌ Sync failed: '+d.error);
    loadAll();
  }

  async function deleteBooking(id, name) {
    if(!confirm('Delete booking for ' + name + '? This cannot be undone.')) return;
    await fetch('/api/bookings/'+id,{method:'DELETE'});
    loadAll();
  }

  // ---- ENGINEERS ----
  function renderEngineers() {
    const grid=document.getElementById('engGrid');
    // Aggregate from intake sheet
    const engMap={};
    intakeSessions.forEach(s=>{
      const name=s['Engineer']||'Unknown';
      if(!engMap[name]) engMap[name]={name,sessions:0,hours:0,earned:0,paid:0};
      engMap[name].sessions++;
      engMap[name].hours+=parseFloat(s['Hours']||0);
      engMap[name].earned+=parseFloat((s['Engineer Pay']||'$0').replace('$',''))||0;
      engMap[name].paid+=(s['Paid']==='✅')?parseFloat((s['Engineer Pay']||'$0').replace('$','')):0;
    });
    // Also pull from local engineers list
    allEngineers.forEach(e=>{
      if(!engMap[e.name]) engMap[e.name]={name:e.name,email:e.email||'',phone:e.phone||'',sessions:0,hours:0,earned:0,paid:0};
    });

    const engs=Object.values(engMap);
    if(!engs.length){grid.innerHTML='<div class="empty">No engineers yet. Add one below.</div>';return;}

    grid.innerHTML=engs.map(e=>{
      const owed=e.earned-e.paid;
      return `<div class="eng-card">
        <h3>${e.name} <span style="font-size:0.75rem;color:#666;">${e.sessions} sessions</span></h3>
        <div class="eng-stat"><span class="k">Total Hours</span><span class="v">${e.hours.toFixed(1)}</span></div>
        <div class="eng-stat"><span class="k">Total Earned</span><span class="v money">$${e.earned.toFixed(2)}</span></div>
        <div class="eng-stat"><span class="k">Paid Out</span><span class="v">$${e.paid.toFixed(2)}</span></div>
        <div class="eng-stat"><span class="k">Balance Owed</span><span class="v ${owed>0?'owed':'money'}">$${owed.toFixed(2)}</span></div>
      </div>`;
    }).join('');
  }

  async function addEngineer() {
    const name=document.getElementById('engName').value.trim();
    const email=document.getElementById('engEmail').value.trim();
    const phone=document.getElementById('engPhone').value.trim();
    if(!name){alert('Enter a name');return;}
    await fetch('/api/engineers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,phone})});
    document.getElementById('engName').value='';
    document.getElementById('engEmail').value='';
    document.getElementById('engPhone').value='';
    loadAll();
  }

  // ---- INTAKE SHEET ----
  async function loadIntake() {
    const status=document.getElementById('intakeStatus');
    status.innerHTML='<span style="color:#888;">Loading from Google Sheets...</span>';
    const r=await fetch('/api/intake');
    const d=await r.json();
    intakeSessions=d.sessions||[];
    if(d.error){status.innerHTML='<span style="color:#f87171;">⚠️ '+d.error+'</span>';return;}
    status.innerHTML='<span style="color:#666;">'+intakeSessions.length+' sessions loaded</span>';
    const tbody=document.getElementById('intakeBody');
    if(!intakeSessions.length){tbody.innerHTML='<tr><td colspan="12" class="empty">No sessions</td></tr>';return;}
    tbody.innerHTML=intakeSessions.map(s=>`<tr>
      <td>${s['Date']||''}</td><td>${s['Artist/Client']||''}</td><td>${s['Session Type']||''}</td>
      <td>${s['Start Time']||''}</td><td>${s['End Time']||''}</td><td>${s['Hours']||''}</td>
      <td>${s['Rate ($/hr)']||''}</td><td>${s['Total Paid']||''}</td><td>${s['Engineer']||''}</td>
      <td>${s['Notes']||''}</td><td>${s['Engineer Pay']||''}</td><td>${s['Prauper Cut']||''}</td>
      <td>${s['Stockz Share']||''}</td><td>${s['Emmanuel Share']||''}</td><td>${s['Paid']||''}</td>
    </tr>`).join('');
    renderEngineers(); // re-render with intake data
  }

  function switchTab(tab, el) {
    document.querySelectorAll('.tabs .tab').forEach(t=>t.classList.remove('active'));
    el.classList.add('active');
    ['bookings','engineers','intake'].forEach(t=>{
      document.getElementById('tab-'+t).style.display=t===tab?'block':'none';
    });
    if(tab==='intake') loadIntake();
    if(tab==='engineers') loadIntake(); // refresh engineers with latest intake data
  }

  loadAll();
</script>
</body>
</html>
"""

# ============================
# ROUTES
# ============================

@app.route("/")
def index():
    return render_template_string(BOOKING_FORM_HTML)

@app.route("/admin")
def admin():
    return render_template_string(ADMIN_HTML)

@app.route("/health")
def health():
    data = load_bookings()
    return jsonify({"status":"ok","brand":BRAND_NAME,"bookings":len(data["bookings"]), "ts":datetime.now().isoformat()})

@app.route("/api/bookings", methods=["GET"])
def list_bookings():
    return jsonify(load_bookings())

@app.route("/api/bookings", methods=["POST"])
def create_booking():
    body = request.get_json()
    if not body: return jsonify({"error":"No data"}), 400
    required = ["artist_name","email","preferred_date","start_time","end_time","session_type"]
    missing = [f for f in required if not body.get(f)]
    if missing: return jsonify({"error":f"Missing: {', '.join(missing)}"}), 400

    hours = calc_hours(body["start_time"], body["end_time"])
    if hours <= 0: return jsonify({"error":"End time must be after start time"}), 400
    total = calc_price(body["session_type"], hours)

    booking = {
        "id": str(uuid.uuid4())[:8],
        "artist_name": body["artist_name"].strip(),
        "email": body.get("email","").strip(),
        "phone": body.get("phone","").strip(),
        "instagram": body.get("instagram","").strip(),
        "preferred_date": body["preferred_date"],
        "start_time": body["start_time"],
        "end_time": body["end_time"],
        "hours": hours,
        "session_type": body["session_type"],
        "session_summary": body.get("session_summary",""),
        "project_details": body.get("project_details","").strip(),
        "total_estimate": total,
        "engineer": body.get("engineer",""),
        "synced": False,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }

    data = load_bookings()
    data["bookings"].append(booking)
    save_bookings(data)
    try: send_booking_notification(booking)
    except Exception as e: print(f"⚠️ Email failed: {e}")
    return jsonify({"success":True,"booking":booking}), 201

@app.route("/api/bookings/<bid>", methods=["PATCH"])
def update_booking(bid):
    body = request.get_json()
    if not body: return jsonify({"error":"No data"}), 400
    data = load_bookings()
    for b in data["bookings"]:
        if b["id"]==bid:
            old=b["status"]
            b["status"]=body.get("status",b["status"])
            if "engineer" in body: b["engineer"]=body["engineer"]
            b["updated_at"]=datetime.now().isoformat()
            save_bookings(data)
            if b["status"]=="confirmed" and old!="confirmed":
                try: send_confirmation_email(b)
                except: pass
            return jsonify({"success":True,"booking":b})
    return jsonify({"error":"Not found"}), 404

@app.route("/api/bookings/<bid>", methods=["DELETE"])
def delete_booking(bid):
    data = load_bookings()
    data["bookings"]=[b for b in data["bookings"] if b["id"]!=bid]
    save_bookings(data)
    return jsonify({"success":True})

@app.route("/api/bookings/<bid>/sync", methods=["POST"])
def sync_booking_to_intake(bid):
    """Sync a confirmed booking to the Intake Sheet."""
    data = load_bookings()
    for b in data["bookings"]:
        if b["id"]==bid:
            row = calc_intake_row(b)
            ok = append_to_intake_sheet(row)
            if ok:
                b["synced"]=True
                b["updated_at"]=datetime.now().isoformat()
                save_bookings(data)
                return jsonify({"success":True})
            return jsonify({"success":False,"error":"Sheet append failed"}), 500
    return jsonify({"error":"Not found"}), 404

@app.route("/api/engineers", methods=["GET"])
def list_engineers():
    return jsonify(load_engineers())

@app.route("/api/engineers", methods=["POST"])
def add_engineer():
    body = request.get_json()
    if not body or not body.get("name"):
        return jsonify({"error":"Name required"}), 400
    eng = {
        "id": str(uuid.uuid4())[:8],
        "name": body["name"].strip(),
        "email": body.get("email","").strip(),
        "phone": body.get("phone","").strip(),
        "created_at": datetime.now().isoformat()
    }
    data = load_engineers()
    data["engineers"].append(eng)
    save_engineers(data)
    return jsonify({"success":True,"engineer":eng}), 201

@app.route("/api/engineers/<eid>", methods=["DELETE"])
def delete_engineer(eid):
    data = load_engineers()
    data["engineers"]=[e for e in data["engineers"] if e["id"]!=eid]
    save_engineers(data)
    return jsonify({"success":True})

@app.route("/api/intake")
def get_intake():
    """Proxy to Google Sheets Intake data."""
    sessions = read_intake_sheet()
    if not sessions:
        return jsonify({"sessions":[],"error":None})
    return jsonify({"sessions":sessions})


# ============================
# STRIPE CHECKOUT
# ============================

CHECKOUT_SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Payment Confirmed — Buy Money Studios @ Prauper</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .center { text-align: center; max-width: 480px; padding: 40px 20px; }
  .check { font-size: 4rem; margin-bottom: 16px; }
  h1 { color: #fff; font-size: 1.5rem; margin-bottom: 8px; }
  p { color: #888; line-height: 1.6; margin-bottom: 12px; }
  .amount { color: #34d399; font-size: 1.3rem; font-weight: 700; margin: 16px 0; }
  .note { background: #141414; border: 1px solid #222; border-radius: 10px; padding: 16px; margin-top: 20px; text-align: left; font-size: 0.85rem; color: #999; }
  .note strong { color: #fff; }
  .btn { display: inline-block; margin-top: 24px; padding: 12px 28px; background: #fff; color: #000; text-decoration: none; border-radius: 8px; font-weight: 600; }
</style>
</head>
<body>
<div class="center">
  <div class="check">✅</div>
  <h1>Deposit Paid!</h1>
  <p>Your session at <strong>Buy Money Studios @ Prauper</strong> is secured.</p>
  <div class="amount" id="amount"></div>
  <p>You will receive a confirmation email shortly with your booking details.</p>
  <div class="note">
    <strong>Next Steps:</strong><br>
    1. Watch your email for the full confirmation<br>
    2. Arrive 15 minutes before your scheduled time<br>
    3. Bring your laptop (No Engineer sessions — UA drivers required)<br>
    <br>
    <strong>Studio:</strong> 3914 Fairhill Dr, Houston, TX
  </div>
  <a href="/" class="btn">Back to Home</a>
</div>
<script>
  const params = new URLSearchParams(window.location.search);
  const amt = params.get('amount');
  if (amt) document.getElementById('amount').textContent = '$' + parseFloat(amt).toFixed(2) + ' deposit paid';
</script>
</body>
</html>
"""

CHECKOUT_CANCEL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Payment Cancelled — Buy Money Studios @ Prauper</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .center { text-align: center; max-width: 480px; padding: 40px 20px; }
  .icon { font-size: 4rem; margin-bottom: 16px; }
  h1 { color: #fff; font-size: 1.5rem; margin-bottom: 8px; }
  p { color: #888; line-height: 1.6; margin-bottom: 12px; }
  .btn { display: inline-block; margin-top: 24px; padding: 12px 28px; background: #fff; color: #000; text-decoration: none; border-radius: 8px; font-weight: 600; }
</style>
</head>
<body>
<div class="center">
  <div class="icon">💳</div>
  <h1>Payment Cancelled</h1>
  <p>Your booking request has not been submitted yet.</p>
  <p>The 50% deposit is required to confirm your session date.</p>
  <a href="/" class="btn">Try Again</a>
</div>
</body>
</html>
"""


@app.route("/success")
def checkout_success():
    return render_template_string(CHECKOUT_SUCCESS_HTML)


@app.route("/cancel")
def checkout_cancel():
    return render_template_string(CHECKOUT_CANCEL_HTML)


@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create a Stripe Checkout session for the 50% deposit."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 500

    body = request.get_json()
    if not body:
        return jsonify({"error": "No data"}), 400

    required = ["artist_name", "email", "preferred_date", "start_time", "end_time", "session_type"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    hours = calc_hours(body["start_time"], body["end_time"])
    if hours <= 0:
        return jsonify({"error": "End time must be after start time"}), 400

    total = calc_price(body["session_type"], hours)
    deposit = round(total * 0.5, 2)
    deposit_cents = int(deposit * 100)

    # Build session summary for Stripe
    summary = body.get("session_summary", "")
    artist = body["artist_name"].strip()
    session_type = body["session_type"]

    try:
        # Determine success/cancel URLs from request
        host_url = request.host_url.rstrip("/")
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=body.get("email", ""),
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"{artist} — {session_type}",
                        "description": f"{summary} · 50% deposit to confirm booking",
                    },
                    "unit_amount": deposit_cents,
                },
                "quantity": 1,
            }],
            metadata={
                "artist_name": artist,
                "email": body.get("email", ""),
                "phone": body.get("phone", ""),
                "instagram": body.get("instagram", ""),
                "preferred_date": body["preferred_date"],
                "start_time": body["start_time"],
                "end_time": body["end_time"],
                "hours": str(hours),
                "session_type": session_type,
                "session_summary": summary,
                "project_details": body.get("project_details", ""),
                "engineer": body.get("engineer", ""),
                "total_estimate": str(total),
                "deposit": str(deposit),
            },
            success_url=f"{host_url}/success?session_id={{CHECKOUT_SESSION_ID}}&amount={deposit}",
            cancel_url=f"{host_url}/cancel",
        )
        return jsonify({"success": True, "url": session.url, "session_id": session.id})
    except Exception as e:
        print(f"❌ Stripe error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook for payment confirmation."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    event = None
    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError as e:
            print(f"⚠️ Stripe webhook signature failed: {e}")
            return jsonify({"error": "Invalid signature"}), 400
    else:
        # No webhook secret — parse raw event (for dev/testing)
        try:
            event = json.loads(payload)
        except:
            return jsonify({"error": "Invalid payload"}), 400

    if event and event.get("type") == "checkout.session.completed":
        session_data = event["data"]["object"]
        meta = session_data.get("metadata", {})
        email = meta.get("email", "")
        artist = meta.get("artist_name", "Unknown")

        # Create booking record
        booking = {
            "id": str(uuid.uuid4())[:8],
            "artist_name": artist,
            "email": email,
            "phone": meta.get("phone", ""),
            "instagram": meta.get("instagram", ""),
            "preferred_date": meta.get("preferred_date", ""),
            "start_time": meta.get("start_time", ""),
            "end_time": meta.get("end_time", ""),
            "hours": float(meta.get("hours", 0)),
            "session_type": meta.get("session_type", ""),
            "session_summary": meta.get("session_summary", ""),
            "project_details": meta.get("project_details", ""),
            "total_estimate": float(meta.get("total_estimate", 0)),
            "engineer": meta.get("engineer", ""),
            "deposit_paid": float(meta.get("deposit", 0)),
            "stripe_session_id": session_data.get("id", ""),
            "synced": False,
            "status": "pending",
            "payment": "deposit_paid",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

        data = load_bookings()
        data["bookings"].append(booking)
        save_bookings(data)

        # Send notification emails
        try:
            send_booking_notification(booking)
        except Exception as e:
            print(f"⚠️ Notification email failed: {e}")

        print(f"✅ Stripe payment confirmed — {artist} — deposit ${meta.get('deposit', 0)}")

    return jsonify({"received": True})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8088
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    print(f"🎙️  {BRAND_NAME} Booking Server")
    stripe_status = "✅ Connected" if STRIPE_SECRET_KEY else "⚠️  Not configured (set STRIPE_SECRET_KEY)"
    print(f"   Public:   http://{host}:{port}/")
    print(f"   Admin:    http://{host}:{port}/admin")
    print(f"   Health:   http://{host}:{port}/health")
    print(f"   API:      http://{host}:{port}/api/bookings")
    print(f"   Stripe:   {stripe_status}")
    print()
    app.run(host=host, port=port, debug=False)
