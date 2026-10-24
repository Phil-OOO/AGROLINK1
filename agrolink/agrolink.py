import flet as ft
import csv
import functools
import shutil
import threading
import json
import os
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime

# Compatibility shim: some versions of flet expose Colors as ft.Colors but not ft.colors.
if not hasattr(ft, "colors"):
    ft.colors = ft.Colors

# ---------------------------------------------------------------------------
# Settings you may want to change
# ---------------------------------------------------------------------------
CURRENCY = "GH₵ "      # shown before every price, e.g. "$", "KES "
STATUS_FLOW = ["pending", "processing", "shipped", "completed", "cancelled"]
NEXT_STATUS = {"pending": "processing", "processing": "shipped", "shipped": "completed"}
TERMINAL_STATUSES = {"completed", "cancelled"}
STATUS_COLORS = {
    "pending": ft.Colors.ORANGE_700,
    "processing": ft.Colors.BLUE_700,
    "shipped": ft.Colors.PURPLE_700,
    "completed": ft.Colors.GREEN_700,
    "cancelled": ft.Colors.RED_700,
}
UNITS = ["kg", "g", "lb", "ton", "piece", "bag", "crate"]
SORT_OPTIONS = [("newest", "Newest"), ("price_low", "Price: low to high"),
                ("price_high", "Price: high to low"), ("name", "Name A-Z")]
DECIMAL_FILTER = r"^\d*\.?\d*$"
# Hosting: set AGROLINK_DATA_DIR to a persistent folder (e.g. /data) and run with PORT or AGROLINK_WEB=1
DATA_DIR = os.environ.get("AGROLINK_DATA_DIR", ".")
HOSTED = bool(os.environ.get("AGROLINK_WEB") or os.environ.get("PORT"))
ASSETS_DIR = os.path.abspath(os.path.join(DATA_DIR, "assets"))          # only this folder is served to browsers
IMAGES_DIR = os.path.join(ASSETS_DIR, "product_images") if HOSTED else None
UPLOAD_DIR = os.path.join(ASSETS_DIR, "tmp_uploads")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_OFFER_ROUNDS = 8

# Sample reference prices (replace with a real price feed later)
SAMPLE_MARKET = [
    {"product": "Maize", "price": 150, "unit": "kg", "trend": "up"},
    {"product": "Wheat", "price": 200, "unit": "kg", "trend": "down"},
    {"product": "Rice", "price": 120, "unit": "kg", "trend": "stable"},
    {"product": "Beans", "price": 300, "unit": "kg", "trend": "up"},
    {"product": "Potatoes", "price": 80, "unit": "kg", "trend": "up"},
    {"product": "Tomatoes", "price": 150, "unit": "kg", "trend": "down"},
]


def wrap_row(controls, spacing=10, run_spacing=10):
    """Wrapping row that works on older and newer Flet versions."""
    return ft.Row(controls, wrap=True, spacing=spacing, run_spacing=run_spacing)


def now_iso():
    return datetime.now().isoformat()


def money(value):
    try:
        return f"{CURRENCY}{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def num(value):
    """Format a quantity without trailing .0"""
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(value)


