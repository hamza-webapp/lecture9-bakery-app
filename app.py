#!/usr/bin/env python3
"""Home Bakery order tracker. Python standard library only."""

from __future__ import annotations

import csv
import html
import http.server
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "bakery.db"
HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
PRODUCTS = {
    "cupcakes": {"label": "Cupcakes (dozens)", "price": 4000, "minutes": 30},
    "cake": {"label": "Cake", "price": 12000, "minutes": 120},
}
BASE_MINUTES = 8 * 60
HELPER_MINUTES = 4 * 60
HELPER_COST = 10000


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def schema(db):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT NOT NULL UNIQUE,
            customer TEXT NOT NULL,
            product TEXT NOT NULL CHECK(product IN ('cupcakes','cake')),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            pickup_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'accepted',
            source TEXT NOT NULL DEFAULT 'Instagram',
            created_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_date TEXT NOT NULL,
            received_time TEXT NOT NULL,
            customer TEXT NOT NULL,
            message_text TEXT NOT NULL,
            request_type TEXT NOT NULL CHECK(request_type IN ('new','change','cancel')),
            order_ref TEXT NOT NULL,
            product TEXT,
            quantity INTEGER,
            pickup_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            decision_note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'Instagram'
        );
        CREATE TABLE IF NOT EXISTS helper_bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pickup_date TEXT NOT NULL UNIQUE,
            booked_on TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'booked'
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            happened_at TEXT NOT NULL,
            summary TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    db.commit()


