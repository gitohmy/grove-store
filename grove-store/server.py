"""
Grove Store — Flask + SQLite backend
Runs on http://localhost:5000
"""
import hashlib, hmac, os, json, time
import urllib.request
import urllib.parse
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, redirect

import jwt as pyjwt

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY   = "grove-super-secret-key-2026"

# ── Wooppay ────────────────────────────────────────────────────────────────
WP_USERNAME = os.environ.get("WP_USERNAME", "")
WP_PASSWORD = os.environ.get("WP_PASSWORD", "")
WP_SERVICE  = os.environ.get("WP_SERVICE", "")   # Имя сервиса из кабинета Wooppay
WP_API_URL  = "https://www.wooppay.com/api/wsdl"

def wp_create_invoice(order_id, amount, description, back_url, result_url, user_email=""):
    import xml.etree.ElementTree as ET, re
    login_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:api="https://www.wooppay.com/api/">'
        '<soapenv:Body><api:core_login>'
        f'<login>{WP_USERNAME}</login><password>{WP_PASSWORD}</password>'
        '</api:core_login></soapenv:Body></soapenv:Envelope>'
    )
    req = urllib.request.Request(WP_API_URL, login_xml.encode(),
                                  {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "core_login"})
    with urllib.request.urlopen(req, timeout=10) as r:
        login_text = r.read().decode()
    m = re.search(r"<session_id[^>]*>(.*?)</session_id>", login_text)
    if not m:
        raise Exception("Wooppay: не удалось войти. Проверьте логин и пароль.")
    session_id = m.group(1)

    from datetime import timedelta
    death_date = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    service_name = WP_SERVICE or WP_USERNAME + "_invoice"
    invoice_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:api="https://www.wooppay.com/api/">'
        '<soapenv:Body><api:cash_createInvoice>'
        f'<session_id>{session_id}</session_id>'
        f'<referenceId>grove-{order_id}</referenceId>'
        f'<backUrl>{back_url}</backUrl>'
        f'<requestUrl>{result_url}</requestUrl>'
        f'<serviceName>{service_name}</serviceName>'
        '<addInfo></addInfo>'
        f'<amount>{float(amount):.2f}</amount>'
        f'<deathDate>{death_date}</deathDate>'
        f'<description>{description}</description>'
        f'<orderNumber>{order_id}</orderNumber>'
        f'<userEmail>{user_email}</userEmail>'
        '<userPhone></userPhone>'
        '</api:cash_createInvoice></soapenv:Body></soapenv:Envelope>'
    )
    req2 = urllib.request.Request(WP_API_URL, invoice_xml.encode(),
                                   {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "cash_createInvoice"})
    with urllib.request.urlopen(req2, timeout=10) as r2:
        inv_text = r2.read().decode()
    err = re.search(r"<error_code[^>]*>(.*?)</error_code>", inv_text)
    if err and err.group(1) != "0":
        desc = re.search(r"<error_description[^>]*>(.*?)</error_description>", inv_text)
        raise Exception(desc.group(1) if desc else f"Wooppay error {err.group(1)}")
    url_m = re.search(r"<invoiceUrl[^>]*>(.*?)</invoiceUrl>", inv_text)
    op_m  = re.search(r"<operationId[^>]*>(.*?)</operationId>", inv_text)
    if not url_m:
        raise Exception("Wooppay не вернул ссылку")
    return url_m.group(1), op_m.group(1) if op_m else ""

TOKEN_TTL    = 3600
REFRESH_TTL  = 604800
TAX_RATE     = 0.0
FREE_SHIP_AT = 50000
SHIP_COST    = 1990

DATABASE_URL = os.environ.get("DATABASE_URL", "")

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
app.config["SECRET_KEY"] = SECRET_KEY

# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"]       = "86400"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        from flask import Response
        res = Response()
        res.headers["Access-Control-Allow-Origin"]  = "*"
        res.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Accept"
        res.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, PUT, DELETE, OPTIONS"
        res.headers["Access-Control-Max-Age"]       = "86400"
        return res, 200


# ── Database (PostgreSQL) ─────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

def query(sql, params=(), one=False):
    # Convert SQLite ? placeholders to PostgreSQL %s
    sql = sql.replace("?", "%s")
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    db.commit()
    if one:
        row = cur.fetchone()
        return dict(row) if row else None
    return [dict(r) for r in cur.fetchall()]

def execute(sql, params=()):
    # Convert SQLite ? to %s, add RETURNING id if INSERT
    sql = sql.replace("?", "%s")
    if sql.strip().upper().startswith("INSERT") and "RETURNING" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    db.commit()
    if sql.strip().upper().startswith("INSERT"):
        row = cur.fetchone()
        return row["id"] if row else None
    return None


def init_db():
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        tables = [
            """CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name     TEXT,
                role          TEXT DEFAULT 'customer',
                is_active     INTEGER DEFAULT 1,
                created_at    TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS categories (
                id          SERIAL PRIMARY KEY,
                name        TEXT UNIQUE NOT NULL,
                slug        TEXT UNIQUE NOT NULL,
                description TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS products (
                id            SERIAL PRIMARY KEY,
                name          TEXT NOT NULL,
                slug          TEXT UNIQUE NOT NULL,
                description   TEXT,
                price         REAL NOT NULL,
                compare_price REAL,
                stock         INTEGER DEFAULT 0,
                sku           TEXT UNIQUE,
                brand         TEXT,
                badge         TEXT,
                images        TEXT DEFAULT '[]',
                specs         TEXT DEFAULT '{}',
                colors        TEXT DEFAULT '[]',
                is_active     INTEGER DEFAULT 1,
                category_id   INTEGER REFERENCES categories(id),
                created_at    TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS cart_items (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                quantity   INTEGER DEFAULT 1,
                color      TEXT,
                added_at   TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS orders (
                id               SERIAL PRIMARY KEY,
                user_id          INTEGER NOT NULL REFERENCES users(id),
                status           TEXT DEFAULT 'pending',
                subtotal         REAL NOT NULL,
                shipping_cost    REAL DEFAULT 0,
                tax              REAL DEFAULT 0,
                total            REAL NOT NULL,
                shipping_address TEXT,
                payment_ref      TEXT,
                paid_at          TIMESTAMP,
                created_at       TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS order_items (
                id           SERIAL PRIMARY KEY,
                order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id   INTEGER REFERENCES products(id),
                product_name TEXT NOT NULL,
                product_sku  TEXT,
                unit_price   REAL NOT NULL,
                quantity     INTEGER NOT NULL,
                color        TEXT,
                subtotal     REAL NOT NULL
            )""",
        ]
        for t in tables:
            cur.execute(t)
        db.commit()


# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(password):
    return hashlib.sha256(password.encode()).hexdigest()

def check_pw(password, hashed):
    return hmac.compare_digest(hash_pw(password), hashed)

def make_token(user_id, kind="access"):
    ttl = TOKEN_TTL if kind == "access" else REFRESH_TTL
    payload = {"sub": str(user_id), "type": kind, "exp": int(time.time()) + ttl}
    return pyjwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token):
    try:
        return pyjwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        return None
    except pyjwt.InvalidTokenError:
        return None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Token required"}), 401
        payload = decode_token(auth[7:])
        if not payload or payload.get("type") != "access":
            return jsonify({"error": "Invalid or expired token"}), 401
        user = query("SELECT * FROM users WHERE id=? AND is_active=1",
                     (payload["sub"],), one=True)
        if not user:
            return jsonify({"error": "User not found"}), 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.current_user["role"] != "admin":
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated

def user_out(u):
    return {k: u[k] for k in ("id","email","full_name","role","is_active","created_at")}

def product_out(p):
    p = dict(p)
    p["images"] = json.loads(p.get("images") or "[]")
    p["specs"]  = json.loads(p.get("specs")  or "{}")
    p["colors"] = json.loads(p.get("colors") or "[]")
    return p


# ════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/register", methods=["POST"])
def register():
    b = request.json or {}
    email, password = b.get("email","").strip(), b.get("password","")
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if query("SELECT id FROM users WHERE email=?", (email,), one=True):
        return jsonify({"error": "Email already registered"}), 400
    uid = execute("INSERT INTO users (email, password_hash, full_name) VALUES (?,?,?)",
                  (email, hash_pw(password), b.get("full_name","")))
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return jsonify(user_out(user)), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    b = request.json or {}
    user = query("SELECT * FROM users WHERE email=?", (b.get("email",""),), one=True)
    if not user or not check_pw(b.get("password",""), user["password_hash"]):
        return jsonify({"error": "Invalid email or password"}), 401
    if not user["is_active"]:
        return jsonify({"error": "Account disabled"}), 403
    return jsonify({
        "access_token":  make_token(user["id"], "access"),
        "refresh_token": make_token(user["id"], "refresh"),
        "token_type": "bearer",
        "user": user_out(user)
    })


@app.route("/api/auth/refresh", methods=["POST"])
def refresh():
    b = request.json or {}
    payload = decode_token(b.get("refresh_token",""))
    if not payload or payload.get("type") != "refresh":
        return jsonify({"error": "Invalid refresh token"}), 401
    user = query("SELECT * FROM users WHERE id=? AND is_active=1", (payload["sub"],), one=True)
    if not user:
        return jsonify({"error": "User not found"}), 401
    return jsonify({
        "access_token":  make_token(user["id"], "access"),
        "refresh_token": make_token(user["id"], "refresh"),
        "token_type": "bearer"
    })


@app.route("/api/auth/me", methods=["GET"])
@login_required
def me():
    return jsonify(user_out(g.current_user))


@app.route("/api/auth/me", methods=["PATCH"])
@login_required
def update_me():
    b = request.json or {}
    uid = g.current_user["id"]
    if "full_name" in b:
        execute("UPDATE users SET full_name=? WHERE id=?", (b["full_name"], uid))
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return jsonify(user_out(user))


@app.route("/api/auth/me/change-password", methods=["POST"])
@login_required
def change_password():
    b = request.json or {}
    user = g.current_user
    if not check_pw(b.get("current_password",""), user["password_hash"]):
        return jsonify({"error": "Current password wrong"}), 400
    if len(b.get("new_password","")) < 8:
        return jsonify({"error": "New password too short"}), 400
    execute("UPDATE users SET password_hash=? WHERE id=?",
            (hash_pw(b["new_password"]), user["id"]))
    return jsonify({"message": "Password changed"}), 200


# ════════════════════════════════════════════════════════════════════════════
# PRODUCT ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/products", methods=["GET"])
def list_products():
    page      = int(request.args.get("page", 1))
    per_page  = int(request.args.get("per_page", 12))
    category  = request.args.get("category")
    search    = request.args.get("search")
    brand     = request.args.get("brand")
    in_stock  = request.args.get("in_stock") == "true"

    where, params = ["p.is_active=1"], []
    if category:
        where.append("c.slug=?"); params.append(category)
    if brand:
        where.append("p.brand LIKE ?"); params.append(f"%{brand}%")
    if search:
        where.append("(p.name LIKE ? OR p.description LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    if in_stock:
        where.append("p.stock>0")

    sql_base = f"""
        FROM products p LEFT JOIN categories c ON p.category_id=c.id
        WHERE {' AND '.join(where)}
    """
    sort_param = request.args.get("sort", "popular")
    sort_map = {
        "popular":    "c.id ASC, p.price DESC",
        "price_asc":  "p.price ASC",
        "price_desc": "p.price DESC",
        "name_asc":   "p.name ASC",
        "newest":     "p.badge='Новинка' DESC, p.id DESC",
    }
    sort_sql = sort_map.get(sort_param, "c.id ASC, p.price DESC")
    total  = query(f"SELECT COUNT(*) as n {sql_base}", params, one=True)["n"]
    rows   = query(f"SELECT p.*, c.name as category_name, c.slug as category_slug "
                   f"{sql_base} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
                   params + [per_page, (page-1)*per_page])

    import math
    return jsonify({
        "items":  [product_out(r) for r in rows],
        "total":  total,
        "page":   page,
        "pages":  math.ceil(total/per_page) if total else 1
    })


@app.route("/api/products/categories", methods=["GET"])
def list_categories():
    return jsonify(query("SELECT * FROM categories"))


@app.route("/api/products/<slug>", methods=["GET"])
def get_product(slug):
    p = query("SELECT p.*, c.name as category_name FROM products p "
              "LEFT JOIN categories c ON p.category_id=c.id "
              "WHERE p.slug=? AND p.is_active=1", (slug,), one=True)
    if not p:
        return jsonify({"error": "Not found"}), 404
    return jsonify(product_out(p))


@app.route("/api/products", methods=["POST"])
@admin_required
def create_product():
    b = request.json or {}
    pid = execute(
        "INSERT INTO products (name,slug,description,price,compare_price,stock,sku,"
        "brand,badge,images,specs,colors,category_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (b["name"], b["slug"], b.get("description"), b["price"],
         b.get("compare_price"), b.get("stock",0), b.get("sku"),
         b.get("brand"), b.get("badge"),
         json.dumps(b.get("images",[])), json.dumps(b.get("specs",{})),
         json.dumps(b.get("colors",[])), b.get("category_id"))
    )
    return jsonify(product_out(query("SELECT * FROM products WHERE id=?", (pid,), one=True))), 201


@app.route("/api/products/<int:pid>", methods=["PATCH"])
@admin_required
def update_product(pid):
    b = request.json or {}
    p = query("SELECT * FROM products WHERE id=?", (pid,), one=True)
    if not p:
        return jsonify({"error": "Not found"}), 404
    fields = {**dict(p), **b}
    execute("UPDATE products SET name=?,description=?,price=?,compare_price=?,"
            "stock=?,badge=?,images=?,specs=?,colors=?,is_active=? WHERE id=?",
            (fields["name"], fields.get("description"), fields["price"],
             fields.get("compare_price"), fields["stock"], fields.get("badge"),
             json.dumps(fields.get("images",[])), json.dumps(fields.get("specs",{})),
             json.dumps(fields.get("colors",[])), fields.get("is_active",1), pid))
    return jsonify(product_out(query("SELECT * FROM products WHERE id=?", (pid,), one=True)))


@app.route("/api/products/<int:pid>", methods=["DELETE"])
@admin_required
def delete_product(pid):
    execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
    return jsonify({"message": "Deleted"}), 200


# ════════════════════════════════════════════════════════════════════════════
# CART ROUTES
# ════════════════════════════════════════════════════════════════════════════

def build_cart(user_id):
    items = query(
        "SELECT ci.*, p.name, p.price, p.images, p.slug, p.brand, p.badge, p.specs, p.colors, p.stock "
        "FROM cart_items ci JOIN products p ON ci.product_id=p.id "
        "WHERE ci.user_id=?", (user_id,))
    out = []
    for item in items:
        item = dict(item)
        item["images"] = json.loads(item.get("images") or "[]")
        item["specs"]  = json.loads(item.get("specs")  or "{}")
        item["colors"] = json.loads(item.get("colors") or "[]")
        item["line_total"] = round(item["price"] * item["quantity"], 2)
        out.append(item)
    subtotal   = round(sum(i["line_total"] for i in out), 2)
    item_count = sum(i["quantity"] for i in out)
    return {"items": out, "subtotal": subtotal, "item_count": item_count}


@app.route("/api/cart", methods=["GET"])
@login_required
def get_cart():
    return jsonify(build_cart(g.current_user["id"]))


@app.route("/api/cart", methods=["POST"])
@login_required
def add_to_cart():
    b = request.json or {}
    product_id = b.get("product_id")
    quantity   = int(b.get("quantity", 1))
    color      = b.get("color")
    uid        = g.current_user["id"]

    product = query("SELECT * FROM products WHERE id=? AND is_active=1", (product_id,), one=True)
    if not product:
        return jsonify({"error": "Product not found"}), 404
    if product["stock"] < quantity:
        return jsonify({"error": f"Only {product['stock']} in stock"}), 400

    existing = query("SELECT * FROM cart_items WHERE user_id=? AND product_id=? AND (color=? OR (color IS NULL AND ? IS NULL))",
                     (uid, product_id, color, color), one=True)
    if existing:
        new_qty = existing["quantity"] + quantity
        if new_qty > product["stock"]:
            return jsonify({"error": f"Cannot exceed stock of {product['stock']}"}), 400
        execute("UPDATE cart_items SET quantity=? WHERE id=?", (new_qty, existing["id"]))
    else:
        execute("INSERT INTO cart_items (user_id, product_id, quantity, color) VALUES (?,?,?,?)",
                (uid, product_id, quantity, color))

    return jsonify(build_cart(uid)), 201


@app.route("/api/cart/<int:item_id>", methods=["PATCH"])
@login_required
def update_cart_item(item_id):
    b    = request.json or {}
    uid  = g.current_user["id"]
    item = query("SELECT * FROM cart_items WHERE id=? AND user_id=?", (item_id, uid), one=True)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    qty = int(b.get("quantity", 1))
    product = query("SELECT * FROM products WHERE id=?", (item["product_id"],), one=True)
    if qty > product["stock"]:
        return jsonify({"error": f"Only {product['stock']} in stock"}), 400
    execute("UPDATE cart_items SET quantity=? WHERE id=?", (qty, item_id))
    return jsonify(build_cart(uid))


@app.route("/api/cart/<int:item_id>", methods=["DELETE"])
@login_required
def remove_cart_item(item_id):
    uid = g.current_user["id"]
    execute("DELETE FROM cart_items WHERE id=? AND user_id=?", (item_id, uid))
    return jsonify(build_cart(uid))


@app.route("/api/cart", methods=["DELETE"])
@login_required
def clear_cart():
    execute("DELETE FROM cart_items WHERE user_id=?", (g.current_user["id"],))
    return jsonify({"message": "Cart cleared"}), 200


# ════════════════════════════════════════════════════════════════════════════
# ORDER ROUTES
# ════════════════════════════════════════════════════════════════════════════

def order_out(order_id):
    o = query("SELECT * FROM orders WHERE id=?", (order_id,), one=True)
    if not o:
        return None
    o = dict(o)
    o["shipping_address"] = json.loads(o.get("shipping_address") or "{}")
    o["items"] = query("SELECT * FROM order_items WHERE order_id=?", (order_id,))
    return o