def full_name(user):
    return f"{user['first_name']} {user['sur_name']}" if user else "Unknown user"


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------
class AgroLinkApp:
    def __init__(self, data_dir=".", images_dir=None):
        self.data_dir = data_dir
        self.current_user = None
        self.users_file = os.path.join(data_dir, "users.json")
        self.messages_file = os.path.join(data_dir, "messages.json")
        self.products_file = os.path.join(data_dir, "products.json")
        self.orders_file = os.path.join(data_dir, "orders.json")
        self.offers_file = os.path.join(data_dir, "offers.json")
        self.images_dir = images_dir or os.path.join(data_dir, "product_images")
        os.makedirs(data_dir, exist_ok=True)
        self._init_file(self.users_file, {})
        self._init_file(self.messages_file, [])
        self._init_file(self.products_file, [])
        self._init_file(self.orders_file, [])
        self._init_file(self.offers_file, [])

    # ---- storage (atomic writes so a crash can't corrupt data) ----
    def _init_file(self, path, default):
        if not os.path.exists(path):
            self._write(path, default)

    def _read(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, type(default)) else default
        except (json.JSONDecodeError, FileNotFoundError):
            return default

    def _write(self, path, data):
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    # ---- passwords ----
    def hash_password(self, password, salt=None):
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
        return f"pbkdf2${salt}${digest}"

    def verify_password(self, password, stored):
        if stored.startswith("pbkdf2$"):
            _, salt, digest = stored.split("$")
            candidate = self.hash_password(password, salt).split("$")[2]
            return hmac.compare_digest(candidate, digest)
        legacy = hashlib.sha256(password.encode()).hexdigest()  # old unsalted hashes
        return hmac.compare_digest(legacy, stored)

    @staticmethod
    def needs_rehash(stored):
        return not stored.startswith("pbkdf2$")

    # ---- validation ----
    @staticmethod
    def validate_email(email):
        if email.count("@") != 1:
            return False
        local, domain = email.split("@")
        return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")

    @staticmethod
    def validate_password(password):
        return len(password) >= 6

    # ---- users ----
    def load_users(self):
        return self._read(self.users_file, {})

    def get_user(self, email):
        return self.load_users().get(email)

    def save_user(self, user_data):
        users = self.load_users()
        users[user_data["user_email"]] = user_data
        self._write(self.users_file, users)

    def update_user(self, email, **changes):
        users = self.load_users()
        user = users.get(email)
        if not user:
            return None
        user.update(changes)
        self._write(self.users_file, users)
        return user

    def get_users_by_category(self, category):
        return [u for u in self.load_users().values() if u["user_category"].lower() == category.lower()]

    def authenticate(self, identifier, password):
        """Log in with an email, or with 'First Surname' (as in older versions)."""
        identifier = " ".join(identifier.split()).lower()
        users = self.load_users()
        if "@" in identifier:
            candidates = [u for e, u in users.items() if e.lower() == identifier]
        else:
            candidates = [u for u in users.values() if full_name(u).lower() == identifier]
        user = next((u for u in candidates if self.verify_password(password, u["account_password"])), None)
        if not user:
            return None
        if self.needs_rehash(user["account_password"]):
            user["account_password"] = self.hash_password(password)
        user["last_login"] = now_iso()
        self.save_user(user)
        return user

    # ---- favorites ----
    @staticmethod
    def is_favorite(user, kind, item_id):
        return item_id in (user or {}).get(f"fav_{kind}", [])

    def toggle_favorite(self, email, kind, item_id):
        """kind is 'products' or 'farmers'. Returns True if now saved."""
        users = self.load_users()
        user = users[email]
        favs = user.setdefault(f"fav_{kind}", [])
        if item_id in favs:
            favs.remove(item_id)
            saved = False
        else:
            favs.append(item_id)
            saved = True
        self._write(self.users_file, users)
        return saved

    # ---- messages ----
    def load_messages(self):
        return self._read(self.messages_file, [])

    @staticmethod
    def make_message(from_email, to_email, content, msg_type="chat"):
        return {"id": uuid.uuid4().hex, "from_email": from_email, "to_email": to_email,
                "content": content, "timestamp": now_iso(), "read": False, "type": msg_type}

    def save_message(self, message_data):
        messages = self.load_messages()
        messages.append(message_data)
        self._write(self.messages_file, messages)

    def get_chat_messages(self, a, b):
        msgs = [m for m in self.load_messages()
                if (m["from_email"] == a and m["to_email"] == b) or (m["from_email"] == b and m["to_email"] == a)]
        return sorted(msgs, key=lambda m: m["timestamp"])

    def get_user_conversations(self, user_email):
        messages = self.load_messages()
        convs = {}
        for msg in messages:
            if msg["from_email"] == user_email:
                other = msg["to_email"]
            elif msg["to_email"] == user_email:
                other = msg["from_email"]
            else:
                continue
            if other not in convs or msg["timestamp"] > convs[other]["last_message"]["timestamp"]:
                convs[other] = {"last_message": msg, "unread_count": 0}
        for other, conv in convs.items():
            conv["unread_count"] = sum(
                1 for m in messages
                if m["from_email"] == other and m["to_email"] == user_email and not m.get("read", False))
        return dict(sorted(convs.items(), key=lambda kv: kv[1]["last_message"]["timestamp"], reverse=True))

    def unread_count(self, user_email):
        return sum(1 for m in self.load_messages() if m["to_email"] == user_email and not m.get("read", False))

    def mark_messages_read(self, from_email, to_email):
        messages = self.load_messages()
        changed = False
        for m in messages:
            if m["from_email"] == from_email and m["to_email"] == to_email and not m.get("read", False):
                m["read"] = True
                changed = True
        if changed:
            self._write(self.messages_file, messages)

    # ---- products ----
    def load_products(self):
        return self._read(self.products_file, [])

    def get_product(self, product_id):
        return next((p for p in self.load_products() if p["id"] == product_id), None)

    def save_product(self, product_data):
        products = self.load_products()
        products.append(product_data)
        self._write(self.products_file, products)

    def update_product(self, product_id, updated_data):
        products = self.load_products()
        for i, p in enumerate(products):
            if p["id"] == product_id:
                products[i] = {**p, **updated_data}
                break
        self._write(self.products_file, products)

    def delete_product(self, product_id):
        product = self.get_product(product_id)
        if product:
            self.delete_product_image(product.get("image"))
        self._write(self.products_file, [p for p in self.load_products() if p["id"] != product_id])

    def get_farmer_products(self, farmer_email):
        return [p for p in self.load_products() if p.get("farmer_email") == farmer_email]

    def low_stock_products(self, farmer_email):
        return [p for p in self.get_farmer_products(farmer_email)
                if p.get("min_stock", 0) > 0 and p["quantity"] <= p["min_stock"]]

    def categories(self):
        return sorted({p["category"].strip().title() for p in self.load_products() if p.get("active", True)})

    def search_products(self, query="", category="All", sort="newest", in_stock=True, users=None):
        """Returns a list of (product, farmer). Query matches name, category, farmer name or location."""
        users = users or self.load_users()
        q = query.lower().strip()
        found = []
        for p in self.load_products():
            farmer = users.get(p["farmer_email"])
            if not farmer or not p.get("active", True):
                continue
            if in_stock and p["quantity"] <= 0:
                continue
            if category and category != "All" and p["category"].strip().lower() != category.lower():
                continue
            haystack = " ".join([p["name"], p["category"], farmer.get("location", ""), full_name(farmer)]).lower()
            if q and q not in haystack:
                continue
            found.append((p, farmer))
        keys = {
            "newest": (lambda x: x[0].get("created_at", ""), True),
            "price_low": (lambda x: x[0]["price"], False),
            "price_high": (lambda x: x[0]["price"], True),
            "name": (lambda x: x[0]["name"].lower(), False),
        }
        key, reverse = keys.get(sort, keys["newest"])
        return sorted(found, key=key, reverse=reverse)

    # ---- orders ----
    def load_orders(self):
        return self._read(self.orders_file, [])

    def get_user_orders(self, user_email):
        orders = [o for o in self.load_orders() if user_email in (o["buyer_email"], o["seller_email"])]
        return sorted(orders, key=lambda o: o["created_at"], reverse=True)

    def place_order(self, product_id, buyer_email, quantity, notes="", unit_price=None, offer_id=None):
        """Returns (order, error). Re-reads the product so stock is always current."""
        product = self.get_product(product_id)
        if not product or not product.get("active", True):
            return None, "This product is no longer available"
        if product["farmer_email"] == buyer_email:
            return None, "You cannot order your own product"
        if quantity <= 0:
            return None, "Quantity must be greater than zero"
        if quantity > product["quantity"]:
            return None, f"Only {num(product['quantity'])} {product['unit']} available"
        price = product["price"] if unit_price is None else unit_price
        if price <= 0:
            return None, "Price must be greater than zero"
        ts = now_iso()
        order = {
            "id": uuid.uuid4().hex, "product_id": product["id"], "product_name": product["name"],
            "buyer_email": buyer_email, "seller_email": product["farmer_email"],
            "quantity": quantity, "unit": product["unit"], "price_per_unit": price,
            "listed_price": product["price"], "offer_id": offer_id,
            "total_price": round(quantity * price, 2), "notes": notes,
            "status": "pending", "created_at": ts, "updated_at": ts,
            "history": [{"status": "pending", "at": ts}],
        }
        orders = self.load_orders()
        orders.append(order)
        self._write(self.orders_file, orders)
        self.update_product(product["id"], {"quantity": product["quantity"] - quantity, "updated_at": ts})
        self.save_message(self.make_message(
            buyer_email, product["farmer_email"],
            f"New order: {num(quantity)} {product['unit']} of {product['name']} at {money(price)} per "
            f"{product['unit']} (total {money(order['total_price'])})", "order"))
        return order, None

    def set_order_status(self, order_id, new_status, actor_email):
        """Returns an error string or None. Sellers manage status; buyers can cancel pending orders."""
        orders = self.load_orders()
        order = next((o for o in orders if o["id"] == order_id), None)
        if not order:
            return "Order not found"
        old = order["status"]
        if old in TERMINAL_STATUSES:
            return f"A {old} order can't be changed"
        if new_status == old:
            return None
        is_seller = actor_email == order["seller_email"]
        is_buyer = actor_email == order["buyer_email"]
        if not (is_seller or is_buyer):
            return "Not allowed"
        if is_buyer and not (new_status == "cancelled" and old == "pending"):
            return "Buyers can only cancel pending orders"

        ts = now_iso()
        history = order.setdefault("history", [{"status": "pending", "at": order["created_at"]}])
        history.append({"status": new_status, "at": ts})
        order["status"] = new_status
        order["updated_at"] = ts
        self._write(self.orders_file, orders)

        if new_status == "cancelled":  # put the stock back
            product = self.get_product(order["product_id"])
            if product:
                self.update_product(product["id"], {"quantity": product["quantity"] + order["quantity"]})
        other = order["buyer_email"] if is_seller else order["seller_email"]
        self.save_message(self.make_message(
            actor_email, other, f"Order for {order['product_name']} is now {new_status}", "status_update"))
        return None

    # ---- product photos ----
    def save_product_image(self, src_path):
        """Copies a chosen image into the app's folder. Returns the stored file name."""
        ext = os.path.splitext(src_path or "")[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            raise ValueError("Please choose a PNG, JPG, GIF, WEBP or BMP image")
        if not os.path.isfile(src_path):
            raise ValueError("That file could not be found")
        if os.path.getsize(src_path) > MAX_IMAGE_BYTES:
            raise ValueError("Image is too large (maximum 5 MB)")
        os.makedirs(self.images_dir, exist_ok=True)
        name = f"{uuid.uuid4().hex}{ext}"
        shutil.copyfile(src_path, os.path.join(self.images_dir, name))
        return name

    def image_path(self, name):
        """Absolute path of a stored image, or None if there is none / it is missing."""
        if not name:
            return None
        path = os.path.abspath(os.path.join(self.images_dir, os.path.basename(name)))
        return path if os.path.isfile(path) else None

    def delete_product_image(self, name):
        path = self.image_path(name)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def price_from(self, farmer_email):
        """Lowest price among a farmer's visible, in-stock products (None if there are none)."""
        prices = [p["price"] for p in self.get_farmer_products(farmer_email)
                  if p.get("active", True) and p["quantity"] > 0]
        return min(prices) if prices else None

    # ---- offers (price negotiation) ----
    def load_offers(self):
        return self._read(self.offers_file, [])

    def get_user_offers(self, email):
        offers = [o for o in self.load_offers() if email in (o["buyer_email"], o["seller_email"])]
        offers.sort(key=lambda o: o["updated_at"], reverse=True)
        return sorted(offers, key=lambda o: o["status"] != "open")  # open offers first

    @staticmethod
    def offer_turn_email(offer):
        return offer["seller_email"] if offer["turn"] == "seller" else offer["buyer_email"]

    def offers_waiting(self, email):
        """Open offers where it is this user's turn to respond."""
        return sum(1 for o in self.load_offers() if o["status"] == "open" and self.offer_turn_email(o) == email)

    def create_offer(self, product_id, buyer_email, quantity, price, note=""):
        """Returns (offer, error). The buyer proposes a price below the listed price."""
        product = self.get_product(product_id)
        if not product or not product.get("active", True):
            return None, "This product is no longer available"
        if product["farmer_email"] == buyer_email:
            return None, "You cannot make an offer on your own product"
        if quantity <= 0 or price <= 0:
            return None, "Quantity and price must be greater than zero"
        if quantity > product["quantity"]:
            return None, f"Only {num(product['quantity'])} {product['unit']} available"
        if price >= product["price"]:
            return None, "Your offer must be below the listed price - or just place an order"
        offers = self.load_offers()
        if any(o["status"] == "open" and o["buyer_email"] == buyer_email and o["product_id"] == product_id
               for o in offers):
            return None, "You already have an open offer on this product"
        ts = now_iso()
        offer = {
            "id": uuid.uuid4().hex, "product_id": product["id"], "product_name": product["name"],
            "buyer_email": buyer_email, "seller_email": product["farmer_email"],
            "quantity": quantity, "unit": product["unit"], "listed_price": product["price"],
            "price": price, "status": "open", "turn": "seller", "order_id": None,
            "rounds": [{"by": "buyer", "price": price, "note": note, "at": ts}],
            "created_at": ts, "updated_at": ts,
        }
        offers.append(offer)
        self._write(self.offers_file, offers)
        text = (f"Offer: {num(quantity)} {product['unit']} of {product['name']} at {money(price)} per "
                f"{product['unit']} (listed {money(product['price'])})")
        self.save_message(self.make_message(buyer_email, product["farmer_email"],
                                            text + (f' - "{note}"' if note else ""), "offer"))
        return offer, None

    def respond_offer(self, offer_id, actor_email, action, price=None, note=""):
        """action: accept | reject | counter | withdraw. Returns (order, error);
        order is only set when an offer is accepted."""
        offers = self.load_offers()
        offer = next((o for o in offers if o["id"] == offer_id), None)
        if not offer:
            return None, "Offer not found"
        if actor_email not in (offer["buyer_email"], offer["seller_email"]):
            return None, "Not allowed"
        if offer["status"] != "open":
            return None, f"This offer is already {offer['status']}"
        role = "buyer" if actor_email == offer["buyer_email"] else "seller"
        other = offer["seller_email"] if role == "buyer" else offer["buyer_email"]
        name, unit = offer["product_name"], offer["unit"]
        order = None

        if action == "withdraw":
            if role != "buyer":
                return None, "Only the buyer can withdraw an offer"
            offer["status"] = "withdrawn"
            text = f"Offer withdrawn for {name}"
        else:
            if actor_email != self.offer_turn_email(offer):
                return None, "It's not your turn to respond"
            if action == "reject":
                offer["status"] = "rejected"
                text = f"Offer rejected for {name}"
            elif action == "counter":
                if price is None or price <= 0:
                    return None, "Enter a valid price"
                if len(offer["rounds"]) >= MAX_OFFER_ROUNDS:
                    return None, "Maximum rounds reached - please accept or reject"
                if price > offer["listed_price"] or (role == "buyer" and price == offer["listed_price"]):
                    return None, "Price must be below the listed price" if role == "buyer" \
                        else "Price can't be above the listed price"
                offer["rounds"].append({"by": role, "price": price, "note": note, "at": now_iso()})
                offer["price"] = price
                offer["turn"] = "buyer" if role == "seller" else "seller"
                text = f"Counter-offer for {name}: {money(price)} per {unit}"
            elif action == "accept":
                order, error = self.place_order(offer["product_id"], offer["buyer_email"], offer["quantity"],
                                                notes="Agreed through a price offer",
                                                unit_price=offer["price"], offer_id=offer["id"])
                if error:
                    return None, error
                offer["status"] = "accepted"
                offer["order_id"] = order["id"]
                text = f"Offer accepted for {name} at {money(offer['price'])} per {unit} - order created"
            else:
                return None, "Unknown action"
        offer["updated_at"] = now_iso()
        self._write(self.offers_file, offers)
        self.save_message(self.make_message(actor_email, other, text + (f' - "{note}"' if note else ""), "offer"))
        return order, None

    # ---- reviews ----
    def rate_order(self, order_id, buyer_email, stars, comment=""):
        orders = self.load_orders()
        order = next((o for o in orders if o["id"] == order_id), None)
        if not order or order["buyer_email"] != buyer_email:
            return "Order not found"
        if order["status"] != "completed":
            return "You can review an order once it is completed"
        if order.get("rating"):
            return "You already reviewed this order"
        if not 1 <= int(stars) <= 5:
            return "Choose 1 to 5 stars"
        order["rating"] = {"stars": int(stars), "comment": comment.strip(), "at": now_iso()}
        self._write(self.orders_file, orders)
        return None

    def reviews_for(self, farmer_email):
        rated = [o for o in self.load_orders() if o["seller_email"] == farmer_email and o.get("rating")]
        return sorted(rated, key=lambda o: o["rating"]["at"], reverse=True)

    def farmer_rating(self, farmer_email):
        """Returns (average, count); average is None when there are no reviews."""
        stars = [o["rating"]["stars"] for o in self.reviews_for(farmer_email)]
        return (sum(stars) / len(stars), len(stars)) if stars else (None, 0)

    # ---- reports ----
    def sales_by_product(self, farmer_email, limit=5):
        totals = {}
        for o in self.load_orders():
            if o["seller_email"] == farmer_email and o["status"] != "cancelled":
                totals[o["product_name"]] = totals.get(o["product_name"], 0) + o["total_price"]
        return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]

    def stats_for(self, user):
        email = user["user_email"]
        orders = self.get_user_orders(email)
        pending = sum(1 for o in orders if o["status"] == "pending")
        unread = self.unread_count(email)
        if user["user_category"] == "Farmer":
            revenue = sum(o["total_price"] for o in orders if o["seller_email"] == email and o["status"] == "completed")
            avg, count = self.farmer_rating(email)
            return [("Products", len(self.get_farmer_products(email))), ("Orders", len(orders)),
                    ("Pending", pending), ("Unread", unread), ("Revenue", money(revenue)),
                    ("Rating", f"{avg:.1f} ★" if avg else "-")]
        spent = sum(o["total_price"] for o in orders if o["buyer_email"] == email and o["status"] == "completed")
        saved = len(user.get("fav_products", [])) + len(user.get("fav_farmers", []))
        return [("Orders", len(orders)), ("Pending", pending), ("Unread", unread),
                ("Spent", money(spent)), ("Saved", saved)]

    def export_orders_csv(self, email, path=None):
        users = self.load_users()
        path = path or os.path.join(self.data_dir, f"agrolink_orders_{datetime.now():%Y%m%d_%H%M}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Order ID", "Date", "Product", "Quantity", "Unit", "Price per unit", "Total",
                             "Status", "Buyer", "Seller", "Notes"])
            for o in reversed(self.get_user_orders(email)):
                writer.writerow([o["id"][:8], o["created_at"][:10], o["product_name"], num(o["quantity"]),
                                 o["unit"], o["price_per_unit"], o["total_price"], o["status"],
                                 full_name(users.get(o["buyer_email"])), full_name(users.get(o["seller_email"])),
                                 o.get("notes", "")])
        return os.path.abspath(path)


DATA_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with DATA_LOCK:
            return fn(*args, **kwargs)
    return wrapper