class InstagramParser(HTMLParser):
    """Extract dated customer message bubbles from the supplied Instagram pages."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.in_title = False
        self.current = None
        self.capture = None
        self.messages = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "title":
            self.in_title = True
        if tag == "div" and "msg" in classes:
            self.current = {"meta": "", "body": ""}
        if self.current is not None:
            if tag == "div" and "meta" in classes:
                self.capture = "meta"
            elif tag == "p" and "message" in classes:
                self.capture = "body"

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "div" and self.capture == "meta":
            self.capture = None
        if tag == "p" and self.capture == "body":
            self.capture = None
            if self.current is not None and self.current.get("body", "").strip():
                self.messages.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.current is not None and self.capture:
            self.current[self.capture] += data


def parse_date_from_title(title):
    match = re.search(r"(\w+ \d{1,2} \w+ \d{4})", title)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%A %d %B %Y").date()


def parse_product(text):
    match = re.search(r"(\d+)\s*(?:dozen|dozens)\s+cupcakes", text, re.I)
    if match:
        return "cupcakes", int(match.group(1))
    match = re.search(r"(\d+)\s*cake", text, re.I)
    if match:
        return "cake", int(match.group(1))
    return None, None


def parse_message(message):
    lines = [line.strip() for line in message["body"].splitlines() if line.strip()]
    fields = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    customer = fields.get("name", "Customer").strip()
    order_line = fields.get("order", "")
    pickup_text = fields.get("pickup date", "")
    pickup = None
    if pickup_text:
        for fmt in ("%a %b %d", "%A %b %d"):
            try:
                parsed = datetime.strptime(pickup_text, fmt)
                pickup = date(2026, parsed.month, parsed.day)
                break
            except ValueError:
                continue
    if not pickup:
        return None
    received_match = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", message["meta"], re.I)
    received_time = received_match.group(1).upper() if received_match else "12:00 PM"
    if re.search(r"\bcancel\b", order_line, re.I):
        kind, product, quantity = "cancel", None, None
    elif re.search(r"\bchange\s+to\b", order_line, re.I):
        kind, product, quantity = "change", *parse_product(order_line)
    else:
        kind, product, quantity = "new", *parse_product(order_line)
    if kind != "cancel" and (not product or not quantity):
        return None
    return {
        "customer": customer,
        "kind": kind,
        "product": product,
        "quantity": quantity,
        "pickup": pickup,
        "received_time": received_time,
        "body": "\n".join(lines),
    }


def money(cents):
    return f"${cents / 100:,.2f}" if cents % 100 else f"${cents // 100:,}"


def oven_minutes(product, quantity):
    return PRODUCTS[product]["minutes"] * int(quantity)


def duration(minutes):
    hours, mins = divmod(int(minutes), 60)
    if mins:
        return f"{hours} hr {mins} min" if hours else f"{mins} min"
    return f"{hours} hr"


def order_value(product, quantity):
    return PRODUCTS[product]["price"] * int(quantity)


def log_activity(db, summary):
    db.execute("INSERT INTO activity(happened_at, summary) VALUES (?,?)", (datetime.now().isoformat(timespec="minutes"), summary))


def initialize(db):
    schema(db)
    seeded = db.execute("SELECT value FROM app_meta WHERE key='seeded'").fetchone()
    if seeded:
        return

    created = date.today().isoformat()
    csv_path = ROOT / "existing_orders.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not row.get("order", "").startswith("#"):
                    continue
                product, quantity = parse_product(row.get("item", ""))
                if not product:
                    continue
                pickup = parse_existing_pickup(row.get("pickup_day", ""))
                if not pickup:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO orders(ref,customer,product,quantity,pickup_date,status,source,created_at) VALUES (?,?,?,?,?,'accepted','Existing orders CSV',?)",
                    (row["order"].strip(), row["customer"].strip(), product, quantity, pickup.isoformat(), created),
                )

    messages = []
    for path in sorted(ROOT.glob("InstagramOrders_*.html")):
        parser = InstagramParser()
        try:
            parser.feed(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError):
            continue
        received = parse_date_from_title(parser.title)
        if not received:
            continue
        for index, raw in enumerate(parser.messages, start=1):
            parsed = parse_message(raw)
            if parsed:
                parsed["received"] = received
                parsed["source"] = path.name
                parsed["index"] = index
                messages.append(parsed)
    messages.sort(key=lambda item: (item["received"], item["received_time"]))

    # Link changes and cancellations to the customer's order for that pickup date.
    refs = {}
    for row in db.execute("SELECT ref, customer, pickup_date FROM orders"):
        refs[(row["customer"].strip().casefold(), row["pickup_date"])] = row["ref"]
    for item in messages:
        key = (item["customer"].strip().casefold(), item["pickup"].isoformat())
        ref = refs.get(key)
        if item["kind"] == "new":
            if not ref:
                slug = re.sub(r"[^A-Za-z0-9]+", "", item["customer"]).upper()[:12] or "CUSTOMER"
                ref = f"DM-{item['received']:%Y%m%d}-{slug}-{item['index']}"
            refs[key] = ref
        elif not ref:
            slug = re.sub(r"[^A-Za-z0-9]+", "", item["customer"]).upper()[:12] or "CUSTOMER"
            ref = f"DM-{item['received']:%Y%m%d}-{slug}-{item['index']}"
        db.execute(
            "INSERT INTO requests(received_date,received_time,customer,message_text,request_type,order_ref,product,quantity,pickup_date,status,source) VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
            (item["received"].isoformat(), item["received_time"], item["customer"], item["body"], item["kind"], ref, item["product"], item["quantity"], item["pickup"].isoformat(), item["source"]),
        )
    db.execute("INSERT INTO app_meta(key,value) VALUES ('seeded','1')")
    db.commit()


def parse_existing_pickup(value):
    match = re.search(r"([A-Za-z]{3})\s+(\d{1,2})\s+([A-Za-z]{3})", value)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)} 2026", "%a %d %b %Y").date()
    except ValueError:
        return None


def parse_form_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def total_for_day(db, pickup, exclude_order_id=None):
    query = "SELECT product, quantity FROM orders WHERE pickup_date=? AND status='accepted'"
    params = [pickup]
    if exclude_order_id:
        query += " AND id<>?"
        params.append(exclude_order_id)
    rows = db.execute(query, params).fetchall()
    return sum(oven_minutes(row["product"], row["quantity"]) for row in rows)


def helper_for_day(db, pickup):
    return db.execute("SELECT 1 FROM helper_bookings WHERE pickup_date=? AND status='booked'", (pickup,)).fetchone() is not None


def request_readiness(db, req):
    today = date.today()
    pickup = date.fromisoformat(req["pickup_date"])
    received = date.fromisoformat(req["received_date"])
    if pickup < today:
        return False, "Pickup date has passed. Record this request as declined or handle it outside the schedule."
    if pickup < max(today, received) + timedelta(days=2):
        return False, "Needs at least 2 days' notice from today and the message date."
    order = db.execute("SELECT * FROM orders WHERE ref=?", (req["order_ref"],)).fetchone()
    if req["request_type"] in ("change", "cancel") and (not order or order["status"] != "accepted"):
        return False, "The linked order is not currently accepted. Decide its earlier order request first."
    if req["request_type"] == "cancel":
        return True, "Cancellation meets the notice rule."
    exclude_id = order["id"] if req["request_type"] == "change" else None
    used = total_for_day(db, req["pickup_date"], exclude_id)
    needed = oven_minutes(req["product"], req["quantity"])
    proposed = used + needed
    capacity = BASE_MINUTES + (HELPER_MINUTES if helper_for_day(db, req["pickup_date"]) else 0)
    if proposed <= capacity:
        return True, f"Fits: {duration(proposed)} of {duration(capacity)} oven time, including this request."
    if proposed <= capacity + HELPER_MINUTES and not helper_for_day(db, req["pickup_date"]):
        return False, f"Needs a helper first: {duration(proposed)} would exceed the 8-hour oven day. A helper adds 4 hours for $100."
    return False, f"Does not fit: {duration(proposed)} needed, with {duration(capacity)} available."


def create_request(db, customer, product, quantity, pickup, message, kind="new", ref=None):
    now = date.today()
    if not customer or len(customer) > 80:
        raise ValueError("Enter a customer name (up to 80 characters).")
    if product not in PRODUCTS or not isinstance(quantity, int) or quantity < 1 or quantity > 100:
        raise ValueError("Choose one product and enter a quantity from 1 to 100.")
    if not pickup:
        raise ValueError("Choose a valid pickup date.")
    message = (message or "").strip()[:500]
    if kind == "new":
        safe_name = re.sub(r"[^A-Za-z0-9]+", "", customer).upper()[:12] or "CUSTOMER"
        ref = ref or f"WEB-{now:%Y%m%d}-{safe_name}-{datetime.now():%H%M%S%f}"
        message = message or f"Name: {customer}\nOrder: {quantity} {PRODUCTS[product]['label']}\nPickup Date: {pickup:%a %b %d}"
    else:
        message = message or f"{kind.title()} request for {ref}"
    db.execute(
        "INSERT INTO requests(received_date,received_time,customer,message_text,request_type,order_ref,product,quantity,pickup_date,status,source) VALUES (?,?,?,?,?,?,?,?,?,'pending','Entered in app')",
        (now.isoformat(), datetime.now().strftime("%I:%M %p"), customer, message, kind, ref, product, quantity, pickup.isoformat()),
    )
    return ref


def decide_request(db, request_id, decision):
    req = db.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
    if not req:
        raise ValueError("That message has already been handled.")
    if decision == "decline":
        db.execute("UPDATE requests SET status='declined',decision_note='Declined by baker' WHERE id=?", (request_id,))
        log_activity(db, f"Declined {req['request_type']} request from {req['customer']} for {req['pickup_date']}.")
        return
    ready, why = request_readiness(db, req)
    if not ready:
        raise ValueError(why)
    order = db.execute("SELECT * FROM orders WHERE ref=?", (req["order_ref"],)).fetchone()
    if req["request_type"] == "new":
        db.execute(
            "INSERT INTO orders(ref,customer,product,quantity,pickup_date,status,source,created_at,note) VALUES (?,?,?,?,?,'accepted',?,?,?)",
            (req["order_ref"], req["customer"], req["product"], req["quantity"], req["pickup_date"], req["source"], datetime.now().isoformat(timespec="minutes"), "Accepted from inbox"),
        )
        summary = f"Accepted {req['order_ref']} for {req['customer']} ({req['pickup_date']})."
    elif req["request_type"] == "change":
        used = total_for_day(db, req["pickup_date"], order["id"])
        needed = oven_minutes(req["product"], req["quantity"])
        available = BASE_MINUTES + (HELPER_MINUTES if helper_for_day(db, req["pickup_date"]) else 0)
        if used + needed > available:
            raise ValueError(f"The change would need {duration(used + needed)} on a {duration(available)} day.")
        db.execute("UPDATE orders SET product=?,quantity=?,note='Changed from Instagram message' WHERE id=?", (req["product"], req["quantity"], order["id"]))
        summary = f"Approved change for {order['ref']} ({req['customer']})."
    else:
        db.execute("UPDATE orders SET status='cancelled',note='Cancelled from Instagram message' WHERE id=?", (order["id"],))
        summary = f"Approved cancellation for {order['ref']} ({req['customer']})."
    db.execute("UPDATE requests SET status='accepted',decision_note=? WHERE id=?", (why, request_id))
    log_activity(db, summary)


def handle_post(db, form):
    action = form.get("action", [""])[0]
    if action == "decide":
        decide_request(db, int(form.get("request_id", ["0"])[0]), form.get("decision", [""])[0])
    elif action == "book_helper":
        pickup = parse_form_date(form.get("pickup_date", [""])[0])
        if not pickup:
            raise ValueError("Choose a valid helper date.")
        if pickup < date.today() + timedelta(days=2):
            raise ValueError("A helper must be booked at least 2 days before the pickup day.")
        if helper_for_day(db, pickup.isoformat()):
            raise ValueError("A helper is already booked for that day.")
        db.execute("INSERT INTO helper_bookings(pickup_date,booked_on,status) VALUES (?,?, 'booked') ON CONFLICT(pickup_date) DO UPDATE SET booked_on=excluded.booked_on,status='booked'", (pickup.isoformat(), date.today().isoformat()))
        log_activity(db, f"Booked a helper for {pickup.isoformat()} ($100; adds 4 oven hours).")
    elif action == "remove_helper":
        helper_id = int(form.get("helper_id", ["0"])[0])
        row = db.execute("SELECT pickup_date FROM helper_bookings WHERE id=? AND status='booked'", (helper_id,)).fetchone()
        if row:
            used = total_for_day(db, row["pickup_date"])
            if used > BASE_MINUTES:
                raise ValueError(f"Keep the helper booked: accepted orders need {duration(used)}, more than the 8-hour oven limit.")
            db.execute("UPDATE helper_bookings SET status='cancelled' WHERE id=?", (helper_id,))
            log_activity(db, f"Removed helper booking for {row['pickup_date']}.")
    elif action == "create_order":
        customer = form.get("customer", [""])[0].strip()
        product = form.get("product", [""])[0]
        try:
            quantity = int(form.get("quantity", ["0"])[0])
        except ValueError:
            quantity = 0
        pickup = parse_form_date(form.get("pickup_date", [""])[0])
        ref = create_request(db, customer, product, quantity, pickup, form.get("message", [""])[0])
        log_activity(db, f"Added a new order request from {customer} ({pickup.isoformat() if pickup else 'date needed'}).")
    elif action in ("change_order", "cancel_order"):
        order_id = int(form.get("order_id", ["0"])[0])
        order = db.execute("SELECT * FROM orders WHERE id=? AND status='accepted'", (order_id,)).fetchone()
        if not order:
            raise ValueError("That order is no longer active.")
        if action == "cancel_order":
            db.execute(
                "INSERT INTO requests(received_date,received_time,customer,message_text,request_type,order_ref,product,quantity,pickup_date,status,source) VALUES (?,?,?,?,?, ?,NULL,NULL,?,'pending','Entered in app')",
                (date.today().isoformat(), datetime.now().strftime("%I:%M %p"), order["customer"], "Cancellation requested in the app", "cancel", order["ref"], order["pickup_date"]),
            )
            log_activity(db, f"Recorded a cancellation request for {order['ref']}.")
        else:
            product = form.get("product", [""])[0]
            try:
                quantity = int(form.get("quantity", ["0"])[0])
            except ValueError:
                quantity = 0
            if product not in PRODUCTS or quantity < 1 or quantity > 100:
                raise ValueError("Choose one product and enter a quantity from 1 to 100.")
            pickup = date.fromisoformat(order["pickup_date"])
            create_request(db, order["customer"], product, quantity, pickup, "Change requested in the app", "change", order["ref"])
            log_activity(db, f"Recorded a change request for {order['ref']}.")
    elif action == "picked_up":
        order_id = int(form.get("order_id", ["0"])[0])
        row = db.execute("SELECT ref,customer,pickup_date FROM orders WHERE id=? AND status='accepted'", (order_id,)).fetchone()
        if not row:
            raise ValueError("That order is not awaiting pickup.")
        if date.fromisoformat(row["pickup_date"]) > date.today():
            raise ValueError("An order can be marked picked up on its pickup day or later.")
        db.execute("UPDATE orders SET status='picked_up' WHERE id=?", (order_id,))
        log_activity(db, f"Marked {row['ref']} picked up by {row['customer']}.")
    else:
        raise ValueError("Unknown action.")
    db.commit()


def e(value):
    return html.escape(str(value), quote=True)


def render_page(db, notice="", error=""):
    today = date.today()
    requests = db.execute("SELECT * FROM requests").fetchall()
    def received_order(req):
        try:
            clock = datetime.strptime(req["received_time"], "%I:%M %p").time()
        except ValueError:
            clock = datetime.min.time()
        return (req["received_date"], clock, req["id"])
    requests.sort(key=received_order)
    pending = [r for r in requests if r["status"] == "pending"]
    orders = db.execute("SELECT * FROM orders ORDER BY pickup_date, customer COLLATE NOCASE, ref").fetchall()
    active_orders = [o for o in orders if o["status"] == "accepted"]
    helpers = db.execute("SELECT * FROM helper_bookings ORDER BY pickup_date").fetchall()
    active_helpers = [h for h in helpers if h["status"] == "booked"]
    activity = db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 12").fetchall()
    revenue = sum(order_value(o["product"], o["quantity"]) for o in active_orders)
    products = '<option value="cupcakes">Cupcakes · $40 per dozen</option><option value="cake">Cake · $120 each</option>'

    inbox_cards = []
    for req in pending:
        ready, reason = request_readiness(db, req)
        order = db.execute("SELECT * FROM orders WHERE ref=?", (req["order_ref"],)).fetchone()
        kind_label = {"new": "New order", "change": "Change request", "cancel": "Cancellation"}[req["request_type"]]
        item_line = "Cancellation request"
        if req["request_type"] == "new":
            item_line = f"{quantity_label(req['product'], req['quantity'])} · {money(order_value(req['product'], req['quantity']))}"
        elif req["request_type"] == "change":
            item_line = f"Change to {quantity_label(req['product'], req['quantity'])}"
        source_note = "Earlier request must be accepted first." if req["request_type"] in ("change", "cancel") and not order else ""
        accept = ""
        if ready:
            accept = f'<form method="post"><input type="hidden" name="action" value="decide"><input type="hidden" name="request_id" value="{req["id"]}"><button class="button primary" name="decision" value="accept">Accept / approve</button></form>'
        else:
            accept = f'<button class="button disabled" disabled title="{e(reason)}">Accept / approve</button>'
        inbox_cards.append(
            f'''<article class="request-card">
              <div class="request-top"><div><span class="tag {e(req['request_type'])}">{e(kind_label)}</span><strong>{e(req['customer'])}</strong></div><span class="date-pill">Pickup {pretty_date(req['pickup_date'])}</span></div>
              <div class="request-item">{e(item_line)}</div><pre class="dm">{e(req['message_text'])}</pre>
              <div class="request-footer"><span class="reason {'good' if ready else 'warn'}">{e(reason if not source_note else source_note)}</span><div class="actions">{accept}<form method="post"><input type="hidden" name="action" value="decide"><input type="hidden" name="request_id" value="{req['id']}"><button class="button secondary" name="decision" value="decline">Decline</button></form></div></div>
            </article>'''
        )
    inbox_html = "".join(inbox_cards) if inbox_cards else '<div class="empty"><strong>You’re all caught up.</strong><br>New messages you add will appear here.</div>'

    days = sorted({o["pickup_date"] for o in orders if o["status"] != "cancelled"} | {h["pickup_date"] for h in active_helpers} | {r["pickup_date"] for r in pending})
    schedule_cards = []
    for pickup in days:
        scheduled = [o for o in active_orders if o["pickup_date"] == pickup]
        used = sum(oven_minutes(o["product"], o["quantity"]) for o in scheduled)
        helper = next((h for h in active_helpers if h["pickup_date"] == pickup), None)
        capacity = BASE_MINUTES + (HELPER_MINUTES if helper else 0)
        pending_count = sum(1 for r in pending if r["pickup_date"] == pickup)
        pct = min(100, round(100 * used / capacity))
        order_lines = "".join(f'<li><span><b>{e(o["customer"])}</b> <small>{e(o["ref"])}</small></span><span>{e(order_description(o))}</span><span>{duration(oven_minutes(o["product"],o["quantity"]))}</span></li>' for o in scheduled)
        if not order_lines:
            order_lines = '<li class="no-order">No accepted orders yet</li>'
        helper_line = f'<div class="helper-note">Helper booked · +4 hr · $100 <form method="post"><input type="hidden" name="action" value="remove_helper"><input type="hidden" name="helper_id" value="{helper["id"]}"><button class="link-button">Remove</button></form></div>' if helper else ""
        can_book = date.fromisoformat(pickup) >= today + timedelta(days=2) and not helper
        helper_cta = ""
        if can_book:
            helper_cta = f'<form method="post" class="inline-form"><input type="hidden" name="action" value="book_helper"><input type="hidden" name="pickup_date" value="{e(pickup)}"><button class="link-button">Book helper for this day · $100</button></form>'
        schedule_cards.append(
            f'''<article class="day-card"><div class="day-head"><div><h3>{pretty_date(pickup)}</h3><p>{len(scheduled)} accepted · {pending_count} awaiting decision</p></div><b>{duration(used)} <span>/ {duration(capacity)}</span></b></div><div class="meter"><span style="width:{pct}%"></span></div><ul class="day-orders">{order_lines}</ul>{helper_line}{helper_cta}</article>'''
        )
    schedule_html = "".join(schedule_cards) if schedule_cards else '<div class="empty">Your pickup schedule will appear here.</div>'

    order_rows = []
    for order in orders:
        status = order["status"].replace("_", " ").title()
        actions = ""
        if order["status"] == "accepted":
            actions = f'''<details><summary>Manage</summary><div class="manage-pop"><form method="post"><input type="hidden" name="action" value="change_order"><input type="hidden" name="order_id" value="{order['id']}"><label>Change to<select name="product">{product_options(order['product'])}</select></label><label>Quantity<input name="quantity" type="number" min="1" max="100" value="{order['quantity']}"></label><button class="button small primary">Request change</button></form><form method="post"><input type="hidden" name="action" value="cancel_order"><input type="hidden" name="order_id" value="{order['id']}"><button class="button small secondary">Request cancellation</button></form></div></details>'''
            if date.fromisoformat(order["pickup_date"]) <= today:
                actions += f'<form method="post" class="inline-form"><input type="hidden" name="action" value="picked_up"><input type="hidden" name="order_id" value="{order["id"]}"><button class="button small secondary">Mark picked up</button></form>'
        order_rows.append(f'<tr><td><strong>{e(order["ref"])}</strong></td><td>{e(order["customer"])}</td><td>{e(order_description(order))}</td><td>{pretty_date(order["pickup_date"])}</td><td>{money(order_value(order["product"],order["quantity"]))}</td><td>{duration(oven_minutes(order["product"],order["quantity"]))}</td><td><span class="status {e(order["status"])}">{e(status)}</span></td><td>{actions}</td></tr>')
    orders_html = "".join(order_rows) if order_rows else '<tr><td colspan="8" class="empty">No orders yet.</td></tr>'

    helper_options = "".join(f'<option value="{e((today + timedelta(days=i)).isoformat())}">{pretty_date((today + timedelta(days=i)).isoformat())}</option>' for i in range(2, 31) if not helper_for_day(db, (today + timedelta(days=i)).isoformat()))
    activity_html = "".join(f'<li><span class="activity-dot"></span><div><b>{e(row["summary"])}</b><small>{e(row["happened_at"].replace("T", " "))}</small></div></li>' for row in activity) or '<li class="empty">Your decisions will be recorded here.</li>'
    notice_html = f'<div class="flash error">{e(error)}</div>' if error else (f'<div class="flash success">{e(notice)}</div>' if notice else "")
    helper_html = f'''<section class="panel helper-panel"><div class="section-head"><div><span class="eyebrow">EXTRA CAPACITY</span><h2>Book a helper</h2><p>A helper adds 4 oven hours for $100 on one pickup day. Book at least 2 days ahead.</p></div></div><form method="post" class="helper-form"><input type="hidden" name="action" value="book_helper"><label>Pickup day<select name="pickup_date">{helper_options}</select></label><button class="button primary">Book helper · $100</button></form></section>'''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home Bakery · Order Tracker</title>
<style>
:root{{--ink:#253346;--muted:#6e7d8d;--line:#e6e9ed;--paper:#fff;--bg:#f4f6f8;--navy:#243d58;--accent:#c35f4a;--teal:#23877c;--pale:#eef6f3;--gold:#c78d32;--red:#b74747;--shadow:0 8px 24px rgba(35,55,77,.07)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:Segoe UI,Arial,sans-serif}}button,input,select{{font:inherit}}.topbar{{background:var(--navy);color:#fff;padding:22px max(24px,calc((100vw - 1240px)/2));display:flex;align-items:center;justify-content:space-between;gap:20px}}.brand{{display:flex;align-items:center;gap:14px}}.brand-mark{{width:46px;height:46px;border-radius:15px;background:#eab6a2;color:#47363a;display:grid;place-items:center;font-size:22px;font-weight:800}}.brand h1{{font-size:23px;margin:0}}.brand p{{margin:4px 0 0;color:#d8e0e8;font-size:13px}}.top-date{{text-align:right;color:#e8edf2;font-size:13px}}.top-date strong{{display:block;font-size:16px;color:white;margin-bottom:4px}}main{{max-width:1240px;margin:0 auto;padding:24px}}.notice{{margin-bottom:18px}}.flash{{padding:12px 16px;border-radius:12px;margin-bottom:16px;font-weight:600}}.flash.error{{background:#fff0ef;color:#913a35;border:1px solid #f0c7c3}}.flash.success{{background:#eaf7f0;color:#206749;border:1px solid #c6ead6}}.stat-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}}.stat{{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:17px 19px;box-shadow:var(--shadow)}}.stat span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:700}}.stat strong{{display:block;font-size:27px;margin-top:8px;color:var(--navy)}}.stat small{{display:block;color:var(--muted);font-size:12px;margin-top:3px}}.panel{{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:22px;margin-bottom:20px;box-shadow:var(--shadow)}}.section-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:16px}}.section-head h2{{font-size:19px;margin:4px 0 5px}}.section-head p{{margin:0;color:var(--muted);font-size:13px;line-height:1.5}}.eyebrow{{font-size:11px;letter-spacing:.12em;color:var(--teal);font-weight:800}}.count-badge{{border-radius:99px;background:#fbe9e4;color:#974e3e;padding:6px 10px;font-size:12px;font-weight:700;white-space:nowrap}}.request-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.request-card{{border:1px solid var(--line);border-radius:14px;padding:15px;background:#fff}}.request-top{{display:flex;align-items:center;justify-content:space-between;gap:10px}}.request-top strong{{margin-left:8px;font-size:15px}}.tag{{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.04em;border-radius:99px;padding:5px 8px;background:#eef1f5;color:#53687b}}.tag.new{{background:#e7f2fc;color:#2a6795}}.tag.change{{background:#fff3d8;color:#8a641f}}.tag.cancel{{background:#fff0ef;color:#9a4642}}.date-pill{{font-size:11px;font-weight:700;color:var(--navy);background:#f0f3f6;padding:6px 9px;border-radius:8px}}.request-item{{font-weight:700;font-size:13px;margin:13px 0 8px}}.dm{{margin:0;border-radius:10px;background:#f7f8fa;padding:10px 12px;white-space:pre-wrap;font:12px/1.5 Segoe UI,Arial,sans-serif;color:#526273;max-height:85px;overflow:auto}}.request-footer{{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin-top:13px}}.reason{{font-size:11px;line-height:1.4;max-width:60%}}.reason.good{{color:#39745e}}.reason.warn{{color:#9b6a2d}}.actions{{display:flex;gap:7px;align-items:center}}.actions form{{margin:0}}.button{{border:0;border-radius:9px;padding:9px 12px;font-size:12px;font-weight:700;cursor:pointer}}.button.primary{{background:var(--teal);color:white}}.button.primary:hover{{background:#176f66}}.button.secondary{{background:#eef1f4;color:#45576a}}.button.disabled{{background:#f1f2f3;color:#969da4;cursor:not-allowed}}.button.small{{padding:7px 9px;font-size:11px}}.helper-panel{{background:#fdfcf9}}.helper-form{{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap}}label{{display:flex;flex-direction:column;gap:6px;color:#596a7a;font-size:12px;font-weight:700}}input,select{{border:1px solid #dce2e7;background:white;border-radius:9px;padding:10px 11px;color:var(--ink);min-width:155px}}input[type=number]{{min-width:100px}}.helper-form select{{min-width:210px}}.schedule-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.day-card{{border:1px solid var(--line);border-radius:14px;padding:15px}}.day-head{{display:flex;justify-content:space-between;align-items:center;gap:10px}}.day-head h3{{margin:0 0 3px;font-size:15px}}.day-head p{{margin:0;color:var(--muted);font-size:11px}}.day-head>b{{font-size:14px;color:var(--navy);white-space:nowrap}}.day-head>b span{{color:var(--muted);font-weight:500}}.meter{{height:7px;background:#edf0f2;border-radius:9px;margin:13px 0;overflow:hidden}}.meter span{{display:block;height:100%;background:var(--teal);border-radius:9px}}.day-orders{{list-style:none;padding:0;margin:0}}.day-orders li{{display:grid;grid-template-columns:1fr 1.2fr auto;gap:8px;padding:7px 0;border-top:1px solid #f0f1f2;font-size:11px;align-items:center}}.day-orders small{{display:block;color:var(--muted);font-size:10px;margin-top:2px}}.day-orders li>span:nth-child(2){{color:#536273}}.day-orders li>span:last-child{{font-weight:700}}.day-orders .no-order{{display:block;color:var(--muted)}}.helper-note{{display:flex;align-items:center;gap:6px;font-size:11px;color:#987128;background:#fff8e7;padding:8px;border-radius:8px;margin-top:8px}}.inline-form{{display:inline;margin:0}}.link-button{{border:0;background:transparent;color:var(--teal);padding:0;font-size:11px;font-weight:700;cursor:pointer}}.order-table-wrap{{overflow-x:auto}}table.orders{{width:100%;border-collapse:collapse;min-width:850px}}.orders th,.orders td{{padding:11px 9px;border-bottom:1px solid #edf0f2;text-align:left;font-size:12px;vertical-align:middle}}.orders th{{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em}}.status{{display:inline-block;border-radius:99px;padding:5px 8px;font-size:10px;font-weight:800;background:#eaf5ef;color:#317353}}.status.cancelled,.status.rejected{{background:#fff0ef;color:#a24e48}}.status.picked_up{{background:#eaf0f8;color:#456b92}}details summary{{cursor:pointer;color:var(--teal);font-size:11px;font-weight:700}}.manage-pop{{position:relative;z-index:2;background:#fff;border:1px solid var(--line);padding:10px;border-radius:10px;box-shadow:var(--shadow);min-width:190px}}.manage-pop form{{display:flex;gap:7px;align-items:flex-end;margin:6px 0;flex-wrap:wrap}}.manage-pop select,.manage-pop input{{min-width:90px;padding:7px}}.activity-list{{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(2,1fr);gap:0 18px}}.activity-list li{{display:flex;gap:10px;padding:11px 0;border-bottom:1px solid #f0f1f2;font-size:12px}}.activity-dot{{width:8px;height:8px;border-radius:50%;background:#d89b55;flex:none;margin-top:5px}}.activity-list small{{display:block;color:var(--muted);margin-top:4px;font-size:10px}}.empty{{padding:20px;text-align:center;color:var(--muted);font-size:13px}}footer{{max-width:1240px;margin:0 auto;padding:0 24px 28px;color:var(--muted);font-size:11px}}@media(max-width:820px){{main{{padding:16px}}.stat-grid{{grid-template-columns:repeat(2,1fr)}}.request-grid,.schedule-grid{{grid-template-columns:1fr}}.topbar{{padding:18px 16px}}.request-footer{{align-items:flex-start;flex-direction:column}}.reason{{max-width:100%}}.activity-list{{grid-template-columns:1fr}}}}@media(max-width:480px){{.topbar{{align-items:flex-start}}.top-date{{font-size:11px}}.top-date strong{{font-size:13px}}.stat strong{{font-size:22px}}.panel{{padding:16px}}.request-top{{align-items:flex-start;flex-direction:column}}}}
</style></head><body>
<header class="topbar"><div class="brand"><div class="brand-mark">HB</div><div><h1>Home Bakery</h1><p>Orders, messages and oven capacity in one place</p></div></div><div class="top-date"><strong>{today.strftime('%A, %B %d, %Y')}</strong>Pickups are at 5:00 PM</div></header>
<main><div class="notice">{notice_html}</div>
<section class="stat-grid"><article class="stat"><span>Needs a decision</span><strong>{len(pending)}</strong><small>Instagram and app requests</small></article><article class="stat"><span>Accepted orders</span><strong>{len(active_orders)}</strong><small>Not cancelled or marked picked up</small></article><article class="stat"><span>Accepted order value</span><strong>{money(revenue)}</strong><small>Before any helper cost</small></article><article class="stat"><span>Helpers booked</span><strong>{len(active_helpers)}</strong><small>{money(len(active_helpers)*HELPER_COST)} total</small></article></section>
<section class="panel"><div class="section-head"><div><span class="eyebrow">INSTAGRAM INBOX</span><h2>Messages to review</h2><p>Accept or decline each order, change or cancellation. The app checks notice and oven time before accepting.</p></div><span class="count-badge">{len(pending)} waiting</span></div><div class="request-grid">{inbox_html}</div></section>
<section class="panel"><div class="section-head"><div><span class="eyebrow">ADD A DM</span><h2>Record a new order request</h2><p>Enter one product per order. It will wait for your decision before it is added to the schedule.</p></div></div><form method="post" class="helper-form"><input type="hidden" name="action" value="create_order"><label>Customer name<input name="customer" required maxlength="80" placeholder="Customer name"></label><label>Product<select name="product">{products}</select></label><label>Quantity<input name="quantity" type="number" min="1" max="100" value="1" required></label><label>Pickup date<input name="pickup_date" type="date" min="{(today+timedelta(days=2)).isoformat()}" required></label><label>Message or note<input name="message" maxlength="500" placeholder="Optional message details"></label><button class="button primary">Add to inbox</button></form></section>
{helper_html}
<section class="panel"><div class="section-head"><div><span class="eyebrow">DAILY OVEN PLAN</span><h2>Pickup schedule</h2><p>Each order is baked on its pickup day. Capacity is 8 hours, or 12 hours with a booked helper.</p></div></div><div class="schedule-grid">{schedule_html}</div></section>
<section class="panel"><div class="section-head"><div><span class="eyebrow">ORDER RECORDS</span><h2>All orders</h2><p>Accepted orders, completed pickups and decisions stay here for reference.</p></div></div><div class="order-table-wrap"><table class="orders"><thead><tr><th>Order</th><th>Customer</th><th>Product</th><th>Pickup</th><th>Value</th><th>Oven time</th><th>Status</th><th></th></tr></thead><tbody>{orders_html}</tbody></table></div></section>
<section class="panel"><div class="section-head"><div><span class="eyebrow">DECISION HISTORY</span><h2>Recent activity</h2><p>A simple record of acceptances, changes, cancellations and helper bookings.</p></div></div><ul class="activity-list">{activity_html}</ul></section>
</main><footer>The app stores its data in bakery.db in this folder. Nothing is sent to an online service.</footer>
<script>document.querySelectorAll('form').forEach(function(form){{form.addEventListener('submit',function(event){{var button=event.submitter;if(button&&button.textContent.toLowerCase().includes('cancel')){{if(!confirm('Record this cancellation request for the baker to review?'))event.preventDefault();}}}});}});</script>
</body></html>'''


def product_options(selected):
    return "".join(f'<option value="{key}"{" selected" if key==selected else ""}>{e(data["label"])}</option>' for key, data in PRODUCTS.items())


def order_description(order):
    return quantity_label(order["product"], order["quantity"])


def quantity_label(product, quantity):
    if product == "cupcakes":
        return f"{quantity} dozen cupcakes"
    return f"{quantity} cake" + ("s" if quantity != 1 else "")


def pretty_date(value):
    try:
        return date.fromisoformat(value).strftime("%a, %b %d")
    except ValueError:
        return value


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        db = connect()
        initialize(db)
        try:
            body = render_page(db).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            db.close()

    def do_POST(self):
        db = connect()
        initialize(db)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 100_000:
                raise ValueError("That form was too large.")
            raw = self.rfile.read(length).decode("utf-8", "replace")
            form = parse_qs(raw, keep_blank_values=True)
            handle_post(db, form)
            body = render_page(db, notice="Your update has been saved.").encode("utf-8")
            status = 200
        except (ValueError, sqlite3.IntegrityError) as exc:
            db.rollback()
            body = render_page(db, error=str(exc)).encode("utf-8")
            status = 400
        except Exception:
            db.rollback()
            body = render_page(db, error="Something went wrong. Your data was not changed.").encode("utf-8")
            status = 500
        finally:
            db.close()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Keep the console calm; errors appear in the page where useful.
        return


def main():
    db = connect()
    initialize(db)
    db.close()
    server = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Home Bakery is ready at http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHome Bakery server stopped.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