@app.route("/api/orders/checkout", methods=["POST"])
@login_required
def checkout():
    b   = request.json or {}
    uid = g.current_user["id"]
    cart_items = query(
        "SELECT ci.*, p.name, p.price, p.sku, p.stock, p.is_active "
        "FROM cart_items ci JOIN products p ON ci.product_id=p.id WHERE ci.user_id=?", (uid,))

    if not cart_items:
        return jsonify({"error": "Cart is empty"}), 400

    # Resolve address
    address = b.get("address") or {}
    if not address.get("city"):
        address = {"line1":"—","city":"—","country":"—","postal_code":"—"}

    # Validate stock
    subtotal = 0.0
    for item in cart_items:
        if not item["is_active"]:
            return jsonify({"error": f"{item['name']} no longer available"}), 400
        if item["stock"] < item["quantity"]:
            return jsonify({"error": f"Not enough stock for {item['name']}"}), 400
        subtotal += item["price"] * item["quantity"]

    subtotal    = round(subtotal, 2)
    shipping    = 0.0 if subtotal >= FREE_SHIP_AT else SHIP_COST
    tax         = round(subtotal * TAX_RATE, 2)
    total       = round(subtotal + shipping + tax, 2)

    oid = execute(
        "INSERT INTO orders (user_id,status,subtotal,shipping_cost,tax,total,shipping_address) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, "pending", subtotal, shipping, tax, total, json.dumps(address)))

    for item in cart_items:
        execute("INSERT INTO order_items (order_id,product_id,product_name,product_sku,"
                "unit_price,quantity,color,subtotal) VALUES (?,?,?,?,?,?,?,?)",
                (oid, item["product_id"], item["name"], item["sku"],
                 item["price"], item["quantity"], item.get("color"),
                 round(item["price"]*item["quantity"], 2)))
        execute("UPDATE products SET stock=stock-? WHERE id=?",
                (item["quantity"], item["product_id"]))

    execute("DELETE FROM cart_items WHERE user_id=?", (uid,))
    return jsonify(order_out(oid)), 201


@app.route("/api/orders", methods=["GET"])
@login_required
def list_orders():
    uid      = g.current_user["id"]
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))
    import math
    total  = query("SELECT COUNT(*) as n FROM orders WHERE user_id=?", (uid,), one=True)["n"]
    orders = query("SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                   (uid, per_page, (page-1)*per_page))
    items = []
    for o in orders:
        o = dict(o)
        o["shipping_address"] = json.loads(o.get("shipping_address") or "{}")
        o["items"] = query("SELECT * FROM order_items WHERE order_id=?", (o["id"],))
        items.append(o)
    return jsonify({"items": items, "total": total, "page": page,
                    "pages": math.ceil(total/per_page) if total else 1})


@app.route("/api/orders/<int:oid>", methods=["GET"])
@login_required
def get_order(oid):
    uid = g.current_user["id"]
    o = query("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid), one=True)
    if not o:
        return jsonify({"error": "Order not found"}), 404
    o = dict(o)
    o["shipping_address"] = json.loads(o.get("shipping_address") or "{}")
    o["items"] = query("SELECT * FROM order_items WHERE order_id=?", (oid,))
    return jsonify(o)


@app.route("/api/orders/<int:oid>/cancel", methods=["POST"])
@login_required
def cancel_order(oid):
    uid = g.current_user["id"]
    o = query("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid), one=True)
    if not o:
        return jsonify({"error": "Order not found"}), 404
    if o["status"] not in ("pending",):
        return jsonify({"error": "Отменить можно только заказы в статусе 'Ожидает'"}), 400
    for item in query("SELECT * FROM order_items WHERE order_id=?", (oid,)):
        execute("UPDATE products SET stock=stock+? WHERE id=?",
                (item["quantity"], item["product_id"]))
    execute("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    return jsonify(order_out(oid))


@app.route("/api/orders/<int:oid>/delete", methods=["DELETE"])
@login_required
def delete_order(oid):
    uid = g.current_user["id"]
    o = query("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid), one=True)
    if not o:
        return jsonify({"error": "Order not found"}), 404
    if o["status"] not in ("cancelled", "refunded", "delivered"):
        return jsonify({"error": "Удалить можно только отменённые или доставленные заказы"}), 400
    execute("DELETE FROM order_items WHERE order_id=?", (oid,))
    execute("DELETE FROM orders WHERE id=?", (oid,))
    return jsonify({"deleted": True})


@app.route("/api/orders/<int:oid>/refund", methods=["POST"])
@login_required
def refund_order(oid):
    uid = g.current_user["id"]
    o = query("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid), one=True)
    if not o:
        return jsonify({"error": "Order not found"}), 404
    if o["status"] not in ("paid", "shipped"):
        return jsonify({"error": "Возврат возможен только для оплаченных заказов"}), 400
    # Restore stock
    for item in query("SELECT * FROM order_items WHERE order_id=?", (oid,)):
        execute("UPDATE products SET stock=stock+? WHERE id=?",
                (item["quantity"], item["product_id"]))
    execute("UPDATE orders SET status='refunded' WHERE id=?", (oid,))
    # TODO: When real payment gateway is connected, trigger actual refund here:
    # e.g. wp_refund(o["payment_ref"], o["total"])
    return jsonify(order_out(oid))


# ════════════════════════════════════════════════════════════════════════════
# PAYMENT ROUTES (simulated — no Stripe needed)
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/payments/pay", methods=["POST"])
@login_required
def initiate_payment():
    """
    Creates a Wooppay invoice and returns redirect URL.
    Accepts: { "order_id": 1 }
    Returns: { "redirect_url": "https://wooppay.com/..." } or { "success": true } if not configured
    """
    b   = request.json or {}
    uid = g.current_user["id"]
    oid = b.get("order_id")

    o = query("SELECT * FROM orders WHERE id=? AND user_id=? AND status='pending'",
              (oid, uid), one=True)
    if not o:
        return jsonify({"error": "Заказ не найден"}), 404

    # If Wooppay not configured — simulate payment
    if not WP_USERNAME or not WP_PASSWORD:
        ref = f"PAY-{oid}-{int(time.time())}"
        execute("UPDATE orders SET status='paid', payment_ref=?, paid_at=? WHERE id=?",
                (ref, datetime.now(timezone.utc).isoformat(), oid))
        return jsonify({"success": True, "payment_ref": ref, "order": order_out(oid)})

    user = g.current_user
    base_url = request.host_url.rstrip("/")
    try:
        redirect_url, op_id = wp_create_invoice(
            order_id    = oid,
            amount      = o["total"],
            description = f"Grove Store — заказ #{oid}",
            back_url    = f"{base_url}/payment/success?order_id={oid}",
            result_url  = f"{base_url}/api/payments/result",
            user_email  = user.get("email", ""),
        )
        execute("UPDATE orders SET payment_ref=? WHERE id=?", (f"WP-{op_id}", oid))
        return jsonify({"redirect_url": redirect_url, "order_id": oid})
    except Exception as e:
        print(f"[Wooppay ERROR] {e}", flush=True)
        return jsonify({"error": f"Wooppay: {e}"}), 500


@app.route("/api/payments/result", methods=["POST", "GET"])
def payment_result():
    """Wooppay calls this URL after payment."""
    data = request.json or request.form or {}
    op_id  = data.get("operation_id") or data.get("operationId", "")
    status = str(data.get("status", "")).lower()
    oid    = data.get("order_number") or data.get("orderNumber", "")

    if status in ("1", "paid", "success", "done") or op_id:
        if oid:
            execute("UPDATE orders SET status='paid', paid_at=? WHERE id=? AND status='pending'",
                    (datetime.now(timezone.utc).isoformat(), oid))
        elif op_id:
            execute("UPDATE orders SET status='paid', paid_at=? WHERE payment_ref=? AND status='pending'",
                    (datetime.now(timezone.utc).isoformat(), f"WP-{op_id}"))

    return jsonify({"result": True})


@app.route("/payment/success")
def payment_success():
    oid = request.args.get("order_id", "")
    if oid:
        execute("UPDATE orders SET status='paid' WHERE id=? AND status='pending'", (oid,))
    return redirect("/")


@app.route("/payment/failure")
def payment_failure():
    return redirect("/")


# ════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/orders", methods=["GET"])
@admin_required
def admin_orders():
    import math
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    status   = request.args.get("status")
    where, params = [], []
    if status:
        where.append("status=?"); params.append(status)
    wstr  = f"WHERE {' AND '.join(where)}" if where else ""
    total = query(f"SELECT COUNT(*) as n FROM orders {wstr}", params, one=True)["n"]
    rows  = query(f"SELECT * FROM orders {wstr} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                  params + [per_page, (page-1)*per_page])
    items = []
    for o in rows:
        o = dict(o)
        o["shipping_address"] = json.loads(o.get("shipping_address") or "{}")
        o["items"] = query("SELECT * FROM order_items WHERE order_id=?", (o["id"],))
        items.append(o)
    return jsonify({"items": items, "total": total, "page": page,
                    "pages": math.ceil(total/per_page) if total else 1})


@app.route("/api/admin/orders/<int:oid>/status", methods=["PATCH"])
@admin_required
def admin_set_status(oid):
    b      = request.json or {}
    status = b.get("status")
    valid  = ("pending","paid","shipped","delivered","cancelled","refunded")
    if status not in valid:
        return jsonify({"error": f"Status must be one of {valid}"}), 400
    execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    return jsonify(order_out(oid))


@app.route("/api/admin/stats", methods=["GET"])
@admin_required
def admin_stats():
    total_orders   = query("SELECT COUNT(*) as n FROM orders", one=True)["n"]
    total_revenue  = query("SELECT SUM(total) as s FROM orders WHERE status IN ('paid','shipped','delivered')", one=True)["s"] or 0
    total_products = query("SELECT COUNT(*) as n FROM products WHERE is_active=1", one=True)["n"]
    total_users    = query("SELECT COUNT(*) as n FROM users WHERE role='customer'", one=True)["n"]
    return jsonify({
        "total_orders":   total_orders,
        "total_revenue":  round(total_revenue, 2),
        "total_products": total_products,
        "total_users":    total_users,
    })


# ════════════════════════════════════════════════════════════════════════════
# HEALTH
# ════════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Grove Store API", "time": datetime.now().isoformat()})

@app.route("/api")
def root():
    return jsonify({"message": "🌿 Grove Store API", "docs": "/api/products"})


# ════════════════════════════════════════════════════════════════════════════
# SEED DATA
# ════════════════════════════════════════════════════════════════════════════

def seed():
    with app.app_context():
        # Admin user
        if not query("SELECT id FROM users WHERE email='admin@grove.com'", one=True):
            execute("INSERT INTO users (email,password_hash,full_name,role) VALUES (?,?,?,?)",
                    ("admin@grove.com", hash_pw("admin1234"), "Grove Admin", "admin"))
            print("✓ Admin: admin@grove.com / admin1234")

        # Demo customer
        demo = query("SELECT id FROM users WHERE email='demo@grove.com'", one=True)
        if not demo:
            execute("INSERT INTO users (email,password_hash,full_name) VALUES (?,?,?)",
                    ("demo@grove.com", hash_pw("demo1234"), "Demo User"))
            print("✓ Customer: demo@grove.com / demo1234")
        else:
            execute("UPDATE users SET password_hash=? WHERE email='demo@grove.com'",
                    (hash_pw("demo1234"),))

        # Categories
        cats = [("Flagship","flagship"),("Mid-Range","mid-range"),("Budget","budget")]
        cat_ids = {}
        for name, slug in cats:
            c = query("SELECT id FROM categories WHERE slug=?", (slug,), one=True)
            if not c:
                cid = execute("INSERT INTO categories (name,slug) VALUES (?,?)", (name,slug))
                cat_ids[slug] = cid
                print(f"✓ Category: {name}")
            else:
                cat_ids[slug] = c["id"]

        # Продукты с technodom.kz (цены в тенге)
        products = [
            # (name, slug, brand, price, compare_price, stock, badge, category, desc, specs, colors)

            # ФЛАГМАНЫ
            ("Apple iPhone 17 Pro 256GB Deep Blue", "iphone-17-pro-blue", "Apple",
             852990, 885990, 20, "Хит", "flagship",
             "Флагман Apple 2025. Titanium корпус, чип A19 Bionic, 48 Мп камера, Dynamic Island.",
             {"Дисплей": "6.3 Super Retina XDR", "Камера": "48 Мп + зум 5x", "Чип": "A19 Bionic", "Батарея": "до 27 ч", "Память": "256 ГБ"},
             [{"name":"Deep Blue","hex":"#2c4a7a"},{"name":"Silver","hex":"#e8e8e8"},{"name":"Cosmic Orange","hex":"#c4622a"},{"name":"Black","hex":"#1a1a1a"}]),
            ("Apple iPhone 17 Pro Max 256GB", "iphone-17-pro-max", "Apple",
             915990, 951990, 12, "Новинка", "flagship",
             "Самый мощный iPhone. Дисплей 6.9 дюйма, батарея до 33 часов, камера 48 Мп.",
             {"Дисплей": "6.9 Super Retina XDR", "Камера": "48 Мп + зум 5x", "Чип": "A19 Bionic", "Батарея": "до 33 ч", "Память": "256 ГБ"},
             [{"name":"Silver","hex":"#e8e8e8"},{"name":"Deep Blue","hex":"#2c4a7a"},{"name":"Desert Gold","hex":"#c9a84c"},{"name":"Black","hex":"#1a1a1a"}]),
            ("Apple iPhone 17 256GB Black", "iphone-17-black", "Apple",
             615990, 639990, 30, None, "flagship",
             "Новый iPhone 17 — тонкий алюминиевый корпус, 48 Мп камера, чип A18, Apple Intelligence.",
             {"Дисплей": "6.3 Super Retina XDR", "Камера": "48 Мп", "Чип": "A18", "Батарея": "до 22 ч", "Память": "256 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"White","hex":"#f5f5f0"},{"name":"Pink","hex":"#e8b0b0"},{"name":"Teal","hex":"#4a9090"}]),
            ("Apple iPhone 16e 256GB", "iphone-16e", "Apple",
             349990, 379990, 35, "Акция", "flagship",
             "Доступный iPhone с чипом A16 Bionic и поддержкой Apple Intelligence.",
             {"Дисплей": "6.1 Super Retina XDR", "Камера": "48 Мп", "Чип": "A16 Bionic", "Батарея": "до 26 ч", "Память": "256 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"White","hex":"#f5f5f0"}]),
            ("Samsung Galaxy S25 Ultra 12/256GB", "samsung-s25-ultra", "Samsung",
             699990, 799990, 18, "Хит", "flagship",
             "Лучший Android 2025. S Pen, 200 Мп камера, Snapdragon 8 Elite, Galaxy AI.",
             {"Дисплей": "6.9 QHD+ AMOLED 120 Гц", "Камера": "200 Мп + S Pen", "Чип": "Snapdragon 8 Elite", "Батарея": "5000 мАч", "Память": "256 ГБ"},
             [{"name":"Titanium Black","hex":"#2a2a2a"},{"name":"Titanium Gray","hex":"#8a8a8a"},{"name":"Whitesilver","hex":"#d8d8d8"},{"name":"Blue","hex":"#2a4a6a"}]),
            ("Samsung Galaxy S25+ 12/256GB", "samsung-s25-plus", "Samsung",
             499990, 549990, 22, None, "flagship",
             "Флагман Samsung с большим экраном 6.7 дюйма и Snapdragon 8 Elite.",
             {"Дисплей": "6.7 FHD+ AMOLED 120 Гц", "Камера": "50 Мп тройная", "Чип": "Snapdragon 8 Elite", "Батарея": "4900 мАч", "Память": "256 ГБ"},
             [{"name":"Navy","hex":"#1a2a4a"},{"name":"Silver Shadow","hex":"#c0c0c0"},{"name":"Icy Blue","hex":"#a0c8d8"},{"name":"Mint","hex":"#a0c8b0"}]),
            ("Samsung Galaxy S25 12/256GB", "samsung-s25", "Samsung",
             389990, 429990, 28, "Популярный", "flagship",
             "Компактный флагман Samsung. Snapdragon 8 Elite, тонкий корпус 7.2 мм.",
             {"Дисплей": "6.2 FHD+ AMOLED 120 Гц", "Камера": "50 Мп", "Чип": "Snapdragon 8 Elite", "Батарея": "4000 мАч", "Корпус": "7.2 мм"},
             [{"name":"Navy","hex":"#1a2a4a"},{"name":"Icy Blue","hex":"#a0c8d8"},{"name":"Mint","hex":"#a0c8b0"},{"name":"Silver Shadow","hex":"#c0c0c0"}]),
            ("Samsung Galaxy Z Flip 7 256GB", "samsung-z-flip7", "Samsung",
             649990, None, 10, "Новинка", "flagship",
             "Складной смартфон-раскладушка 2025 года.",
             {"Дисплей": "6.7 FHD+ AMOLED", "Камера": "50 Мп", "Чип": "Snapdragon 8 Elite", "Батарея": "4300 мАч", "Форм-фактор": "Складной"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Blue","hex":"#2a4a7a"},{"name":"Mint","hex":"#80c0a0"}]),

            # СРЕДНИЙ КЛАСС
            ("Samsung Galaxy A56 5G 8/256GB", "samsung-a56", "Samsung",
             249990, 279990, 45, "Популярный", "mid-range",
             "Флагман серии A 2025. 5G, AMOLED 120 Гц, IP67, тройная камера 50 Мп.",
             {"Дисплей": "6.7 FHD+ AMOLED 120 Гц", "Камера": "50 Мп тройная", "Чип": "Exynos 1580", "Батарея": "5000 мАч", "Защита": "IP67"},
             [{"name":"Iceblue","hex":"#8ab8d0"},{"name":"Navy","hex":"#1c2e4a"},{"name":"Lilac","hex":"#a888c8"},{"name":"White","hex":"#f0f0f0"}]),
            ("Samsung Galaxy A36 5G 8/256GB", "samsung-a36", "Samsung",
             189990, 219990, 50, None, "mid-range",
             "AMOLED 120 Гц, IP67 и Snapdragon 6 Gen 3.",
             {"Дисплей": "6.66 FHD+ AMOLED 120 Гц", "Камера": "50 Мп", "Чип": "Snapdragon 6 Gen 3", "Батарея": "5000 мАч", "Защита": "IP67"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"White","hex":"#f0f0f0"},{"name":"Lilac","hex":"#a888c8"}]),
            ("Samsung Galaxy A26 5G 8/256GB", "samsung-a26", "Samsung",
             149990, None, 60, None, "mid-range",
             "5G смартфон с Super AMOLED дисплеем и батареей 5000 мАч.",
             {"Дисплей": "6.6 FHD+ Super AMOLED", "Камера": "50 Мп", "Чип": "Exynos 850", "Батарея": "5000 мАч", "5G": "Да"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Iceblue","hex":"#8ab8d0"},{"name":"Lilac","hex":"#a888c8"}]),
            ("Xiaomi Redmi Note 14 Pro+ 5G 8/256GB", "redmi-note14-proplus", "Xiaomi",
             179990, 199990, 55, "Популярный", "mid-range",
             "5G, AMOLED 120 Гц, камера 200 Мп, Snapdragon 7s Gen 3.",
             {"Дисплей": "6.67 AMOLED 120 Гц", "Камера": "200 Мп", "Чип": "Snapdragon 7s Gen 3", "Батарея": "5110 мАч", "5G": "Да"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Lavender","hex":"#9878c8"},{"name":"Ocean Blue","hex":"#1a4a6a"}]),
            ("Xiaomi Redmi Note 14 8/256GB", "redmi-note14", "Xiaomi",
             107990, 119990, 70, None, "mid-range",
             "Лучшее за деньги. AMOLED 120 Гц, камера 108 Мп, батарея 5500 мАч.",
             {"Дисплей": "6.67 AMOLED 120 Гц", "Камера": "108 Мп", "Чип": "Helio G99 Ultra", "Батарея": "5500 мАч", "Память": "256 ГБ"},
             [{"name":"Ocean Blue","hex":"#1a4a6a"},{"name":"Black","hex":"#1a1a1a"},{"name":"Silver","hex":"#c8d8e0"}]),
            ("OPPO Reno14 F 8/256GB", "oppo-reno14f", "OPPO",
             139990, 159990, 40, None, "mid-range",
             "Тонкий смартфон с AMOLED, AI-камерой 50 Мп и зарядкой 67 Вт.",
             {"Дисплей": "6.67 AMOLED 120 Гц", "Камера": "50 Мп AI", "Чип": "Dimensity 7300", "Батарея": "5000 мАч", "Зарядка": "67 Вт"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Gold","hex":"#c8a840"},{"name":"Purple","hex":"#7858a0"}]),
            ("Vivo V60 8/256GB", "vivo-v60", "Vivo",
             229990, 259990, 30, None, "mid-range",
             "AMOLED 120 Гц, камера 50 Мп, батарея 6500 мАч и зарядка 90 Вт.",
             {"Дисплей": "6.78 AMOLED 120 Гц", "Камера": "50 Мп", "Чип": "Snapdragon 7 Gen 3", "Батарея": "6500 мАч", "Зарядка": "90 Вт"},
             [{"name":"Titanium Gray","hex":"#707070"},{"name":"Starlight Silver","hex":"#c0c8d0"}]),
            ("Vivo V50 Lite 8/256GB", "vivo-v50-lite", "Vivo",
             154990, 174990, 45, None, "mid-range",
             "Тонкий 7.9 мм смартфон с ярким AMOLED и батареей 6500 мАч.",
             {"Дисплей": "6.77 AMOLED 120 Гц", "Камера": "50 Мп", "Чип": "Snapdragon 685", "Батарея": "6500 мАч", "Корпус": "7.9 мм"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Gold","hex":"#d0a850"}]),
            ("HONOR Magic7 8/256GB", "honor-magic7", "HONOR",
             319990, 349990, 25, None, "mid-range",
             "Kirin 9020, AMOLED 120 Гц, тройная камера 50 Мп, зарядка 100 Вт.",
             {"Дисплей": "6.8 LTPO AMOLED 120 Гц", "Камера": "50 Мп тройная", "Чип": "Kirin 9020", "Батарея": "5600 мАч", "Зарядка": "100 Вт"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Silver","hex":"#d0d0d0"},{"name":"Green","hex":"#405840"}]),
            ("Motorola Edge 60 Fusion 8/256GB", "motorola-edge60", "Motorola",
             179990, None, 35, None, "mid-range",
             "Тонкий смартфон с pOLED 144 Гц и защитой IP68.",
             {"Дисплей": "6.7 pOLED 144 Гц", "Камера": "50 Мп", "Чип": "Dimensity 7400", "Батарея": "5000 мАч", "Защита": "IP68"},
             [{"name":"Lavender","hex":"#9898b8"},{"name":"Orange","hex":"#d07840"}]),

            # БЮДЖЕТНЫЕ
            ("Samsung Galaxy A17 6/128GB", "samsung-a17", "Samsung",
             99890, 114890, 100, None, "budget",
             "Большой дисплей 6.7 дюйма и тройная камера 50 Мп.",
             {"Дисплей": "6.7 FHD+ PLS LCD", "Камера": "50 Мп тройная", "Чип": "Helio G99", "Батарея": "5000 мАч", "Память": "128 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Blue","hex":"#2050a0"},{"name":"Gold","hex":"#c8a040"}]),
            ("Samsung Galaxy A16 6/128GB", "samsung-a16", "Samsung",
             89890, None, 110, None, "budget",
             "Super AMOLED и обновления ОС на 6 лет.",
             {"Дисплей": "6.7 FHD+ Super AMOLED", "Камера": "50 Мп", "Чип": "Helio G85", "Батарея": "5000 мАч", "Обновления": "6 лет"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Light Green","hex":"#70a868"},{"name":"Light Blue","hex":"#88a8c8"}]),
            ("Samsung Galaxy A07 6/128GB", "samsung-a07", "Samsung",
             74890, 84890, 120, None, "budget",
             "Простой и надёжный смартфон для повседневных задач.",
             {"Дисплей": "6.5 FHD+ PLS LCD", "Камера": "50 Мп", "Чип": "Exynos 850", "Батарея": "5000 мАч", "Память": "128 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Light Violet","hex":"#a898c8"},{"name":"Light Blue","hex":"#88a8c8"}]),
            ("Xiaomi Redmi A5 4/128GB", "xiaomi-redmi-a5", "Xiaomi",
             49990, None, 150, None, "budget",
             "Самый доступный Xiaomi 2025. Дисплей 6.88 дюйма, батарея 5160 мАч.",
             {"Дисплей": "6.88 IPS LCD 90 Гц", "Камера": "13 Мп", "Чип": "Helio G36", "Батарея": "5160 мАч", "Память": "128 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Silver","hex":"#c8c8c8"},{"name":"Blue","hex":"#2040a0"}]),
            ("Vivo Y29 6/128GB", "vivo-y29", "Vivo",
             74990, 89990, 90, None, "budget",
             "Батарея 6000 мАч и зарядка 44 Вт. Хватает на два дня.",
             {"Дисплей": "6.68 IPS LCD 90 Гц", "Камера": "50 Мп", "Чип": "Snapdragon 680", "Батарея": "6000 мАч", "Зарядка": "44 Вт"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Gold","hex":"#c8a840"},{"name":"Green","hex":"#406040"}]),
            ("TECNO Spark 20 Pro+ 8/256GB", "tecno-spark20", "TECNO",
             84990, 99990, 80, None, "budget",
             "AMOLED 120 Гц и камера 108 Мп в бюджетном сегменте.",
             {"Дисплей": "6.78 AMOLED 120 Гц", "Камера": "108 Мп", "Чип": "Helio G100", "Батарея": "5000 мАч", "Память": "256 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Starry Night","hex":"#1a1a3a"},{"name":"Golden","hex":"#c0a040"}]),
            ("Infinix Hot 40i 4/128GB", "infinix-hot40i", "Infinix",
             44990, 54990, 160, None, "budget",
             "Бюджетный смартфон с большим экраном и AI-камерой.",
             {"Дисплей": "6.56 IPS LCD 90 Гц", "Камера": "50 Мп AI", "Чип": "Helio G88", "Батарея": "5000 мАч", "Память": "128 ГБ"},
             [{"name":"Black","hex":"#1a1a1a"},{"name":"Gold","hex":"#c8a848"},{"name":"Blue","hex":"#2050a0"}]),
            ("OPPO A5 Pro 8/256GB", "oppo-a5-pro", "OPPO",
             109990, 124990, 65, None, "budget",
             "Защита IP69, AMOLED и зарядка 45 Вт в бюджетном сегменте.",
             {"Дисплей": "6.67 AMOLED 120 Гц", "Камера": "50 Мп", "Чип": "Dimensity 6300", "Батарея": "5100 мАч", "Защита": "IP69"},
             [{"name":"Black","hex":"#1a1a2a"},{"name":"White","hex":"#f0f0f8"},{"name":"Blue","hex":"#204878"}]),
        ]
        for name,slug,brand,price,cmp,stock,badge,cat_key,desc,specs,colors in products:
            if not query("SELECT id FROM products WHERE slug=?", (slug,), one=True):
                execute(
                    "INSERT INTO products (name,slug,brand,price,compare_price,stock,badge,"
                    "category_id,description,specs,colors) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name,slug,brand,price,cmp,stock,badge,cat_ids[cat_key],desc,
                     json.dumps(specs), json.dumps(colors)))
                print(f"✓ Product: {name}")

        print("\n🌿 Seed complete!")


# Serve the frontend HTML (embedded)
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grove — Phones, Naturally</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;1,400;1,600&family=Jost:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root{--moss:#3b4a32;--fern:#5a7247;--sage:#8aaa76;--mist:#c8d9bc;--cream:#f4f0e6;--parchment:#ede7d5;--bark:#7a5c3e;--clay:#b5836a;--sky:#a8c5c0;--dusk:#2e3d30;--text:#2a2e22;--muted:#7a806e;--border:#d4cdb8;}
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box;}
html{scroll-behavior:smooth;}
body{font-family:'Jost',sans-serif;background:var(--cream);color:var(--text);overflow-x:hidden;}

/* ── PROMO BANNER ── */
.promo-bar{background:var(--moss);color:var(--cream);text-align:center;padding:.55rem 1rem;font-size:.78rem;font-weight:500;letter-spacing:.06em;position:relative;}
.promo-bar a{color:var(--sage);text-decoration:underline;cursor:pointer;}
.promo-close{position:absolute;right:1rem;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--cream);cursor:pointer;font-size:1rem;opacity:.6;}
.promo-close:hover{opacity:1;}

/* ── NAV ── */
nav{position:sticky;top:0;z-index:200;background:rgba(244,240,230,0.95);backdrop-filter:blur(18px);border-bottom:1px solid var(--border);}
.nav-inner{display:flex;align-items:center;justify-content:space-between;padding:1rem 3rem;gap:1rem;}
.nav-brand{font-family:'Playfair Display',serif;font-size:1.6rem;font-style:italic;color:var(--moss);text-decoration:none;white-space:nowrap;}
.nav-brand span{color:var(--clay);font-style:normal;}
.nav-links{display:flex;gap:1.8rem;list-style:none;}
.nav-links a{font-size:.78rem;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-decoration:none;transition:color .2s;cursor:pointer;}
.nav-links a:hover{color:var(--moss);}
.nav-search{display:flex;align-items:center;gap:.5rem;background:var(--parchment);border:1px solid var(--border);border-radius:100px;padding:.4rem .9rem;flex:1;max-width:240px;transition:border-color .2s;}
.nav-search:focus-within{border-color:var(--fern);}
.nav-search input{background:none;border:none;outline:none;font-family:'Jost',sans-serif;font-size:.82rem;color:var(--text);width:100%;}
.nav-search input::placeholder{color:var(--muted);}
.nav-right{display:flex;gap:.6rem;align-items:center;white-space:nowrap;}
.nav-btn{background:none;border:none;font-family:'Jost',sans-serif;font-size:.78rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;padding:.5rem 1rem;border-radius:100px;transition:all .2s;}
.nav-btn.primary{background:var(--moss);color:var(--cream);}
.nav-btn.primary:hover{background:var(--dusk);}
.nav-btn.ghost{border:1px solid var(--border);color:var(--muted);}
.nav-btn.ghost:hover{border-color:var(--moss);color:var(--moss);}
.nav-icon-btn{background:none;border:none;cursor:pointer;font-size:1.1rem;padding:.3rem;position:relative;transition:transform .2s;}
.nav-icon-btn:hover{transform:scale(1.15);}
.badge{position:absolute;top:-5px;right:-5px;background:var(--clay);color:#fff;border-radius:50%;width:17px;height:17px;font-size:.58rem;display:flex;align-items:center;justify-content:center;font-weight:700;}

/* search dropdown */
.search-dropdown{position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid var(--border);border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.1);z-index:300;overflow:hidden;display:none;margin-top:.4rem;}
.search-wrap{position:relative;}
.search-item{display:flex;align-items:center;gap:.8rem;padding:.7rem 1.2rem;cursor:pointer;transition:background .15s;}
.search-item:hover{background:var(--parchment);}
.search-item-name{font-size:.88rem;font-weight:500;color:var(--text);}
.search-item-price{font-size:.78rem;color:var(--muted);}

/* ── HERO ── */
.hero{background:var(--dusk);min-height:90vh;display:grid;grid-template-columns:1fr 1fr;align-items:center;padding:5rem 5rem 4rem;gap:4rem;position:relative;overflow:hidden;}
.hero-bg{position:absolute;inset:0;pointer-events:none;opacity:.05;}
.hero-eyebrow{font-size:.72rem;font-weight:500;letter-spacing:.2em;text-transform:uppercase;color:var(--sage);margin-bottom:1.2rem;display:flex;align-items:center;gap:.6rem;}
.hero-eyebrow::before{content:'';width:24px;height:1px;background:var(--sage);display:inline-block;}
.hero-title{font-family:'Playfair Display',serif;font-size:clamp(2.8rem,5.5vw,5rem);line-height:1.05;color:var(--cream);margin-bottom:1rem;}
.hero-title em{color:var(--sage);font-style:italic;}
.hero-body{font-size:.95rem;font-weight:300;line-height:1.8;color:rgba(244,240,230,.55);max-width:400px;margin-bottom:2rem;}
.hero-ctas{display:flex;gap:1rem;flex-wrap:wrap;}
.btn-leaf{background:var(--sage);color:var(--dusk);padding:.85rem 2rem;border-radius:100px;font-family:'Jost',sans-serif;font-size:.85rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;border:none;cursor:pointer;transition:all .25s;}
.btn-leaf:hover{background:var(--mist);transform:scale(1.03);}
.btn-outline{background:transparent;color:var(--cream);padding:.85rem 2rem;border-radius:100px;border:1px solid rgba(244,240,230,.25);font-family:'Jost',sans-serif;font-size:.85rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;transition:all .2s;}
.btn-outline:hover{border-color:var(--sage);color:var(--sage);}
.hero-visual{display:flex;justify-content:center;align-items:center;}
.hero-phone{width:200px;filter:drop-shadow(0 32px 64px rgba(0,0,0,.5));animation:float 5s ease-in-out infinite;}
@keyframes float{0%,100%{transform:translateY(0);}50%{transform:translateY(-14px);}}

/* ── STRIP ── */
.strip{background:var(--parchment);display:flex;justify-content:center;border-bottom:1px solid var(--border);flex-wrap:wrap;}
.strip-item{display:flex;align-items:center;gap:.5rem;padding:.9rem 2rem;border-right:1px solid var(--border);font-size:.73rem;font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);}
.strip-item:last-child{border-right:none;}

/* ── SECTION ── */
.section{max-width:1180px;margin:0 auto;padding:5rem 2rem;}
.section-label{font-size:.7rem;font-weight:500;letter-spacing:.18em;text-transform:uppercase;color:var(--fern);margin-bottom:.6rem;display:flex;align-items:center;gap:.5rem;}
.section-label::before{content:'';width:18px;height:1px;background:var(--fern);display:inline-block;}
.section-title{font-family:'Playfair Display',serif;font-size:clamp(1.8rem,3vw,2.6rem);font-weight:700;color:var(--moss);margin-bottom:.4rem;}
.section-title em{color:var(--clay);font-style:italic;}
.section-sub{font-size:.9rem;color:var(--muted);font-weight:300;max-width:440px;line-height:1.7;margin-bottom:2rem;}

/* ── FILTERS ── */
.filters{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.8rem;align-items:center;}
.filter-btn{background:none;border:1px solid var(--border);color:var(--muted);padding:.4rem 1.1rem;border-radius:100px;font-family:'Jost',sans-serif;font-size:.76rem;font-weight:500;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:all .2s;}
.filter-btn:hover,.filter-btn.active{background:var(--moss);border-color:var(--moss);color:var(--cream);}
.compare-bar{margin-left:auto;display:none;align-items:center;gap:.6rem;}
.compare-bar.show{display:flex;}
.compare-bar span{font-size:.78rem;color:var(--muted);}
.compare-bar button{background:var(--fern);color:#fff;border:none;padding:.4rem 1rem;border-radius:100px;font-size:.76rem;cursor:pointer;}

/* ── PRODUCT GRID ── */
.products-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.6rem;}
.prod-card{background:#fff;border:1px solid var(--border);border-radius:20px;overflow:hidden;cursor:pointer;transition:transform .3s cubic-bezier(.34,1.56,.64,1),box-shadow .3s,border-color .3s;position:relative;}
.prod-card:hover{transform:translateY(-7px);box-shadow:0 20px 50px rgba(58,74,50,.12);border-color:var(--sage);}
.prod-img{height:220px;display:flex;align-items:center;justify-content:center;position:relative;background:#fff;overflow:hidden;}
.prod-img img{max-width:150px;max-height:195px;object-fit:contain;transition:transform .4s cubic-bezier(.34,1.56,.64,1);}






.prod-emoji{font-size:5.5rem;filter:drop-shadow(0 8px 16px rgba(0,0,0,.1));transition:transform .4s cubic-bezier(.34,1.56,.64,1);}
.prod-card:hover .prod-img img{transform:scale(1.08) rotate(-2deg);}
.prod-badge{position:absolute;top:.8rem;left:.8rem;font-size:.6rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:.22rem .6rem;border-radius:100px;}
.badge-new{background:var(--moss);color:var(--cream);}
.badge-popular{background:var(--clay);color:#fff;}
.badge-limited{background:var(--sky);color:var(--dusk);}
.badge-хит{background:var(--clay);color:#fff;}
.badge-новинка{background:var(--moss);color:var(--cream);}
.badge-акция{background:#b5392a;color:#fff;}
.badge-популярный{background:var(--bark);color:#fff;}
.prod-actions{position:absolute;top:.7rem;right:.7rem;display:flex;flex-direction:column;gap:.4rem;opacity:0;transition:opacity .2s;}
.prod-card:hover .prod-actions{opacity:1;}
.prod-action-btn{width:32px;height:32px;border-radius:50%;background:#fff;border:1px solid var(--border);cursor:pointer;font-size:.9rem;display:flex;align-items:center;justify-content:center;transition:all .2s;box-shadow:0 2px 8px rgba(0,0,0,.1);}
.prod-action-btn:hover{background:var(--moss);color:#fff;border-color:var(--moss);}
.prod-action-btn.active{background:var(--clay);color:#fff;border-color:var(--clay);}
.prod-body{padding:1.3rem;}
.prod-brand{font-size:.62rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--fern);margin-bottom:.25rem;}
.prod-name{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;}
.prod-installment{font-size:.72rem;color:var(--bark);margin-bottom:.6rem;font-weight:500;}
.prod-tags{display:flex;gap:.35rem;flex-wrap:wrap;margin-bottom:.9rem;}
.prod-tag{font-size:.62rem;color:var(--muted);background:var(--cream);padding:.18rem .5rem;border-radius:6px;border:1px solid var(--border);}
.swatches{display:flex;gap:4px;margin-bottom:.7rem;}
.swatch{width:12px;height:12px;border-radius:50%;cursor:pointer;outline:1.5px solid rgba(0,0,0,.12);transition:transform .15s;}
.swatch:hover{transform:scale(1.25);}
.swatch.active{outline:2px solid var(--fern);outline-offset:2px;}
.prod-footer{display:flex;justify-content:space-between;align-items:center;padding-top:.8rem;border-top:1px solid var(--border);}
.prod-price-main{font-family:'Playfair Display',serif;font-size:1.35rem;font-weight:700;color:var(--moss);}
.prod-price-old{font-size:.72rem;color:var(--muted);text-decoration:line-through;}
.add-btn{width:40px;height:40px;border-radius:50%;background:var(--moss);color:var(--cream);border:none;font-size:1.2rem;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .25s;}
.add-btn:hover{background:var(--fern);transform:scale(1.1) rotate(90deg);}
.out-stock{font-size:.68rem;color:var(--clay);font-weight:500;}
.stars{color:#f4a636;font-size:.8rem;letter-spacing:1px;}
.rating-count{font-size:.7rem;color:var(--muted);margin-left:.2rem;}

/* ── LOADING ── */
.loading{text-align:center;padding:4rem;color:var(--muted);}
.spinner{width:28px;height:28px;border:2px solid var(--border);border-top-color:var(--fern);border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 1rem;}
@keyframes spin{to{transform:rotate(360deg);}}

/* ── COMPARISON TABLE ── */
.compare-overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:700;display:none;align-items:flex-end;backdrop-filter:blur(4px);}
.compare-overlay.open{display:flex;}
.compare-panel{background:var(--cream);width:100%;max-height:80vh;border-radius:24px 24px 0 0;overflow:auto;padding:2rem;}
.compare-table{width:100%;border-collapse:collapse;margin-top:1.5rem;}
.compare-table th{font-family:'Playfair Display',serif;font-size:1rem;color:var(--moss);padding:.8rem 1rem;border-bottom:2px solid var(--border);text-align:center;}
.compare-table td{padding:.7rem 1rem;border-bottom:1px solid var(--border);font-size:.85rem;color:var(--text);text-align:center;vertical-align:middle;}
.compare-table tr td:first-child{font-weight:500;color:var(--muted);text-align:left;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;}
.compare-emoji{font-size:2.5rem;display:block;margin:0 auto .4rem;}
.compare-best{background:rgba(90,114,71,.08);color:var(--fern);font-weight:600;}

/* ── REVIEWS ── */
.reviews-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.4rem;}
.review-card{background:#fff;border:1px solid var(--border);border-radius:16px;padding:1.5rem;}
.review-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.7rem;}
.reviewer{display:flex;align-items:center;gap:.7rem;}
.reviewer-avatar{width:36px;height:36px;border-radius:50%;background:var(--moss);color:var(--cream);display:flex;align-items:center;justify-content:center;font-size:.9rem;font-weight:700;flex-shrink:0;}
.reviewer-name{font-weight:500;font-size:.88rem;color:var(--text);}
.reviewer-date{font-size:.72rem;color:var(--muted);}
.review-product{font-size:.72rem;color:var(--fern);font-weight:500;margin-bottom:.5rem;}
.review-text{font-size:.85rem;color:var(--muted);line-height:1.6;}
.review-verified{font-size:.68rem;color:var(--fern);background:rgba(90,114,71,.08);padding:.15rem .5rem;border-radius:100px;display:inline-block;margin-top:.6rem;}

/* ── FAQ ── */
.faq-item{border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:.7rem;background:#fff;}
.faq-q{width:100%;background:none;border:none;text-align:left;padding:1.1rem 1.4rem;font-family:'Jost',sans-serif;font-size:.92rem;font-weight:500;color:var(--text);cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:background .15s;}
.faq-q:hover{background:var(--parchment);}
.faq-q .arrow{font-size:.8rem;transition:transform .3s;color:var(--muted);}
.faq-q.open .arrow{transform:rotate(180deg);}
.faq-a{max-height:0;overflow:hidden;transition:max-height .35s ease,padding .3s;}
.faq-a.open{max-height:200px;padding:.2rem 1.4rem 1.2rem;}
.faq-a p{font-size:.88rem;color:var(--muted);line-height:1.7;}

/* ── OUR STORY / SUSTAINABILITY ── */
.dark-section{background:var(--dusk);padding:6rem 2rem;}
.light-section{background:var(--cream);padding:6rem 2rem;}
.parchment-section{background:var(--parchment);padding:5rem 2rem;}
.section-inner{max-width:1180px;margin:0 auto;}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:5rem;align-items:center;}
.stat-row{display:flex;gap:2.5rem;flex-wrap:wrap;margin-top:2rem;}
.stat-item .n{font-family:'Playfair Display',serif;font-size:2.2rem;color:var(--sage);font-weight:700;}
.stat-item .l{font-size:.72rem;color:rgba(244,240,230,.4);text-transform:uppercase;letter-spacing:.1em;}
.card-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:1rem;}
.dark-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:1.6rem;text-align:center;}
.dark-card-hl{background:rgba(138,170,118,.1);border:1px solid rgba(138,170,118,.18);}
.dark-card .icon{font-size:2rem;margin-bottom:.7rem;}
.dark-card .title{font-family:'Playfair Display',serif;font-size:.92rem;color:var(--cream);margin-bottom:.3rem;}
.dark-card.dark-card-hl .title{color:var(--sage);}
.dark-card .body{font-size:.76rem;color:rgba(244,240,230,.4);line-height:1.5;}

.progress-item{margin-bottom:1rem;}
.progress-label{display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted);margin-bottom:.35rem;}
.progress-label span:last-child{color:var(--fern);font-weight:600;}
.progress-track{background:var(--border);border-radius:100px;height:6px;}
.progress-fill{height:6px;border-radius:100px;}

.eco-tags{display:flex;flex-wrap:wrap;gap:.5rem;}
.eco-tag{background:var(--cream);border:1px solid var(--border);padding:.3rem .9rem;border-radius:100px;font-size:.76rem;color:var(--muted);}

/* ── CONTACTS ── */
.contact-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.4rem;margin-bottom:1.4rem;}
.contact-card{background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.8rem;}
.contact-card .icon{font-size:1.8rem;margin-bottom:.8rem;}
.contact-card .title{font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.5rem;}
.contact-card a{color:var(--fern);text-decoration:none;display:block;font-size:.85rem;line-height:2;}
.contact-card p{font-size:.85rem;color:var(--muted);line-height:1.7;}
.delivery-card{background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.8rem;}

/* ── FOOTER ── */
footer{background:var(--dusk);color:rgba(244,240,230,.45);padding:4rem 2rem 2rem;}
.footer-inner{max-width:1180px;margin:0 auto;}
.footer-top{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:3rem;margin-bottom:2.5rem;}
.footer-brand{font-family:'Playfair Display',serif;font-size:1.7rem;font-style:italic;color:var(--cream);margin-bottom:.3rem;}
.footer-brand span{color:var(--sage);}
.footer-desc{font-size:.82rem;line-height:1.7;margin-bottom:1rem;}
.footer-col h4{font-size:.68rem;font-weight:600;letter-spacing:.15em;text-transform:uppercase;color:var(--cream);margin-bottom:.9rem;}
.footer-col a{display:block;font-size:.82rem;color:rgba(244,240,230,.4);text-decoration:none;line-height:2;cursor:pointer;transition:color .2s;}
.footer-col a:hover{color:var(--sage);}
.footer-bottom{border-top:1px solid rgba(255,255,255,.07);padding-top:1.5rem;font-size:.72rem;display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;}

/* ── MODALS ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:500;display:none;align-items:center;justify-content:center;backdrop-filter:blur(4px);}
.overlay.open{display:flex;}
.modal{background:var(--cream);border-radius:24px;padding:2.2rem;width:90%;max-width:420px;position:relative;animation:modalIn .3s cubic-bezier(.34,1.56,.64,1);max-height:90vh;overflow-y:auto;}
.modal-lg{max-width:520px;}
@keyframes modalIn{from{opacity:0;transform:scale(.92);}to{opacity:1;transform:scale(1);}}
.modal-close{position:absolute;top:1rem;right:1rem;background:none;border:none;font-size:1.3rem;cursor:pointer;color:var(--muted);line-height:1;z-index:1;}
.modal h2{font-family:'Playfair Display',serif;font-size:1.5rem;color:var(--moss);margin-bottom:.3rem;}
.modal-sub{font-size:.83rem;color:var(--muted);margin-bottom:1.4rem;}
.form-group{margin-bottom:.9rem;}
.form-group label{display:block;font-size:.72rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.35rem;}
.form-group input,.form-group select,.form-group textarea{width:100%;padding:.7rem .9rem;border:1px solid var(--border);border-radius:10px;font-family:'Jost',sans-serif;font-size:.88rem;background:#fff;color:var(--text);outline:none;transition:border-color .2s;}
.form-group input:focus,.form-group select:focus,.form-group textarea:focus{border-color:var(--fern);}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;}
.form-error{font-size:.76rem;color:var(--clay);margin-top:.3rem;display:none;}
.modal-btn{width:100%;padding:.82rem;background:var(--moss);color:var(--cream);border:none;border-radius:100px;font-family:'Jost',sans-serif;font-size:.83rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;transition:background .2s;margin-top:.4rem;}
.modal-btn:hover{background:var(--dusk);}
.modal-btn.secondary{background:var(--parchment);color:var(--moss);border:1px solid var(--border);}
.modal-btn.secondary:hover{background:var(--mist);}
.modal-switch{text-align:center;font-size:.78rem;color:var(--muted);margin-top:.9rem;}
.modal-switch a{color:var(--fern);cursor:pointer;text-decoration:underline;}

/* ── CART ── */
.cart-panel{position:fixed;right:0;top:0;height:100%;width:400px;background:var(--cream);z-index:600;transform:translateX(100%);transition:transform .35s cubic-bezier(.4,0,.2,1);box-shadow:-8px 0 40px rgba(0,0,0,.1);display:flex;flex-direction:column;}
.cart-panel.open{transform:translateX(0);}
.cart-header{padding:1.4rem 1.6rem;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;}
.cart-header h2{font-family:'Playfair Display',serif;font-size:1.35rem;color:var(--moss);}
.cart-body{flex:1;overflow-y:auto;padding:1rem 1.6rem;}
.cart-empty{text-align:center;padding:3rem 1rem;color:var(--muted);}
.cart-empty .ce-icon{font-size:2.8rem;margin-bottom:.8rem;}
.cart-item{display:flex;gap:.9rem;padding:.9rem 0;border-bottom:1px solid var(--border);align-items:center;}
.ci-emoji{font-size:2.5rem;width:54px;height:54px;background:var(--parchment);border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.ci-info{flex:1;min-width:0;}
.ci-name{font-family:'Playfair Display',serif;font-size:.9rem;font-weight:700;color:var(--moss);}
.ci-color{font-size:.7rem;color:var(--muted);}
.ci-qty{display:flex;align-items:center;gap:.4rem;margin-top:.35rem;}
.qty-btn{width:22px;height:22px;border-radius:50%;border:1px solid var(--border);background:none;cursor:pointer;font-size:.85rem;display:flex;align-items:center;justify-content:center;transition:all .15s;}
.qty-btn:hover{background:var(--moss);color:#fff;border-color:var(--moss);}
.ci-qty span{font-size:.82rem;font-weight:500;min-width:18px;text-align:center;}
.ci-price{font-family:'Playfair Display',serif;font-size:.95rem;font-weight:700;color:var(--moss);flex-shrink:0;}
.ci-remove{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.9rem;padding:.2rem;transition:color .15s;}
.ci-remove:hover{color:var(--clay);}
.cart-footer{padding:1.3rem 1.6rem;border-top:1px solid var(--border);}
.cart-row{display:flex;justify-content:space-between;font-size:.83rem;color:var(--muted);margin-bottom:.35rem;}
.cart-row.total{font-family:'Playfair Display',serif;font-size:1.05rem;font-weight:700;color:var(--moss);margin-top:.5rem;padding-top:.5rem;border-top:1px solid var(--border);}
.cart-bg{position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:599;display:none;}

/* ── ORDER SUCCESS ── */
.success-icon{font-size:3rem;text-align:center;margin-bottom:.8rem;}
.order-summary{background:var(--parchment);border-radius:12px;padding:1rem 1.2rem;margin:.8rem 0;}
.order-row{display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted);margin-bottom:.25rem;}
.order-row.bold{color:var(--moss);font-weight:600;}

/* ── WISHLIST PANEL ── */
.wishlist-panel{position:fixed;right:0;top:0;height:100%;width:360px;background:var(--cream);z-index:600;transform:translateX(100%);transition:transform .35s cubic-bezier(.4,0,.2,1);box-shadow:-8px 0 40px rgba(0,0,0,.1);display:flex;flex-direction:column;}
.wishlist-panel.open{transform:translateX(0);}

/* ── TOAST ── */
.toast{position:fixed;bottom:2rem;left:50%;transform:translateX(-50%) translateY(60px);background:var(--moss);color:var(--cream);padding:.75rem 1.6rem;border-radius:100px;font-size:.8rem;font-weight:500;z-index:9999;opacity:0;transition:all .35s cubic-bezier(.34,1.56,.64,1);white-space:nowrap;box-shadow:0 6px 24px rgba(0,0,0,.2);}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
.toast.error{background:var(--clay);}


/* ── PROFILE PANEL ── */
.profile-panel{position:fixed;right:0;top:0;height:100%;width:420px;background:var(--cream);z-index:600;transform:translateX(100%);transition:transform .35s cubic-bezier(.4,0,.2,1);box-shadow:-8px 0 40px rgba(0,0,0,.1);display:flex;flex-direction:column;overflow-y:auto;}
.profile-panel.open{transform:translateX(0);}
.profile-header{background:var(--dusk);padding:2.5rem 1.8rem 2rem;position:relative;}
.profile-avatar{width:72px;height:72px;border-radius:50%;background:var(--moss);color:var(--cream);font-family:"Playfair Display",serif;font-size:2rem;font-weight:700;display:flex;align-items:center;justify-content:center;margin-bottom:1rem;border:3px solid rgba(138,170,118,.4);}
.profile-name{font-family:"Playfair Display",serif;font-size:1.4rem;color:var(--cream);margin-bottom:.2rem;}
.profile-email{font-size:.78rem;color:rgba(244,240,230,.5);}
.profile-role{display:inline-block;margin-top:.5rem;font-size:.65rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:.22rem .7rem;border-radius:100px;background:rgba(138,170,118,.2);color:var(--sage);}
.profile-body{padding:1.5rem 1.8rem;flex:1;}
.profile-section{margin-bottom:1.8rem;}
.profile-section-title{font-size:.68rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:.9rem;display:flex;align-items:center;gap:.5rem;}
.profile-section-title::before{content:"";width:14px;height:1px;background:var(--border);display:inline-block;}
.profile-menu-item{display:flex;align-items:center;gap:.9rem;padding:.85rem 1rem;border-radius:12px;cursor:pointer;transition:background .15s;margin-bottom:.3rem;}
.profile-menu-item:hover{background:var(--parchment);}
.profile-menu-icon{font-size:1.1rem;width:28px;text-align:center;}
.profile-menu-label{font-size:.88rem;font-weight:500;color:var(--text);flex:1;}
.profile-menu-arrow{color:var(--muted);font-size:.75rem;}
.order-mini{background:#fff;border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem;margin-bottom:.6rem;}
.order-mini-top{display:flex;justify-content:space-between;margin-bottom:.3rem;}
.order-mini-id{font-size:.78rem;font-weight:600;color:var(--moss);}
.order-mini-status{font-size:.68rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:.18rem .6rem;border-radius:100px;}
.status-paid{background:rgba(90,114,71,.12);color:var(--fern);}
.status-pending{background:rgba(181,131,106,.12);color:var(--bark);}
.status-delivered{background:rgba(58,74,50,.12);color:var(--moss);}
.order-mini-items{font-size:.78rem;color:var(--muted);}
.order-mini-total{font-family:"Playfair Display",serif;font-size:.95rem;font-weight:700;color:var(--moss);margin-top:.3rem;}
.edit-form{background:var(--parchment);border-radius:14px;padding:1.2rem;margin-top:.5rem;display:none;}
.edit-form.open{display:block;}
.profile-footer{padding:1.2rem 1.8rem;border-top:1px solid var(--border);}
.logout-btn{width:100%;padding:.75rem;background:none;border:1px solid var(--border);border-radius:100px;font-family:"Jost",sans-serif;font-size:.82rem;color:var(--muted);cursor:pointer;transition:all .2s;}
.logout-btn:hover{border-color:var(--clay);color:var(--clay);}

/* ── REVEAL ── */
.reveal{opacity:0;transform:translateY(20px);transition:opacity .55s ease,transform .55s ease;}
.reveal.visible{opacity:1;transform:none;}

/* ── RESPONSIVE ── */

/* ── BURGER ── */
.burger{display:none;flex-direction:column;gap:5px;background:none;border:none;cursor:pointer;padding:.3rem;}
.burger span{display:block;width:22px;height:2px;background:var(--moss);border-radius:2px;transition:all .3s;}
.burger.open span:nth-child(1){transform:translateY(7px) rotate(45deg);}
.burger.open span:nth-child(2){opacity:0;}
.burger.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg);}

/* Mobile menu drawer */
.mobile-menu{position:fixed;inset:0;top:0;z-index:199;background:var(--cream);transform:translateY(-100%);transition:transform .35s cubic-bezier(.4,0,.2,1);padding:6rem 2rem 2rem;overflow-y:auto;}
.mobile-menu.open{transform:translateY(0);}
.mobile-menu ul{list-style:none;}
.mobile-menu ul li{border-bottom:1px solid var(--border);}
.mobile-menu ul li a{display:flex;align-items:center;justify-content:space-between;padding:1.1rem 0;font-size:1.1rem;font-weight:500;color:var(--text);text-decoration:none;cursor:pointer;}
.mobile-menu ul li a span{color:var(--muted);font-size:.9rem;}
.mobile-menu-footer{margin-top:2rem;display:flex;flex-direction:column;gap:.8rem;}

@media(max-width:960px){
  .nav-links{display:none;}
  .nav-inner{padding:1rem 1.2rem;}
  .nav-search{max-width:160px;}
  .burger{display:flex;}
  .hero{grid-template-columns:1fr;padding:4rem 1.5rem 3rem;text-align:center;}
  .hero-visual{display:none;}
  .hero-body{margin:0 auto 1.8rem;}
  .hero-ctas{justify-content:center;}
  .products-grid{grid-template-columns:1fr 1fr;}
  .reviews-grid{grid-template-columns:1fr;}
  .two-col{grid-template-columns:1fr;gap:2rem;}
  .contact-grid{grid-template-columns:1fr;}
  .footer-top{grid-template-columns:1fr 1fr;gap:2rem;}
  .cart-panel,.wishlist-panel,.profile-panel{width:100%;}
  .dark-section,.light-section,.parchment-section{padding:3.5rem 1.2rem;}
  .section{padding:3.5rem 1.2rem;}
  .compare-panel{padding:1.2rem;}
  .modal{padding:1.6rem;}
}
@media(max-width:560px){
  .products-grid{grid-template-columns:1fr;}
  .strip{display:none;}
  .promo-bar{font-size:.7rem;padding:.5rem .8rem;}
  .hero-title{font-size:2.4rem;}
  .hero{padding:3.5rem 1.2rem 2.5rem;}
  .hero-eyebrow{justify-content:center;}
  .section-title{font-size:1.7rem;}
  .filter-btn{font-size:.7rem;padding:.35rem .85rem;}
  .footer-top{grid-template-columns:1fr;}
  .reviews-grid{grid-template-columns:1fr;}
  .card-grid-2{grid-template-columns:1fr;}
  .stat-row{gap:1.5rem;}
  .contact-grid{grid-template-columns:1fr;}
  .nav-search{display:none;}
  .compare-bar{display:none !important;}
  .cart-panel,.wishlist-panel,.profile-panel{width:100%;border-radius:0;}
  .modal{width:95%;padding:1.4rem;}
  .form-row{grid-template-columns:1fr;}
}

</style>
</head>
<body>

<!-- PROMO BAR -->
<div class="promo-bar" id="promoBar">
  🌿 Бесплатная доставка по Казахстану при заказе от 50 000 тг · <a onclick="document.getElementById('shop').scrollIntoView({behavior:'smooth'})">Смотреть телефоны</a>
  <button class="promo-close" onclick="document.getElementById('promoBar').style.display='none'">✕</button>
</div>

<!-- NAV -->
<nav>
  <div class="nav-inner">
    <a href="#" class="nav-brand">Grove<span>.</span></a>
    <ul class="nav-links">
      <li><a onclick="goTo('shop')">Телефоны</a></li>
      <li><a onclick="goTo('our-story')">О нас</a></li>
      <li><a onclick="goTo('sustainability')">Экология</a></li>
      <li><a onclick="goTo('reviews')">Отзывы</a></li>
      <li><a onclick="goTo('faq')">FAQ</a></li>
      <li><a onclick="goTo('contacts')">Контакты</a></li>
    </ul>
    <div class="search-wrap" style="flex:1;max-width:240px;position:relative;">
      <div class="nav-search">
        <span style="color:var(--muted);font-size:.9rem;">🔍</span>
        <input type="text" id="searchInput" placeholder="Поиск телефонов…" oninput="onSearch(this.value)" onblur="setTimeout(()=>hideSearch(),200)">
      </div>
      <div class="search-dropdown" id="searchDropdown"></div>
    </div>
    <div class="nav-right">
      <button class="nav-btn ghost" id="authBtn" onclick="openAuth('login')">Войти</button>
      <button class="nav-icon-btn" id="profileBtn" onclick="openProfile()" title="Профиль" style="display:none;font-size:1rem;">👤</button>
      <button class="nav-icon-btn" onclick="openWishlist()" title="Избранное">❤️<span class="badge" id="wishBadge" style="display:none">0</span></button>
      <button class="nav-icon-btn" onclick="openCart()" title="Корзина">🌿<span class="badge" id="cartCount" style="display:none">0</span></button>
      <button class="burger" id="burgerBtn" onclick="toggleMobileMenu()">
        <span></span><span></span><span></span>
      </button>
    </div>
  </div>
</nav>

<!-- MOBILE MENU -->
<div class="mobile-menu" id="mobileMenu">
  <ul>
    <li><a onclick="closeMobileMenu();goTo('shop')">Телефоны <span>›</span></a></li>
    <li><a onclick="closeMobileMenu();goTo('our-story')">О нас <span>›</span></a></li>
    <li><a onclick="closeMobileMenu();goTo('sustainability')">Экология <span>›</span></a></li>
    <li><a onclick="closeMobileMenu();goTo('reviews')">Отзывы <span>›</span></a></li>
    <li><a onclick="closeMobileMenu();goTo('faq')">FAQ <span>›</span></a></li>
    <li><a onclick="closeMobileMenu();goTo('contacts')">Контакты <span>›</span></a></li>
  </ul>
  <div class="mobile-menu-footer">
    <button class="modal-btn" id="mobileAuthBtn" onclick="closeMobileMenu();openAuth('login')">Войти</button>
    <button class="modal-btn secondary" onclick="closeMobileMenu();openCart()">🌿 Корзина</button>
    <button class="modal-btn secondary" onclick="closeMobileMenu();openWishlist()">❤️ Избранное</button>
  </div>
</div>

<!-- HERO -->
<section class="hero">
  <svg class="hero-bg" viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice">
    <path d="M200,400 Q400,100 700,300 Q1000,500 800,700 Q600,900 200,400Z" fill="#8aaa76"/>
    <path d="M900,200 Q1100,50 1150,350 Q1200,600 950,550 Q700,500 900,200Z" fill="#5a7247"/>
    <circle cx="100" cy="150" r="120" fill="#8aaa76"/><circle cx="1100" cy="650" r="90" fill="#5a7247"/>
  </svg>
  <div>
    <div class="hero-eyebrow">Новая коллекция — 2026</div>
    <h1 class="hero-title">Технологии,<br>рождённые<br><em>природой.</em></h1>
    <p class="hero-body">Телефоны, созданные с уважением к земле. Натуральные материалы, органичные формы и связь с миром вокруг вас.</p>
    <div class="hero-ctas">
      <button class="btn-leaf" onclick="goTo('shop')">Смотреть телефоны</button>
      <button class="btn-outline" onclick="goTo('sustainability')">Наши материалы</button>
    </div>
  </div>
  <div class="hero-visual">
    <svg class="hero-phone" viewBox="0 0 220 450" fill="none">
      <defs>
        <linearGradient id="hbg" x1="0" y1="0" x2="220" y2="450" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#4a6840"/><stop offset="100%" stop-color="#2e3d30"/></linearGradient>
        <linearGradient id="hsc" x1="0" y1="0" x2="0" y2="420" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#1a2e1c"/><stop offset="100%" stop-color="#1a2a1c"/></linearGradient>
        <radialGradient id="hgl" cx="50%" cy="40%" r="45%"><stop offset="0%" stop-color="rgba(138,170,118,0.5)"/><stop offset="100%" stop-color="transparent"/></radialGradient>
      </defs>
      <rect width="220" height="450" rx="42" fill="url(#hbg)"/>
      <rect x="11" y="11" width="198" height="428" rx="32" fill="url(#hsc)"/>
      <rect x="11" y="11" width="198" height="428" rx="32" fill="url(#hgl)"/>
      <g opacity=".15"><path d="M80 120 C110 80 160 100 150 160 C140 220 80 200 80 120Z" fill="#8aaa76"/><path d="M80 120 L150 160" stroke="#5a7247" stroke-width="1"/></g>
      <rect x="35" y="330" width="60" height="40" rx="10" fill="rgba(255,255,255,.06)"/>
      <rect x="105" y="330" width="60" height="40" rx="10" fill="rgba(255,255,255,.06)"/>
      <rect x="70" y="30" width="80" height="12" rx="6" fill="rgba(255,255,255,.12)"/>
      <rect x="78" y="427" width="64" height="3" rx="2" fill="rgba(255,255,255,.18)"/>
      <rect x="78" y="18" width="64" height="20" rx="10" fill="#1a2e1c"/>
      <rect x="218" y="120" width="4" height="60" rx="2" fill="#3a5430"/>
      <rect x="-2" y="105" width="4" height="40" rx="2" fill="#3a5430"/>
      <rect x="-2" y="155" width="4" height="40" rx="2" fill="#3a5430"/>
      <g transform="translate(90,140)"><path d="M20 2 C32 12 38 28 20 46 C2 28 8 12 20 2Z" fill="rgba(138,170,118,.35)" stroke="rgba(138,170,118,.6)" stroke-width=".5"/><path d="M20 2 L20 46" stroke="rgba(138,170,118,.4)" stroke-width=".8"/></g>
    </svg>
  </div>
</section>

<!-- STRIP -->
<div class="strip">
  <div class="strip-item">🌿 Экологичные материалы</div>
  <div class="strip-item">📦 Бесплатная доставка от 50 000 тг</div>
  <div class="strip-item">♻️ Программа возврата</div>
  <div class="strip-item">🌱 1 дерево с каждого заказа</div>
  <div class="strip-item">🔒 Гарантия 2 года</div>
</div>

<!-- PRODUCTS -->
<div class="section" id="shop">
  <div class="section-label reveal">Наша коллекция</div>
  <h2 class="section-title reveal">Телефон для <em>каждого.</em></h2>
  <p class="section-sub reveal">Создано с любовью к природе и к вам.</p>
  <div class="filters reveal" id="filterBtns">
    <button class="filter-btn active" data-cat="">Все</button>
    <button class="filter-btn" data-cat="flagship">Флагман</button>
    <button class="filter-btn" data-cat="mid-range">Средний класс</button>
    <button class="filter-btn" data-cat="budget">Бюджетный</button>
    <select id="sortSelect" style="margin-left:auto;background:#fff;border:1px solid var(--border);color:var(--muted);padding:.4rem .8rem;border-radius:100px;font-family:'Jost',sans-serif;font-size:.76rem;font-weight:500;cursor:pointer;outline:none;">
      <option value="popular">Популярные</option>
      <option value="price_asc">Цена ↑</option>
      <option value="price_desc">Цена ↓</option>
      <option value="newest">Новинки</option>
      <option value="name_asc">По имени</option>
    </select>
    <div class="compare-bar" id="compareBar">
      <span id="compareCount">0 выбрано</span>
      <button onclick="openCompare()" style="cursor:pointer;">Сравнить →</button>
      <button onclick="clearCompare()" style="background:var(--muted);">✕</button>
    </div>
  </div>
  <div class="products-grid" id="productsGrid">
    <div class="loading"><div class="spinner"></div>Загрузка…</div>
  </div>
</div>

<!-- REVIEWS -->
<div class="parchment-section" id="reviews">
  <div class="section-inner">
    <div class="section-label reveal">Отзывы</div>
    <h2 class="section-title reveal">Что говорят <em>покупатели.</em></h2>
    <p class="section-sub reveal">Реальные люди, реальные впечатления.</p>
    <div class="reviews-grid reveal">
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar">А</div>
            <div><div class="reviewer-name">Айгерим М.</div><div class="reviewer-date">15 февраля 2026</div></div>
          </div>
          <div class="stars">★★★★★</div>
        </div>
        <div class="review-product">Forest Pro</div>
        <div class="review-text">Просто влюбилась в этот телефон! Бамбуковая рамка такая приятная на ощупь, а камера снимает невероятно. Уже посоветовала всем подругам 🌿</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar" style="background:var(--bark);">Д</div>
            <div><div class="reviewer-name">Данияр С.</div><div class="reviewer-date">3 января 2026</div></div>
          </div>
          <div class="stars">★★★★★</div>
        </div>
        <div class="review-product">Terra</div>
        <div class="review-text">Взял Terra вместо нового iPhone — ни разу не пожалел. Батарея держит двое суток, экран яркий, и приятно знать что телефон сделан экологично.</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar" style="background:var(--sky);color:var(--dusk);">З</div>
            <div><div class="reviewer-name">Зарина К.</div><div class="reviewer-date">20 января 2026</div></div>
          </div>
          <div class="stars">★★★★☆</div>
        </div>
        <div class="review-product">Amber SE</div>
        <div class="review-text">Хороший телефон за свои деньги. Быстрая доставка, красивая экоупаковка. Единственное — хотелось бы чуть лучше фронталку, но в целом очень довольна!</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar" style="background:var(--fern);">Т</div>
            <div><div class="reviewer-name">Тимур Н.</div><div class="reviewer-date">8 февраля 2026</div></div>
          </div>
          <div class="stars">★★★★★</div>
        </div>
        <div class="review-product">Mist Ultra</div>
        <div class="review-text">Mist Ultra — шедевр! Зум 5× позволяет снимать горы в Алматы с потрясающими деталями. Статусный телефон с экологичной миссией.</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar" style="background:var(--clay);">С</div>
            <div><div class="reviewer-name">Сабина А.</div><div class="reviewer-date">25 января 2026</div></div>
          </div>
          <div class="stars">★★★★★</div>
        </div>
        <div class="review-product">Canopy</div>
        <div class="review-text">Отличный выбор за средние деньги. OLED экран — красивый, батарея 5200mAh — живёт долго. Цвет Canopy вживую ещё красивее чем на сайте!</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
      <div class="review-card">
        <div class="review-header">
          <div class="reviewer">
            <div class="reviewer-avatar" style="background:#888;">Е</div>
            <div><div class="reviewer-name">Ерлан Б.</div><div class="reviewer-date">12 февраля 2026</div></div>
          </div>
          <div class="stars">★★★★☆</div>
        </div>
        <div class="review-product">Dusk Mini</div>
        <div class="review-text">Идеальный компактный телефон. Помещается в любой карман, работает шустро. Батарея немного меньше чем у старшей линейки, но для моих задач хватает.</div>
        <div class="review-verified">✓ Verified purchase</div>
      </div>
    </div>
  </div>
</div>

<!-- OUR STORY -->
<div class="dark-section" id="our-story">
  <div class="section-inner">
    <div class="two-col">
      <div>
        <div style="font-size:.7rem;font-weight:500;letter-spacing:.2em;text-transform:uppercase;color:var(--sage);margin-bottom:.9rem;display:flex;align-items:center;gap:.5rem;"><span style="width:20px;height:1px;background:var(--sage);display:inline-block;"></span>Наша история</div>
        <h2 style="font-family:'Playfair Display',serif;font-size:clamp(2rem,3.5vw,3rem);color:var(--cream);line-height:1.1;margin-bottom:1.1rem;">Рождены в <em style="color:var(--sage);font-style:italic;">Казахстане.</em></h2>
        <p style="font-size:.92rem;color:rgba(244,240,230,.55);font-weight:300;line-height:1.8;margin-bottom:1rem;">Мы основали Grove в 2019 году в Алматы, у подножия Заилийского Алатау. Горы каждый день напоминают нам зачем мы работаем — технологии не должны разрушать природу.</p>
        <p style="font-size:.92rem;color:rgba(244,240,230,.55);font-weight:300;line-height:1.8;margin-bottom:1.8rem;">Каждый телефон Grove — это бамбук, пробка и переработанный алюминий. Мы верим: красивые вещи могут быть и ответственными.</p>
        <div class="stat-row">
          <div class="stat-item"><div class="n">2019</div><div class="l">Основана</div></div>
          <div class="stat-item"><div class="n">50К+</div><div class="l">Клиентов</div></div>
          <div class="stat-item"><div class="n">6</div><div class="l">Моделей</div></div>
          <div class="stat-item"><div class="n">🇰🇿</div><div class="l">Казахстан</div></div>
        </div>
      </div>
      <div class="card-grid-2">
        <div class="dark-card"><div class="icon">🌿</div><div class="title">Натуральные материалы</div><div class="body">Бамбук, пробка и сертифицированный металл в каждом устройстве</div></div>
        <div class="dark-card" style="margin-top:1.5rem;"><div class="icon">🏔️</div><div class="title">Дух гор</div><div class="body">Вдохновлены красотой природы Казахстана каждый день</div></div>
        <div class="dark-card"><div class="icon">🤝</div><div class="title">Честный бизнес</div><div class="body">Прозрачная цепочка поставок без компромиссов</div></div>
        <div class="dark-card dark-card-hl" style="margin-top:1.5rem;"><div class="icon">❤️</div><div class="title">С любовью к земле</div><div class="body">Каждый заказ = одно посаженное дерево в Казахстане</div></div>
      </div>
    </div>
  </div>
</div>

<!-- SUSTAINABILITY -->
<div class="light-section" id="sustainability">
  <div class="section-inner">
    <div class="section-label reveal">Экология</div>
    <h2 class="section-title reveal">Sustainability — <em>наш приоритет.</em></h2>
    <p class="section-sub reveal">Мы несём ответственность перед планетой. Вот что делаем прямо сейчас.</p>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:1.3rem;margin-bottom:1.5rem;" class="reveal">
      <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.7rem;">
        <div style="font-size:2rem;margin-bottom:.8rem;">🌳</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;">50 000 деревьев</div>
        <div style="font-size:.83rem;color:var(--muted);line-height:1.6;">С 2019 года вместе с фондом «Жасыл Ел» посадили более 50 тысяч деревьев по всему Казахстану.</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.7rem;">
        <div style="font-size:2rem;margin-bottom:.8rem;">♻️</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;">Программа возврата</div>
        <div style="font-size:.83rem;color:var(--muted);line-height:1.6;">Сдайте старый телефон при доставке нового — мы переработаем его без вреда для природы.</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.7rem;">
        <div style="font-size:2rem;margin-bottom:.8rem;">📦</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;">Эко-упаковка</div>
        <div style="font-size:.83rem;color:var(--muted);line-height:1.6;">100% переработанный картон и соевые чернила. Никакого пластика в каждой коробке Grove.</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.7rem;">
        <div style="font-size:2rem;margin-bottom:.8rem;">☀️</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;">Солнечная энергия</div>
        <div style="font-size:.83rem;color:var(--muted);line-height:1.6;">Наш офис и склад в Алматы работают на 100% солнечной энергии с 2023 года.</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.7rem;">
        <div style="font-size:2rem;margin-bottom:.8rem;">🌍</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:.4rem;">Нейтральный след</div>
        <div style="font-size:.83rem;color:var(--muted);line-height:1.6;">К 2027 году обязуемся достичь нулевого углеродного следа по всей цепочке поставок.</div>
      </div>
      <div style="background:var(--moss);border-radius:18px;padding:1.7rem;color:var(--cream);">
        <div style="font-size:2rem;margin-bottom:.8rem;">🎯</div>
        <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;margin-bottom:.4rem;">Цель 2030</div>
        <div style="font-size:.83rem;color:rgba(244,240,230,.65);line-height:1.6;">Стать первой полностью безотходной компанией по производству телефонов в Центральной Азии.</div>
      </div>
    </div>
    <div style="background:#fff;border:1px solid var(--border);border-radius:18px;padding:1.8rem;" class="reveal">
      <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:1.3rem;">Прогресс наших целей</div>
      <div class="progress-item"><div class="progress-label"><span>Переработанные материалы в корпусе</span><span>78%</span></div><div class="progress-track"><div class="progress-fill" style="background:var(--fern);width:78%;"></div></div></div>
      <div class="progress-item"><div class="progress-label"><span>Углеродная нейтральность (план к 2027)</span><span>54%</span></div><div class="progress-track"><div class="progress-fill" style="background:var(--sage);width:54%;"></div></div></div>
      <div class="progress-item"><div class="progress-label"><span>Эко-упаковка по всей линейке</span><span>100%</span></div><div class="progress-track"><div class="progress-fill" style="background:var(--moss);width:100%;"></div></div></div>
    </div>
  </div>
</div>

<!-- FAQ -->
<div class="parchment-section" id="faq">
  <div class="section-inner">
    <div class="section-label reveal">Вопросы и ответы</div>
    <h2 class="section-title reveal">Часто <em>спрашивают.</em></h2>
    <p class="section-sub reveal">Если не нашли ответ — пишите нам напрямую.</p>
    <div style="max-width:720px;" class="reveal">
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Как быстро доставят мой заказ? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>По Алматы доставляем на следующий день. В Астану, Шымкент и Карагандf — 2 рабочих дня. По всему Казахстану — до 5 дней. Доставка бесплатная при заказе от 50 000 тг, при меньшей сумме — 1 500 тг.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Можно ли вернуть телефон? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>Да! Вы можете вернуть устройство в течение 14 дней с момента получения, если оно не было в использовании и сохранена оригинальная упаковка. Возврат денег — в течение 3 рабочих дней.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Какая гарантия на телефоны Grove? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>Все телефоны Grove поставляются с официальной гарантией 2 года. Гарантия покрывает производственные дефекты. Механические повреждения и попадание воды в гарантию не входят.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Есть ли рассрочка или кредит? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>Да! Мы предлагаем рассрочку 0% через Kaspi Bank на 3, 6 и 12 месяцев. При оформлении заказа выберите оплату через Kaspi — вас перенаправят на их платёжную страницу.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Телефоны оригинальные? Есть ли документы? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>Все устройства 100% оригинальные, с официальным ввозом на территорию Казахстана. К каждому телефону прилагается: гарантийный талон, товарный чек и сертификат соответствия ГОСТ.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Как работает программа возврата старого телефона? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>При покупке нового телефона Grove вы можете сдать старый смартфон любой марки. Мы оцениваем его стоимость и даём скидку на новый телефон. Старое устройство мы перерабатываем экологично.</p></div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">Можно ли сравнить несколько телефонов? <span class="arrow">▼</span></button>
        <div class="faq-a"><p>Да! В каталоге нажмите кнопку «⚖» на карточке любого телефона (появляется при наведении) — можно выбрать до 3 телефонов и сравнить их характеристики в таблице.</p></div>
      </div>
    </div>
  </div>
</div>

<!-- CONTACTS -->
<div class="parchment-section" id="contacts" style="background:var(--parchment);">
  <div class="section-inner">
    <div class="section-label reveal">Свяжитесь с нами</div>
    <h2 class="section-title reveal">Мы в <em>Казахстане.</em></h2>
    <p class="section-sub reveal">Работаем по всей стране. Звоните, пишите — всегда на связи.</p>
    <div class="contact-grid reveal">
      <div class="contact-card">
        <div class="icon">📍</div>
        <div class="title">Адрес</div>
        <p>Казахстан, г. Алматы<br>пр. Достык, 5<br>БЦ «Нурлы Тау», офис 412<br>050000</p>
      </div>
      <div class="contact-card">
        <div class="icon">📞</div>
        <div class="title">Телефон и WhatsApp</div>
        <a href="tel:+77001234567">+7 (700) 123-45-67</a>
        <a href="tel:+77271234567">+7 (727) 123-45-67</a>
        <p style="margin-top:.5rem;">Пн–Пт: 9:00 — 18:00<br>Сб: 10:00 — 16:00</p>
      </div>
      <div class="contact-card">
        <div class="icon">✉️</div>
        <div class="title">Email и соцсети</div>
        <a href="mailto:info@grove.kz">info@grove.kz</a>
        <a href="mailto:support@grove.kz">support@grove.kz</a>
        <a href="#">Instagram: @grove.kz</a>
        <a href="#">Telegram: @grovekz</a>
      </div>
    </div>
    <div class="delivery-card reveal">
      <div style="font-family:'Playfair Display',serif;font-size:1rem;font-weight:700;color:var(--moss);margin-bottom:1rem;">🚚 Доставка по Казахстану</div>
      <div class="eco-tags">
        <span class="eco-tag">Алматы — 1 день</span>
        <span class="eco-tag">Астана — 2 дня</span>
        <span class="eco-tag">Шымкент — 2 дня</span>
        <span class="eco-tag">Караганда — 2 дня</span>
        <span class="eco-tag">Актобе — 3 дня</span>
        <span class="eco-tag">Усть-Каменогорск — 3 дня</span>
        <span class="eco-tag">Тараз — 3 дня</span>
        <span class="eco-tag">Все города КЗ — до 5 дней</span>
      </div>
    </div>
  </div>
</div>

<!-- FOOTER -->
<footer>
  <div class="footer-inner">
    <div class="footer-top">
      <div>
        <div class="footer-brand">Grove<span>.</span></div>
        <p class="footer-desc">Телефоны, созданные с уважением к природе. Казахстан 🇰🇿</p>
        <div style="display:flex;gap:.6rem;margin-top:.5rem;">
          <a href="#" style="color:var(--sage);font-size:1.2rem;text-decoration:none;">📸</a>
          <a href="#" style="color:var(--sage);font-size:1.2rem;text-decoration:none;">✈️</a>
          <a href="#" style="color:var(--sage);font-size:1.2rem;text-decoration:none;">💼</a>
        </div>
      </div>
      <div class="footer-col">
        <h4>Магазин</h4>
        <a onclick="goTo('shop')">Все телефоны</a>
        <a onclick="goTo('shop');setFilter('flagship')">Флагманы</a>
        <a onclick="goTo('shop');setFilter('mid-range')">Средний класс</a>
        <a onclick="goTo('shop');setFilter('budget')">Бюджетные</a>
        <a onclick="openWishlist()">Избранное</a>
      </div>
      <div class="footer-col">
        <h4>Компания</h4>
        <a onclick="goTo('our-story')">О нас</a>
        <a onclick="goTo('sustainability')">Экология</a>
        <a onclick="goTo('reviews')">Отзывы</a>
        <a onclick="goTo('faq')">FAQ</a>
        <a onclick="goTo('contacts')">Контакты</a>
      </div>
      <div class="footer-col">
        <h4>Помощь</h4>
        <a onclick="goTo('faq')">Доставка</a>
        <a onclick="goTo('faq')">Возврат</a>
        <a onclick="goTo('faq')">Гарантия</a>
        <a onclick="goTo('faq')">Рассрочка</a>
        <a href="mailto:support@grove.kz">Поддержка</a>
      </div>
    </div>
    <div class="footer-bottom">
      <span>© 2026 Grove Technologies Kazakhstan · ТОО «Гроув» · БИН 123456789012</span>
      <span>Политика конфиденциальности · Условия использования</span>
    </div>
  </div>
</footer>

<!-- AUTH MODAL -->
<div class="overlay" id="authOverlay" onclick="if(event.target===this)closeAuth()">
  <div class="modal"><button class="modal-close" onclick="closeAuth()">✕</button><div id="authContent"></div></div>
</div>

<!-- CHECKOUT MODAL -->
<div class="overlay" id="checkoutOverlay" onclick="if(event.target===this)closeCheckout()">
  <div class="modal modal-lg"><button class="modal-close" onclick="closeCheckout()">✕</button><div id="checkoutContent"></div></div>
</div>

<!-- COMPARE OVERLAY -->
<div class="compare-overlay" id="compareOverlay" onclick="if(event.target===this)closeCompare()">
  <div class="compare-panel">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <h2 style="font-family:'Playfair Display',serif;font-size:1.5rem;color:var(--moss);">Сравнение телефонов</h2>
      <button onclick="closeCompare()" style="background:none;border:none;font-size:1.3rem;cursor:pointer;color:var(--muted);">✕</button>
    </div>
    <div id="compareContent"></div>
  </div>
</div>

<!-- CART PANEL -->
<div class="cart-panel" id="cartPanel">
  <div class="cart-header"><h2>Корзина</h2><button class="modal-close" style="position:static" onclick="closeCart()">✕</button></div>
  <div class="cart-body" id="cartBody"></div>
  <div class="cart-footer" id="cartFooter"></div>
</div>
<div class="cart-bg" id="cartBg" onclick="closeAllPanels()"></div>

<!-- WISHLIST PANEL -->
<div class="wishlist-panel" id="wishlistPanel">
  <div class="cart-header"><h2>❤️ Избранное</h2><button class="modal-close" style="position:static" onclick="closeWishlist()">✕</button></div>
  <div class="cart-body" id="wishlistBody"></div>
  <div class="cart-footer" id="wishlistFooter"></div>
</div>


<!-- PROFILE PANEL -->
<div class="profile-panel" id="profilePanel">
  <div class="profile-header">
    <button class="modal-close" style="position:absolute;top:1rem;right:1rem;color:rgba(244,240,230,.5);" onclick="closeProfile()">✕</button>
    <div class="profile-avatar" id="profileAvatar">?</div>
    <div class="profile-name" id="profileName">—</div>
    <div class="profile-email" id="profileEmailDisp">—</div>
    <span class="profile-role" id="profileRole">customer</span>
  </div>
  <div class="profile-body">

    <!-- Quick stats -->
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:.6rem;margin-bottom:1.8rem;" id="profileStats">
      <div style="background:#fff;border:1px solid var(--border);border-radius:10px;padding:.8rem;text-align:center;">
        <div style="font-family:Playfair Display,serif;font-size:1.4rem;font-weight:700;color:var(--moss);" id="statOrders">—</div>
        <div style="font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;">Заказов</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:10px;padding:.8rem;text-align:center;">
        <div style="font-family:Playfair Display,serif;font-size:1.4rem;font-weight:700;color:var(--moss);" id="statWish">—</div>
        <div style="font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;">Избранное</div>
      </div>
      <div style="background:#fff;border:1px solid var(--border);border-radius:10px;padding:.8rem;text-align:center;">
        <div style="font-family:Playfair Display,serif;font-size:1.4rem;font-weight:700;color:var(--moss);" id="statCart">—</div>
        <div style="font-size:.65rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;">В корзине</div>
      </div>
    </div>

    <!-- Orders -->
    <div class="profile-section">
      <div class="profile-section-title">Мои заказы</div>
      <div id="profileOrders"><div class="loading" style="padding:1rem;"><div class="spinner"></div></div></div>
      <div style="text-align:center;margin-top:.5rem;">
        <button onclick="openAllOrders()" style="background:none;border:1px solid var(--sage);color:var(--moss);padding:.4rem 1rem;border-radius:20px;font-size:.78rem;cursor:pointer;">Все заказы →</button>
      </div>
    </div>

    <!-- Edit profile -->
    <div class="profile-section">
      <div class="profile-section-title">Данные аккаунта</div>
      <div class="profile-menu-item" onclick="toggleEditForm()">
        <span class="profile-menu-icon">✏️</span>
        <span class="profile-menu-label">Изменить имя</span>
        <span class="profile-menu-arrow">›</span>
      </div>
      <div class="edit-form" id="editNameForm">
        <div class="form-group"><label>Новое имя</label><input id="editNameInput" placeholder="Ваше имя"/></div>
        <button class="modal-btn" style="margin-top:.3rem" onclick="saveName()">Сохранить</button>
      </div>
      <div class="profile-menu-item" onclick="togglePassForm()">
        <span class="profile-menu-icon">🔒</span>
        <span class="profile-menu-label">Изменить пароль</span>
        <span class="profile-menu-arrow">›</span>
      </div>
      <div class="edit-form" id="editPassForm">
        <div class="form-group"><label>Текущий пароль</label><input id="curPass" type="password"/></div>
        <div class="form-group"><label>Новый пароль</label><input id="newPass" type="password"/></div>
        <div class="form-error" id="passErr"></div>
        <button class="modal-btn" style="margin-top:.3rem" onclick="savePass()">Сохранить</button>
      </div>
    </div>

    <!-- Wishlist shortcut -->
    <div class="profile-section">
      <div class="profile-section-title">Быстрые ссылки</div>
      <div class="profile-menu-item" onclick="closeProfile();openWishlist()">
        <span class="profile-menu-icon">❤️</span>
        <span class="profile-menu-label">Избранное</span>
        <span class="profile-menu-arrow">›</span>
      </div>
      <div class="profile-menu-item" onclick="closeProfile();openCart()">
        <span class="profile-menu-icon">🛍</span>
        <span class="profile-menu-label">Корзина</span>
        <span class="profile-menu-arrow">›</span>
      </div>
      <div class="profile-menu-item" onclick="closeProfile();goTo('contacts')">
        <span class="profile-menu-icon">💬</span>
        <span class="profile-menu-label">Поддержка</span>
        <span class="profile-menu-arrow">›</span>
      </div>
    </div>

  </div>
  <div class="profile-footer">
    <button class="logout-btn" onclick="logout();closeProfile()">Выйти из аккаунта</button>
  </div>
</div>

<!-- All Orders Panel -->
<div id="ordersPanel" style="position:fixed;top:0;right:-100%;width:min(420px,100vw);height:100vh;background:var(--cream);z-index:1100;overflow-y:auto;transition:right .35s cubic-bezier(.4,0,.2,1);box-shadow:-4px 0 24px rgba(0,0,0,.12);">
  <div style="display:flex;align-items:center;justify-content:space-between;padding:1.2rem 1.2rem .8rem;border-bottom:1px solid var(--border);background:#fff;position:sticky;top:0;z-index:10;">
    <div style="display:flex;align-items:center;gap:.6rem;">
      <button onclick="closeAllOrders()" style="background:none;border:none;font-size:1.3rem;cursor:pointer;color:var(--muted);">‹</button>
      <span style="font-family:Playfair Display,serif;font-size:1.1rem;font-weight:700;color:var(--moss);">Мои заказы</span>
    </div>
  </div>
  <div id="allOrdersList" style="padding:1rem;"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = window.location.origin + '/api';

// ── State ───────────────────────────────────────────────────────────────
let token       = localStorage.getItem('grove_token');
let currentUser = JSON.parse(localStorage.getItem('grove_user') || 'null');
let cart        = {items:[],subtotal:0,item_count:0};
let wishlist    = JSON.parse(localStorage.getItem('grove_wish') || '[]'); // product ids
let compareList = []; // product objects, max 3
let allProducts = [];

// ── Helpers ─────────────────────────────────────────────────────────────
function goTo(id){ document.getElementById(id)?.scrollIntoView({behavior:'smooth'}); }

function setFilter(cat){
  setTimeout(()=>{
    document.querySelectorAll('.filter-btn').forEach(b=>{
      b.classList.toggle('active', b.dataset.cat===cat);
    });
    loadProducts(cat);
  }, 400);
}

function closeAllPanels(){
  document.getElementById('cartPanel').classList.remove('open');
  document.getElementById('wishlistPanel').classList.remove('open');
  document.getElementById('profilePanel').classList.remove('open');
  document.getElementById('ordersPanel').style.right = '-100%';
  document.getElementById('cartBg').style.display = 'none';
}

async function apiCall(path, options={}) {
  const headers = {'Content-Type':'application/json'};
  if(token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(API+path, {...options, headers:{...headers,...(options.headers||{})}});
  const data = await res.json();
  if(!res.ok) throw new Error(data.error || 'Ошибка сервера');
  return data;
}

const PHONE_IMAGES = {"iphone-17-pro-blue": "/static/images/iphone-17-pro-blue.svg", "iphone-17-pro-max": "/static/images/iphone-17-pro-max.svg", "iphone-17-black": "/static/images/iphone-17-black.svg", "iphone-16e": "/static/images/iphone-16e.svg", "samsung-s25-ultra": "/static/images/samsung-s25-ultra.svg", "samsung-s25-plus": "/static/images/samsung-s25-plus.svg", "samsung-s25": "/static/images/samsung-s25.svg", "samsung-z-flip7": "/static/images/samsung-z-flip7.svg", "samsung-a56": "/static/images/samsung-a56.svg", "samsung-a36": "/static/images/samsung-a36.svg", "samsung-a26": "/static/images/samsung-a26.svg", "redmi-note14-proplus": "/static/images/redmi-note14-proplus.svg", "redmi-note14": "/static/images/redmi-note14.svg", "oppo-reno14f": "/static/images/oppo-reno14f.svg", "vivo-v60": "/static/images/vivo-v60.svg", "vivo-v50-lite": "/static/images/vivo-v50-lite.svg", "honor-magic7": "/static/images/honor-magic7.svg", "motorola-edge60": "/static/images/motorola-edge60.svg", "samsung-a17": "/static/images/samsung-a17.svg", "samsung-a16": "/static/images/samsung-a16.svg", "samsung-a07": "/static/images/samsung-a07.svg", "xiaomi-redmi-a5": "/static/images/xiaomi-redmi-a5.svg", "vivo-y29": "/static/images/vivo-y29.svg", "tecno-spark20": "/static/images/tecno-spark20.svg", "infinix-hot40i": "/static/images/infinix-hot40i.svg", "oppo-a5-pro": "/static/images/oppo-a5-pro.svg"};
const BRAND_BG = {'Apple':'#1d1d1f','Samsung':'#1428a0','Xiaomi':'#ff6900','OPPO':'#1a1a2e','Vivo':'#415fff','TECNO':'#00a0e9','Motorola':'#5d2d91','HONOR':'#c8392b','Infinix':'#e60012'};
function getEmoji(i){ return '📱'; }
function getPhoneImg(slug, brand, name) {
  const url = PHONE_IMAGES[slug];
  if(url) return `<img src="${url}" alt="${name}" style="width:130px;height:195px;object-fit:contain;filter:drop-shadow(0 6px 18px rgba(0,0,0,.18));transition:transform .4s cubic-bezier(.34,1.56,.64,1);">`;
  return `<div style="font-size:5rem;filter:drop-shadow(0 8px 16px rgba(0,0,0,.1));">📱</div>`;
}
function stars(n=5){ return '★'.repeat(n)+'☆'.repeat(5-n); }

function showToast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr?' error':'');
  clearTimeout(t._t);
  t._t = setTimeout(()=> t.className='toast', 2800);
}

// ── Reveal ───────────────────────────────────────────────────────────────
function observeReveal(){
  const obs = new IntersectionObserver(entries=>{
    entries.forEach(e=>{if(e.isIntersecting) e.target.classList.add('visible');});
  },{threshold:0.08});
  document.querySelectorAll('.reveal:not(.visible)').forEach(el=>obs.observe(el));
}

// ── Auth UI ──────────────────────────────────────────────────────────────
function updateAuthUI(){
  const btn = document.getElementById('authBtn');
  const profileBtn = document.getElementById('profileBtn');
  if(currentUser){
    btn.style.display = 'none';
    profileBtn.style.display = 'flex';
  } else {
    btn.style.display = '';
    profileBtn.style.display = 'none';
  }
  updateMobileMenu();
}
function logout(){
  token=null; currentUser=null;
  localStorage.removeItem('grove_token'); localStorage.removeItem('grove_user');
  cart={items:[],subtotal:0,item_count:0};
  updateAuthUI(); updateCartBadge();
  showToast('👋 Вы вышли из аккаунта');
}

// ── Auth modal ───────────────────────────────────────────────────────────
function openAuth(mode){ document.getElementById('authOverlay').classList.add('open'); renderAuthForm(mode); }
function closeAuth(){ document.getElementById('authOverlay').classList.remove('open'); }

function renderAuthForm(mode){
  const isLogin = mode==='login';
  document.getElementById('authContent').innerHTML = `
    <h2>${isLogin ? 'Добро пожаловать' : 'Создать аккаунт'}</h2>
    <p class="modal-sub">${isLogin ? 'Войдите в ваш аккаунт Grove.' : 'Присоединяйтесь к Grove.'}</p>
    ${!isLogin?`<div class="form-group"><label>Имя</label><input id="aName" placeholder="Ваше имя"/></div>`:''}
    <div class="form-group"><label>Email</label><input id="aEmail" type="email" placeholder="you@example.com"/></div>
    <div class="form-group"><label>Пароль</label><input id="aPass" type="password" placeholder="Минимум 8 символов"/>
      <div class="form-error" id="aErr"></div></div>
    <button class="modal-btn" onclick="submitAuth('${mode}')">${isLogin ? 'Войти' : 'Зарегистрироваться'}</button>
    <p class="modal-switch">${isLogin
      ? `Нет аккаунта? <a onclick="renderAuthForm('register')">Создать</a>`
      : `Уже есть аккаунт? <a onclick="renderAuthForm('login')">Войти</a>`}</p>
    ${isLogin?`<p class="modal-switch" style="margin-top:.4rem"><a onclick="fillDemo()" style="color:var(--clay)">→ Тестовый аккаунт</a></p>`:''}
  `;
}
function fillDemo(){ document.getElementById('aEmail').value='demo@grove.com'; document.getElementById('aPass').value='demo1234'; }

async function submitAuth(mode){
  const email=document.getElementById('aEmail').value.trim();
  const pass=document.getElementById('aPass').value;
  const name=document.getElementById('aName')?.value.trim();
  const errEl=document.getElementById('aErr');
  errEl.style.display='none';
  try{
    if(mode==='register') await apiCall('/auth/register',{method:'POST',body:JSON.stringify({email,password:pass,full_name:name})});
    const d = await apiCall('/auth/login',{method:'POST',body:JSON.stringify({email,password:pass})});
    token=d.access_token; currentUser=d.user;
    localStorage.setItem('grove_token',token); localStorage.setItem('grove_user',JSON.stringify(currentUser));
    updateAuthUI(); await loadCart(); closeAuth();
    showToast(`🌿 Привет, ${currentUser.full_name?.split(' ')[0] || currentUser.email.split('@')[0]}!`);
  }catch(e){ errEl.textContent='Ошибка: ' + (e.message||'попробуйте ещё раз'); errEl.style.display='block'; console.error(e); }
}

// ── Products ─────────────────────────────────────────────────────────────
let currentCat = '';
async function loadProducts(cat=''){
  currentCat = cat;
  const sort = document.getElementById('sortSelect')?.value || 'popular';
  const grid=document.getElementById('productsGrid');
  grid.innerHTML='<div class="loading"><div class="spinner"></div>Загрузка…</div>';
  try{
    const q=new URLSearchParams({per_page:50});
    if(cat) q.set('category',cat);
    q.set('sort', sort);
    const d = await apiCall(`/products?${q}`);
    allProducts = d.items;
    renderProducts(allProducts);
  }catch(e){
    grid.innerHTML=`<div class="loading">⚠️ Сервер недоступен. Запустите python3 server.py<br><small>${e.message}</small></div>`;
  }
}

function getMonthly(price){ return Math.ceil(price/12).toLocaleString(); }

function renderProducts(products){
  const grid=document.getElementById('productsGrid');
  if(!products.length){ grid.innerHTML='<div class="loading">Ничего не найдено.</div>'; return; }
  grid.innerHTML = products.map((p,i)=>{
    const specs = Object.entries(p.specs||{}).slice(0,3).map(([,v])=>`<span class="prod-tag">${v}</span>`).join('');
    const sw = (p.colors||[]).map((c,ci)=>`<div class="swatch${ci===0?' active':''}" style="background:${c.hex}" title="${c.name}" onclick="event.stopPropagation();selectSwatch(event,this)"></div>`).join('');
    const badge = p.badge?`<span class="prod-badge badge-${p.badge.toLowerCase()}">${p.badge}</span>`:'';
    const isWished = wishlist.includes(p.id);
    const inCompare = compareList.some(x=>x.id===p.id);
    const addBtn = p.stock>0
      ? `<button class="add-btn" onclick="event.stopPropagation();addToCart(${p.id})" title="В корзину">+</button>`
      : `<span class="out-stock">Нет в наличии</span>`;
    // fake reviews 4-5 stars
    const r = 4 + (p.id%2); const rc = 12 + (p.id*7)%83;
    return `
    <div class="prod-card reveal" style="transition-delay:${i*.06}s" onclick="openProductModal(${p.id})">
      ${badge}
      <div class="prod-img">
        ${getPhoneImg(p.slug, p.brand, p.name)}
        <div class="prod-actions">
          <button class="prod-action-btn${isWished?' active':''}" onclick="event.stopPropagation();toggleWish(${p.id},this)" title="Избранное">${isWished?'❤️':'🤍'}</button>
          <button class="prod-action-btn${inCompare?' active':''}" onclick="event.stopPropagation();toggleCompare(${p.id},this)" title="Сравнить">⚖</button>
        </div>
      </div>
      <div class="prod-body">
        <div class="prod-brand">${p.brand||'Grove'}</div>
        <div class="prod-name">${p.name}</div>
        <div><span class="stars">${'★'.repeat(r)}${'☆'.repeat(5-r)}</span><span class="rating-count">(${rc})</span></div>
        <div class="prod-installment">от ${getMonthly(p.price)} тг/мес в рассрочку</div>
        ${sw?`<div class="swatches">${sw}</div>`:''}
        <div class="prod-tags">${specs}</div>
        <div class="prod-footer">
          <div>
            <div class="prod-price-main">${Math.round(p.price).toLocaleString()} тг</div>
            ${p.compare_price?`<div class="prod-price-old">${Math.round(p.compare_price).toLocaleString()} тг</div>`:''}
          </div>
          ${addBtn}
        </div>
      </div>
    </div>`;
  }).join('');
  observeReveal();
}

function selectSwatch(e,el){
  el.closest('.swatches').querySelectorAll('.swatch').forEach(s=>s.classList.remove('active'));
  el.classList.add('active');
}

// Product detail modal
function openProductModal(pid){
  const p = allProducts.find(x=>x.id===pid); if(!p) return;
  const specs = Object.entries(p.specs||{}).map(([k,v])=>`<tr><td style="color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;padding:.5rem .8rem .5rem 0;">${k}</td><td style="font-size:.85rem;font-weight:500;padding:.5rem 0;">${v}</td></tr>`).join('');
  const sw = (p.colors||[]).map((c,ci)=>`<div class="swatch${ci===0?' active':''}" style="background:${c.hex};width:18px;height:18px;" title="${c.name}" onclick="selectSwatch(event,this)"></div>`).join('');
  const r=4+(p.id%2); const rc=12+(p.id*7)%83;
  const imgUrl = PHONE_IMAGES[p.slug];
  const detailImg = imgUrl
    ? `<div style="text-align:center;margin-bottom:1rem;background:var(--parchment);border-radius:16px;padding:1.2rem;"><img src="${imgUrl}" alt="${p.name}" style="max-height:200px;object-fit:contain;filter:drop-shadow(0 4px 12px rgba(0,0,0,.12));"></div>`
    : '';
  document.getElementById('checkoutContent').innerHTML = `
    ${detailImg}
    <h2>${p.name}</h2>
    <div style="margin:.3rem 0 .8rem;"><span class="stars" style="font-size:.9rem;">${'★'.repeat(r)}${'☆'.repeat(5-r)}</span><span class="rating-count">(${rc} отзывов)</span></div>
    <div style="font-size:1.6rem;font-family:'Playfair Display',serif;font-weight:700;color:var(--moss);">${Math.round(p.price).toLocaleString()} тг <span style="font-size:.9rem;color:var(--muted);font-weight:400;font-family:'Jost',sans-serif;">или ${getMonthly(p.price)} тг/мес</span></div>
    ${p.compare_price?`<div style="font-size:.8rem;color:var(--clay);text-decoration:line-through;">${Math.round(p.compare_price).toLocaleString()} тг</div>`:''}
    <p style="font-size:.88rem;color:var(--muted);line-height:1.7;margin:1rem 0;">${p.description||'Флагманский телефон Grove с натуральными материалами.'}</p>
    ${sw?`<div style="margin:.5rem 0 1rem;"><div style="font-size:.72rem;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;">Цвет</div><div class="swatches" style="gap:6px;">${sw}</div></div>`:''}
    <table style="width:100%;margin:1rem 0;">${specs}</table>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-top:1rem;">
      <button class="modal-btn" onclick="addToCart(${p.id});closeCheckout()">+ В корзину</button>
      <button class="modal-btn secondary" onclick="toggleWish(${p.id});closeCheckout()">❤️ Избранное</button>
    </div>
    <p style="text-align:center;font-size:.72rem;color:var(--muted);margin-top:.8rem;">✓ В наличии ${p.stock} шт. · Гарантия 2 года · Бесплатный возврат 14 дней</p>
  `;
  document.getElementById('checkoutOverlay').classList.add('open');
}
function closeCheckout(){ document.getElementById('checkoutOverlay').classList.remove('open'); }

// ── Wishlist ─────────────────────────────────────────────────────────────
function updateWishBadge(){
  const b=document.getElementById('wishBadge');
  b.textContent=wishlist.length;
  b.style.display=wishlist.length?'flex':'none';
}
function toggleWish(pid, btn){
  const idx=wishlist.indexOf(pid);
  if(idx>=0){ wishlist.splice(idx,1); showToast('Удалено из избранного'); }
  else { wishlist.push(pid); showToast('❤️ Добавлено в избранное'); }
  localStorage.setItem('grove_wish',JSON.stringify(wishlist));
  updateWishBadge();
  // update button if provided
  if(btn){ btn.textContent=wishlist.includes(pid)?'❤️':'🤍'; btn.classList.toggle('active',wishlist.includes(pid)); }
  renderWishlist();
}
function openWishlist(){ document.getElementById('wishlistPanel').classList.add('open'); document.getElementById('cartBg').style.display='block'; renderWishlist(); }
function closeWishlist(){ document.getElementById('wishlistPanel').classList.remove('open'); document.getElementById('cartBg').style.display='none'; }
function renderWishlist(){
  const body=document.getElementById('wishlistBody');
  const footer=document.getElementById('wishlistFooter');
  const items=allProducts.filter(p=>wishlist.includes(p.id));
  if(!items.length){ body.innerHTML='<div class="cart-empty"><div class="ce-icon">❤️</div><p>Избранное пусто.</p><p style="font-size:.78rem;margin-top:.4rem;color:var(--muted)">Нажмите 🤍 на любом телефоне.</p></div>'; footer.innerHTML=''; return; }
  body.innerHTML=items.map((p,i)=>{
    const imgUrl = PHONE_IMAGES[p.slug];
    const imgHtml = imgUrl
      ? `<img src="${imgUrl}" alt="${p.name}" style="width:42px;height:52px;object-fit:contain;">`
      : '📱';
    return `
    <div class="cart-item">
      <div class="ci-emoji">${imgHtml}</div>
      <div class="ci-info">
        <div class="ci-name">${p.name}</div>
        <div class="ci-color">${Math.round(p.price).toLocaleString()} тг</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:.3rem;align-items:flex-end;">
        <button class="ci-remove" onclick="toggleWish(${p.id})">✕</button>
        <button onclick="addToCart(${p.id});closeWishlist()" style="background:var(--moss);color:#fff;border:none;padding:.3rem .7rem;border-radius:100px;font-size:.72rem;cursor:pointer;">+ В корзину</button>
      </div>
    </div>`;
  }).join('');
  footer.innerHTML=`<button class="modal-btn" onclick="wishlist.slice().forEach(pid=>addToCart(pid));closeWishlist()">Всё в корзину 🌿</button>`;
}

// ── Compare ──────────────────────────────────────────────────────────────
function toggleCompare(pid, btn){
  const p=allProducts.find(x=>x.id===pid); if(!p) return;
  const idx=compareList.findIndex(x=>x.id===pid);
  if(idx>=0){ compareList.splice(idx,1); showToast('Убрано из сравнения'); }
  else if(compareList.length>=3){ showToast('Максимум 3 телефона для сравнения',true); return; }
  else { compareList.push(p); showToast('⚖ Добавлено для сравнения'); }
  if(btn){ btn.classList.toggle('active',compareList.some(x=>x.id===pid)); }
  const bar=document.getElementById('compareBar');
  document.getElementById('compareCount').textContent=`${compareList.length} выбрано`;
  bar.classList.toggle('show',compareList.length>0);
}
function clearCompare(){ compareList=[]; document.getElementById('compareBar').classList.remove('show'); document.querySelectorAll('.prod-action-btn.active[title="Сравнить"]').forEach(b=>b.classList.remove('active')); }
function openCompare(){
  if(compareList.length<2){ showToast('Выберите минимум 2 телефона',true); return; }
  const specKeys=[...new Set(compareList.flatMap(p=>Object.keys(p.specs||{})))];
  const headers=compareList.map((p,i)=>{
    const cImg = PHONE_IMAGES[p.slug];
    const cImgHtml = cImg ? `<img src="${cImg}" style="width:50px;height:70px;object-fit:contain;display:block;margin:0 auto .4rem;">` : `<span class="compare-emoji">📱</span>`;
    return `<th>${cImgHtml}${p.name}<br><span style="font-family:'Playfair Display',serif;font-size:1.1rem;color:var(--moss);">${Math.round(p.price).toLocaleString()} тг</span></th>`;
  }).join('');
  const rows=[
    `<tr><td>Цена</td>${compareList.map(p=>`<td class="${compareList.every(x=>x.price>=p.price)?'compare-best':''}">${Math.round(p.price).toLocaleString()} тг</td>`).join('')}</tr>`,
    `<tr><td>В наличии</td>${compareList.map(p=>`<td>${p.stock} шт.</td>`).join('')}</tr>`,
    ...specKeys.map(k=>`<tr><td>${k}</td>${compareList.map(p=>`<td>${(p.specs||{})[k]||'—'}</td>`).join('')}</tr>`),
  ].join('');
  document.getElementById('compareContent').innerHTML=`<table class="compare-table"><thead><tr><th></th>${headers}</tr></thead><tbody>${rows}</tbody></table>
  <div style="display:grid;grid-template-columns:repeat(${compareList.length},1fr);gap:.6rem;margin-top:1rem;">
    ${compareList.map(p=>`<button class="modal-btn" style="margin:0" onclick="addToCart(${p.id});closeCompare()">+ В корзину</button>`).join('')}
  </div>`;
  document.getElementById('compareOverlay').classList.add('open');
}
function closeCompare(){ document.getElementById('compareOverlay').classList.remove('open'); }

// ── Search ───────────────────────────────────────────────────────────────
function onSearch(q){
  const dd=document.getElementById('searchDropdown');
  if(!q.trim()||!allProducts.length){ dd.style.display='none'; return; }
  const results=allProducts.filter(p=>p.name.toLowerCase().includes(q.toLowerCase())||p.brand?.toLowerCase().includes(q.toLowerCase())).slice(0,5);
  if(!results.length){ dd.style.display='none'; return; }
  dd.innerHTML=results.map(p=>`
    <div class="search-item" onclick="openProductModal(${p.id});document.getElementById('searchDropdown').style.display='none';document.getElementById('searchInput').value=''">
      <img src="${PHONE_IMAGES[p.slug]||''}" style="width:30px;height:40px;object-fit:contain;flex-shrink:0;${PHONE_IMAGES[p.slug]?'':'display:none'}">
      <div><div class="search-item-name">${p.name}</div><div class="search-item-price">${Math.round(p.price).toLocaleString()} тг</div></div>
    </div>`).join('');
  dd.style.display='block';
}
function hideSearch(){ document.getElementById('searchDropdown').style.display='none'; }

// ── Cart ─────────────────────────────────────────────────────────────────
function updateCartBadge(){
  const c=document.getElementById('cartCount');
  c.textContent=cart.item_count||0;
  c.style.display=cart.item_count>0?'flex':'none';
}
async function loadCart(){
  if(!token) return;
  try{ cart=await apiCall('/cart'); updateCartBadge(); renderCartPanel(); }catch(_){}
}
async function addToCart(productId){
  if(!token){ openAuth('login'); showToast('🔐 Войдите чтобы добавить в корзину'); return; }
  try{
    cart=await apiCall('/cart',{method:'POST',body:JSON.stringify({product_id:productId,quantity:1})});
    updateCartBadge(); renderCartPanel();
    showToast('🌿 Добавлено в корзину!');
  }catch(e){ showToast('⚠️ '+e.message,true); }
}
async function updateQty(itemId,qty){
  if(qty<1) return removeItem(itemId);
  try{ cart=await apiCall(`/cart/${itemId}`,{method:'PATCH',body:JSON.stringify({quantity:qty})}); updateCartBadge(); renderCartPanel(); }catch(e){ showToast(e.message,true); }
}
async function removeItem(itemId){
  try{ cart=await apiCall(`/cart/${itemId}`,{method:'DELETE'}); updateCartBadge(); renderCartPanel(); }catch(_){}
}
function renderCartPanel(){
  const body=document.getElementById('cartBody'), footer=document.getElementById('cartFooter');
  if(!cart.items?.length){
    body.innerHTML='<div class="cart-empty"><div class="ce-icon">🛍</div><p>Корзина пуста.</p><p style="font-size:.78rem;color:var(--muted);margin-top:.4rem">Добавьте телефоны из каталога.</p></div>';
    footer.innerHTML=''; return;
  }
  body.innerHTML=cart.items.map(item=>{
    const imgUrl = PHONE_IMAGES[item.slug];
    const imgHtml = imgUrl
      ? `<img src="${imgUrl}" alt="${item.name}" style="width:42px;height:52px;object-fit:contain;">`
      : '📱';
    return `
    <div class="cart-item">
      <div class="ci-emoji">${imgHtml}</div>
      <div class="ci-info">
        <div class="ci-name">${item.name}</div>
        ${item.color?`<div class="ci-color">Цвет: ${item.color}</div>`:''}
        <div class="ci-qty">
          <button class="qty-btn" onclick="updateQty(${item.id},${item.quantity-1})">−</button>
          <span>${item.quantity}</span>
          <button class="qty-btn" onclick="updateQty(${item.id},${item.quantity+1})">+</button>
        </div>
      </div>
      <div><div class="ci-price">${Math.round(item.line_total).toLocaleString()} тг</div><button class="ci-remove" onclick="removeItem(${item.id})">✕</button></div>
    </div>`;
  }).join('');
  const ship=cart.subtotal>=50000?0:1990;
  const tax=0;
  const total=(cart.subtotal+ship).toFixed(2);
  footer.innerHTML=`
    <div class="cart-totals" style="margin-bottom:1rem;">
      <div class="cart-row"><span>Товары</span><span>${Math.round(cart.subtotal).toLocaleString()} тг</span></div>
      <div class="cart-row"><span>Доставка</span><span>${ship===0?'Бесплатно':Math.round(ship).toLocaleString()+' тг'}</span></div>
      <div class="cart-row total"><span>Итого</span><span>${Math.round(parseFloat(total)).toLocaleString()} тг</span></div>
    </div>
    <button class="modal-btn" style="margin-top:0" onclick="openCheckoutForm()">Оформить заказ →</button>`;
}
function openCart(){ document.getElementById('cartPanel').classList.add('open'); document.getElementById('cartBg').style.display='block'; renderCartPanel(); }
function closeCart(){ document.getElementById('cartPanel').classList.remove('open'); document.getElementById('wishlistPanel').classList.remove('open'); document.getElementById('cartBg').style.display='none'; }

// ── Checkout form ─────────────────────────────────────────────────────────
function openCheckoutForm(){
  closeCart();
  if(!token){ openAuth('login'); return; }
  const ship=cart.subtotal>=50000?0:1990;
  const tax=0;
  const total=(cart.subtotal+ship).toFixed(2);
  document.getElementById('checkoutContent').innerHTML=`
    <h2>Оформление заказа</h2>
    <p class="modal-sub">${cart.item_count} товар(ов) · ${Math.round(parseFloat(total)).toLocaleString()} тг</p>
    <div class="form-group"><label>ФИО</label><input id="coName" placeholder="Иванов Иван" value="${currentUser?.full_name||''}"/></div>
    <div class="form-group"><label>Адрес</label><input id="coAddr" placeholder="ул. Достык, 5"/></div>
    <div class="form-row">
      <div class="form-group"><label>Город</label><input id="coCity" placeholder="Алматы"/></div>
      <div class="form-group"><label>Индекс</label><input id="coZip" placeholder="050000"/></div>
    </div>
    <div style="background:#fff;border:1px solid var(--border);border-radius:14px;padding:1rem;margin-bottom:.5rem;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.8rem;">
        <span style="font-size:.8rem;font-weight:600;color:var(--moss);">🔒 Данные карты</span>
        <span style="font-size:.75rem;color:var(--muted);">VISA &nbsp;·&nbsp; Mastercard</span>
      </div>
      <div class="form-group" style="margin-bottom:.6rem;">
        <input id="coCardNum" placeholder="0000 0000 0000 0000" maxlength="19"
          oninput="fmtCard(this)"
          style="font-size:1rem;letter-spacing:.1em;font-family:monospace;width:100%;"/>
      </div>
      <div style="display:flex;gap:.6rem;">
        <div class="form-group" style="flex:1;margin-bottom:0;">
          <input id="coExpiry" placeholder="ММ/ГГ" maxlength="5" oninput="fmtExpiry(this)"/>
        </div>
        <div class="form-group" style="width:80px;margin-bottom:0;">
          <input id="coCvv" placeholder="CVV" maxlength="3" type="password"/>
        </div>
      </div>
    </div>
    <div class="form-error" id="coErr"></div>
    <button class="modal-btn" id="payBtn" onclick="submitCheckout()">
      Оплатить ${Math.round(parseFloat(total)).toLocaleString()} тг 🌿
    </button>
    <p style="text-align:center;font-size:.68rem;color:var(--muted);margin-top:.6rem;">
      🔒 Защищено SSL · Данные карты не хранятся
    </p>
  `;
  document.getElementById('checkoutOverlay').classList.add('open');
}

// Card input formatters
function fmtCard(el){
  let v = el.value.replace(/\D/g,'').slice(0,16);
  el.value = v.replace(/(\d{4})(?=\d)/g,'$1 ');
}
function fmtExpiry(el){
  let v = el.value.replace(/\D/g,'').slice(0,4);
  if(v.length>=3) v = v.slice(0,2)+'/'+v.slice(2);
  el.value = v;
}
function luhn(n){
  let s=0,alt=false;
  for(let i=n.length-1;i>=0;i--){
    let d=parseInt(n[i]);
    if(alt){ d*=2; if(d>9) d-=9; }
    s+=d; alt=!alt;
  }
  return s%10===0;
}

async function submitCheckout(){
  const city   = document.getElementById('coCity').value.trim();
  const zip    = document.getElementById('coZip').value.trim();
  const errEl  = document.getElementById('coErr');
  const btn    = document.getElementById('payBtn');
  const cardEl = document.getElementById('coCardNum');
  const expEl  = document.getElementById('coExpiry');
  const cvvEl  = document.getElementById('coCvv');
  errEl.style.display='none';

  // Validate city
  if(!city){ errEl.textContent='Укажите город.'; errEl.style.display='block'; return; }

  // Validate card
  const cardRaw = (cardEl?.value||'').replace(/\s/g,'');
  const expiry  = expEl?.value||'';
  const cvv     = cvvEl?.value||'';

  if(cardEl){
    if(cardRaw.length<16){ errEl.textContent='Введите номер карты (16 цифр).'; errEl.style.display='block'; cardEl.focus(); return; }
    if(!luhn(cardRaw)){ errEl.textContent='Неверный номер карты.'; errEl.style.display='block'; cardEl.focus(); return; }
    if(!/^\d{2}\/\d{2}$/.test(expiry)){ errEl.textContent='Введите срок карты (ММ/ГГ).'; errEl.style.display='block'; expEl.focus(); return; }
    const [mm,yy]=expiry.split('/').map(Number);
    const now=new Date(); const cy=now.getFullYear()%100; const cm=now.getMonth()+1;
    if(mm<1||mm>12||yy<cy||(yy===cy&&mm<cm)){ errEl.textContent='Срок действия карты истёк.'; errEl.style.display='block'; return; }
    if(cvv.length<3){ errEl.textContent='Введите CVV (3 цифры).'; errEl.style.display='block'; cvvEl.focus(); return; }
  }

  // Animate button
  btn.disabled=true;
  btn.innerHTML='<span style="display:inline-block;animation:spin 1s linear infinite">⏳</span> Обработка…';

  try{
    const order=await apiCall('/orders/checkout',{method:'POST',body:JSON.stringify({address:{line1:document.getElementById('coAddr').value,city,postal_code:zip,country:'KZ'}})});
    const pay=await apiCall('/payments/pay',{method:'POST',body:JSON.stringify({order_id:order.id})});

    cart={items:[],subtotal:0,item_count:0}; updateCartBadge();

    if(pay.redirect_url){
      showToast('Переходим на страницу оплаты…');
      setTimeout(()=>{ window.location.href=pay.redirect_url; }, 600);
    } else if(pay.success){
      document.getElementById('checkoutContent').innerHTML=`
        <div class="success-icon">🌿</div>
        <h2>Заказ оформлен!</h2>
        <p class="modal-sub">Спасибо за покупку в Grove!</p>
        <div class="order-summary">
          <div class="order-row"><span>Заказ #${order.id}</span><span style="color:var(--fern)">✅ Оплачен</span></div>
          ${(order.items||[]).map(i=>`<div class="order-row"><span>${i.product_name} ×${i.quantity}</span><span>${Math.round(i.subtotal).toLocaleString()} тг</span></div>`).join('')}
          <div class="order-row" style="margin-top:.4rem"><span>Доставка</span><span>${order.shipping_cost===0?'Бесплатно':Math.round(order.shipping_cost).toLocaleString()+' тг'}</span></div>
          <div class="order-row bold"><span>Итого</span><span>${Math.round(order.total).toLocaleString()} тг</span></div>
        </div>
        <button class="modal-btn" onclick="closeCheckout()">Продолжить покупки 🌿</button>`;
      showToast('✅ Заказ подтверждён!');
    }
  }catch(e){
    errEl.textContent='Ошибка: '+(e.message||'попробуйте ещё раз');
    errEl.style.display='block';
    btn.disabled=false;
    btn.innerHTML=`Оплатить 🌿`;
  }
}

// ── FAQ ──────────────────────────────────────────────────────────────────
function toggleFaq(btn){
  const a=btn.nextElementSibling;
  const isOpen=btn.classList.contains('open');
  document.querySelectorAll('.faq-q.open').forEach(b=>{ b.classList.remove('open'); b.nextElementSibling.classList.remove('open'); });
  if(!isOpen){ btn.classList.add('open'); a.classList.add('open'); }
}

// ── Filters ──────────────────────────────────────────────────────────────
document.getElementById('sortSelect')?.addEventListener('change', ()=>{ loadProducts(currentCat); });
document.getElementById('filterBtns').addEventListener('click',e=>{
  const btn=e.target.closest('.filter-btn'); if(!btn) return;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadProducts(btn.dataset.cat);
});


// ── Profile ───────────────────────────────────────────────────────────────
function openProfile(){
  if(!currentUser){ openAuth('login'); return; }
  const p = currentUser;
  document.getElementById('profileAvatar').textContent = (p.full_name||p.email)[0].toUpperCase();
  document.getElementById('profileName').textContent = p.full_name || 'Без имени';
  document.getElementById('profileEmailDisp').textContent = p.email;
  document.getElementById('profileRole').textContent = p.role==='admin'?'Администратор':'Покупатель';
  document.getElementById('editNameInput').value = p.full_name || '';
  document.getElementById('statWish').textContent = wishlist.length;
  document.getElementById('statCart').textContent = cart.item_count || 0;
  document.getElementById('profilePanel').classList.add('open');
  document.getElementById('cartBg').style.display = 'block';
  loadProfileOrders();
}
function closeProfile(){
  document.getElementById('profilePanel').classList.remove('open');
  document.getElementById('cartBg').style.display = 'none';
}
async function loadProfileOrders(){
  const el = document.getElementById('profileOrders');
  const statEl = document.getElementById('statOrders');
  try{
    const d = await apiCall('/orders?per_page=3');
    statEl.textContent = d.total;
    if(!d.items.length){
      el.innerHTML = '<p style="font-size:.82rem;color:var(--muted);padding:.5rem 0;">Заказов пока нет.</p>'; return;
    }
    el.innerHTML = d.items.map(o=>{
      const statusClass = {paid:'status-paid',pending:'status-pending',delivered:'status-delivered'}[o.status]||'status-pending';
      const statusLabel = {paid:'Оплачен',pending:'Ожидает',delivered:'Доставлен',shipped:'Отправлен',cancelled:'Отменён'}[o.status]||o.status;
      const itemNames = o.items.map(i=>i.product_name).join(', ');
      return `<div class="order-mini">
        <div class="order-mini-top">
          <span class="order-mini-id">Заказ #${o.id}</span>
          <span class="order-mini-status ${statusClass}">${statusLabel}</span>
        </div>
        <div class="order-mini-items">${itemNames}</div>
        <div class="order-mini-total">${Math.round(o.total).toLocaleString()} тг</div>
      </div>`;
    }).join('');
  }catch(e){ el.innerHTML='<p style="font-size:.8rem;color:var(--muted);">Не удалось загрузить заказы.</p>'; }
}
function toggleEditForm(){ document.getElementById('editNameForm').classList.toggle('open'); }
function togglePassForm(){ document.getElementById('editPassForm').classList.toggle('open'); }
async function saveName(){
  const name = document.getElementById('editNameInput').value.trim();
  if(!name) return;
  try{
    const u = await apiCall('/auth/me',{method:'PATCH',body:JSON.stringify({full_name:name})});
    currentUser = u; localStorage.setItem('grove_user', JSON.stringify(u));
    document.getElementById('profileName').textContent = u.full_name;
    document.getElementById('profileAvatar').textContent = u.full_name[0].toUpperCase();
    document.getElementById('editNameForm').classList.remove('open');
    showToast('✅ Имя изменено');
  }catch(e){ showToast(e.message, true); }
}
async function savePass(){
  const cur = document.getElementById('curPass').value;
  const nw  = document.getElementById('newPass').value;
  const err = document.getElementById('passErr');
  err.style.display='none';
  if(nw.length<8){ err.textContent='Минимум 8 символов'; err.style.display='block'; return; }
  try{
    await apiCall('/auth/me/change-password',{method:'POST',body:JSON.stringify({current_password:cur,new_password:nw})});
    document.getElementById('editPassForm').classList.remove('open');
    document.getElementById('curPass').value=''; document.getElementById('newPass').value='';
    showToast('✅ Пароль изменён');
  }catch(e){ err.textContent=e.message; err.style.display='block'; }
}


// ── Mobile menu ────────────────────────────────────────────────────────────
async function openAllOrders(){
  closeProfile();
  const panel = document.getElementById('ordersPanel');
  const list  = document.getElementById('allOrdersList');
  // slight delay so closeProfile doesn't hide cartBg again
  setTimeout(()=>{ 
    panel.style.right = '0';
    document.getElementById('cartBg').style.display = 'block';
  }, 50);
  await renderAllOrders();
}
async function renderAllOrders(){
  const list = document.getElementById('allOrdersList');
  list.innerHTML = '<div class="loading" style="padding:2rem;text-align:center;"><div class="spinner"></div></div>';
  try{
    const d = await apiCall('/orders?per_page=50');
    if(!d.items.length){
      list.innerHTML = '<p style="text-align:center;color:var(--muted);padding:2rem 1rem;">Заказов пока нет.<br><span style="font-size:.8rem;">Добавьте товар в корзину и оформите заказ.</span></p>'; return;
    }
    const statusInfo = {
      paid:      {label:'✅ Оплачен',    color:'#2d7a3a', bg:'#f0faf2'},
      pending:   {label:'⏳ Ожидает',    color:'#b45309', bg:'#fffbeb'},
      shipped:   {label:'🚚 Отправлен',  color:'#7c3aed', bg:'#f5f3ff'},
      delivered: {label:'📦 Доставлен',  color:'#1d4ed8', bg:'#eff6ff'},
      cancelled: {label:'❌ Отменён',    color:'#dc2626', bg:'#fef2f2'},
      refunded:  {label:'↩️ Возврат',    color:'#6b7280', bg:'#f3f4f6'},
    };
    list.innerHTML = d.items.map(o=>{
      const si = statusInfo[o.status] || {label:o.status, color:'#666', bg:'#f5f5f5'};
      const itemNames = (o.items||[]).map(i=>`<span style="display:flex;justify-content:space-between;"><span>${i.product_name} ×${i.quantity}</span><span style="color:var(--muted)">${Math.round(i.subtotal).toLocaleString()} тг</span></span>`).join('');
      const date = new Date(o.created_at).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'});

      const cancelBtn = o.status==='pending'
        ? `<button onclick="cancelOrder(${o.id},this)" style="flex:1;background:none;border:1px solid #dc2626;color:#dc2626;padding:.4rem;border-radius:8px;font-size:.75rem;cursor:pointer;font-weight:500;">✕ Отменить</button>`
        : '';
      const refundBtn = (o.status==='paid'||o.status==='shipped')
        ? `<button onclick="refundOrder(${o.id},this)" style="flex:1;background:none;border:1px solid #6b7280;color:#6b7280;padding:.4rem;border-radius:8px;font-size:.75rem;cursor:pointer;font-weight:500;">↩ Возврат</button>`
        : '';
      const deleteBtn = ['cancelled','refunded','delivered'].includes(o.status)
        ? `<button onclick="deleteOrder(${o.id},this)" style="flex:1;background:none;border:1px solid #dc2626;color:#dc2626;padding:.4rem;border-radius:8px;font-size:.75rem;cursor:pointer;font-weight:500;">🗑 Удалить</button>`
        : '';
      const actionRow = (cancelBtn||refundBtn||deleteBtn)
        ? `<div style="display:flex;gap:.5rem;margin-top:.7rem;">${cancelBtn}${refundBtn}${deleteBtn}</div>` : '';

      return `<div style="background:#fff;border:1px solid var(--border);border-radius:14px;padding:1rem;margin-bottom:.8rem;box-shadow:0 1px 4px rgba(0,0,0,.04);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem;">
          <span style="font-weight:700;color:var(--moss);font-size:.95rem;">Заказ #${o.id}</span>
          <span style="font-size:.73rem;font-weight:600;color:${si.color};background:${si.bg};padding:.2rem .6rem;border-radius:20px;">${si.label}</span>
        </div>
        <div style="font-size:.73rem;color:var(--muted);margin-bottom:.6rem;">${date}</div>
        <div style="font-size:.8rem;color:var(--text);line-height:1.8;border-top:1px solid var(--border);padding-top:.6rem;margin-bottom:.6rem;">${itemNames}</div>
        <div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--border);padding-top:.6rem;">
          <span style="font-size:.78rem;color:var(--muted);">Доставка: ${o.shipping_cost===0?'Бесплатно':Math.round(o.shipping_cost).toLocaleString()+' тг'}</span>
          <span style="font-weight:700;color:var(--moss);font-size:1rem;">${Math.round(o.total).toLocaleString()} тг</span>
        </div>
        ${actionRow}
      </div>`;
    }).join('');
  }catch(e){ list.innerHTML=`<p style="text-align:center;color:var(--muted);padding:2rem;">${e.message}</p>`; }
}
function closeAllOrders(){
  document.getElementById('ordersPanel').style.right = '-100%';
  document.getElementById('cartBg').style.display = 'none';
}
async function deleteOrder(oid, btn){
  if(!confirm('Удалить заказ #'+oid+' из истории?')) return;
  btn.disabled=true; btn.textContent='…';
  try{
    await apiCall('/orders/'+oid+'/delete',{method:'DELETE'});
    showToast('🗑 Заказ #'+oid+' удалён');
    await renderAllOrders();
  }catch(e){ showToast(e.message,true); btn.disabled=false; btn.textContent='🗑 Удалить'; }
}
async function cancelOrder(oid, btn){
  if(!confirm('Отменить заказ #'+oid+'?\nТовары вернутся на склад.')) return;
  btn.disabled=true; btn.textContent='…';
  try{
    await apiCall('/orders/'+oid+'/cancel',{method:'POST'});
    showToast('Заказ #'+oid+' отменён');
    await renderAllOrders();
  }catch(e){ showToast(e.message,true); btn.disabled=false; btn.textContent='✕ Отменить'; }
}
async function refundOrder(oid, btn){
  if(!confirm('Запросить возврат по заказу #'+oid+'?\nСредства будут возвращены на карту в течение 3–5 рабочих дней.')) return;
  btn.disabled=true; btn.textContent='…';
  try{
    await apiCall('/orders/'+oid+'/refund',{method:'POST'});
    showToast('↩ Возврат по заказу #'+oid+' оформлен');
    await renderAllOrders();
  }catch(e){ showToast(e.message,true); btn.disabled=false; btn.textContent='↩ Возврат'; }
}

function toggleMobileMenu(){
  const menu = document.getElementById('mobileMenu');
  const btn  = document.getElementById('burgerBtn');
  const isOpen = menu.classList.contains('open');
  if(isOpen){ closeMobileMenu(); }
  else { menu.classList.add('open'); btn.classList.add('open'); document.body.style.overflow='hidden'; }
}
function closeMobileMenu(){
  document.getElementById('mobileMenu').classList.remove('open');
  document.getElementById('burgerBtn').classList.remove('open');
  document.body.style.overflow='';
}
// Update mobile auth button
function updateMobileMenu(){
  const btn = document.getElementById('mobileAuthBtn');
  if(!btn) return;
  if(currentUser){
    btn.textContent = '👤 ' + (currentUser.full_name?.split(' ')[0] || 'Профиль');
    btn.onclick = ()=>{ closeMobileMenu(); openProfile(); };
  } else {
    btn.textContent = 'Войти';
    btn.onclick = ()=>{ closeMobileMenu(); openAuth('login'); };
  }
}

// ── Init ─────────────────────────────────────────────────────────────────
updateAuthUI();
updateWishBadge();
loadProducts();
if(token) loadCart();
observeReveal();
</script>
</body>
</html>
"""

@app.route("/")
@app.route("/store")
@app.route("/index.html")
def store():
    return HTML_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}


# Always initialize DB on startup (works with gunicorn, Railway, and direct run)
init_db()
seed()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    init_db()
    seed()
    print("\n🌿 Grove Store API starting...")
    print(f"   http://localhost:{port}/store")
    print("   Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)