for _name in ("save_user", "update_user", "toggle_favorite", "authenticate", "save_message",
              "mark_messages_read", "save_product", "update_product", "delete_product", "place_order",
              "set_order_status", "rate_order", "create_offer", "respond_offer"):
    setattr(AgroLinkApp, _name, _locked(getattr(AgroLinkApp, _name)))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def main(page: ft.Page):
    page.title = "AgroLink - Agricultural Trading Platform"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.GREEN)
    page.padding = 20
    page.scroll = None
    if hasattr(page, "window") and hasattr(page.window, "width"):
        page.window.width, page.window.height = 1000, 720
    else:
        page.window_width, page.window_height = 1000, 720

    app = AgroLinkApp(DATA_DIR, IMAGES_DIR)
    picker = ft.FilePicker()
    page.overlay.append(picker)
    NAV = ["home", "market", "orders", "messages", "account"]
    marketplace = {"tab": "products", "query": "", "category": "All", "sort": "newest", "instock": True}

    # ---------- shell: side navigation + content area ----------
    body = ft.Container(expand=True, padding=ft.padding.only(left=15, right=5))
    rail = ft.NavigationRail(
        selected_index=0, label_type=ft.NavigationRailLabelType.ALL, min_width=90,
        destinations=[ft.NavigationRailDestination(icon=ft.Icons.HOME, label="Home")],
        trailing=ft.IconButton(ft.Icons.LOGOUT, tooltip="Log out", icon_color=ft.Colors.RED_400,
                               on_click=lambda e: logout()),
        on_change=lambda e: go(NAV[e.control.selected_index]))
    shell = ft.Row([rail, ft.VerticalDivider(width=1), body], expand=True)

    # ---------- generic helpers ----------
    def toast(message, error=False):
        page.open(ft.SnackBar(ft.Text(message), bgcolor=ft.Colors.RED_700 if error else None))

    def me():
        return app.current_user

    def is_trader():
        return me()["user_category"] == "Trader"

    def is_farmer():
        return me()["user_category"] == "Farmer"

    def refresh_user():
        app.current_user = app.get_user(me()["user_email"])

    def build_destinations():
        market = ("My Products", ft.Icons.STOREFRONT_OUTLINED, ft.Icons.STOREFRONT) if is_farmer() else \
                 ("Marketplace", ft.Icons.SHOPPING_BAG_OUTLINED, ft.Icons.SHOPPING_BAG)
        items = [("Home", ft.Icons.HOME_OUTLINED, ft.Icons.HOME), market,
                 ("Orders", ft.Icons.RECEIPT_LONG_OUTLINED, ft.Icons.RECEIPT_LONG),
                 ("Messages", ft.Icons.CHAT_BUBBLE_OUTLINE, ft.Icons.CHAT_BUBBLE),
                 ("Account", ft.Icons.PERSON_OUTLINE, ft.Icons.PERSON)]
        return [ft.NavigationRailDestination(icon=i, selected_icon=s, label=l) for l, i, s in items]

    def refresh_rail():
        if me():
            unread = app.unread_count(me()["user_email"])
            rail.destinations[3].label = f"Messages ({unread})" if unread else "Messages"
            waiting = app.offers_waiting(me()["user_email"])
            rail.destinations[2].label = f"Orders ({waiting})" if waiting else "Orders"

    def set_nav(key):
        rail.selected_index = NAV.index(key)

    def show(*controls):
        if me():
            refresh_rail()
            body.content = ft.Column(list(controls), expand=True)
        else:
            page.controls.clear()
            page.add(*controls)
        page.update()

    def header(title, back=None, actions=None):
        row = []
        if back:
            row.append(ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Back", on_click=lambda e: back()))
        row.append(ft.Text(title, style=ft.TextThemeStyle.HEADLINE_MEDIUM, expand=True))
        row.extend(actions or [])
        return ft.Column([ft.Row(row), ft.Divider()], spacing=0)

    def view(title, content, back=None, actions=None, nav=None):
        """Standard screen: header + scrolling content."""
        if nav:
            set_nav(nav)
        show(ft.Column([header(title, back, actions),
                        ft.Column(content, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)], expand=True))

    def section(text):
        return ft.Text(text, style=ft.TextThemeStyle.TITLE_MEDIUM, weight=ft.FontWeight.BOLD)

    def empty_state(icon, title, subtitle="", action=None):
        items = [ft.Icon(icon, size=64, color=ft.Colors.GREY_400),
                 ft.Text(title, size=18, color=ft.Colors.GREY_600),
                 ft.Text(subtitle, size=14, color=ft.Colors.GREY_500)]
        if action:
            items.append(action)
        return ft.Container(content=ft.Column(items, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                            padding=40, alignment=ft.alignment.center)

    def avatar(user, radius=25, bg=ft.Colors.GREEN_600):
        initials = f"{user['first_name'][:1]}{user['sur_name'][:1]}".upper() if user else "?"
        return ft.CircleAvatar(content=ft.Text(initials), bgcolor=bg, radius=radius)

    def status_chip(status):
        return ft.Container(content=ft.Text(status.capitalize(), size=12, color=ft.Colors.WHITE),
                            bgcolor=STATUS_COLORS.get(status, ft.Colors.GREY),
                            padding=ft.padding.symmetric(horizontal=10, vertical=3), border_radius=12)

    def decimal_field(label, **kw):
        return ft.TextField(label=label, input_filter=ft.InputFilter(regex_string=DECIMAL_FILTER, allow=True),
                            keyboard_type=ft.KeyboardType.NUMBER, **kw)

    def parse_positive(field, name, required=True):
        text = field.value.strip()
        if not text:
            if required:
                field.error_text = f"{name} required"
            return None if required else 0.0
        try:
            value = float(text)
        except ValueError:
            field.error_text = f"Invalid {name.lower()}"
            return None
        if value <= 0 and required:
            field.error_text = f"{name} must be greater than zero"
            return None
        return value

    def stars_text(avg, count):
        return f"★ {avg:.1f} ({count})" if avg else "No reviews yet"

    def close(dlg):
        page.close(dlg)

    def card(controls):
        return ft.Card(content=ft.Container(content=ft.Column(controls), padding=10))

    # ---------- navigation ----------
    def go(key):
        {"home": show_dashboard,
         "market": show_farmer_products if is_farmer() else show_marketplace,
         "orders": show_trades, "messages": show_messages, "account": show_account}[key]()

    def enter_app():
        rail.destinations = build_destinations()
        rail.selected_index = 0
        page.padding = 10
        page.on_resize = None
        page.controls.clear()
        page.add(shell)
        show_dashboard()

    def logout(e=None):
        app.current_user = None
        try:
            page.client_storage.remove("agrolink_email")
        except Exception:
            pass
        page.padding = 20
        show_landing()

    # ---------- landing / auth ----------
    def show_landing():
        page.padding = 0
        W = ft.Colors.WHITE
        soft = lambda opacity: ft.Colors.with_opacity(opacity, W)
        farmers = len(app.get_users_by_category("Farmer"))
        traders = len(app.get_users_by_category("Trader"))
        listings = len([p for p in app.load_products() if p.get("active", True) and p["quantity"] > 0])

        def logo(on_dark=True, size=30):
            return ft.Row([
                ft.Container(content=ft.Icon(ft.Icons.ECO, color=ft.Colors.GREEN_800 if on_dark else W, size=size - 6),
                             width=size + 14, height=size + 14, border_radius=(size + 14) / 2, alignment=ft.alignment.center,
                             bgcolor=W if on_dark else ft.Colors.GREEN_700),
                ft.Text("AgroLink", size=size, weight=ft.FontWeight.BOLD, color=W if on_dark else ft.Colors.GREEN_800)],
                spacing=12)

        INK, FIELD, BOARD, MAIZE, PAPER, MUTED = "#1E2B22", "#1F4D32", "#173325", "#F2C14E", "#F4F7F1", "#51604F"
        faint = soft(0.16)
        all_users = app.load_users()
        live = app.search_products("", "All", "newest", True, all_users)
        if live:
            board_title = "On sale today"
            rows = [(p["name"], f.get("location") or full_name(f), f"{money(p['price'])} / {p['unit']}")
                    for p, f in live[:6]]
            footer = f"Log in to see all {len(live)} listings" if len(live) > 6 else ""
        else:
            board_title = "Sample market prices"
            rows = [(i["product"], "", f"{money(i['price'])} / {i['unit']}") for i in SAMPLE_MARKET]
            footer = "No listings yet. Sign up as a farmer to add the first one."
        if all_users:
            community = (f"{farmers} farmer{'s' if farmers != 1 else ''} and "
                         f"{traders} trader{'s' if traders != 1 else ''} already trade here.")
        else:
            community = "Join as a farmer to sell, or as a trader to buy."

        def narrow(control):  # keeps wide-screen content from stretching edge to edge
            return ft.Row([ft.Container(control, expand=4), ft.Container(expand=1)])

        board_rows = []
        for idx, (name, place, price) in enumerate(rows):
            if idx:
                board_rows.append(ft.Divider(height=1, color=faint))
            board_rows.append(ft.Container(
                ft.Row([ft.Text(name, size=18, weight=ft.FontWeight.W_500, color=W, expand=3,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(place, size=13, color=soft(0.6), expand=2,
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(price, size=18, weight=ft.FontWeight.BOLD, color=MAIZE, width=150,
                                text_align=ft.TextAlign.END)]),
                padding=ft.padding.symmetric(vertical=12)))
        board_content = [ft.Container(ft.Text(board_title, size=22, weight=ft.FontWeight.W_700, color=W),
                                      padding=ft.padding.only(bottom=10)),
                         ft.Divider(height=1, color=faint)] + board_rows
        if footer:
            board_content.append(ft.Container(ft.Text(footer, size=13, color=soft(0.7)),
                                              padding=ft.padding.only(top=14)))
        board = ft.Container(ft.Column(board_content, spacing=0), bgcolor=BOARD, border_radius=16,
                             padding=ft.padding.symmetric(horizontal=28, vertical=22))

        hero = ft.Container(
            expand=5, bgcolor=PAPER, padding=ft.padding.symmetric(horizontal=64, vertical=40),
            content=ft.Column([
                logo(on_dark=False, size=28),
                ft.Container(height=20),
                ft.Text("Buy and sell farm produce directly.", size=46, weight=ft.FontWeight.W_800, color=INK),
                narrow(ft.Text("Farmers list what they have. Traders see the price, make an offer and track "
                               "the order.", size=18, color=MUTED)),
                ft.Container(height=12),
                narrow(board),
            ], expand=True, alignment=ft.MainAxisAlignment.CENTER, scroll=ft.ScrollMode.AUTO, spacing=12))

        # ---- inline login card ----
        who = ft.TextField(label="Email or full name", prefix_icon=ft.Icons.PERSON, autofocus=True, border_radius=12)
        password = ft.TextField(label="Password", password=True, can_reveal_password=True,
                                prefix_icon=ft.Icons.LOCK, border_radius=12)
        keep = ft.Checkbox(label="Keep me signed in on this computer", value=False)

        def do_login(ev=None):
            who.error_text = password.error_text = None
            if not who.value.strip() or not password.value:
                password.error_text = "Enter your email (or full name) and password"
                page.update()
                return
            try:
                user = app.authenticate(who.value, password.value)
            except Exception as ex:
                password.error_text = f"Login error: {ex}"
                page.update()
                return
            if not user:
                password.error_text = "Incorrect email/name or password"
                page.update()
                return
            app.current_user = user
            if keep.value:
                try:
                    page.client_storage.set("agrolink_email", user["user_email"])
                except Exception:
                    pass
            enter_app()

        who.on_submit = lambda e: password.focus()
        password.on_submit = do_login
        brand_small = logo(on_dark=False, size=26)
        card_ = ft.Container(
            content=ft.Column([
                brand_small,
                ft.Text("Welcome back", size=28, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_900),
                ft.Text("Log in to manage your products, orders and offers.", color=ft.Colors.GREY_600),
                ft.Container(height=4),
                who, password, keep,
                ft.Row([ft.ElevatedButton("Log in", icon=ft.Icons.LOGIN, height=50, expand=True, on_click=do_login,
                                          style=ft.ButtonStyle(bgcolor=ft.Colors.GREEN_700, color=W,
                                                               shape=ft.RoundedRectangleBorder(radius=12)))]),
                ft.Row([ft.Container(ft.Divider(), expand=True),
                        ft.Text("New to AgroLink?", size=12, color=ft.Colors.GREY_600),
                        ft.Container(ft.Divider(), expand=True)], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Row([ft.OutlinedButton("Create a free account", icon=ft.Icons.PERSON_ADD, height=48, expand=True,
                                          on_click=open_signup,
                                          style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12)))]),
                ft.Text(community, size=12, color=ft.Colors.GREY_500,
                        text_align=ft.TextAlign.CENTER),
            ], spacing=14, tight=True),
            width=340)
        right = ft.Container(content=card_, expand=4, alignment=ft.alignment.center, padding=24, bgcolor=W,
                             border=ft.border.only(left=ft.BorderSide(1, "#DCE4D7")))

        def on_resize(e=None):
            wide = (getattr(page, "width", None) or 1000) >= 860
            hero.visible = wide
            brand_small.visible = not wide
            if e is not None:
                page.update()

        on_resize()
        page.on_resize = on_resize
        show(ft.Row([hero, right], expand=True, spacing=0))

    def open_signup(e=None):
        first = ft.TextField(label="First name", prefix_icon=ft.Icons.PERSON, width=320)
        last = ft.TextField(label="Surname", prefix_icon=ft.Icons.PERSON_OUTLINE, width=320)
        email = ft.TextField(label="Email", prefix_icon=ft.Icons.EMAIL, width=320)
        phone = ft.TextField(label="Phone", prefix_icon=ft.Icons.PHONE, width=320)
        location = ft.TextField(label="Town / region (optional)", prefix_icon=ft.Icons.LOCATION_ON, width=320)
        password = ft.TextField(label="Password (6+ characters)", password=True, can_reveal_password=True,
                                prefix_icon=ft.Icons.LOCK, width=320)
        confirm = ft.TextField(label="Confirm password", password=True, can_reveal_password=True,
                               prefix_icon=ft.Icons.LOCK_OUTLINE, width=320)
        category = ft.Dropdown(label="I am a...", width=320,
                               options=[ft.dropdown.Option("Farmer"), ft.dropdown.Option("Trader")])
        all_fields = [first, last, email, phone, location, password, confirm, category]

        def do_signup(ev=None):
            for f in all_fields:
                f.error_text = None
            data = {"first_name": first.value.strip(), "sur_name": last.value.strip(),
                    "user_email": email.value.strip().lower(), "phone_number": phone.value.strip(),
                    "location": location.value.strip(), "user_category": category.value}
            if not data["first_name"]:
                first.error_text = "First name required"
            if not data["sur_name"]:
                last.error_text = "Surname required"
            if not app.validate_email(data["user_email"]):
                email.error_text = "Valid email required"
            elif app.get_user(data["user_email"]):
                email.error_text = "Email already registered"
            if not data["phone_number"]:
                phone.error_text = "Phone required"
            if not app.validate_password(password.value):
                password.error_text = "Password must be 6+ characters"
            elif password.value != confirm.value:
                confirm.error_text = "Passwords do not match"
            if not data["user_category"]:
                category.error_text = "Select a category"
            if any(f.error_text for f in all_fields):
                page.update()
                return
            data.update(account_password=app.hash_password(password.value), created_at=now_iso(), last_login=None)
            app.save_user(data)
            close(dlg)
            toast("Account created! Please log in.")

        confirm.on_submit = do_signup
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("📝 Sign Up"),
            content=ft.Column(all_fields, tight=True, width=320, scroll=ft.ScrollMode.AUTO, height=440),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Sign Up", on_click=do_signup)],
            actions_alignment=ft.MainAxisAlignment.END)
        page.open(dlg)

    # ---------- home ----------
    def stat_card(label, value, on_click=None):
        return ft.Container(
            content=ft.Column([ft.Text(str(value), size=22, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_800),
                               ft.Text(label, size=12, color=ft.Colors.GREY_700)],
                              horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
            bgcolor=ft.Colors.WHITE, border=ft.border.all(1, ft.Colors.GREEN_100),
            border_radius=10, padding=14, width=130, on_click=on_click, ink=bool(on_click))

    def alert_tile(icon, color, text, button_text, on_click):
        return ft.Container(
            content=ft.Row([ft.Icon(icon, color=color), ft.Text(text, expand=True),
                            ft.TextButton(button_text, on_click=on_click)]),
            bgcolor=ft.Colors.WHITE, border=ft.border.all(1, color), border_radius=8,
            padding=ft.padding.symmetric(horizontal=12, vertical=4))

    def order_row(order, users):
        email = me()["user_email"]
        other = users.get(order["seller_email"] if order["buyer_email"] == email else order["buyer_email"])
        return ft.Container(
            content=ft.ListTile(
                title=ft.Text(order["product_name"], weight=ft.FontWeight.BOLD),
                subtitle=ft.Text(f"{num(order['quantity'])} {order['unit']} · {money(order['total_price'])} · "
                                 f"{full_name(other)} · {order['created_at'][:10]}"),
                trailing=status_chip(order["status"]), on_click=lambda e, o=order: show_order_details(o)),
            bgcolor=ft.Colors.WHITE, border=ft.border.all(1, ft.Colors.GREY_300), border_radius=8)

    def show_dashboard():
        user, email = me(), me()["user_email"]
        users = app.load_users()
        orders = app.get_user_orders(email)
        content = [wrap_row([stat_card(l, v, on_click=lambda e, l=l: stat_click(l))
                             for l, v in app.stats_for(user)])]

        # things that need attention
        alerts = []
        if is_farmer():
            waiting = sum(1 for o in orders if o["seller_email"] == email and o["status"] == "pending")
            if waiting:
                alerts.append(alert_tile(ft.Icons.NOTIFICATIONS_ACTIVE, ft.Colors.ORANGE_700,
                                         f"{waiting} new order{'s' if waiting != 1 else ''} waiting for you",
                                         "Review", lambda e: show_trades("pending")))
            for p in app.low_stock_products(email):
                alerts.append(alert_tile(ft.Icons.WARNING_AMBER, ft.Colors.RED_700,
                                         f"{p['name']} is running low ({num(p['quantity'])} {p['unit']} left)",
                                         "Restock", lambda e, p=p: open_restock_dialog(p, show_dashboard)))
        else:
            active = sum(1 for o in orders if o["buyer_email"] == email and o["status"] in ("processing", "shipped"))
            if active:
                alerts.append(alert_tile(ft.Icons.LOCAL_SHIPPING, ft.Colors.BLUE_700,
                                         f"{active} order{'s' if active != 1 else ''} on the way",
                                         "Track", lambda e: show_trades()))
            to_review = [o for o in orders if o["buyer_email"] == email and o["status"] == "completed"
                         and not o.get("rating")]
            if to_review:
                alerts.append(alert_tile(ft.Icons.STAR_BORDER, ft.Colors.AMBER_800,
                                         f"{len(to_review)} completed order{'s' if len(to_review) != 1 else ''} "
                                         "to review", "Review", lambda e, o=to_review[0]: show_order_details(o)))
        waiting_offers = app.offers_waiting(email)
        if waiting_offers:
            alerts.append(alert_tile(ft.Icons.LOCAL_OFFER, ft.Colors.AMBER_800,
                                     f"{waiting_offers} price offer{'s' if waiting_offers != 1 else ''} "
                                     "waiting for your response", "Respond", lambda e: show_offers()))
        unread = app.unread_count(email)
        if unread:
            alerts.append(alert_tile(ft.Icons.MARK_CHAT_UNREAD, ft.Colors.GREEN_700,
                                     f"{unread} unread message{'s' if unread != 1 else ''}", "Open",
                                     lambda e: show_messages()))
        if alerts:
            content += [section("Needs your attention")] + alerts

        # quick actions
        if is_farmer():
            quick = [ft.ElevatedButton("Add product", icon=ft.Icons.ADD_BUSINESS, on_click=lambda e: show_product_form())]
        else:
            quick = [ft.ElevatedButton("Find products", icon=ft.Icons.SEARCH, on_click=lambda e: go("market")),
                     ft.ElevatedButton("Saved items", icon=ft.Icons.FAVORITE,
                                       on_click=lambda e: open_marketplace_tab("saved"))]
        quick += [ft.ElevatedButton("Offers", icon=ft.Icons.LOCAL_OFFER, on_click=lambda e: show_offers()),
                  ft.ElevatedButton("Market prices", icon=ft.Icons.TRENDING_UP, on_click=lambda e: show_market()),
                  ft.ElevatedButton("New message", icon=ft.Icons.EDIT, on_click=lambda e: open_new_message_dialog())]
        content += [section("Quick actions"), wrap_row(quick)]

        if is_farmer():
            sales = app.sales_by_product(email)
            if sales:
                top = sales[0][1] or 1
                content.append(section("Top products by sales"))
                for name, total in sales:
                    content.append(ft.Row([ft.Text(name, width=140), ft.ProgressBar(value=total / top, expand=True,
                                                                                    color=ft.Colors.GREEN_600),
                                           ft.Text(money(total), width=110, text_align=ft.TextAlign.END)]))

        content.append(section("Recent orders"))
        recent = [order_row(o, users) for o in orders[:5]]
        content += recent or [empty_state(ft.Icons.RECEIPT_LONG, "No orders yet")]
        view(f"Welcome, {user['first_name']}!", content, nav="home")

    def stat_click(label):
        if label in ("Orders", "Revenue", "Spent"):
            show_trades()
        elif label == "Pending":
            show_trades("pending")
        elif label == "Unread":
            show_messages()
        elif label == "Products":
            show_farmer_products()
        elif label == "Saved":
            open_marketplace_tab("saved")
        elif label == "Rating":
            show_farmer_details(me(), back=show_dashboard)

    # ---------- product / farmer cards ----------
    def heart_button(kind, item_id):
        saved = app.is_favorite(me(), kind, item_id)
        btn = ft.IconButton(ft.Icons.FAVORITE if saved else ft.Icons.FAVORITE_BORDER,
                            icon_color=ft.Colors.RED_400, tooltip="Save / unsave")

        def toggle(e):
            now_saved = app.toggle_favorite(me()["user_email"], kind, item_id)
            refresh_user()
            btn.icon = ft.Icons.FAVORITE if now_saved else ft.Icons.FAVORITE_BORDER
            if marketplace["tab"] == "saved" and not now_saved:
                show_marketplace()
            else:
                page.update()

        btn.on_click = toggle
        return btn

    def image_src(name):
        path = app.image_path(name)
        if not path:
            return None
        return f"product_images/{os.path.basename(path)}" if page.web else path

    def product_thumb(product, size=64):
        src = image_src(product.get("image"))
        if src:
            return ft.Container(content=ft.Image(src=src, width=size, height=size, fit=ft.ImageFit.COVER),
                                width=size, height=size, border_radius=8, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        return ft.Container(content=ft.Icon(ft.Icons.SHOPPING_BAG, color=ft.Colors.GREEN_600),
                            width=size, height=size, border_radius=8, bgcolor=ft.Colors.GREEN_50,
                            alignment=ft.alignment.center)

    def price_note(email):
        low = app.price_from(email)
        return f" · from {money(low)}" if low is not None else ""

    def product_card(product, farmer=None, owner_view=False):
        title_row = [ft.ListTile(
            title=ft.Text(product["name"], weight=ft.FontWeight.BOLD),
            subtitle=ft.Text(f"Category: {product['category']}"),
            leading=product_thumb(product),
            trailing=heart_button("products", product["id"]) if (is_trader() and not owner_view) else None)]
        low = owner_view and product.get("min_stock", 0) > 0 and product["quantity"] <= product["min_stock"]
        controls = title_row + [ft.Row([
            ft.Text(f"{money(product['price'])} per {product['unit']}", weight=ft.FontWeight.BOLD),
            ft.Text(f"Available: {num(product['quantity'])} {product['unit']}",
                    color=ft.Colors.RED_700 if (product["quantity"] <= 0 or low) else ft.Colors.GREEN_700)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN)]
        if owner_view and not product.get("active", True):
            controls.append(ft.Text("⏸ Listing paused - hidden from traders", color=ft.Colors.ORANGE_800))
        if low:
            controls.append(ft.Text("⚠ Low stock", color=ft.Colors.RED_700))
        if farmer and not owner_view:
            avg, count = app.farmer_rating(farmer["user_email"])
            place = f" · 📍 {farmer['location']}" if farmer.get("location") else ""
            controls.append(ft.Text(f"Farmer: {full_name(farmer)}{place} · {stars_text(avg, count)}"))
        if product.get("description"):
            controls.append(ft.Text(product["description"], color=ft.Colors.GREY_600))

        buttons = []
        if owner_view:
            paused = not product.get("active", True)
            buttons = [
                ft.OutlinedButton("Resume" if paused else "Pause",
                                  icon=ft.Icons.PLAY_ARROW if paused else ft.Icons.PAUSE,
                                  on_click=lambda e, p=product: toggle_product_active(p)),
                ft.OutlinedButton("Restock", icon=ft.Icons.ADD_BOX,
                                  on_click=lambda e, p=product: open_restock_dialog(p, show_farmer_products)),
                ft.ElevatedButton("Edit", icon=ft.Icons.EDIT, on_click=lambda e, p=product: show_product_form(p)),
                ft.ElevatedButton("Delete", icon=ft.Icons.DELETE, color=ft.Colors.RED,
                                  on_click=lambda e, p=product: confirm_delete_product(p))]
        elif is_trader() and farmer:
            buttons = [ft.OutlinedButton("View farmer", icon=ft.Icons.PERSON,
                                         on_click=lambda e, f=farmer: show_farmer_details(f))]
            if product["quantity"] > 0:
                buttons.append(ft.OutlinedButton("Make offer", icon=ft.Icons.LOCAL_OFFER,
                                                 on_click=lambda e, p=product: open_offer_dialog(p)))
                buttons.append(ft.ElevatedButton("Order", icon=ft.Icons.SHOPPING_CART,
                                                 on_click=lambda e, p=product: open_order_dialog(p)))
        if buttons:
            controls.append(wrap_row(buttons, spacing=8, run_spacing=8))
        return card(controls)

    def farmer_tile(farmer):
        count = len([p for p in app.get_farmer_products(farmer["user_email"]) if p.get("active", True)])
        avg, reviews = app.farmer_rating(farmer["user_email"])
        place = f" · 📍 {farmer['location']}" if farmer.get("location") else ""
        return ft.Container(
            content=ft.ListTile(
                leading=avatar(farmer), title=ft.Text(full_name(farmer), weight=ft.FontWeight.BOLD),
                subtitle=ft.Text(f"{count} product{'s' if count != 1 else ''}{place} · {stars_text(avg, reviews)}"
                                 f"{price_note(farmer['user_email'])}"),
                trailing=heart_button("farmers", farmer["user_email"]),
                on_click=lambda e, f=farmer: show_farmer_details(f)),
            bgcolor=ft.Colors.WHITE, border=ft.border.all(1, ft.Colors.GREY_300), border_radius=8, padding=5)

    # ---------- trader: marketplace ----------
    def open_marketplace_tab(tab):
        marketplace["tab"] = tab
        show_marketplace()

    def show_marketplace():
        tabs = [("products", "Products", ft.Icons.SHOPPING_BAG), ("farmers", "Farmers", ft.Icons.AGRICULTURE),
                ("saved", "Saved", ft.Icons.FAVORITE)]

        def tab_button(key, label, icon):
            cls = ft.ElevatedButton if marketplace["tab"] == key else ft.OutlinedButton
            return cls(label, icon=icon, on_click=lambda e, k=key: open_marketplace_tab(k))

        if marketplace["tab"] == "products":
            content = products_tab()
        elif marketplace["tab"] == "farmers":
            content = [farmer_tile(f) for f in app.get_users_by_category("Farmer")] or \
                      [empty_state(ft.Icons.AGRICULTURE, "No farmers yet")]
        else:
            content = saved_tab()
        view("Marketplace", [ft.Row([tab_button(*t) for t in tabs])] + content, nav="market")

    def products_tab():
        users = app.load_users()
        results = ft.Column([], spacing=8)

        def populate():
            found = app.search_products(marketplace["query"], marketplace["category"], marketplace["sort"],
                                        marketplace["instock"], users)
            results.controls = [ft.Text(f"{len(found)} result{'s' if len(found) != 1 else ''}",
                                        color=ft.Colors.GREY_600)] + \
                               [product_card(p, f) for p, f in found] if found else \
                               [empty_state(ft.Icons.SEARCH, "No products found", "Try another search or filter")]

        def on_change(e=None):
            marketplace["query"] = query.value
            marketplace["category"] = category.value
            marketplace["sort"] = sort.value
            marketplace["instock"] = in_stock.value
            populate()
            page.update()

        query = ft.TextField(hint_text="Search product, category, farmer or location...", value=marketplace["query"],
                             expand=True, prefix_icon=ft.Icons.SEARCH, on_change=on_change, on_submit=on_change)
        category = ft.Dropdown(label="Category", width=190, value=marketplace["category"], on_change=on_change,
                               options=[ft.dropdown.Option("All")] +
                                       [ft.dropdown.Option(c) for c in app.categories()])
        sort = ft.Dropdown(label="Sort by", width=200, value=marketplace["sort"], on_change=on_change,
                           options=[ft.dropdown.Option(k, t) for k, t in SORT_OPTIONS])
        in_stock = ft.Switch(label="In stock only", value=marketplace["instock"], on_change=on_change)
        populate()
        return [ft.Row([query]), wrap_row([category, sort, in_stock]), results]

    def saved_tab():
        user = me()
        users = app.load_users()
        out = []
        fav_products = [(app.get_product(pid)) for pid in user.get("fav_products", [])]
        fav_products = [p for p in fav_products if p and users.get(p["farmer_email"])]
        fav_farmers = [users[e] for e in user.get("fav_farmers", []) if e in users]
        if fav_products:
            out += [section("Saved products")] + [product_card(p, users[p["farmer_email"]]) for p in fav_products]
        if fav_farmers:
            out += [section("Saved farmers")] + [farmer_tile(f) for f in fav_farmers]
        return out or [empty_state(ft.Icons.FAVORITE_BORDER, "Nothing saved yet",
                                   "Tap the heart on a product or farmer to keep it here")]

    def show_farmer_details(farmer, back=None):
        email = farmer["user_email"]
        users = app.load_users()
        avg, count = app.farmer_rating(email)
        is_self = email == me()["user_email"]
        back = back or show_marketplace
        products = [p for p in app.get_farmer_products(email) if p.get("active", True) or is_self]
        content = [ft.ListTile(
            leading=avatar(farmer), title=ft.Text(full_name(farmer), weight=ft.FontWeight.BOLD, size=18),
            subtitle=ft.Text(f"{farmer.get('location') or 'Location not set'} · {stars_text(avg, count)}"))]
        if farmer.get("bio"):
            content.append(ft.Text(farmer["bio"], italic=True, color=ft.Colors.GREY_700))
        content.append(section("Products"))
        content += [product_card(p, farmer, owner_view=False) for p in products] or \
                   [ft.Text("No products available", color=ft.Colors.GREY_600)]
        reviews = app.reviews_for(email)[:5]
        if reviews:
            content.append(section("Recent reviews"))
            for o in reviews:
                r = o["rating"]
                content.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.STAR, color=ft.Colors.AMBER),
                    title=ft.Text("★" * r["stars"] + "☆" * (5 - r["stars"])),
                    subtitle=ft.Text(f"{r['comment'] or 'No comment'} - {full_name(users.get(o['buyer_email']))}")))
        actions = []
        if not is_self:
            actions = [ft.ElevatedButton("Message", icon=ft.Icons.MESSAGE,
                                         on_click=lambda e: show_chat(email, back=lambda: show_farmer_details(farmer, back)))]
        view(f"{farmer['first_name']}'s farm", content, back=back, actions=actions)

    def open_order_dialog(product):
        qty = decimal_field("Quantity", value="1", width=300)
        notes = ft.TextField(label="Notes for the farmer (optional)", multiline=True, width=300)
        total = ft.Text("", weight=ft.FontWeight.BOLD)

        def update_total(e=None):
            try:
                total.value = f"Total: {money(float(qty.value or 0) * product['price'])}"
            except ValueError:
                total.value = ""
            page.update()

        def submit(e):
            qty.error_text = None
            q = parse_positive(qty, "Quantity")
            if q is None:
                page.update()
                return
            order, error = app.place_order(product["id"], me()["user_email"], q, notes.value.strip())
            if error:
                qty.error_text = error
                page.update()
                return
            close(dlg)
            toast("Order placed!")
            show_order_details(order, back=show_trades)

        qty.on_change = update_total
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(f"Order {product['name']}"),
            content=ft.Column([ft.Text(f"Price: {money(product['price'])} per {product['unit']}"),
                               ft.Text(f"Available: {num(product['quantity'])} {product['unit']}"),
                               qty, notes, total], tight=True, width=300),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Place order", on_click=submit)])
        page.open(dlg)
        update_total()

    # ---------- farmer: products ----------
    def show_farmer_products():
        products = sorted(app.get_farmer_products(me()["user_email"]), key=lambda p: p["name"].lower())
        content = [card_ for card_ in (product_card(p, owner_view=True) for p in products)] or \
                  [empty_state(ft.Icons.STOREFRONT, "No products yet", "Add your first product to start selling",
                               ft.ElevatedButton("Add product", icon=ft.Icons.ADD,
                                                 on_click=lambda e: show_product_form()))]
        view("My Products", content, nav="market", actions=[
            ft.ElevatedButton("Add product", icon=ft.Icons.ADD, on_click=lambda e: show_product_form())])

    def toggle_product_active(product):
        app.update_product(product["id"], {"active": not product.get("active", True)})
        toast("Listing resumed" if not product.get("active", True) else "Listing paused")
        show_farmer_products()

    def open_restock_dialog(product, after):
        add = decimal_field("Quantity to add", width=260, autofocus=True)

        def save(e):
            add.error_text = None
            q = parse_positive(add, "Quantity")
            if q is None:
                page.update()
                return
            fresh = app.get_product(product["id"])
            app.update_product(product["id"], {"quantity": fresh["quantity"] + q, "updated_at": now_iso()})
            close(dlg)
            toast(f"Added {num(q)} {product['unit']} to {product['name']}")
            after()

        add.on_submit = save
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(f"Restock {product['name']}"),
            content=ft.Column([ft.Text(f"Current stock: {num(product['quantity'])} {product['unit']}"), add],
                              tight=True, width=260),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Add stock", on_click=save)])
        page.open(dlg)

    def show_product_form(product=None):
        editing = product is not None
        name = ft.TextField(label="Product name", width=300, prefix_icon=ft.Icons.SHOPPING_BAG,
                            value=product["name"] if editing else "")
        category = ft.TextField(label="Category (e.g. Grains, Vegetables)", width=300, prefix_icon=ft.Icons.CATEGORY,
                                value=product["category"] if editing else "")
        price = decimal_field("Price per unit", width=190, value=num(product["price"]) if editing else "")
        quantity = decimal_field("Quantity", width=190, value=num(product["quantity"]) if editing else "")
        unit = ft.Dropdown(label="Unit", width=190, options=[ft.dropdown.Option(u) for u in UNITS],
                           value=product["unit"] if editing else None)
        min_stock = decimal_field("Low-stock alert at (optional)", width=260,
                                  value=num(product.get("min_stock", 0)) if editing and product.get("min_stock") else "")
        description = ft.TextField(label="Description", width=600, multiline=True, min_lines=3, max_lines=5,
                                   value=product.get("description", "") if editing else "")
        active = ft.Switch(label="Listing is visible to traders", value=product.get("active", True) if editing else True)
        fields_ = (name, category, price, quantity, unit, min_stock)

        img = {"new": None, "remove": False, "pending": None}
        preview_box = ft.Container(width=120, height=120, border_radius=10, bgcolor=ft.Colors.GREY_200,
                                   alignment=ft.alignment.center, clip_behavior=ft.ClipBehavior.HARD_EDGE)

        def draw_preview():
            if img["new"]:
                src = f"tmp_uploads/{os.path.basename(img['new'])}" if page.web else img["new"]
            elif editing and not img["remove"]:
                src = image_src(product.get("image"))
            else:
                src = None
            preview_box.content = ft.Image(src=src, width=120, height=120, fit=ft.ImageFit.COVER) if src else \
                ft.Icon(ft.Icons.ADD_A_PHOTO, size=40, color=ft.Colors.GREY_500)

        def on_picked(e):
            if not e.files:
                return
            chosen = e.files[0]
            if page.web:  # browsers can't share a file path, so upload the picture to the server
                ext = os.path.splitext(chosen.name)[1].lower()
                if ext not in IMAGE_EXTENSIONS:
                    toast("Please choose a PNG, JPG, GIF, WEBP or BMP image", error=True)
                    return
                if chosen.size and chosen.size > MAX_IMAGE_BYTES:
                    toast("Image is too large (maximum 5 MB)", error=True)
                    return
                img["pending"] = f"{uuid.uuid4().hex}{ext}"
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                picker.upload([ft.FilePickerUploadFile(chosen.name, upload_url=page.get_upload_url(img["pending"], 600))])
                toast("Uploading photo...")
                return
            path = chosen.path
            if not path:
                toast("Could not read that file", error=True)
                return
            img["new"], img["remove"] = path, False
            draw_preview()
            page.update()

        def on_uploaded(e):
            if e.error:
                toast(f"Upload failed: {e.error}", error=True)
            elif e.progress == 1 and img["pending"]:
                img["new"], img["remove"] = os.path.join(UPLOAD_DIR, img["pending"]), False
                draw_preview()
                page.update()

        def choose_photo(e):
            picker.on_result = on_picked
            picker.on_upload = on_uploaded
            picker.pick_files(allow_multiple=False, file_type=ft.FilePickerFileType.IMAGE)

        def remove_photo(e):
            img["new"], img["remove"] = None, True
            draw_preview()
            page.update()

        draw_preview()
        photo_row = ft.Row([preview_box, ft.Column([
            ft.Text("Product photo (optional)", weight=ft.FontWeight.BOLD),
            ft.Text("PNG or JPG, up to 5 MB", size=12, color=ft.Colors.GREY_600),
            ft.Row([ft.OutlinedButton("Choose photo", icon=ft.Icons.UPLOAD, on_click=choose_photo),
                    ft.TextButton("Remove", on_click=remove_photo)])], spacing=4)], spacing=15)

        def save(e):
            for f in fields_:
                f.error_text = None
            if not name.value.strip():
                name.error_text = "Name required"
            if not category.value.strip():
                category.error_text = "Category required"
            p = parse_positive(price, "Price")
            q = parse_positive(quantity, "Quantity")
            m = parse_positive(min_stock, "Alert level", required=False)
            if not unit.value:
                unit.error_text = "Unit required"
            if any(f.error_text for f in fields_):
                page.update()
                return
            image_name = product.get("image") if editing else None
            try:
                if img["new"]:
                    new_name = app.save_product_image(img["new"])
                    if page.web:  # the upload was only a temporary copy
                        try:
                            os.remove(img["new"])
                        except OSError:
                            pass
                    app.delete_product_image(image_name)
                    image_name = new_name
                elif img["remove"] and image_name:
                    app.delete_product_image(image_name)
                    image_name = None
            except (ValueError, OSError) as ex:
                toast(str(ex), error=True)
                return
            data = {"name": name.value.strip(), "category": category.value.strip(), "price": p, "quantity": q,
                    "unit": unit.value, "min_stock": m, "description": description.value.strip(),
                    "active": active.value, "image": image_name, "updated_at": now_iso()}
            if editing:
                app.update_product(product["id"], data)
                toast("Product updated!")
            else:
                app.save_product({"id": uuid.uuid4().hex, "farmer_email": me()["user_email"],
                                  "created_at": now_iso(), **data})
                toast("Product added!")
            show_farmer_products()

        view("Edit product" if editing else "Add product", [
            photo_row, name, category, wrap_row([price, quantity, unit]), min_stock, description, active,
            ft.Row([ft.OutlinedButton("Cancel", on_click=lambda e: show_farmer_products()),
                    ft.ElevatedButton("Update" if editing else "Save", icon=ft.Icons.SAVE, on_click=save)],
                   alignment=ft.MainAxisAlignment.END)], back=show_farmer_products, nav="market")

    def confirm_delete_product(product):
        def do_delete(e):
            app.delete_product(product["id"])
            close(dlg)
            toast("Product deleted")
            show_farmer_products()

        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Delete product?"),
            content=ft.Text(f"Delete {product['name']}? This cannot be undone. "
                            "You can use Pause instead to hide it temporarily."),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Delete", color=ft.Colors.RED, on_click=do_delete)])
        page.open(dlg)

    # ---------- orders ----------
    def show_trades(status_filter="all"):
        email = me()["user_email"]
        users = app.load_users()
        all_orders = app.get_user_orders(email)
        orders = all_orders if status_filter == "all" else [o for o in all_orders if o["status"] == status_filter]
        filter_dd = ft.Dropdown(
            label="Status", width=170, value=status_filter, on_change=lambda e: show_trades(e.control.value),
            options=[ft.dropdown.Option("all", "All")] + [ft.dropdown.Option(s, s.capitalize()) for s in STATUS_FLOW])

        def export(e):
            try:
                if page.web:
                    raise OSError("export is only available in the desktop app")
                path = app.export_orders_csv(email)
                toast(f"Exported {len(all_orders)} orders to {path}")
            except OSError as ex:
                toast(f"Could not export: {ex}", error=True)

        cards = []
        for o in orders:
            buyer_side = o["buyer_email"] == email
            other = users.get(o["seller_email"] if buyer_side else o["buyer_email"])
            buttons = [ft.OutlinedButton("Details", on_click=lambda e, o=o: show_order_details(o))]
            if o["seller_email"] == email and o["status"] in NEXT_STATUS:
                nxt = NEXT_STATUS[o["status"]]
                buttons.append(ft.ElevatedButton(f"Mark {nxt}", icon=ft.Icons.ARROW_FORWARD,
                                                 on_click=lambda e, o=o, n=nxt: change_status(o, n, show_trades)))
            if o["seller_email"] == email and o["status"] not in TERMINAL_STATUSES:
                buttons.append(ft.OutlinedButton("Decline / cancel", icon=ft.Icons.CANCEL,
                                                 on_click=lambda e, o=o: confirm_cancel_order(o, show_trades)))
            if buyer_side and o["status"] == "pending":
                buttons.append(ft.OutlinedButton("Cancel order", icon=ft.Icons.CANCEL,
                                                 on_click=lambda e, o=o: confirm_cancel_order(o, show_trades)))
            if buyer_side and o["status"] == "completed" and not o.get("rating"):
                buttons.append(ft.ElevatedButton("Rate", icon=ft.Icons.STAR,
                                                 on_click=lambda e, o=o: open_rate_dialog(o, show_trades)))
            cards.append(card([
                ft.ListTile(title=ft.Text(o["product_name"], weight=ft.FontWeight.BOLD),
                            subtitle=ft.Text(f"{'From' if buyer_side else 'To'}: {full_name(other)} "
                                             f"({'Buyer' if buyer_side else 'Seller'})"),
                            trailing=status_chip(o["status"])),
                ft.Row([ft.Text(f"Quantity: {num(o['quantity'])} {o['unit']}"),
                        ft.Text(f"Total: {money(o['total_price'])}", weight=ft.FontWeight.BOLD)],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Text(f"Ordered on: {o['created_at'][:10]}", color=ft.Colors.GREY_600),
                wrap_row(buttons, spacing=8, run_spacing=8)]))
        view("Orders", [orders_offers_toggle("orders"),
                        ft.Row([filter_dd, ft.OutlinedButton("Export CSV", icon=ft.Icons.DOWNLOAD, on_click=export)])] +
             (cards or [empty_state(ft.Icons.RECEIPT_LONG, "No orders found")]), nav="orders")

    def change_status(order, new_status, after):
        error = app.set_order_status(order["id"], new_status, me()["user_email"])
        toast(error or f"Order marked {new_status}", error=bool(error))
        after()

    def confirm_cancel_order(order, after):
        is_seller = order["seller_email"] == me()["user_email"]

        def do_cancel(e):
            error = app.set_order_status(order["id"], "cancelled", me()["user_email"])
            close(dlg)
            toast(error or "Order cancelled", error=bool(error))
            after()

        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Decline this order?" if is_seller else "Cancel this order?"),
            content=ft.Text(f"{order['product_name']} - the stock will be returned to the listing."),
            actions=[ft.TextButton("Keep order", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Yes, cancel", color=ft.Colors.RED, on_click=do_cancel)])
        page.open(dlg)

    def open_rate_dialog(order, after):
        state = {"stars": 5}
        row = ft.Row(spacing=0)
        comment = ft.TextField(label="Comment (optional)", multiline=True, width=300)

        def draw():
            row.controls = [ft.IconButton(ft.Icons.STAR if i <= state["stars"] else ft.Icons.STAR_BORDER,
                                          icon_color=ft.Colors.AMBER, icon_size=32,
                                          on_click=lambda e, i=i: pick(i)) for i in range(1, 6)]

        def pick(i):
            state["stars"] = i
            draw()
            page.update()

        def submit(e):
            error = app.rate_order(order["id"], me()["user_email"], state["stars"], comment.value)
            close(dlg)
            toast(error or "Thanks for your review!", error=bool(error))
            after()

        draw()
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("Rate this farmer"),
            content=ft.Column([ft.Text(f"How was your order of {order['product_name']}?"), row, comment],
                              tight=True, width=300),
            actions=[ft.TextButton("Later", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Submit", on_click=submit)])
        page.open(dlg)

    def show_order_details(order, back=None):
        order = next((o for o in app.load_orders() if o["id"] == order["id"]), order)  # always show latest
        email = me()["user_email"]
        users = app.load_users()
        buyer, seller = users.get(order["buyer_email"]), users.get(order["seller_email"])
        buyer_side = order["buyer_email"] == email
        other_email = order["seller_email"] if buyer_side else order["buyer_email"]
        back = back or show_trades

        steps = [s for s in STATUS_FLOW if s != "cancelled"]
        history = order.get("history") or [{"status": "pending", "at": order["created_at"]}]
        reached = {h["status"]: h["at"] for h in history}
        timeline = []
        if order["status"] == "cancelled":
            timeline.append(ft.ListTile(leading=ft.Icon(ft.Icons.CANCEL, color=ft.Colors.RED_700),
                                        title=ft.Text("Cancelled"),
                                        subtitle=ft.Text(reached.get("cancelled", "")[:16].replace("T", " "))))
        else:
            for s in steps:
                done = s in reached
                timeline.append(ft.ListTile(
                    leading=ft.Icon(ft.Icons.CHECK_CIRCLE if done else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                    color=STATUS_COLORS[s] if done else ft.Colors.GREY_400),
                    title=ft.Text(s.capitalize(), weight=ft.FontWeight.BOLD if done else ft.FontWeight.NORMAL),
                    subtitle=ft.Text(reached[s][:16].replace("T", " ") if done else "")))

        actions = [ft.OutlinedButton("Message " + ("seller" if buyer_side else "buyer"), icon=ft.Icons.MESSAGE,
                                     on_click=lambda e: show_chat(other_email, back=lambda: show_order_details(order, back)))]
        if order["seller_email"] == email and order["status"] in NEXT_STATUS:
            nxt = NEXT_STATUS[order["status"]]
            actions.append(ft.ElevatedButton(f"Mark {nxt}", icon=ft.Icons.ARROW_FORWARD,
                                             on_click=lambda e: change_status(order, nxt, lambda: show_order_details(order, back))))
        if order["status"] not in TERMINAL_STATUSES and (order["seller_email"] == email or order["status"] == "pending"):
            actions.append(ft.OutlinedButton("Cancel order", icon=ft.Icons.CANCEL,
                                             on_click=lambda e: confirm_cancel_order(order, lambda: show_order_details(order, back))))
        if buyer_side and order["status"] == "completed" and not order.get("rating"):
            actions.append(ft.ElevatedButton("Rate farmer", icon=ft.Icons.STAR,
                                             on_click=lambda e: open_rate_dialog(order, lambda: show_order_details(order, back))))

        content = [
            ft.ListTile(title=ft.Text(order["product_name"], weight=ft.FontWeight.BOLD, size=18),
                        subtitle=ft.Text(f"Order #{order['id'][:8]}"), trailing=status_chip(order["status"])),
            wrap_row(actions, spacing=8, run_spacing=8),
            section("Summary"),
            ft.Text(f"Quantity: {num(order['quantity'])} {order['unit']}"),
            ft.Text(f"Price per unit: {money(order['price_per_unit'])}" +
                    (f" (listed at {money(order['listed_price'])})"
                     if order.get("listed_price") and abs(order["listed_price"] - order["price_per_unit"]) > 1e-9
                     else "")),
            ft.Text(f"Total price: {money(order['total_price'])}", weight=ft.FontWeight.BOLD),
            ft.Text(f"Buyer: {full_name(buyer)} ({order['buyer_email']})"),
            ft.Text(f"Seller: {full_name(seller)} ({order['seller_email']})"),
            ft.Text(f"Notes: {order.get('notes') or '-'}"),
            section("Progress")] + timeline
        if order.get("rating"):
            r = order["rating"]
            content += [section("Review"), ft.Text("★" * r["stars"] + "☆" * (5 - r["stars"]), size=18,
                                                   color=ft.Colors.AMBER_800),
                        ft.Text(r["comment"] or "No comment")]
        view("Order details", content, back=back, nav="orders")

    # ---------- offers (price negotiation) ----------
    def offer_chip(text, color):
        return ft.Container(content=ft.Text(text, size=12, color=ft.Colors.WHITE), bgcolor=color,
                            padding=ft.padding.symmetric(horizontal=10, vertical=3), border_radius=12)

    def orders_offers_toggle(active):
        waiting = app.offers_waiting(me()["user_email"])

        def button(key, label, icon, handler):
            cls = ft.ElevatedButton if active == key else ft.OutlinedButton
            return cls(label, icon=icon, on_click=handler)

        return ft.Row([button("orders", "Orders", ft.Icons.RECEIPT_LONG, lambda e: show_trades()),
                       button("offers", f"Offers ({waiting} waiting)" if waiting else "Offers",
                              ft.Icons.LOCAL_OFFER, lambda e: show_offers())])

    def open_offer_dialog(product):
        listed = product["price"]
        qty = decimal_field("Quantity", value="1", width=300)
        price = decimal_field(f"Your price per {product['unit']}", value=num(round(listed * 0.9, 2)), width=300)
        note = ft.TextField(label="Message to the farmer (optional)", multiline=True, width=300)
        summary = ft.Text("", weight=ft.FontWeight.BOLD)

        def update_summary(e=None):
            try:
                q, p = float(qty.value or 0), float(price.value or 0)
                summary.value = f"Total: {money(q * p)}  ({(1 - p / listed) * 100:.0f}% below listed price)"
            except (ValueError, ZeroDivisionError):
                summary.value = ""
            page.update()

        def submit(e):
            qty.error_text = price.error_text = None
            q = parse_positive(qty, "Quantity")
            p = parse_positive(price, "Price")
            if q is None or p is None:
                page.update()
                return
            offer, error = app.create_offer(product["id"], me()["user_email"], q, p, note.value.strip())
            if error:
                target = price if ("price" in error.lower() or "offer" in error.lower()) else qty
                target.error_text = error
                page.update()
                return
            close(dlg)
            toast("Offer sent! The farmer will be notified.")
            show_offers()

        qty.on_change = update_summary
        price.on_change = update_summary
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(f"Make an offer: {product['name']}"),
            content=ft.Column([ft.Text(f"Listed price: {money(listed)} per {product['unit']}"),
                               ft.Text(f"Available: {num(product['quantity'])} {product['unit']}"),
                               qty, price, note, summary], tight=True, width=300),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Send offer", on_click=submit)])
        page.open(dlg)
        update_summary()

    def open_counter_dialog(offer):
        price = decimal_field(f"Your counter price per {offer['unit']}", value=num(offer["price"]),
                              width=300, autofocus=True)
        note = ft.TextField(label="Message (optional)", multiline=True, width=300)

        def submit(e):
            price.error_text = None
            p = parse_positive(price, "Price")
            if p is None:
                page.update()
                return
            _, error = app.respond_offer(offer["id"], me()["user_email"], "counter", p, note.value.strip())
            if error:
                price.error_text = error
                page.update()
                return
            close(dlg)
            toast("Counter-offer sent")
            show_offers()

        price.on_submit = submit
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(f"Counter-offer: {offer['product_name']}"),
            content=ft.Column([ft.Text(f"Listed: {money(offer['listed_price'])} · "
                                       f"current offer: {money(offer['price'])} per {offer['unit']}"),
                               price, note], tight=True, width=300),
            actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg)),
                     ft.ElevatedButton("Send counter-offer", on_click=submit)])
        page.open(dlg)

    def confirm_offer_action(offer, action):
        texts = {
            "accept": ("Accept this offer?", f"An order for {num(offer['quantity'])} {offer['unit']} of "
                       f"{offer['product_name']} will be created at {money(offer['price'])} per {offer['unit']}.",
                       "Accept"),
            "reject": ("Reject this offer?", "The other person will be told the offer was declined.", "Reject"),
            "withdraw": ("Withdraw your offer?", "The farmer will be told you withdrew it.", "Withdraw")}
        title, text, label = texts[action]

        def do(e):
            order, error = app.respond_offer(offer["id"], me()["user_email"], action)
            close(dlg)
            if error:
                toast(error, error=True)
                show_offers()
            elif order:
                toast("Offer accepted - order created")
                show_order_details(order, back=show_offers)
            else:
                toast("Offer rejected" if action == "reject" else "Offer withdrawn")
                show_offers()

        dlg = ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Text(text),
                             actions=[ft.TextButton("Back", on_click=lambda e: close(dlg)),
                                      ft.ElevatedButton(label, on_click=do)])
        page.open(dlg)

    def show_offers():
        email = me()["user_email"]
        users = app.load_users()
        orders_by_id = {o["id"]: o for o in app.load_orders()}
        cards = []
        for o in app.get_user_offers(email):
            buyer_side = o["buyer_email"] == email
            other = users.get(o["seller_email"] if buyer_side else o["buyer_email"])
            my_role = "buyer" if buyer_side else "seller"
            my_turn = o["status"] == "open" and o["turn"] == my_role
            if o["status"] == "open":
                chip = offer_chip("Your turn" if my_turn else "Waiting", ft.Colors.ORANGE_700 if my_turn
                                  else ft.Colors.BLUE_700)
            else:
                chip = offer_chip(o["status"].capitalize(), {"accepted": ft.Colors.GREEN_700,
                                                              "rejected": ft.Colors.RED_700}.get(o["status"], ft.Colors.GREY_600))
            history = []
            for r in o["rounds"]:
                who = "You" if r["by"] == my_role else full_name(other)
                note_txt = f' - "{r["note"]}"' if r.get("note") else ""
                history.append(ft.Text(f"{who}: {money(r['price'])} per {o['unit']}{note_txt}", size=13))
            buttons = []
            if my_turn:
                buttons += [ft.ElevatedButton("Accept", icon=ft.Icons.CHECK, on_click=lambda e, o=o: confirm_offer_action(o, "accept")),
                            ft.OutlinedButton("Counter", icon=ft.Icons.SWAP_HORIZ, on_click=lambda e, o=o: open_counter_dialog(o)),
                            ft.OutlinedButton("Reject", icon=ft.Icons.CLOSE, on_click=lambda e, o=o: confirm_offer_action(o, "reject"))]
            if buyer_side and o["status"] == "open":
                buttons.append(ft.OutlinedButton("Withdraw", icon=ft.Icons.UNDO,
                                                 on_click=lambda e, o=o: confirm_offer_action(o, "withdraw")))
            if o["status"] == "accepted" and orders_by_id.get(o.get("order_id")):
                buttons.append(ft.OutlinedButton("View order", icon=ft.Icons.RECEIPT_LONG,
                                                 on_click=lambda e, od=orders_by_id[o["order_id"]]: show_order_details(od, back=show_offers)))
            cards.append(card([
                ft.ListTile(title=ft.Text(o["product_name"], weight=ft.FontWeight.BOLD),
                            subtitle=ft.Text(f"{'To' if buyer_side else 'From'}: {full_name(other)} · "
                                             f"{num(o['quantity'])} {o['unit']}"), trailing=chip),
                ft.Row([ft.Text(f"Listed: {money(o['listed_price'])}"),
                        ft.Text(f"Current offer: {money(o['price'])} per {o['unit']}", weight=ft.FontWeight.BOLD)],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Text(f"Total at this price: {money(o['price'] * o['quantity'])}", color=ft.Colors.GREY_600),
                *history,
                wrap_row(buttons, spacing=8, run_spacing=8)]))
        hint = "Use \"Make offer\" on a product to propose a price" if is_trader() else \
               "Traders can propose prices on your products - you'll see them here"
        view("Offers", [orders_offers_toggle("offers")] +
             (cards or [empty_state(ft.Icons.LOCAL_OFFER, "No offers yet", hint)]), nav="orders")

    # ---------- market ----------
    def show_market():
        def trend_icon(trend):
            icon = {"up": ft.Icons.ARROW_UPWARD, "down": ft.Icons.ARROW_DOWNWARD}.get(trend, ft.Icons.ARROW_RIGHT)
            color = {"up": ft.Colors.GREEN, "down": ft.Colors.RED}.get(trend, ft.Colors.GREY)
            return ft.Icon(icon, color=color)

        reference = ft.DataTable(
            columns=[ft.DataColumn(ft.Text("Product")), ft.DataColumn(ft.Text("Price")), ft.DataColumn(ft.Text("Trend"))],
            rows=[ft.DataRow(cells=[ft.DataCell(ft.Text(i["product"])),
                                    ft.DataCell(ft.Text(f"{i['price']} per {i['unit']}")),
                                    ft.DataCell(trend_icon(i["trend"]))]) for i in SAMPLE_MARKET])
        groups = {}
        for p in app.load_products():
            if p["quantity"] > 0 and p.get("active", True):
                groups.setdefault((p["name"].strip().title(), p["unit"]), []).append(p["price"])
        live_rows = [ft.DataRow(cells=[ft.DataCell(ft.Text(n)),
                                       ft.DataCell(ft.Text(f"{money(sum(v) / len(v))} per {u}")),
                                       ft.DataCell(ft.Text(f"{money(min(v))} - {money(max(v))}")),
                                       ft.DataCell(ft.Text(str(len(v))))])
                     for (n, u), v in sorted(groups.items())]
        live = ft.DataTable(
            columns=[ft.DataColumn(ft.Text("Product")), ft.DataColumn(ft.Text("Average")),
                     ft.DataColumn(ft.Text("Range")), ft.DataColumn(ft.Text("Listings"))], rows=live_rows) \
            if live_rows else ft.Text("No active listings yet", color=ft.Colors.GREY_600)
        view("Market prices", [section("On AgroLink now"), live, ft.Divider(),
                               section("Reference prices (sample data)"), reference],
             back=show_dashboard, nav="home")

    # ---------- messages ----------
    def format_message_time(timestamp):
        try:
            dt = datetime.fromisoformat(timestamp)
            today = datetime.now().date()
            if dt.date() == today:
                return dt.strftime("%H:%M")
            if (today - dt.date()).days == 1:
                return "Yesterday"
            return dt.strftime("%d/%m") if dt.year == datetime.now().year else dt.strftime("%d/%m/%y")
        except (ValueError, TypeError):
            return ""

    def show_messages():
        email = me()["user_email"]
        users = app.load_users()
        cards = []
        for other_email, conv in app.get_user_conversations(email).items():
            other = users.get(other_email)
            if not other:
                continue
            last, unread = conv["last_message"], conv["unread_count"]
            sent = last["from_email"] == email
            preview = last["content"][:50] + ("..." if len(last["content"]) > 50 else "")
            trailing = ft.Column([
                ft.Text(format_message_time(last["timestamp"]), size=12, color=ft.Colors.GREY_600),
                ft.Container(content=ft.Text(str(unread), size=11, color=ft.Colors.WHITE), bgcolor=ft.Colors.GREEN_600,
                             border_radius=10, padding=ft.padding.symmetric(horizontal=7, vertical=2))
                if unread else ft.Container()], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END)
            cards.append(ft.Container(
                content=ft.ListTile(
                    leading=avatar(other),
                    title=ft.Text(full_name(other), weight=ft.FontWeight.BOLD if unread else ft.FontWeight.NORMAL),
                    subtitle=ft.Row([ft.Icon(ft.Icons.DONE_ALL if sent else ft.Icons.INBOX, size=16,
                                             color=ft.Colors.BLUE if sent else ft.Colors.GREY_600),
                                     ft.Text(preview, color=ft.Colors.GREY_700, size=14)]),
                    trailing=trailing, on_click=lambda e, em=other_email: show_chat(em)),
                bgcolor=ft.Colors.WHITE, border=ft.border.all(1, ft.Colors.GREY_300), border_radius=8, padding=5))
        view("Messages", cards or [empty_state(ft.Icons.MESSAGE_OUTLINED, "No conversations yet",
                                               "Start one with the pencil button")],
             nav="messages", actions=[ft.ElevatedButton("New message", icon=ft.Icons.EDIT,
                                                        on_click=lambda e: open_new_message_dialog())])

    def show_chat(other_email, back=None):
        email = me()["user_email"]
        other = app.get_user(other_email)
        if not other:
            return
        set_nav("messages")
        back = back or show_messages
        app.mark_messages_read(other_email, email)
        bubbles = ft.ListView(expand=True, spacing=4, padding=10, auto_scroll=True)
        entry = ft.TextField(hint_text="Type a message...", expand=True, autofocus=True, border_radius=20,
                             content_padding=ft.padding.symmetric(horizontal=15, vertical=10))

        def bubble(msg):
            sent = msg["from_email"] == email
            system = msg.get("type") in ("order", "status_update", "offer")
            return ft.Row([ft.Container(
                content=ft.Column([
                    ft.Text(("📦 " if system else "") + msg["content"],
                            color=ft.Colors.WHITE if sent else ft.Colors.BLACK, size=14, selectable=True),
                    ft.Text(format_message_time(msg["timestamp"]),
                            color=ft.Colors.WHITE70 if sent else ft.Colors.GREY_600, size=11)], tight=True, spacing=2),
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                bgcolor=ft.Colors.GREEN_600 if sent else ft.Colors.WHITE,
                border=None if sent else ft.border.all(1, ft.Colors.GREY_300),
                border_radius=ft.border_radius.only(top_left=18, top_right=18, bottom_left=18 if not sent else 4,
                                                    bottom_right=4 if not sent else 18))],
                alignment=ft.MainAxisAlignment.END if sent else ft.MainAxisAlignment.START)

        def render():
            msgs = app.get_chat_messages(email, other_email)
            bubbles.controls = [bubble(m) for m in msgs] or [
                ft.Container(content=ft.Text("No messages yet - say hello 👋", color=ft.Colors.GREY_600),
                             alignment=ft.alignment.center, padding=40)]

        def send(e=None):
            text = entry.value.strip()
            if not text:
                return
            app.save_message(app.make_message(email, other_email, text))
            entry.value = ""
            render()
            entry.focus()
            page.update()

        entry.on_submit = send
        render()
        top = ft.Container(
            content=ft.Row([ft.IconButton(ft.Icons.ARROW_BACK, icon_color=ft.Colors.WHITE, on_click=lambda e: back()),
                            avatar(other, radius=18, bg=ft.Colors.GREEN_800),
                            ft.Column([ft.Text(full_name(other), color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD, size=16),
                                       ft.Text(other["user_category"], color=ft.Colors.WHITE70, size=12)],
                                      spacing=0, expand=True)], spacing=10),
            bgcolor=ft.Colors.GREEN_600, padding=10)
        bottom = ft.Container(
            content=ft.Row([entry, ft.IconButton(ft.Icons.SEND, on_click=send, bgcolor=ft.Colors.GREEN_600,
                                                 icon_color=ft.Colors.WHITE)], spacing=5),
            bgcolor=ft.Colors.WHITE, padding=10, border=ft.border.only(top=ft.BorderSide(1, ft.Colors.GREY_300)))
        show(ft.Container(
            content=ft.Column([top, ft.Container(bubbles, expand=True, bgcolor=ft.Colors.GREY_50), bottom], spacing=0),
            expand=True, border_radius=10, border=ft.border.all(1, ft.Colors.GREY_300),
            clip_behavior=ft.ClipBehavior.HARD_EDGE))

    def open_new_message_dialog():
        email = me()["user_email"]
        others = [u for e, u in app.load_users().items() if e != email]
        tiles = ft.ListView(height=300)

        def fill(e=None):
            q = (search.value or "").lower().strip()
            tiles.controls = [ft.ListTile(
                leading=avatar(u, radius=20), title=ft.Text(full_name(u), weight=ft.FontWeight.BOLD),
                subtitle=ft.Text(u["user_category"], color=ft.Colors.GREY_600),
                on_click=lambda e, em=u["user_email"]: (close(dlg), show_chat(em)))
                for u in others if q in full_name(u).lower()] or [ft.Text("No users found")]
            if e:
                page.update()

        search = ft.TextField(hint_text="Search people...", prefix_icon=ft.Icons.SEARCH, on_change=fill)
        fill()
        dlg = ft.AlertDialog(title=ft.Text("New conversation"),
                             content=ft.Container(ft.Column([search, tiles], tight=True), width=400),
                             actions=[ft.TextButton("Cancel", on_click=lambda e: close(dlg))])
        page.open(dlg)

    # ---------- account ----------
    def show_account():
        user = me()
        first = ft.TextField(label="First name", value=user["first_name"], width=250)
        last = ft.TextField(label="Surname", value=user["sur_name"], width=250)
        phone = ft.TextField(label="Phone", value=user["phone_number"], width=250)
        location = ft.TextField(label="Town / region", value=user.get("location", ""), width=250)
        bio = ft.TextField(label="About me (shown on your profile)", value=user.get("bio", ""), width=510,
                           multiline=True, min_lines=2, max_lines=4)
        current = ft.TextField(label="Current password", password=True, can_reveal_password=True, width=250)
        new = ft.TextField(label="New password", password=True, can_reveal_password=True, width=250)

        def save_profile(e):
            for f in (first, last, phone):
                f.error_text = None
            for f, label in ((first, "First name"), (last, "Surname"), (phone, "Phone")):
                if not f.value.strip():
                    f.error_text = f"{label} required"
            if any(f.error_text for f in (first, last, phone)):
                page.update()
                return
            app.update_user(user["user_email"], first_name=first.value.strip(), sur_name=last.value.strip(),
                            phone_number=phone.value.strip(), location=location.value.strip(), bio=bio.value.strip())
            refresh_user()
            toast("Profile saved")
            show_account()

        def change_password(e):
            current.error_text = new.error_text = None
            if not app.verify_password(current.value, me()["account_password"]):
                current.error_text = "Current password is incorrect"
            elif not app.validate_password(new.value):
                new.error_text = "Password must be 6+ characters"
            if current.error_text or new.error_text:
                page.update()
                return
            app.update_user(user["user_email"], account_password=app.hash_password(new.value))
            refresh_user()
            current.value = new.value = ""
            toast("Password changed")

        def export(e):
            try:
                if page.web:
                    raise OSError("export is only available in the desktop app")
                toast(f"Exported to {app.export_orders_csv(user['user_email'])}")
            except OSError as ex:
                toast(f"Could not export: {ex}", error=True)

        avg, count = app.farmer_rating(user["user_email"]) if is_farmer() else (None, 0)
        subtitle = f"{user['user_category']} · {user['user_email']}" + (f" · {stars_text(avg, count)}" if is_farmer() else "")
        content = [
            ft.ListTile(leading=avatar(user, radius=30), title=ft.Text(full_name(user), weight=ft.FontWeight.BOLD, size=18),
                        subtitle=ft.Text(subtitle)),
            ft.Text(f"Member since {(user.get('created_at') or 'unknown')[:10]}", color=ft.Colors.GREY_600),
            section("Profile"), wrap_row([first, last, phone, location]), bio,
            ft.ElevatedButton("Save profile", icon=ft.Icons.SAVE, on_click=save_profile),
            ft.Divider(), section("Change password"), wrap_row([current, new]),
            ft.ElevatedButton("Change password", icon=ft.Icons.LOCK_RESET, on_click=change_password),
            ft.Divider(), section("Your data"),
            wrap_row([ft.OutlinedButton("Export my orders (CSV)", icon=ft.Icons.DOWNLOAD, on_click=export),
                      ft.OutlinedButton("Log out", icon=ft.Icons.LOGOUT, on_click=logout)])]
        if is_farmer():
            content.insert(2, ft.OutlinedButton("View my public profile", icon=ft.Icons.STOREFRONT,
                                                on_click=lambda e: show_farmer_details(me(), back=show_account)))
        view("Account", content, nav="account")

    # ---------- start ----------
    def start():
        email = None
        try:
            email = page.client_storage.get("agrolink_email")
        except Exception:
            pass
        user = app.get_user(email) if email else None
        if user:
            app.current_user = user
            enter_app()
        else:
            show_landing()

    start()


if __name__ == "__main__":
    if HOSTED:  # run as a website
        os.makedirs(IMAGES_DIR, exist_ok=True)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        ft.app(target=main, view=None, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
               assets_dir=ASSETS_DIR, upload_dir=UPLOAD_DIR)
    else:  # run as a desktop app
        ft.app(target=main)
